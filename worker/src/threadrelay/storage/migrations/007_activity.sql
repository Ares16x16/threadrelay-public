-- Metadata only; no operational foreign keys, native objects or template bodies.
CREATE TABLE activity_entries (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
 kind TEXT NOT NULL,revision_id TEXT,run_id TEXT,delivery_id TEXT,job_id TEXT,
 detail TEXT,due_at REAL,destination TEXT
);
CREATE INDEX activity_revision ON activity_entries(revision_id,seq);
CREATE TRIGGER activity_history_saved AFTER INSERT ON archive_revisions
WHEN NEW.provenance='captured' BEGIN
 INSERT INTO activity_entries(at,kind,revision_id) VALUES(NEW.created_at,'history_saved',NEW.id);
END;
CREATE TRIGGER activity_model_started AFTER INSERT ON archive_runs
WHEN NEW.provenance='captured' AND NEW.kind IN ('collection','sample') AND NEW.status='running' AND NEW.started_at IS NOT NULL BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,run_id) VALUES(NEW.started_at,'classification_started',NEW.revision_id,NEW.id);
END;
CREATE TRIGGER activity_model_finished AFTER UPDATE OF status ON archive_runs
WHEN NEW.provenance='captured' AND NEW.kind IN ('collection','sample') AND OLD.status IS NOT NEW.status
 AND NEW.status IN ('succeeded','failed','cancelled') AND NEW.finished_at IS NOT NULL BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,run_id,detail)
 VALUES(NEW.finished_at,'classification_'||NEW.status,NEW.revision_id,NEW.id,substr(NEW.error_code,1,200));
END;
CREATE TRIGGER activity_delivery_insert AFTER INSERT ON archive_deliveries
WHEN json_extract(NEW.payload,'$.state') IN ('pending','sending','sent','failed','unknown','cancelled','retryable') BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,delivery_id,job_id,due_at,destination)
 VALUES(NEW.created_at,'delivery_'||CASE json_extract(NEW.payload,'$.state') WHEN 'sending' THEN 'preparing' ELSE json_extract(NEW.payload,'$.state') END,
  (SELECT revision_id FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),NEW.id,
  json_extract(NEW.payload,'$.workflow_job_id'),
  (SELECT due_at FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),
  substr(json_extract(NEW.payload,'$.destination_id'),1,500));
END;
CREATE TRIGGER activity_delivery_state AFTER UPDATE OF payload ON archive_deliveries
WHEN json_extract(OLD.payload,'$.state') IS NOT json_extract(NEW.payload,'$.state')
 AND json_extract(NEW.payload,'$.state') IN ('pending','sending','sent','failed','unknown','cancelled','retryable') BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,delivery_id,job_id,due_at,destination)
 VALUES(NEW.updated_at,'delivery_'||CASE json_extract(NEW.payload,'$.state') WHEN 'sending' THEN 'preparing' ELSE json_extract(NEW.payload,'$.state') END,
  (SELECT revision_id FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),NEW.id,
  json_extract(NEW.payload,'$.workflow_job_id'),
  (SELECT due_at FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),
  substr(json_extract(NEW.payload,'$.destination_id'),1,500));
END;
-- The first correlated temporary ID is local acceptance, never final Sent.
CREATE TRIGGER activity_delivery_sending AFTER UPDATE OF payload ON archive_deliveries
WHEN json_extract(OLD.payload,'$.state')='sending' AND json_extract(NEW.payload,'$.state')='sending'
 AND json_extract(OLD.payload,'$.temporary_message_id') IS NULL AND json_extract(NEW.payload,'$.temporary_message_id') IS NOT NULL
 AND json_extract(OLD.payload,'$.attempt_id')=json_extract(NEW.payload,'$.attempt_id')
 AND json_extract(OLD.payload,'$.request_extra')=json_extract(NEW.payload,'$.request_extra') BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,delivery_id,job_id,due_at,destination)
 VALUES(NEW.updated_at,'delivery_sending',
  (SELECT revision_id FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),NEW.id,
  json_extract(NEW.payload,'$.workflow_job_id'),
  (SELECT due_at FROM workflow_jobs WHERE id=json_extract(NEW.payload,'$.workflow_job_id')),
  substr(json_extract(NEW.payload,'$.destination_id'),1,500));
END;
CREATE TRIGGER activity_job_insert AFTER INSERT ON workflow_jobs
WHEN NEW.status IN ('draft','waiting','blocked','paused','cancelled') BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,job_id,detail,due_at,destination)
 VALUES(NEW.created_at,'job_'||NEW.status,NEW.revision_id,NEW.id,substr(json_extract(NEW.reasons,'$[0]'),1,200),
  NEW.due_at,substr(json_extract(NEW.context,'$.spec.target.destination_id'),1,500));
END;
CREATE TRIGGER activity_job_update AFTER UPDATE OF status,reasons,due_at ON workflow_jobs
WHEN NEW.status IN ('draft','waiting','blocked','paused','cancelled')
 AND (OLD.status IS NOT NEW.status OR OLD.reasons IS NOT NEW.reasons OR OLD.due_at IS NOT NEW.due_at) BEGIN
 INSERT INTO activity_entries(at,kind,revision_id,job_id,detail,due_at,destination)
 VALUES(NEW.updated_at,'job_'||NEW.status,NEW.revision_id,NEW.id,substr(json_extract(NEW.reasons,'$[0]'),1,200),
  NEW.due_at,substr(json_extract(NEW.context,'$.spec.target.destination_id'),1,500));
END;
