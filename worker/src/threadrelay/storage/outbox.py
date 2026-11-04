"""Identity-bound durable intents and ordered own-send correlation.

Claiming reserves storage work; it is NOT a send permit. The later routing and
transport layers must re-read current route/account/content and synchronize
with Running immediately before any network dispatch. Only Pending is claimed;
Retryable requires the later adapter's explicit existing-message retry path.
Unknown has no automatic outgoing transition and cannot become a fresh send.
"""
from contextlib import nullcontext
import json
import re
import sqlite3
from typing import Any
from uuid import uuid4

from threadrelay.contracts import validate_contract
from .database import Database, encode

Delivery = dict[str, Any]
TelegramAttempt = dict[str, Any]


def _has_unresolved_work(con: sqlite3.Connection) -> bool:
    from .isolation import isolated_work
    isolated=isolated_work(con)[0]
    return any(row['state']!='unknown' or isolated.get(row['id'])!=row['payload']
        for row in con.execute("SELECT id,payload,state FROM deliveries WHERE state IN ('pending','sending','retryable','unknown')"))


def _has_native_uncertainty(con: sqlite3.Connection) -> bool:
    """Pristine app queue entries are independent of a saved TDLib connection."""
    from .isolation import isolated_work
    isolated_deliveries,isolated_attempts,isolated_latches=isolated_work(con)
    rows=con.execute("""SELECT d.id,d.payload,d.state FROM deliveries d
        LEFT JOIN routing_delivery_context r ON r.delivery_id=d.id
        LEFT JOIN workflow_delivery_context w ON w.delivery_id=d.id
        WHERE d.state IN ('sending','retryable','unknown') OR (d.state='pending' AND (
            json_extract(d.payload,'$.attempt_id') IS NOT NULL OR json_extract(d.payload,'$.request_extra') IS NOT NULL
            OR json_extract(d.payload,'$.message_id') IS NOT NULL OR json_extract(d.payload,'$.temporary_message_id') IS NOT NULL
            OR json_extract(d.payload,'$.final_message_id') IS NOT NULL OR r.initiated_attempt_id IS NOT NULL
            OR w.initiated_attempt_id IS NOT NULL OR EXISTS(SELECT 1 FROM telegram_attempts t WHERE t.delivery_id=d.id)))
        """).fetchall()
    if any(row['state']!='unknown' or isolated_deliveries.get(row['id'])!=row['payload'] for row in rows):return True
    for row in con.execute("SELECT * FROM telegram_attempts WHERE state IN ('pending','sending','retryable','unknown')"):
        if row['state']!='unknown' or isolated_attempts.get(row['attempt_id'])!=dict(row):return True
    for row in con.execute("SELECT name,payload FROM local_policy WHERE name GLOB 'automatic-incomplete:*' OR name GLOB 'manual-history-incomplete:*'"):
        if isolated_latches.get(row['name'])!=row['payload']:return True
    return False


def _set_state(con: sqlite3.Connection, row: sqlite3.Row, state: str, now: float) -> None:
    value = json.loads(row["payload"])
    value["state"] = state
    con.execute("UPDATE deliveries SET state=?,payload=?,updated_at=? WHERE id=?", (state, encode(value), now, row["id"]))
    if value["attempt_id"] is not None:
        con.execute("UPDATE telegram_attempts SET state=? WHERE attempt_id=?", (state, value["attempt_id"]))


def _current_account(con: sqlite3.Connection, value: dict[str, Any]) -> None:
    row = con.execute("SELECT * FROM accounts WHERE account_id=? AND active=1", (value["account_id"],)).fetchone()
    if row is None or row["epoch"] != value["account_epoch"]:
        raise ValueError("Inactive or stale account binding")
    snapshot = json.loads(row["snapshot"])
    if snapshot["auth_state"] != "ready" or snapshot["revocation_pending"]:
        raise ValueError("Unavailable account binding")


def _approval_bindings(con: sqlite3.Connection, delivery: Delivery) -> None:
    for ref in delivery["route_refs"]:
        if ref["approval_id"] is None:
            continue  # Auto-mode authorization belongs to Task 6.
        row = con.execute("SELECT payload FROM approvals WHERE id=? AND valid=1", (ref["approval_id"],)).fetchone()
        approval = json.loads(row[0]) if row else {}
        keys = ("account_id", "account_epoch", "sender", "post_id", "content_hash", "destination_id")
        if any(approval.get(key) != delivery[key] for key in keys) or any(approval.get(key) != ref[key] for key in ("route_id", "route_version")):
            raise ValueError("Approval identity/content/route binding mismatch")


def _invalidate(con: sqlite3.Connection, account_id: str, now: float) -> int:
    row = con.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown account")
    epoch = row["epoch"] + 1
    validate_contract("AccountEpoch", epoch)
    snapshot = json.loads(row["snapshot"])
    snapshot.update(epoch=epoch, auth_state="closed", network_state="offline", revocation_pending=True)
    con.execute("UPDATE accounts SET epoch=?,active=0,snapshot=? WHERE account_id=?", (epoch, encode(snapshot), account_id))
    con.execute("INSERT OR IGNORE INTO account_revocations(account_id,epoch,snapshot) VALUES (?,?,?)", (account_id,epoch,encode(snapshot)))
    con.execute("UPDATE approvals SET valid=0 WHERE account_id=?", (account_id,))
    for item in con.execute("SELECT * FROM deliveries WHERE account_id=? AND state IN ('pending','retryable','sending')", (account_id,)).fetchall():
        _set_state(con, item, "unknown" if item["state"] == "sending" else "cancelled", now)
    return epoch


class AccountStore:
    """Non-secret identity/reference persistence, not authentication or logout.

    activate takes a trusted verified snapshot; it does not query/getMe or infer
    identity from a current login. Epoch invalidation does not claim server-side
    revocation: revocation_pending remains set until trusted onboarding resolves it.
    """
    def __init__(self, database: Database):
        self.db = database

    def activate(self, snapshot: dict[str, Any], *, vault_ref: str | None = None, session_ref: str | None = None, require_clear_revocation: bool = False) -> None:
        validate_contract("AccountSnapshot", snapshot)
        if snapshot["account_id"] is None or snapshot["auth_state"] != "ready" or snapshot["revocation_pending"]:
            raise ValueError("A verified ready account snapshot is required")
        for prefix, value in (("vault", vault_ref), ("session", session_ref)):
            if value is not None and (not isinstance(value, str) or re.fullmatch(prefix + r":[A-Za-z0-9_-]{1,100}", value) is None):
                raise ValueError("Only opaque account references may be stored")
        with self.db.transaction() as con:
            old = con.execute("SELECT epoch,snapshot,local_removed FROM accounts WHERE account_id=?", (snapshot["account_id"],)).fetchone()
            if old and require_clear_revocation and not old["local_removed"] and json.loads(old["snapshot"])["revocation_pending"]:
                raise ValueError("Account revocation pending")
            if old and snapshot["epoch"] < old["epoch"]:
                raise ValueError("Stale account epoch")
            # Validate desired identity first; switching must not partially revoke.
            for active in con.execute("SELECT account_id,epoch FROM accounts WHERE active=1").fetchall():
                if active["account_id"] != snapshot["account_id"] or active["epoch"] != snapshot["epoch"]:
                    _invalidate(con, active["account_id"], self.db.now())
            con.execute("""INSERT INTO accounts(account_id,epoch,snapshot,active,vault_ref,session_ref) VALUES (?,?,?,1,?,?)
                ON CONFLICT(account_id) DO UPDATE SET epoch=excluded.epoch,snapshot=excluded.snapshot,active=1,local_removed=0,
                vault_ref=COALESCE(excluded.vault_ref,accounts.vault_ref),session_ref=COALESCE(excluded.session_ref,accounts.session_ref)""",
                (snapshot["account_id"], snapshot["epoch"], encode(snapshot), vault_ref, session_ref))

    def invalidate_epoch(self, account_id: str) -> int:
        validate_contract("TelegramId", account_id)
        with self.db.transaction() as con:
            return _invalidate(con, account_id, self.db.now())

    def suspend_if_current(self, account_id: str, epoch: int) -> bool:
        """Close live readiness without revoking a preserved login or epoch."""
        validate_contract("TelegramId", account_id)
        validate_contract("AccountEpoch", epoch)
        with self.db.transaction() as con:
            row = con.execute("SELECT snapshot FROM accounts WHERE account_id=? AND epoch=? AND active=1", (account_id, epoch)).fetchone()
            if row is None:
                return False
            snapshot = json.loads(row["snapshot"])
            snapshot.update(auth_state="closed", network_state="offline")
            con.execute("UPDATE accounts SET snapshot=? WHERE account_id=? AND epoch=? AND active=1", (encode(snapshot), account_id, epoch))
            return True

    def revoke_if_current(self, account_id: str, epoch: int) -> int | None:
        """Atomically revoke only the observed generation, at most once."""
        validate_contract("TelegramId", account_id)
        validate_contract("AccountEpoch", epoch)
        with self.db.transaction() as con:
            row = con.execute("SELECT epoch,active,snapshot FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None or row["epoch"] != epoch:
                return None
            if json.loads(row["snapshot"])["revocation_pending"]:
                return epoch
            if not row["active"]:
                return None
            return _invalidate(con, account_id, self.db.now())

    def confirm_revocation(self, account_id, epoch):
        with self.db.transaction() as con:
            row = con.execute('SELECT snapshot FROM accounts WHERE account_id=? AND epoch=? AND active=0', (account_id, epoch)).fetchone()
            if row is None:
                raise ValueError('Revocation binding changed')
            snapshot = json.loads(row[0])
            snapshot['revocation_pending'] = False
            con.execute('UPDATE account_revocations SET confirmed=1 WHERE account_id=? AND epoch=?', (account_id, epoch))
            con.execute('UPDATE accounts SET snapshot=? WHERE account_id=? AND epoch=?', (encode(snapshot), account_id, epoch))

    def cleanup_intent(self):
        with self.db.connection() as con:
            row = con.execute("SELECT payload FROM local_policy WHERE name='account-cleanup'").fetchone()
        return json.loads(row[0]) if row else None

    def save_cleanup_intent(self, mode, account_id, epoch, *, failed=False):
        if mode not in {'server_logout','remove_local_offline'}:
            raise ValueError('Invalid cleanup operation')
        with self.db.transaction() as con:
            row = con.execute('SELECT 1 FROM accounts WHERE account_id=? AND epoch=? AND active=0', (account_id,epoch)).fetchone()
            if row is None:
                raise ValueError('Cleanup binding changed')
            payload = encode({'mode':mode,'account_id':account_id,'epoch':epoch,'failed':failed})
            con.execute("INSERT INTO local_policy VALUES ('account-cleanup',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (payload,))

    def revocation_confirmed(self, account_id, epoch):
        with self.db.connection() as con:
            return con.execute('SELECT 1 FROM account_revocations WHERE account_id=? AND epoch=? AND confirmed=1', (account_id,epoch)).fetchone() is not None

    def forget_local_reference(self, account_id, epoch):
        with self.db.transaction() as con:
            changed = con.execute('UPDATE accounts SET vault_ref=NULL,session_ref=NULL,local_removed=1 WHERE account_id=? AND epoch=? AND active=0', (account_id, epoch)).rowcount
            if not changed:
                raise ValueError('Cleanup binding changed')
            con.execute("DELETE FROM local_policy WHERE name='account-cleanup'")

    def has_unresolved(self) -> bool:
        """Conservative clean-auth preflight; not a synchronized network permit."""
        with self.db.connection() as con:
            return _has_unresolved_work(con)

    def account_state(self, account_id: str) -> dict[str, Any] | None:
        """Read non-secret generation/revocation state, including inactive accounts."""
        validate_contract("TelegramId", account_id)
        with self.db.connection() as con:
            row = con.execute("SELECT snapshot FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def locally_removed(self, account_id):
        with self.db.connection() as con:
            row = con.execute('SELECT local_removed FROM accounts WHERE account_id=?', (account_id,)).fetchone()
        return bool(row and row[0])

    def pending_revocations(self) -> list[dict[str, Any]]:
        with self.db.connection() as con:
            rows = con.execute('SELECT snapshot FROM account_revocations WHERE confirmed=0 ORDER BY epoch DESC').fetchall()
        values = {}
        for row in rows:
            snapshot = json.loads(row[0])
            values.setdefault(snapshot['account_id'], snapshot)
        return list(values.values())

    def current(self) -> dict[str, Any] | None:
        with self.db.connection() as con:
            row = con.execute("SELECT snapshot FROM accounts WHERE active=1").fetchone()
        return json.loads(row[0]) if row else None


class Outbox:
    def __init__(self, database: Database):
        self.db = database

    def save_approval(self, approval: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        validate_contract("Approval", approval)
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as con:
            _current_account(con, approval)
            post = con.execute("SELECT content_hash FROM posts WHERE post_id=?", (approval["post_id"],)).fetchone()
            if post is None or post[0] != approval["content_hash"]:
                raise ValueError("Approval does not match observed content")
            previous = con.execute("SELECT payload,valid FROM approvals WHERE id=?", (approval["id"],)).fetchone()
            if previous:
                if previous["payload"] != encode(approval) or not previous["valid"]:
                    raise ValueError("Approval identities are immutable")
                return
            con.execute("INSERT INTO approvals(id,account_id,post_id,payload,valid,created_at) VALUES (?,?,?,?,1,?)", (approval["id"], approval["account_id"], approval["post_id"], encode(approval), self.db.now()))

    def enqueue(self, delivery: Delivery, *, connection: sqlite3.Connection | None = None) -> str:
        validate_contract("Delivery", delivery)
        if delivery["state"] != "pending" or any(delivery[key] is not None for key in ("attempt_id", "request_extra", "message_id", "temporary_message_id", "final_message_id")):
            raise ValueError("Only a pristine pending delivery may be enqueued")
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as con:
            _current_account(con, delivery)
            existing = con.execute("SELECT id FROM deliveries WHERE account_id=? AND post_id=? AND destination_id=?", (delivery["account_id"], delivery["post_id"], delivery["destination_id"])).fetchone()
            if existing:
                return existing[0]  # Sender/content edits never bypass dedup.
            post = con.execute("SELECT content_hash FROM posts JOIN post_text USING(post_id) WHERE post_id=?", (delivery["post_id"],)).fetchone()
            if post is None or post[0] != delivery["content_hash"]:
                raise ValueError("Delivery must bind retained observed content")
            _approval_bindings(con, delivery)
            now = self.db.now()
            con.execute("INSERT INTO deliveries(id,account_id,account_epoch,post_id,destination_id,content_hash,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)", (delivery["id"], delivery["account_id"], delivery["account_epoch"], delivery["post_id"], delivery["destination_id"], delivery["content_hash"], encode(delivery), now, now))
            return delivery["id"]

    def get(self, id: str) -> Delivery | None:
        with self.db.connection() as con:
            row = con.execute("SELECT payload FROM deliveries WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def attempts(self, delivery_id: str) -> list[TelegramAttempt]:
        with self.db.connection() as con:
            return [dict(row) for row in con.execute("SELECT delivery_id,attempt_id,request_extra,temporary_message_id,final_message_id,state FROM telegram_attempts WHERE delivery_id=? ORDER BY rowid", (delivery_id,))]

    def claim_next(self, worker_id: str, *, exclude_ids=(), only_id=None, exclude_scheduled=False) -> Delivery | None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("Invalid worker identity")
        with self.db.transaction() as con:
            excluded = list(exclude_ids)
            clause = ' AND id NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
            if exclude_scheduled and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_queue'").fetchone():
                clause += ' AND id NOT IN (SELECT delivery_id FROM route_queue)'
            if only_id is not None:
                clause += ' AND id=?'
                excluded.append(only_id)
            for row in con.execute("SELECT * FROM deliveries WHERE state='pending'" + clause + " ORDER BY created_at,id LIMIT 128", excluded).fetchall():
                value = json.loads(row["payload"])
                if value["id"] in exclude_ids:
                    continue
                try:
                    _current_account(con, value)
                    _approval_bindings(con, value)
                except ValueError:
                    _set_state(con, row, "cancelled", self.db.now())
                    continue
                post = con.execute("SELECT content_hash FROM posts JOIN post_text USING(post_id) WHERE post_id=?", (value["post_id"],)).fetchone()
                if post is None or post[0] != value["content_hash"]:
                    _set_state(con, row, "cancelled", self.db.now())
                    continue
                value.update(state="sending", attempt_id=str(uuid4()), request_extra=str(uuid4()))
                con.execute("UPDATE deliveries SET state='sending',payload=?,worker_id=?,updated_at=? WHERE id=? AND state='pending'", (encode(value), worker_id, self.db.now(), value["id"]))
                con.execute("INSERT INTO telegram_attempts(delivery_id,attempt_id,request_extra,state) VALUES (?,?,?,'sending')", (value["id"], value["attempt_id"], value["request_extra"]))
                return value
        return None

    def cancel_pending(self, id):
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM deliveries WHERE id=? AND state='pending'", (id,)).fetchone()
            if row is not None:
                _set_state(con, row, 'cancelled', self.db.now())

    def release_unstarted(self, claim, *, defer, before_native=False):
        """Return ONLY an exact durably proven uninitiated claim, never an ambiguous attempt."""
        with self.db.transaction() as con:
            row = con.execute("SELECT d.*,c.initiated_attempt_id FROM deliveries d JOIN routing_delivery_context c ON c.delivery_id=d.id WHERE d.id=?", (claim['id'],)).fetchone()
            if (row is None or row['state'] != 'sending' or json.loads(row['payload']) != claim
                    or (row['initiated_attempt_id'] is not None and not (before_native and row['initiated_attempt_id'] == claim['attempt_id']))):
                return False
            con.execute("UPDATE routing_delivery_context SET initiated_attempt_id=NULL WHERE delivery_id=?", (claim['id'],))
            con.execute("UPDATE telegram_attempts SET state='cancelled' WHERE attempt_id=? AND request_extra=? AND state='sending'", (claim['attempt_id'], claim['request_extra']))
            value = {**claim, 'state':'pending' if defer else 'cancelled', 'attempt_id':None, 'request_extra':None}
            con.execute('UPDATE deliveries SET state=?,payload=?,worker_id=NULL,updated_at=? WHERE id=?', (value['state'], encode(value), self.db.now(), claim['id']))
            return True

    def unknown_retry(self, id: str, expected_attempt: str) -> bool:
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM deliveries WHERE id=? AND state='retryable'", (id,)).fetchone()
            if row is None or json.loads(row['payload'])['attempt_id'] != expected_attempt:
                return False
            _set_state(con, row, 'unknown', self.db.now())
            return True

    def claim_retry(self, id: str, expected_attempt: str, worker_id: str) -> Delivery | None:
        """Commit a new identity only for an exact confirmed retryable attempt.

        This reserves storage only; the delivery coordinator must separately
        establish native evidence, renewed authority and an existing-message retry.
        """
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("Invalid worker identity")
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM deliveries WHERE id=? AND state='retryable'", (id,)).fetchone()
            if row is None: return None
            value = json.loads(row['payload'])
            context = con.execute('SELECT initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?', (id,)).fetchone()
            if value['attempt_id'] != expected_attempt or context is None or context[0] != expected_attempt:
                return None
            try:
                _current_account(con, value)
                _approval_bindings(con, value)
            except ValueError:
                _set_state(con, row, 'cancelled', self.db.now())
                return None
            value.update(state='sending', attempt_id=str(uuid4()), request_extra=str(uuid4()),
                temporary_message_id=None, final_message_id=None, message_id=None)
            con.execute("UPDATE deliveries SET state='sending',payload=?,worker_id=?,updated_at=? WHERE id=?", (encode(value), worker_id, self.db.now(), id))
            con.execute("INSERT INTO telegram_attempts(delivery_id,attempt_id,request_extra,state) VALUES (?,?,?,'sending')", (id, value['attempt_id'], value['request_extra']))
            con.execute('UPDATE routing_delivery_context SET initiated_attempt_id=NULL WHERE delivery_id=?', (id,))
            return value

    def transition(self, id: str, expected_state: str, next_state: str, metadata: TelegramAttempt) -> bool:
        validate_contract("TelegramAttempt", metadata)
        allowed = {"sending": {"sending", "sent", "retryable", "failed", "unknown", "cancelled"}}
        if next_state not in allowed.get(expected_state, set()) or metadata["delivery_id"] != id or metadata["state"] != next_state:
            return False
        if next_state != "sent" and metadata["final_message_id"] is not None:
            raise ValueError("Only confirmed Sent may carry a final message ID")
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM deliveries WHERE id=? AND state=?", (id, expected_state)).fetchone()
            if row is None:
                return False
            value = json.loads(row["payload"])
            if metadata["attempt_id"] != value["attempt_id"] or metadata["request_extra"] != value["request_extra"]:
                return False
            # A delayed initial Message cannot erase or replace known correlation.
            if value["temporary_message_id"] is not None and metadata["temporary_message_id"] not in (None, value["temporary_message_id"]):
                return False
            temporary = value["temporary_message_id"] or metadata["temporary_message_id"]
            value.update(state=next_state, temporary_message_id=temporary, final_message_id=metadata["final_message_id"], message_id=metadata["final_message_id"])
            con.execute("UPDATE deliveries SET state=?,payload=?,updated_at=? WHERE id=?", (next_state, encode(value), self.db.now(), id))
            con.execute("UPDATE telegram_attempts SET state=?,temporary_message_id=?,final_message_id=? WHERE attempt_id=? AND request_extra=?", (next_state, temporary, metadata["final_message_id"], value["attempt_id"], value["request_extra"]))
            return True
