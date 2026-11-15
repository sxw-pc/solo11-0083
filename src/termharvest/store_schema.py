"""SQLite schema for the termharvest ledger."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  record_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS frames (
  run_id TEXT NOT NULL,
  frame_seq INTEGER NOT NULL,
  screen_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  frame_sha256 TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  PRIMARY KEY (run_id, frame_seq),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS records (
  record_key TEXT PRIMARY KEY,
  screen_id TEXT NOT NULL,
  frame_seq INTEGER NOT NULL,
  frame_sha256 TEXT NOT NULL,
  primary_key_json TEXT NOT NULL,
  raw_values_json TEXT NOT NULL,
  normalized_values_json TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  validation_status TEXT NOT NULL,
  validation_errors_json TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  first_run_id TEXT NOT NULL,
  last_run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  code TEXT,
  message TEXT NOT NULL,
  context_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_records_screen ON records(screen_id);
"""
