"""Configurable proposed 7/30/7-day defaults; not confirmed product policy.

Text includes author/quote/provenance body JSON. Dedup expiry deliberately
permits future rediscovery: the UI must disclose that trade-off. Logical SQL
deletion is not forensic secure erasure, and doesn't prune external backups or
TDLib's separately encrypted native caches. A still-Sending attempt retains
its identity/correlation even after body expiry, until an actual result or
legitimate crash recovery resolves its state; TTL is not a transport outcome.
"""
from datetime import datetime, timedelta
from typing import Any

from .database import Database, utc_timestamp
from .outbox import _set_state


class Retention:
    def __init__(self, database: Database, *, text_days: int = 7, dedup_days: int = 30, diagnostic_days: int = 7):
        if any(type(value) is not int or value <= 0 for value in (text_days, dedup_days, diagnostic_days)) or dedup_days < text_days:
            raise ValueError("Retention days must be positive, dedup at least text")
        self.db = database
        self.text = timedelta(days=text_days).total_seconds()
        self.dedup = timedelta(days=dedup_days).total_seconds()
        self.diagnostics = timedelta(days=diagnostic_days).total_seconds()

    def prune(self, now: datetime) -> dict[str, Any]:
        timestamp = utc_timestamp(now)
        with self.db.transaction() as con:
            expired = [row[0] for row in con.execute("SELECT post_id FROM posts WHERE last_seen<=? AND post_id IN (SELECT post_id FROM post_text) ORDER BY last_seen,post_id LIMIT 200", (timestamp-self.text,))]
            for post_id in expired:
                for row in con.execute("SELECT * FROM deliveries WHERE post_id=? AND state IN ('pending','retryable')", (post_id,)).fetchall():
                    _set_state(con, row, "cancelled", timestamp)
            text = 0
            for post_id in expired:
                text += con.execute("DELETE FROM post_text WHERE post_id=?", (post_id,)).rowcount
            approvals = con.execute("DELETE FROM approvals WHERE id IN (SELECT id FROM approvals WHERE created_at<=? OR post_id NOT IN (SELECT post_id FROM post_text) LIMIT 200)", (timestamp-self.dedup,)).rowcount
            deliveries = con.execute("DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries WHERE created_at<=? AND state!='sending' LIMIT 200)", (timestamp-self.dedup,)).rowcount
            posts = con.execute("DELETE FROM posts WHERE post_id IN (SELECT post_id FROM posts WHERE last_seen<=? AND post_id NOT IN (SELECT post_id FROM deliveries) LIMIT 200)", (timestamp-self.dedup,)).rowcount
            diagnostics = con.execute("DELETE FROM diagnostics WHERE id IN (SELECT id FROM diagnostics WHERE created_at<=? LIMIT 200)", (timestamp-self.diagnostics,)).rowcount
        return {"deleted_counts": {"post_text": text, "posts": posts, "approvals": approvals, "deliveries": deliveries, "diagnostics": diagnostics}}
