"""SQLite schema for the termharvest ledger and the signed-cassette trust store."""

# Trust material is public-only: key_id, Ed25519 public key, validity window,
# revocation state and the highest accepted recording sequence per session.
# Private keys are deliberately absent from this schema.
TRUST_SCHEMA = """
CREATE TABLE IF NOT EXISTS trust_keys (
  key_id TEXT PRIMARY KEY,
  algorithm TEXT NOT NULL DEFAULT 'ed25519',
  public_key_b64 TEXT NOT NULL,
  public_key_fingerprint TEXT NOT NULL,
  valid_from TEXT,
  valid_until TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  revoked_at TEXT,
  revoked_reason TEXT,
  enrolled_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trust_keys_fingerprint
  ON trust_keys(public_key_fingerprint);
CREATE TABLE IF NOT EXISTS trust_sessions (
  session_id TEXT PRIMARY KEY,
  highest_record_seq INTEGER NOT NULL,
  package_digest TEXT NOT NULL,
  policy TEXT NOT NULL,
  signers_json TEXT NOT NULL,
  trusted_at TEXT NOT NULL
);
"""

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
CREATE TABLE IF NOT EXISTS run_provenance (
  run_id TEXT PRIMARY KEY,
  trust_status TEXT NOT NULL,
  trusted INTEGER NOT NULL,
  policy TEXT,
  cassette_version INTEGER,
  record_seq INTEGER,
  contract_digest TEXT,
  signers_json TEXT NOT NULL DEFAULT '[]',
  package_digest TEXT,
  error_code TEXT,
  provenance_json TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_records_screen ON records(screen_id);
"""
