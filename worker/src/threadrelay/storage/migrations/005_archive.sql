-- Permanent archive has no foreign keys to expiring operational source/authority.
CREATE TABLE archive_revisions (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 post_id TEXT NOT NULL, payload TEXT NOT NULL, provenance TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE INDEX archive_post_identity ON archive_revisions(post_id,seq);
CREATE TABLE archive_observations (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 revision_id TEXT NOT NULL REFERENCES archive_revisions(id),
 observed_at TEXT NOT NULL, source_session_id TEXT NOT NULL, recorded_at REAL NOT NULL
);
CREATE TABLE archive_runs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, revision_id TEXT REFERENCES archive_revisions(id),
 input_snapshot TEXT NOT NULL, topics TEXT NOT NULL, binding TEXT NOT NULL,
 started_at REAL, finished_at REAL, status TEXT NOT NULL,
 classification TEXT, error_code TEXT, provenance TEXT NOT NULL
);
CREATE INDEX archive_run_revision ON archive_runs(revision_id,seq);
INSERT INTO archive_revisions(id,post_id,payload,provenance,created_at)
 SELECT 'legacy:'||t.post_id,t.post_id,t.payload,'legacy_import',p.last_seen
 FROM post_text t JOIN posts p USING(post_id);
INSERT INTO archive_observations(revision_id,observed_at,source_session_id,recorded_at)
 SELECT id,json_extract(payload,'$.observed_at'),json_extract(payload,'$.source_session_id'),created_at FROM archive_revisions;
-- Legacy results have no recoverable exact run time, input or topic bindings.
-- Retain the result honestly; never pretend it classified today's retained text.
INSERT INTO archive_runs(id,kind,revision_id,input_snapshot,topics,binding,started_at,finished_at,status,classification,error_code,provenance)
 SELECT 'legacy:'||c.post_id,'legacy',NULL,'{}','[]','{}',NULL,NULL,'succeeded',c.payload,NULL,'legacy_import'
 FROM collection_classifications c JOIN posts p USING(post_id);
INSERT INTO archive_runs(id,kind,revision_id,input_snapshot,topics,binding,started_at,finished_at,status,classification,error_code,provenance)
 SELECT 'legacy-plan:'||id,'legacy',NULL,'{}','[]',
 CASE WHEN json_type(payload,'$.snapshot_key')='text' THEN json_object('snapshot_key',json_extract(payload,'$.snapshot_key')) ELSE '{}' END,
 NULL,NULL,'succeeded',json_extract(payload,'$.classification'),NULL,'legacy_import'
 FROM routing_plans WHERE json_type(payload,'$.classification')='object';
CREATE TABLE archive_deliveries (id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE archive_attempts (attempt_id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,request_extra TEXT NOT NULL,temporary_message_id TEXT,final_message_id TEXT,state TEXT NOT NULL);
INSERT INTO archive_deliveries SELECT id,payload,created_at,updated_at FROM deliveries;
INSERT INTO archive_attempts SELECT attempt_id,delivery_id,request_extra,temporary_message_id,final_message_id,state FROM telegram_attempts;
CREATE TRIGGER archive_delivery_insert AFTER INSERT ON deliveries BEGIN
 INSERT INTO archive_deliveries VALUES(NEW.id,NEW.payload,NEW.created_at,NEW.updated_at);
END;
CREATE TRIGGER archive_delivery_update AFTER UPDATE ON deliveries BEGIN
 UPDATE archive_deliveries SET payload=NEW.payload,updated_at=NEW.updated_at WHERE id=NEW.id;
END;
CREATE TRIGGER archive_attempt_insert AFTER INSERT ON telegram_attempts BEGIN
 INSERT INTO archive_attempts VALUES(NEW.attempt_id,NEW.delivery_id,NEW.request_extra,NEW.temporary_message_id,NEW.final_message_id,NEW.state);
END;
CREATE TRIGGER archive_attempt_update AFTER UPDATE ON telegram_attempts BEGIN
 UPDATE archive_attempts SET temporary_message_id=NEW.temporary_message_id,final_message_id=NEW.final_message_id,state=NEW.state WHERE attempt_id=NEW.attempt_id;
END;
