"""Separate content retention from stable post/delivery dedup identities."""
from datetime import datetime
import json
from typing import Any

from threadrelay.contracts import validate_contract
from .database import Database, encode, utc_timestamp

ObservedPost = dict[str, Any]


class PostStore:
    def __init__(self, database: Database):
        self.db = database

    def upsert(self, post: ObservedPost) -> bool:
        validate_contract("ObservedPost", post)
        observed = utc_timestamp(datetime.fromisoformat(post["observed_at"].replace("Z", "+00:00")))
        with self.db.transaction() as con:
            existing = con.execute("SELECT last_seen FROM posts WHERE post_id=?", (post["post_id"],)).fetchone()
            if existing is not None and observed < existing["last_seen"]:
                return False  # Out-of-order observation must not restore stale text.
            con.execute("""INSERT INTO posts(post_id,content_hash,first_seen,last_seen) VALUES (?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET content_hash=excluded.content_hash,last_seen=excluded.last_seen""",
                (post["post_id"], post["content_hash"], observed, observed))
            con.execute("INSERT INTO post_text(post_id,payload) VALUES (?,?) ON CONFLICT(post_id) DO UPDATE SET payload=excluded.payload", (post["post_id"], encode(post)))
            return existing is None

    def get(self, post_id: str) -> ObservedPost | None:
        with self.db.connection() as con:
            row = con.execute("SELECT payload FROM post_text WHERE post_id=?", (post_id,)).fetchone()
        return json.loads(row[0]) if row else None
