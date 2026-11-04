"""Single-profile ownership and transactional SQLite primitives.

The owner lock is process-held; each operation uses its own SQLite connection.
Claims therefore synchronize in SQLite, not merely a Python thread lock. No
connection is shared between threads. This is a local filesystem database, not
an NFS/multi-host coordination mechanism. Backup files contain private app data
and opaque references; redacted_export is the separate sharing boundary.
"""
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Condition
from typing import Any


class DatabaseInUse(RuntimeError):
    """Another owner already holds this profile/database."""


def utc_timestamp(value: datetime) -> float:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("A timezone-aware UTC datetime is required")
    return value.timestamp()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _statements(script: str) -> Iterator[str]:
    pending = ""
    for char in script:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        # A trailing comment is harmless, but an unterminated statement is not.
        if any(line.strip() and not line.lstrip().startswith("--") for line in pending.splitlines()):
            raise ValueError("Migration has an incomplete statement")


class Database:
    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] | None = None,
        migrations: Sequence[tuple[int, Path]] | None = None,
    ):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._condition = Condition()
        self._operations = 0
        self._closed = False
        self._lock_file = open(str(self.path) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0)
                if not self._lock_file.read(1):
                    self._lock_file.write(b"\0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise DatabaseInUse("Profile database is already in use") from None
        try:
            # Restrict application data before SQLite creates WAL/shared files.
            os.chmod(self._lock_file.name, 0o600)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            selected = list(migrations) if migrations is not None else [
                (version, files(__package__).joinpath(f"migrations/{name}"))
                for version, name in ((1, "001_initial.sql"), (2, "002_routing.sql"), (3, "003_local_policy.sql"), (4, "004_collection.sql"), (5, "005_archive.sql"), (6, "006_delivery_workflow.sql"), (7, "007_activity.sql"), (8, "008_route_queue.sql"))
            ]
            with self.connection() as con:
                current_version = con.execute("PRAGMA user_version").fetchone()[0]
                if current_version > max((version for version, _ in selected), default=0):
                    raise ValueError("Database schema is newer than this application")
                con.execute("PRAGMA journal_mode=WAL")
                for version, resource in selected:
                    if version <= con.execute("PRAGMA user_version").fetchone()[0]:
                        continue
                    if version != con.execute("PRAGMA user_version").fetchone()[0] + 1:
                        raise ValueError("Migration versions must be consecutive")
                    con.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in _statements(resource.read_text()):
                            con.execute(statement)
                        con.execute(f"PRAGMA user_version={version}")
                        con.commit()
                    except BaseException:
                        con.rollback()
                        raise
            self.recovery = self.recover()
            if self.schema_version >= 5:
                with self.transaction() as con:
                    con.execute("UPDATE archive_runs SET status='cancelled',error_code='application_interrupted',finished_at=? WHERE status='running'", (self.now(),))
        except BaseException:
            self.close()
            raise

    def now(self) -> float:
        return utc_timestamp(self._clock())

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._condition:
            if self._closed:
                raise RuntimeError("Database is closed")
            self._operations += 1
        try:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("PRAGMA secure_delete=ON")
                yield con
            finally:
                con.close()
        finally:
            with self._condition:
                self._operations -= 1
                self._condition.notify_all()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    @property
    def schema_version(self) -> int:
        with self.connection() as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    def recover(self) -> dict[str, Any]:
        """Observational recovery only: no account/network/session activation."""
        from .outbox import _set_state
        with self.transaction() as con:
            rows = con.execute("SELECT * FROM deliveries WHERE state IN ('sending','retryable')").fetchall()
            reasons = []
            if any(row['state'] == 'sending' for row in rows): reasons.append('unclean_sending')
            if any(row['state'] == 'retryable' for row in rows): reasons.append('retry_evidence_not_durable')
            for row in rows:
                _set_state(con, row, "unknown", self.now())
            con.execute("UPDATE telegram_attempts SET state='unknown' WHERE state='sending'")
        return {"counts": {"unknown": len(rows)}, "reasons": reasons}

    def record_diagnostic(self, code: str) -> None:
        # Static identifiers only, not raw exception/TDLib/message payloads.
        if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("Diagnostic code must be a static identifier")
        with self.transaction() as con:
            con.execute("INSERT INTO diagnostics(code,created_at) VALUES (?,?)", (code, self.now()))

    def backup(self, destination: str | Path) -> None:
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("Backup cannot overwrite the source")
        # Exclusive creation prevents accidental overwrite of another backup.
        with target.open("xb"):
            pass
        os.chmod(target, 0o600)
        try:
            with self.connection() as source, sqlite3.connect(target) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def redacted_export(self) -> dict[str, Any]:
        """Return a previewable allowlist, not a raw DB dump or profile archive."""
        with self.connection() as con:
            counts = dict(con.execute("SELECT state,COUNT(*) FROM deliveries GROUP BY state").fetchall())
            history = [dict(row) for row in con.execute("SELECT state,created_at,updated_at FROM deliveries ORDER BY created_at,id")]
        return {"format_version": 1, "delivery_counts": counts, "history": history}

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.wait_for(lambda: self._operations == 0)
        # Closing releases the OS lock. Never unlink its inode while owned.
        self._lock_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
