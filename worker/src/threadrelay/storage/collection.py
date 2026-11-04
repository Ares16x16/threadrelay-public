"""Read-only collection projection, independent of delivery authorization."""
import hashlib
import json

from threadrelay.contracts import validate_contract
from .database import encode
from .routing import ConfigStore


def _digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _enabled(topics):
    return sorted((topic for topic in topics if topic["enabled"]), key=lambda topic: topic["id"])


class CollectionStore:
    def __init__(self, database):
        self.db = database

    def record(self, post, topics, classification):
        validate_contract("ObservedPost", post)
        validate_contract("Classification", classification)
        if classification["post_id"] != post["post_id"]:
            raise ValueError("Classification post mismatch")
        selected = _enabled(topics)
        if sorted(item["topic_id"] for item in classification["topic_results"]) != [topic["id"] for topic in selected]:
            raise ValueError("Classification topics mismatch")
        with self.db.transaction() as con:
            current = con.execute("SELECT payload FROM post_text WHERE post_id=?", (post["post_id"],)).fetchone()
            if (current is None or json.loads(current["payload"]) != post
                    or _enabled(ConfigStore.read_all(con, "routing_topics")) != selected):
                return False
            labels = [{"id": topic["id"], "label": topic["description"][:160]} for topic in selected]
            con.execute("""INSERT INTO collection_classifications VALUES (?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET post_hash=excluded.post_hash,
                topics_hash=excluded.topics_hash,payload=excluded.payload,topics=excluded.topics""",
                (post["post_id"], _digest(post), _digest(selected), encode(classification), encode(labels)))
            return True

    def query(self):
        # One SQLite read snapshot prevents combining new topics with old text.
        # Leave ample frame headroom for envelope/JSON escaping and never query
        # or start a service, model or delivery while reading this projection.
        with self.db.connection() as con:
            con.execute("BEGIN")
            topics_hash = _digest(_enabled(ConfigStore.read_all(con, "routing_topics")))
            rows = con.execute("""SELECT t.payload AS post,c.post_hash,c.topics_hash,
                    c.payload AS classification,c.topics FROM post_text t
                JOIN posts p ON p.post_id=t.post_id
                LEFT JOIN collection_classifications c ON c.post_id=t.post_id
                ORDER BY p.last_seen DESC,p.post_id LIMIT 100""")
            result, size = [], 2
            for row in rows:
                post = json.loads(row["post"])
                current = row["post_hash"] == _digest(post) and row["topics_hash"] == topics_hash
                item = {"post": post, "classification": json.loads(row["classification"]) if current else None,
                        "topics": json.loads(row["topics"]) if current else []}
                validate_contract("CollectionItem", item)
                # Protocol framing uses ASCII escaping, including Unicode text.
                size += len(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1
                if size > 900_000:
                    break
                result.append(item)
            return result
