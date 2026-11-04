-- Local planning is separate from immutable archive inputs and native outcomes.
CREATE TABLE workflow_templates (
 id TEXT PRIMARY KEY,version INTEGER NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL,
 created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE INDEX archive_delivery_dedup ON archive_deliveries(
 json_extract(payload,'$.account_id'),json_extract(payload,'$.post_id'),json_extract(payload,'$.destination_id')
);
CREATE TABLE workflow_drafts (
 id TEXT PRIMARY KEY,version INTEGER NOT NULL,spec TEXT NOT NULL,
 preview TEXT,preview_hash TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE workflow_jobs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,version INTEGER NOT NULL,
 draft_id TEXT NOT NULL REFERENCES workflow_drafts(id),draft_version INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('job','rule')),revision_id TEXT REFERENCES archive_revisions(id),
 post_id TEXT,text TEXT,due_at REAL,status TEXT NOT NULL CHECK(status IN
 ('draft','blocked','waiting','paused','sending','sent','failed','unknown','cancelled')),
 reasons TEXT NOT NULL,approval_hash TEXT,requires_approval INTEGER NOT NULL,
 rule_id TEXT REFERENCES workflow_jobs(id),delivery_id TEXT,attempt_id TEXT,
 context TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE INDEX workflow_due ON workflow_jobs(status,due_at,seq);
CREATE INDEX workflow_source ON workflow_jobs(post_id,rule_id);
CREATE TABLE workflow_enrollments (
 rule_id TEXT NOT NULL REFERENCES workflow_jobs(id),revision_id TEXT NOT NULL REFERENCES archive_revisions(id),
 job_id TEXT NOT NULL REFERENCES workflow_jobs(id),PRIMARY KEY(rule_id,revision_id)
);
-- Retention may expire operational ledger rows; permanent archive outcome
-- triggers and the workflow job still retain the operation identity/status.
CREATE TABLE workflow_delivery_context (
 delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id) ON DELETE CASCADE,
 job_id TEXT NOT NULL UNIQUE REFERENCES workflow_jobs(id),initiated_attempt_id TEXT
);
