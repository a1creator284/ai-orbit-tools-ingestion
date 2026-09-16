#!/usr/bin/env bash
# Durability watchdog for the live verification pass.
#
# Highest-priority rule for this project: a run must never end with
# meaningful work only in the local workspace. The verification runner
# already appends every result to candidates_verified.jsonl immediately,
# so this script's only job is to get those completed rows off the
# sandbox and onto origin/main at regular intervals.
#
# It is deliberately conservative:
#   * stages ONLY the three verification artefacts by explicit path
#     (never `git add -A`, never logs/pipeline.jsonl),
#   * commits only when the persisted record count actually grew,
#   * never rewrites history, never resets, never cleans.
#
# Usage: scripts/checkpoint_push.sh [interval_seconds]

set -uo pipefail

cd /home/user/webapp || exit 1

INTERVAL="${1:-300}"
VERIFIED="data/interim/candidates_verified.jsonl"
STATE="data/interim/candidates_verification_state.json"
REPORT="data/interim/candidates_verification_report.json"

last_count=$(git show HEAD:"$VERIFIED" 2>/dev/null | wc -l | tr -d ' ')
[ -z "$last_count" ] && last_count=0

while true; do
  sleep "$INTERVAL"

  [ -f "$VERIFIED" ] || continue
  count=$(wc -l < "$VERIFIED" | tr -d ' ')

  # Only commit when new verification rows actually reached disk.
  if [ "$count" -le "$last_count" ]; then
    verify_running=$(pgrep -f "run.py verify" | wc -l | tr -d ' ')
    [ "$verify_running" -eq 0 ] && break
    continue
  fi

  git add "$VERIFIED" "$STATE" "$REPORT" 2>/dev/null

  if ! git diff --cached --quiet; then
    git commit -q -m "data(verify): live verification checkpoint — ${count} records persisted

Automatic durability checkpoint from the resumable verification pass.
Rows are appended by the runner as each candidate is verified; this
commit publishes the completed rows so no verified work is stranded in
the sandbox. Input candidates_resolved.jsonl is untouched and no
verifier rule or threshold was changed."
    if git push -q origin main 2>/dev/null; then
      echo "$(date -u +%FT%TZ) pushed checkpoint ${count}"
      last_count="$count"
    else
      echo "$(date -u +%FT%TZ) push FAILED at ${count}, will retry next cycle"
    fi
  fi

  verify_running=$(pgrep -f "run.py verify" | wc -l | tr -d ' ')
  [ "$verify_running" -eq 0 ] && break
done

echo "$(date -u +%FT%TZ) watchdog exiting"
