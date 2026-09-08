#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/havriil.pietukhin/uni/masterThesis/revamp"
BATCH_LOG="$ROOT/data/pi/runs/batch_2d766ca7-9083-4c71-83f5-1a913c024967/all.log"
CASE_LOG_DIR="$ROOT/data/pi/runs/gertvv_drugis-common"
STALL_REPORT="$ROOT/data/pi/sweep_logs/bfs_remaining_stall_report.jsonl"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"
STALL_MINUTES="${STALL_MINUTES:-15}"

latest_case_log() {
  find "$CASE_LOG_DIR" -name all.log -type f -print0 2>/dev/null \
    | xargs -0 ls -t 2>/dev/null \
    | head -1
}

collect_logs() {
  local case_log
  case_log="$(latest_case_log || true)"
  printf '%s\n' "$BATCH_LOG"
  if [[ -n "${case_log:-}" && -f "$case_log" ]]; then
    printf '%s\n' "$case_log"
  fi
}

while true; do
  if ! pgrep -f "bun experiments/pi_batch.ts dataset/manifest_bfs_remaining.jsonl" >/dev/null 2>&1; then
    echo 'AGENT_LOOP_TICK_bfs-babysit {"prompt":"BFS batch process ended. Inspect batch/case Eliot logs and manifest resume, then report completion or failure to the user."}'
    exit 0
  fi

  mapfile -t logs < <(collect_logs)
  if ((${#logs[@]} == 0)); then
    sleep "$INTERVAL_SECONDS"
    continue
  fi

  report="$(cd "$ROOT" && uv run python experiments/monitor_pi_stall.py "${logs[@]}" --stall-minutes "$STALL_MINUTES" || true)"
  stalled="$(printf '%s' "$report" | uv run python -c 'import json,sys; print(json.load(sys.stdin).get("stalled", False))' 2>/dev/null || echo false)"

  printf '%s\n' "$report" >>"$STALL_REPORT"

  if [[ "$stalled" == "True" || "$stalled" == "true" ]]; then
    escaped="$(printf '%s' "$report" | uv run python -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    echo "AGENT_LOOP_TICK_bfs-babysit {\"prompt\":\"BFS batch stall detected (> ${STALL_MINUTES} min on one refactoring). Report to user with reason. Monitor payload: ${escaped}\"}"
  fi

  sleep "$INTERVAL_SECONDS"
done
