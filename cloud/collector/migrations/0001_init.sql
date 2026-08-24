PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS collection_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workday TEXT NOT NULL,
    window_key TEXT NOT NULL CHECK (window_key IN ('morning', 'afternoon', 'evening', 'technical-trial')),
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'partial', 'complete', 'missed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE(workday, window_key)
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES collection_windows(id),
    source_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    fetched_count INTEGER,
    snapshot_sha256 TEXT,
    error_kind TEXT,
    error_message TEXT,
    UNIQUE(window_id, source_key, attempt),
    UNIQUE(window_id, source_key, snapshot_sha256)
);

CREATE TABLE IF NOT EXISTS staged_jobs (
    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id, source_key, external_id)
);

CREATE TABLE IF NOT EXISTS source_heads (
    source_key TEXT PRIMARY KEY,
    window_id INTEGER NOT NULL REFERENCES collection_windows(id),
    window_opens_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES source_runs(id)
);

CREATE TABLE IF NOT EXISTS cloud_jobs (
    source_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY(source_key, external_id)
);

CREATE TABLE IF NOT EXISTS job_changes (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES source_runs(id),
    source_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('opened', 'updated', 'closed')),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(run_id, source_key, external_id, kind)
);

CREATE TABLE IF NOT EXISTS sync_clients (
    client_id TEXT PRIMARY KEY,
    acknowledged_cursor INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catch_up_gate (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_requested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_runs_window_status
    ON source_runs(window_id, status, source_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_runs_one_success
    ON source_runs(window_id, source_key) WHERE status='success';
CREATE INDEX IF NOT EXISTS idx_job_changes_cursor ON job_changes(cursor);
CREATE INDEX IF NOT EXISTS idx_cloud_jobs_open ON cloud_jobs(source_key, closed_at);
