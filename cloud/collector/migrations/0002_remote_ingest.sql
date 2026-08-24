PRAGMA foreign_keys = ON;

ALTER TABLE source_runs ADD COLUMN remote_session_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_runs_remote_session
    ON source_runs(remote_session_id) WHERE remote_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS remote_collection_rounds (
    id TEXT PRIMARY KEY CHECK (length(id) = 64),
    window_id INTEGER NOT NULL REFERENCES collection_windows(id),
    mode TEXT NOT NULL CHECK (mode IN ('official', 'technical-trial')),
    expected_source_count INTEGER NOT NULL CHECK (expected_source_count = 5),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'complete', 'failed')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS remote_ingest_sessions (
    id TEXT PRIMARY KEY CHECK (length(id) = 64),
    collection_id TEXT NOT NULL REFERENCES remote_collection_rounds(id),
    window_id INTEGER NOT NULL REFERENCES collection_windows(id),
    run_id INTEGER NOT NULL UNIQUE REFERENCES source_runs(id),
    source_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('official', 'technical-trial')),
    expected_count INTEGER NOT NULL CHECK (expected_count BETWEEN 1 AND 20000),
    expected_bytes INTEGER NOT NULL CHECK (expected_bytes BETWEEN 1 AND 75000000),
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    lease_owner TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'committing', 'committed', 'failed')),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(collection_id, source_key)
);

CREATE TABLE IF NOT EXISTS remote_ingest_chunks (
    session_id TEXT NOT NULL REFERENCES remote_ingest_sessions(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index BETWEEN 0 AND 999),
    chunk_sha256 TEXT NOT NULL CHECK (length(chunk_sha256) = 64),
    row_count INTEGER NOT NULL CHECK (row_count BETWEEN 1 AND 40),
    byte_count INTEGER NOT NULL CHECK (byte_count BETWEEN 1 AND 1500000),
    received_at TEXT NOT NULL,
    PRIMARY KEY(session_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_remote_ingest_status
    ON remote_ingest_sessions(status, expires_at);
