-- Configuration revisions/tombstones and provenance; approvals remain in approvals.
CREATE TABLE routing_topics (id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT);
CREATE TABLE routing_routes (id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT);
CREATE TABLE routing_destinations (
    account_id TEXT NOT NULL REFERENCES accounts(account_id), chat_id TEXT NOT NULL,
    account_epoch INTEGER NOT NULL, version INTEGER NOT NULL, payload TEXT,
    sender_selected INTEGER NOT NULL CHECK(sender_selected IN (0,1)),
    PRIMARY KEY(account_id,chat_id)
);
-- No post text or examples duplicated in plan receipts. Expiring retained post
-- content deletes its plans and therefore any remaining send authority.
CREATE TABLE routing_plans (
    id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES post_text(post_id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
CREATE TABLE routing_approval_context (
    approval_id TEXT PRIMARY KEY REFERENCES approvals(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES routing_plans(id) ON DELETE CASCADE
);
CREATE TABLE routing_delivery_context (
    delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES routing_plans(id) ON DELETE CASCADE,
    initiated_attempt_id TEXT
);
