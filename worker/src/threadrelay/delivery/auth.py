"""Transient, state-driven personal-account authorization.

This is a trusted backend interface, not the ordinary command/event channel.
No secret or private QR is stored in snapshots or application SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Protocol
from threading import RLock
from functools import wraps
import math
from uuid import uuid4

from threadrelay.storage.outbox import AccountStore, _has_native_uncertainty, _has_unresolved_work
from .telegram import NetworkPermit, TelegramBoundaryError, TelegramClient


class AuthFailure(RuntimeError):
    pass


class SecretHandoff(Protocol):
    """One-shot reference resolution from a platform-owned secure handoff."""
    def take(self, ref: str) -> str: ...


@dataclass(frozen=True)
class AccountReadiness:
    account_id: str | None
    epoch: int
    auth_state: str
    display_name: str | None
    network_state: str
    revocation_pending: bool

    def as_contract(self) -> dict:
        return vars(self).copy()


@dataclass(frozen=True)
class Challenge:
    kind: str
    challenge_id: str
    expires_at: str | None = None
    code_delivery: dict | None = None


def _synchronized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)
    return guarded


class AuthController:
    def __init__(self, client: TelegramClient, accounts: AccountStore, handoff: SecretHandoff,
                 *, clock: Callable[[], float], challenge_id: Callable[[], str] = lambda: uuid4().hex,
                 challenge_ttl: float = 300, lifecycle_lock=None):
        self._lifecycle_lock = lifecycle_lock or RLock()
        self._client, self._accounts, self._handoff = client, accounts, handoff
        self._clock, self._challenge_id, self._ttl = clock, challenge_id, challenge_ttl
        saved = accounts.current()
        pending = accounts.pending_revocations()
        cleanup = accounts.cleanup_intent()
        if saved:
            self._snapshot = AccountReadiness(saved["account_id"], saved["epoch"], "unknown", None, "offline", False)
        elif cleanup:
            saved_cleanup = accounts.account_state(cleanup['account_id'])
            if saved_cleanup is None or saved_cleanup['epoch'] != cleanup['epoch']:
                raise AuthFailure('Cleanup binding changed')
            self._snapshot = AccountReadiness(**saved_cleanup)
        elif pending and not accounts.locally_removed(pending[0]["account_id"]):
            self._snapshot = AccountReadiness(pending[0]["account_id"], pending[0]["epoch"], "closed", None, "offline", True)
        else:
            self._snapshot = AccountReadiness(None, 0, "logged_out", None, "offline", False)
        self._challenge: Challenge | None = None
        self._deadline = 0.0
        self._private_qr: str | None = None
        self._waiting_identity = False
        self._submitted_kind: str | None = None
        self._vault_ref: str | None = None
        self._revocation_required = False
        self.status = "cleanup_pending" if cleanup else "idle"
        self._bootstrap_account = None
        self._bootstrap_running = False
        self._bootstrap_network_pending = False
        self._expected_account = None
        self._network_requested = False
        self._logout_requested = False
        self.verify_isolations=None
        self._code_delivery=None
        self._resend_deadline=0.0
        self._resending=False

    @_synchronized
    def snapshot(self) -> AccountReadiness:
        return self._snapshot

    @_synchronized
    def challenge_with_remaining(self) -> tuple[Challenge | None, float | None]:
        """Return the current challenge and monotonic remaining TTL atomically."""
        if self._challenge is None:
            return None, None
        now = self._clock()
        if not math.isfinite(now) or not math.isfinite(self._deadline) or now >= self._deadline:
            self._invalidate_challenge()
            if self.status in {'authorizing','authentication_error'}:self.status='challenge_expired'
            return None, None
        challenge=self._challenge
        if challenge.kind=='code' and self._code_delivery is not None:
            delivery={**self._code_delivery,'resend_after_seconds':max(0,math.ceil(self._resend_deadline-now)) if self._code_delivery['next_method'] is not None else None}
            challenge=replace(challenge,code_delivery=delivery)
        return challenge, self._deadline - now

    @_synchronized
    def challenge(self) -> Challenge | None:
        return self.challenge_with_remaining()[0]

    def _invalidate_challenge(self) -> None:
        self._challenge = None
        self._private_qr = None
        self._deadline = 0.0

    def _new_challenge(self, kind: str, qr: str | None = None, code_delivery=None) -> None:
        self._invalidate_challenge()
        self._challenge = Challenge(kind, self._challenge_id())
        self._deadline = self._clock() + self._ttl
        self._private_qr = qr
        if kind!='code':self._code_delivery=None;self._resend_deadline=0.0
        elif code_delivery is not None:
            self._code_delivery=dict(code_delivery)
            self._resend_deadline=self._clock()+(code_delivery['resend_after_seconds'] or 0)

    @_synchronized
    def begin(self, vault_ref: str, *, pending: bool, running=False, restoring=False) -> None:
        if self.status not in {"idle", "closed", "locally_removed", "server_revoked"}:
            raise AuthFailure("Authentication already started")
        from threadrelay.storage.isolation import records
        with self._accounts.db.connection() as con:isolations=records(con)
        if isolations:
            try:
                if self.verify_isolations is None:raise ValueError
                self.verify_isolations()
            except Exception:raise AuthFailure('Saved account isolation proof unavailable') from None
        saved = self._accounts.current()
        if restoring and saved is None:raise AuthFailure('Saved account restore unavailable')
        self._expected_account = (saved["account_id"], saved["epoch"]) if saved else None
        self._bootstrap_account = None
        self._bootstrap_network_pending = False
        if saved is not None or running:
            with self._accounts.db.connection() as con:
                uncertain = _has_native_uncertainty(con)
                reference=con.execute('SELECT vault_ref FROM accounts WHERE active=1').fetchone()
            if saved is None or saved['revocation_pending'] or uncertain:
                raise AuthFailure('Saved account restore unavailable')
            if (reference is None or reference[0] is None) and (pending or self._accounts.has_unresolved()):
                raise AuthFailure('Authentication blocked by pending sends')
            if reference and reference[0] is not None and reference[0]!=vault_ref:
                raise AuthFailure('Saved account reference changed')
            self._bootstrap_account = (saved['account_id'], saved['epoch'])
        elif pending or self._accounts.has_unresolved():
            raise AuthFailure("Authentication blocked by pending sends")
        self._bootstrap_running=running
        self._network_requested = False
        self._logout_requested = False
        self._code_delivery=None;self._resend_deadline=0.0
        self._invalidate_challenge()
        try:
            if restoring:self._client.initialize_offline(vault_ref,require_existing=True)
            else:self._client.initialize_offline(vault_ref)
        except TelegramBoundaryError:
            self.status = "authentication_unavailable"
            raise AuthFailure("Authentication unavailable") from None
        self._vault_ref = vault_ref
        self._bootstrap_network_pending = self._bootstrap_account is not None
        self._snapshot = AccountReadiness(self._snapshot.account_id, self._snapshot.epoch, "authorizing", None, "offline", self._snapshot.revocation_pending)
        self.status = "authorizing"

    @_synchronized
    def saved_restore_reference(self):
        if self.status not in {'idle','closed'} or self._accounts.cleanup_intent() is not None:return None
        saved=self._accounts.current()
        if saved is None or saved['revocation_pending']:return None
        with self._accounts.db.connection() as con:
            if _has_native_uncertainty(con):raise AuthFailure('Saved account restore unavailable')
            row=con.execute('SELECT vault_ref FROM accounts WHERE account_id=? AND epoch=? AND active=1 AND local_removed=0',
                (saved['account_id'],saved['epoch'])).fetchone()
        return row[0] if row is not None else None

    @_synchronized
    def enable_clean_auth(self, permit: NetworkPermit) -> None:
        if self.status not in {"authorizing","ready"} or permit.purpose != "clean_auth" or permit.account_epoch != self._snapshot.epoch or permit.pending or not self._client.network_config_ready:
            raise AuthFailure("Network authority unavailable")
        try:
            # Lifecycle -> database writer -> synchronous network request. This
            # makes the clean-queue check atomic with concurrent outbox changes.
            with self._accounts.db.transaction() as con:
                if _has_unresolved_work(con):
                    raise AuthFailure("Network authority unavailable")
                self._client.enable_network(permit)
            self._snapshot = replace(self._snapshot, network_state="connecting")
            self._network_requested = True
        except Exception:
            self.interrupt_clean_auth()
            raise AuthFailure("Network authority unavailable") from None

    @_synchronized
    def interrupt_clean_auth(self) -> None:
        """Latch an interrupted attempt until Cancel and observed native Closed.

        Keep ownership and the original network-request latch. Neither polling
        nor another Begin may resume this native client or reuse its challenge.
        """
        self._client.begin_shutdown()
        was_authorizing = self._snapshot.auth_state == 'authorizing'
        try:
            self._latch_terminal(revoke=False)
        finally:
            if was_authorizing:
                self._snapshot = replace(self._snapshot, auth_state='authorizing')
            self.status = 'authentication_error'

    @_synchronized
    def enable_running_bootstrap(self, session):
        if (not session.can_transmit(session._clock.now()) or self.status not in {'authorizing','ready'}
                or self._bootstrap_account is None or self._network_requested or not self._client.network_config_ready):
            raise AuthFailure('Running bootstrap unavailable')
        account_id, epoch = self._bootstrap_account
        with self._accounts.db.transaction() as con:
            self._client.enable_network(NetworkPermit('running_bootstrap', epoch, False),
                connection=con, running_account=account_id)
        self._network_requested = True
        self._snapshot = replace(self._snapshot, network_state='connecting')

    @_synchronized
    def enable_saved_connection(self):
        """Reconnect one saved identity without granting collection or sending."""
        if (self.status not in {'authorizing','ready'} or self._bootstrap_account is None
                or self._bootstrap_running or self._network_requested or not self._client.network_config_ready):
            raise AuthFailure('Saved account restore unavailable')
        account_id,epoch=self._bootstrap_account
        try:
            with self._accounts.db.transaction() as con:
                self._client.enable_network(NetworkPermit('saved_account_restore',epoch,False),
                    connection=con,running_account=account_id)
        except Exception:
            self.interrupt_clean_auth()
            raise AuthFailure('Saved account restore unavailable') from None
        self._network_requested=True
        self._snapshot=replace(self._snapshot,network_state='connecting')

    @_synchronized
    def awaiting_bootstrap_connection(self) -> bool:
        """Only the current saved-account lifecycle may start offline in Running."""
        if (not self._bootstrap_network_pending or self._bootstrap_account is None
                or self.status not in {'authorizing', 'ready'}
                or self._bootstrap_account != (self._snapshot.account_id, self._snapshot.epoch)
                or self._snapshot.revocation_pending):
            return False
        saved = self._accounts.current()
        return (saved is not None and not saved['revocation_pending']
                and self._bootstrap_account == (saved['account_id'], saved['epoch']))

    def _latch_terminal(self, *, revoke: bool, closed: bool = False) -> None:
        """Immediately remove live readiness; revoke only for observed logout."""
        self._bootstrap_network_pending = False
        old = self._snapshot
        if revoke and old.account_id is not None and not old.revocation_pending:
            self._revocation_required = True
        revoke = revoke or self._revocation_required
        self._waiting_identity = False
        self._submitted_kind = None
        self._invalidate_challenge()
        self._snapshot = replace(old, auth_state="closed", network_state="offline")
        self.status = "closed" if closed else ("revocation_pending" if revoke or old.revocation_pending else "closing")
        if old.account_id is None or old.revocation_pending:
            return
        try:
            if revoke:
                epoch = self._accounts.revoke_if_current(old.account_id, old.epoch)
                if epoch is not None:
                    self._snapshot = replace(self._snapshot, epoch=epoch, revocation_pending=True)
                else:
                    # A write may have committed before its caller saw an error.
                    # Accept only already-pending durable revocation, never a
                    # newly active account or a stale generation as authority.
                    durable = self._accounts.account_state(old.account_id)
                    if durable and durable["epoch"] > old.epoch and durable["revocation_pending"]:
                        self._snapshot = replace(self._snapshot, epoch=durable["epoch"], revocation_pending=True)
                self._revocation_required = False
            else:
                self._accounts.suspend_if_current(old.account_id, old.epoch)
        except Exception:
            # The local client stays latched; do not conceal failed durable safety.
            raise AuthFailure("Account authority unavailable") from None

    @_synchronized
    def poll(self) -> str | None:
        if self._revocation_required:
            self._latch_terminal(revoke=True)
        try:
            event = self._client.authorization_event()
        except TelegramBoundaryError:
            # An interrupted ordered receive invalidates live readiness; never
            # recover authorization merely because a later poll returns empty.
            was_networked = self._client._network_enabled
            self._client.begin_shutdown()
            try:
                self._latch_terminal(revoke=False)
            finally:
                if was_networked:
                    try:
                        self._client.close()
                    except TelegramBoundaryError:
                        pass  # authority remains latched; native closure is unproved
            self.status = "authentication_unavailable"
            raise AuthFailure("Authentication unavailable") from None
        if event is None:
            return None
        kind = event.kind
        if self.status in {"closing", "revocation_pending", "closed"} and kind not in {"closing", "logging_out", "closed", "logout_confirmed"}:
            return None
        if self._snapshot.auth_state == 'ready' and kind in {'phone','code','password','qr','ready_pending_identity','unsupported','error','submit_error','parameters'}:
            old = self._snapshot
            self._snapshot = replace(old, auth_state='authorizing')
            self.status = 'authorizing'
            try:
                if old.account_id is not None:
                    self._accounts.suspend_if_current(old.account_id, old.epoch)
            except Exception:
                self._client.begin_shutdown()
                raise AuthFailure('Account authority unavailable') from None
        if kind in {"network_online", "network_offline", "network_connecting"}:
            if kind == 'network_online':
                # Never revive the bootstrap exception after an observed live
                # connection, even if identity/enable acknowledgement arrives later.
                self._bootstrap_network_pending = False
            self._snapshot = replace(self._snapshot, network_state=kind.removeprefix("network_"))
        elif kind in {"phone", "code", "password", "qr"}:
            self._resending=False
            self._submitted_kind = None
            self._waiting_identity = False
            self._new_challenge(kind, event.private_qr, event.code_delivery)
            self.status='authorizing'
        elif kind == "ready_pending_identity":
            self._invalidate_challenge()
            self._waiting_identity = True
            self._client.get_me()
        elif kind == "identity":
            if not self._waiting_identity or event.account_id is None:
                return None
            if self._expected_account is not None and event.account_id != self._expected_account[0]:
                self._client.begin_shutdown()
                self._latch_terminal(revoke=True)
                raise AuthFailure('Saved account identity changed')
            self._waiting_identity = False
            # Retain the existing generation for a returning identity; switching
            # invalidates the old generation atomically inside AccountStore.
            previous = self._accounts.account_state(event.account_id)
            if self._expected_account is not None and (previous is None or previous['epoch'] != self._expected_account[1]):
                self._client.begin_shutdown()
                self._latch_terminal(revoke=False)
                raise AuthFailure('Saved account generation changed')
            if previous and previous["revocation_pending"] and not self._accounts.locally_removed(event.account_id):
                self.status = "account_unavailable"
                raise AuthFailure("Account identity unavailable")
            epoch = previous["epoch"] if previous else self._snapshot.epoch
            snapshot = AccountReadiness(event.account_id, epoch, "ready", None, self._snapshot.network_state, False)
            try:
                self._accounts.activate(snapshot.as_contract(), vault_ref=self._vault_ref, require_clear_revocation=True)
            except ValueError:
                self.status = "account_unavailable"
                raise AuthFailure("Account identity unavailable") from None
            self._snapshot = snapshot
            self.status = "ready"
        elif kind == "submit_error":
            if self._resending and self._code_delivery is not None:
                if event.retry_after is not None:self._resend_deadline=self._clock()+event.retry_after
                else:self._code_delivery={**self._code_delivery,'next_method':None,'resend_after_seconds':None}
            self._resending=False
            if self._submitted_kind:
                self._new_challenge(self._submitted_kind)
            self._submitted_kind = None
            self.status = "authentication_error"
            return "error"
        elif kind in {"unsupported", "error"}:
            self._waiting_identity = False
            self._invalidate_challenge()
            self.status = "unsupported_auth_state" if kind == "unsupported" else "authentication_error"
        elif kind == 'logout_confirmed':
            if self._logout_requested and self._snapshot.account_id is not None:
                self._accounts.confirm_revocation(self._snapshot.account_id, self._snapshot.epoch)
                self._snapshot = replace(self._snapshot, revocation_pending=False, auth_state='closed', network_state='offline')
                self.status = 'server_revoked'
        elif kind in {"closing", "logging_out", "closed"}:
            self._latch_terminal(revoke=(kind == "logging_out"), closed=(kind == "closed"))
        return kind

    def _check(self, challenge_id: str, kind: str) -> None:
        current = self.challenge()
        if current is None or current.challenge_id != challenge_id or current.kind != kind:
            raise AuthFailure("Invalid or expired challenge")

    @_synchronized
    def submit(self, challenge_id: str, kind: str, secret_ref: str) -> None:
        self._check(challenge_id, kind)
        if kind not in {"phone", "code", "password"}:
            raise AuthFailure("Invalid or expired challenge")
        self._invalidate_challenge()  # one-shot, including on vault failure
        self._resending=False
        try:
            value = self._handoff.take(secret_ref)
            self._client.submit_secret(kind, value, challenge_id)
            self._submitted_kind = kind
        except Exception:
            raise AuthFailure("Secure input unavailable") from None
        finally:
            value = None

    @_synchronized
    def request_qr(self, challenge_id: str) -> None:
        current=self.challenge()
        if current is None or current.kind not in {'phone','code','password'}:raise AuthFailure('Invalid or expired challenge')
        self._check(challenge_id,current.kind)
        previous_kind=current.kind
        self._resending=False
        self._invalidate_challenge()
        try:
            self._client.request_qr(challenge_id)
            self._submitted_kind = previous_kind
        except TelegramBoundaryError:
            self._new_challenge(previous_kind)
            self.status = "authentication_error"
            raise AuthFailure("Authentication unavailable") from None

    @_synchronized
    def resend_code(self, challenge_id: str) -> None:
        self._check(challenge_id,'code')
        if (self._code_delivery is None or self._code_delivery['next_method'] is None
                or self._clock()<self._resend_deadline):raise AuthFailure('Code resend unavailable')
        self._invalidate_challenge()
        self._resending=True
        try:
            self._client.resend_code(challenge_id)
            self._submitted_kind='code'
        except TelegramBoundaryError:
            self._resending=False
            self._code_delivery={**self._code_delivery,'next_method':None,'resend_after_seconds':None}
            self._new_challenge('code')
            self.status='authentication_error'
            raise AuthFailure('Authentication unavailable') from None

    @_synchronized
    def take_private_qr(self) -> str | None:
        if self.challenge() is None or self._challenge.kind != "qr":
            return None
        value, self._private_qr = self._private_qr, None
        return value

    @_synchronized
    def cancel(self) -> None:
        self._client.begin_shutdown()
        self._latch_terminal(revoke=False)
        try:
            self._client.close()
        except TelegramBoundaryError:
            raise AuthFailure("Close unavailable") from None

    @_synchronized
    def logout(self, *, pending: bool, permit: NetworkPermit | None = None) -> None:
        if self._snapshot.account_id is None or self._snapshot.auth_state != "ready":
            raise AuthFailure("Account unavailable")
        self._client.begin_shutdown()
        self._latch_terminal(revoke=True)
        if not self._snapshot.revocation_pending:
            raise AuthFailure("Account authority unavailable")
        # Invalidate first. Server logout is a separately permitted network action.
        if not pending and permit is not None and permit.account_epoch == self._snapshot.epoch:
            try:
                with self._accounts.db.transaction() as con:
                    if not _has_unresolved_work(con):
                        self._logout_requested = True
                        self._client.logout(permit)
            except Exception:
                raise AuthFailure("Remote logout unavailable") from None
