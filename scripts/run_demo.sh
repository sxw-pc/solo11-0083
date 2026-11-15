#!/usr/bin/env bash
# Offline demo: start loopback mock, collect live, replay, cross-check, clean up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
mkdir -p data

MOCK_STDOUT="data/mock.stdout.log"
MOCK_STDERR="data/mock.stderr.log"
: >"$MOCK_STDOUT"
: >"$MOCK_STDERR"

"$PYTHON" -m termharvest.cli serve-mock \
  --scenario fixtures/mock_host/demo.json \
  --port 0 \
  --once \
  --frame-timeout 3 \
  --log-level WARNING \
  >"$MOCK_STDOUT" 2>"$MOCK_STDERR" &
MOCK_PID=$!

cleanup() {
  if kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

PORT=""
for _ in $(seq 1 100); do
  PORT="$(sed -n 's/^MOCK_READY .*port=\([0-9][0-9]*\).*$/\1/p' "$MOCK_STDOUT" | head -n 1)"
  if [ -n "$PORT" ]; then
    break
  fi
  sleep 0.05
done
if [ -z "$PORT" ]; then
  echo "mock terminal did not become ready" >&2
  cat "$MOCK_STDERR" >&2 || true
  exit 1
fi
echo "mock terminal ready on 127.0.0.1:$PORT"

"$PYTHON" -m termharvest.cli run \
  --session contracts/sessions/demo_session.yaml \
  --screens contracts/screens \
  --validators contracts/validators/demo_rules.yaml \
  --db data/termharvest.db \
  --out data/report \
  --mode live \
  --port "$PORT" \
  --evidence-dir data/evidence \
  --record-cassette data/demo_live.jsonl \
  --log-file data/termharvest.log \
  --log-level WARNING

wait "$MOCK_PID" 2>/dev/null || true

"$PYTHON" -m termharvest.cli run \
  --session contracts/sessions/demo_session.yaml \
  --screens contracts/screens \
  --validators contracts/validators/demo_rules.yaml \
  --db data/termharvest.db \
  --out data/report_replay \
  --mode replay \
  --cassette fixtures/cassettes/demo_replay.jsonl \
  --evidence-dir data/evidence_replay \
  --log-file data/termharvest.log \
  --log-level WARNING

"$PYTHON" - "$ROOT" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
live = json.loads((root / "data/report/report.json").read_text(encoding="utf-8"))
replay = json.loads((root / "data/report_replay/report.json").read_text(encoding="utf-8"))
live_records = [item["normalized_values"] for item in live["records"]]
replay_records = [item["normalized_values"] for item in replay["records"]]
if live_records != replay_records:
    raise SystemExit("live/replay record mismatch")
with sqlite3.connect(root / "data/termharvest.db") as connection:
    rows = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(
    "DEMO_OK "
    f"records={len(live_records)} "
    f"idempotent_db_records={rows} "
    f"audit_events={events} "
    "live_replay_equivalent=true"
)
print(f"report: {root / 'data/report/report.html'}")
PY
