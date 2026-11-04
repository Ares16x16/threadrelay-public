-- Application data only. TDLib encrypted native caches live outside this DB.
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL CHECK(epoch >= 0),
    snapshot TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
    vault_ref TEXT,
    session_ref TEXT
);
CREATE UNIQUE INDEX one_active_account ON accounts(active) WHERE active = 1;
CREATE TABLE posts (
    post_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE post_text (
    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
    created_at REAL NOT NULL
);
CREATE TABLE deliveries (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    account_epoch INTEGER NOT NULL CHECK(account_epoch >= 0),
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    destination_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','retryable','failed','unknown','cancelled')),
    worker_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(account_id, post_id, destination_id)
);
CREATE INDEX pending_deliveries ON deliveries(state, created_at);
CREATE TABLE telegram_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    request_extra TEXT NOT NULL UNIQUE,
    temporary_message_id TEXT,
    final_message_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','retryable','failed','unknown','cancelled'))
);
CREATE TABLE diagnostics (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    created_at REAL NOT NULL
);
