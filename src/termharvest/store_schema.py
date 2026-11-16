"""SQLite schema for the termharvest ledger.

The ledger keeps an immutable, per-run history of screen frames, events and
record *versions*. A run publishes a :dfn:`generation`: the complete set of
records extracted during that run, together with its validation verdict and
the identity relationship against the previously published generation.

Only one generation is the *current* (effective) snapshot. It is referenced by
a single-row ``ledger_state`` pointer, which is switched inside the same
transaction that marks a generation published. Read-only snapshot queries
always follow that pointer, so an unfinished or failed run can never expose
half-written rows, while every completed run remains reviewable forever.
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
  summary_json TEXT NOT NULL DEFAULT '{}',
  generation_id TEXT
);
CREATE TABLE IF NOT EXISTS generations (
  generation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  parent_generation_id TEXT,
  status TEXT NOT NULL,
  record_count INTEGER NOT NULL DEFAULT 0,
  frame_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  records_sha256 TEXT,
  report_manifest_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  published_at TEXT,
  abandoned_at TEXT,
  abandon_reason TEXT
);
CREATE TABLE IF NOT EXISTS ledger_state (
  singleton_id INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
  current_generation_id TEXT,
  updated_at TEXT,
  FOREIGN KEY (current_generation_id) REFERENCES generations(generation_id)
);
INSERT OR IGNORE INTO ledger_state(singleton_id, current_generation_id, updated_at)
  VALUES (1, NULL, NULL);
CREATE TABLE IF NOT EXISTS record_versions (
  generation_id TEXT NOT NULL,
  record_key TEXT NOT NULL,
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
  staged_at TEXT NOT NULL,
  first_run_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  occurrence_seq INTEGER NOT NULL DEFAULT 0,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  variant_sha_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (generation_id, record_key, occurrence_seq),
  FOREIGN KEY (generation_id) REFERENCES generations(generation_id)
);
CREATE INDEX IF NOT EXISTS idx_record_versions_key
  ON record_versions(record_key, generation_id);
CREATE TABLE IF NOT EXISTS generation_key_changes (
  generation_id TEXT NOT NULL,
  record_key TEXT NOT NULL,
  screen_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  parent_generation_id TEXT,
  parent_record_sha256 TEXT,
  record_sha256 TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (generation_id, record_key),
  FOREIGN KEY (generation_id) REFERENCES generations(generation_id)
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
CREATE INDEX IF NOT EXISTS idx_records_screen ON records(screen_id);
"""

# Migrations for ledger files created by earlier releases. Each statement is
# keyed by PRAGMA user_version so upgrades run at most once and are idempotent.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE runs ADD COLUMN generation_id TEXT",
    ),
}
SCHEMA_USER_VERSION = 2
