#!/usr/bin/env bash
set -euo pipefail

# Thin local copy of the Composite Refactorings 2020 Neo4j stop script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVAMP_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOT_DIR="$(cd "$REVAMP_ROOT/../datasets/composite_refactorings_2020_runtime" && pwd)"
EXTRACTED_DIR="$ROOT_DIR/work/neo4j-community-3.1.0-msr-2019"
PID_FILE="$EXTRACTED_DIR/run/neo4j.pid"

if [[ ! -d "$EXTRACTED_DIR" ]]; then
  echo "Nothing to stop; extracted runtime not found at:" >&2
  echo "  $EXTRACTED_DIR" >&2
  exit 1
fi

cd "$EXTRACTED_DIR"
./bin/neo4j stop || true

# Console-mode leftovers may ignore `neo4j stop`; fall back to the pid file.
if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# Last resort: any CommunityEntryPoint for this extracted home dir.
pkill -f "org.neo4j.server.CommunityEntryPoint --home-dir=$EXTRACTED_DIR" 2>/dev/null || true

echo "Neo4j stop requested"
