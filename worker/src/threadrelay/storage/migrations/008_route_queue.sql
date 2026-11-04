CREATE TABLE route_queue (
 delivery_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, route_version INTEGER NOT NULL,
 text TEXT NOT NULL, destination_id TEXT NOT NULL, destination_title TEXT NOT NULL,
 due_at REAL NOT NULL, gap_seconds REAL NOT NULL, started_at REAL,
 cancelled INTEGER NOT NULL DEFAULT 0, reason TEXT, snapshot TEXT NOT NULL,
 sequence INTEGER NOT NULL UNIQUE
);
CREATE TABLE route_queue_control (route_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0);
CREATE INDEX route_queue_order ON route_queue(route_id,sequence);
