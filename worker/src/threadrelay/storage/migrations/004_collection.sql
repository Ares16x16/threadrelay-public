-- Classification observations have no account, route, approval or send authority.
-- Expiring retained source text also expires its local classification snapshot.
CREATE TABLE collection_classifications (
    post_id TEXT PRIMARY KEY REFERENCES post_text(post_id) ON DELETE CASCADE,
    post_hash TEXT NOT NULL,
    topics_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    topics TEXT NOT NULL
);
