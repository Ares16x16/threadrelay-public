"""One bounded owner for TDLib receive, bootstrap, pending work and deadlines."""
from dataclasses import replace
import math
from queue import Empty, Full, Queue
from threading import Event, Thread
import time

from .telegram import NetworkPermit

MAX_RECEIVE_EVENTS = 256
RECEIVE_BUDGET_SECONDS = .020


class DeliveryOwner:
    def __init__(self, sender, telegram, session, *, native_gate=lambda: False,
                 auth_gate=None, vault_reference=None, maintenance=lambda: None, remove_local=None, workflow=None, listing=None, owner_test=None, owner_history=None, owner_automatic=None):
        self.sender, self.telegram, self.session = sender, telegram, session
        self.native_gate, self.vault_reference = native_gate, vault_reference
        self.auth_gate = native_gate if auth_gate is None else auth_gate
        self.maintenance, self.remove_local = maintenance, remove_local
        self.workflow = workflow
        self.listing = listing
        self.owner_test = owner_test
        self.owner_history = owner_history
        self.owner_automatic = owner_automatic
        self.recovery=None
        self._receive_clock = time.monotonic
        self._queue = Queue(maxsize=16)
        self._done = Event()
        self._running = None
        intent = sender.auth._accounts.cleanup_intent()
        self._account_removal = (intent['mode'], intent['account_id'], intent['epoch']) if intent else None
        self._cleanup_failed = bool(intent and intent['failed'])
        self._thread = Thread(target=self._run, name='ordered-telegram-owner', daemon=True)
        self._thread.start()

    def _enqueue(self, operation):
        if self._done.is_set():
            return False
        try:
            self._queue.put_nowait(operation)
            return True
        except Full:
            return False

    def wake(self):
        # Wake is merely a hint; bounded periodic progression cannot lose work.
        self._enqueue(('wake', None))

    def bind(self, cancelled):
        owner = self
        class BoundDelivery:
            def start_next(self, worker_id):
                if not cancelled.is_set():
                    owner.wake()
        return BoundDelivery()

    def running(self, config, cancelled):
        if config.get('mode') != 'dry_run' and self.native_gate() and not self._enqueue(('reconnect', cancelled)):
            self.session.require_attention('telegram_owner_unavailable')

    def account_action(self, mode):
        if mode not in {'server_logout','remove_local_offline'}:
            raise ValueError('Invalid account operation')
        with self.session.authority_lock:
            if self._account_removal is not None and not (mode == 'remove_local_offline' and (self._account_removal[0] == 'server_logout' or self._cleanup_failed)):
                raise ValueError('Account cleanup pending')
            auth = self.sender.auth
            old = auth.snapshot()
            if old.account_id is None:
                raise ValueError('Account unavailable')
            # Revoke route/approval epoch before any remote or local destructive action.
            self.session.pause('account_revoked')
            self._running = None
            if mode == 'server_logout':
                if not self.auth_gate():
                    raise ValueError('Native account authority unavailable')
                auth.logout(pending=False, permit=NetworkPermit('clean_auth', old.epoch + 1, False))
            else:
                auth._latch_terminal(revoke=True)
                if self.telegram._client is not None and not self.telegram._client._closed:
                    self.telegram.close()
            self._account_removal = (mode, auth.snapshot().account_id, auth.snapshot().epoch)
            auth._accounts.save_cleanup_intent(*self._account_removal)
            self._cleanup_failed = False
            self.wake()

    def _pump_receive(self):
        """Drain queued updates in order, yielding for lifecycle work promptly.

        TDLib startup/chat loading can place hundreds of unrelated updates ahead
        of a correlated response. Keep the existing sole Sender/AuthController
        receive path, bounded by both count and elapsed time under its lock.
        """
        clock = getattr(self, '_receive_clock', time.monotonic)
        try:started = clock()
        except Exception:started = None
        for _ in range(MAX_RECEIVE_EVENTS):
            self.sender.poll()
            if not getattr(self.telegram, '_last_receive_had_event', False):
                return True
            try:now = clock()
            except Exception:return False
            if (started is None or not math.isfinite(started) or not math.isfinite(now)
                    or now < started or now - started >= RECEIVE_BUDGET_SECONDS):
                return False
        return False

    def step(self, operation=None, argument=None):
        """One bounded iteration; tests drive this with an injected monotonic clock."""
        with self.session.authority_lock:
            if operation == 'reconnect' and not argument.is_set():
                if not self.native_gate():
                    raise ValueError('Native capability unavailable')
                self._running = argument
                auth = self.sender.auth
                if self.telegram._client is None or self.telegram._client._closed:
                    if not self.native_gate() or self.vault_reference is None:
                        raise ValueError('Account bootstrap unavailable')
                    auth.begin(self.vault_reference(), pending=False, running=True)
                elif auth.status == 'ready':
                    self.sender.enable_running_network()
            if self.telegram._client is not None:
                self.sender.expire_deadlines()
                auth = self.sender.auth
                if (auth.status == 'authorizing' and auth._bootstrap_account is None
                        and auth._network_requested and not self.telegram._network_enabled):
                    # Session cleanup may revoke a clean-auth transport. Keep
                    # that revocation terminal for this attempt; never silently
                    # reconnect it or leave an unusable challenge on screen.
                    auth.interrupt_clean_auth()
                self._pump_receive()
                auth = self.sender.auth
                active = self._running is not None and not self._running.is_set() and self.session.can_transmit(self.session._clock.now())
                # A deliberate begin request grants only this clean-auth progression.
                if (auth.status in {'authorizing','ready'} and not auth._network_requested
                        and self.telegram.network_config_ready and self.auth_gate()):
                    if auth._bootstrap_account is not None:
                        if not auth._bootstrap_running:
                            auth.enable_saved_connection()
                        elif active and self.native_gate():
                            auth.enable_running_bootstrap(self.session)
                    elif auth.status in {'authorizing','ready'}:
                        auth.enable_clean_auth(NetworkPermit('clean_auth', auth.snapshot().epoch, False))
                if self.native_gate():
                    self.sender.maintain()
                if active and self.native_gate() and auth.status == 'ready' and self.telegram.transport_ready:
                    # One start per tick bounds work, but all destinations/backlog
                    # progress without another collector observation. Every start
                    # still re-reads current authority under the writer/lifecycle gate.
                    if self.sender.prepare_destination() and self.sender._prepared_delivery_id is not None:
                        self.sender.start_next('ordered-owner', delivery_id=self.sender._prepared_delivery_id)
            if getattr(self, 'owner_test', None) is not None:
                self.owner_test.tick()
            if getattr(self, 'owner_history', None) is not None:
                self.owner_history.tick()
            if getattr(self, 'owner_automatic', None) is not None:
                self.owner_automatic.tick()
            if getattr(self, 'listing', None) is not None:
                self.listing.tick()
            if getattr(self, 'workflow', None) is not None:
                self.workflow.tick()
            if getattr(self,'recovery',None) is not None:self.recovery.tick()
            if self._account_removal is not None:
                mode, account_id, epoch = self._account_removal
                closed = self.telegram._client is None or self.telegram._client._closed
                auth = self.sender.auth
                if self._cleanup_failed:
                    auth.status = 'cleanup_pending'
                # Remote mode needs positive correlated server acknowledgement AND
                # Closed. No timeout or local deletion claims remote revocation.
                if not self._cleanup_failed and closed and (mode == 'remove_local_offline' or auth._accounts.revocation_confirmed(account_id, epoch)):
                    try:
                        if self.remove_local is None:
                            raise ValueError('Secure cleanup unavailable')
                        self.remove_local()
                        auth._accounts.forget_local_reference(account_id, epoch)
                    except Exception:
                        self._cleanup_failed = True
                        auth.status = 'cleanup_pending'
                        auth._accounts.save_cleanup_intent(mode, account_id, epoch, failed=True)
                        return  # No automatic retry of partial cleanup; explicit action required.
                    auth._snapshot = replace(auth.snapshot(), account_id=None, auth_state='logged_out', network_state='offline', revocation_pending=False)
                    auth.status = 'locally_removed' if mode == 'remove_local_offline' else 'server_revoked'
                    self._account_removal = None
        self.maintenance()

    def _run(self):
        while not self._done.is_set():
            try:
                operation, argument = self._queue.get(timeout=.1)
            except Empty:
                operation = argument = None
            if self._done.is_set():
                break
            try:
                self.step(operation, argument)
            except Exception:
                if self.session.snapshot['state'] in {'starting', 'running'}:
                    self.session.require_attention('telegram_owner_failed')
            # Wakeups cannot turn a busy approval stream into an unbounded pump.
            self._done.wait(.1)

    def close(self, timeout=5):
        self._done.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()
