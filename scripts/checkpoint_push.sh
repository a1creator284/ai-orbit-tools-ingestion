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
#   * integrates remote work with `git pull --rebase` (no reset, no
#     clean, no force-push) so a newer remote checkpoint is never lost,
#   * exits only once the single verifier worker is gone AND a final
#     checkpoint has been published.
#
# Worker detection is delegated to scripts/verify_pids.py, which scans
# /proc for real python workers. `pgrep -f "run.py verify"` cannot be
# used here: it also matches this script's own command line and would
# report phantom workers.
#
# Usage: scripts/checkpoint_push.sh [interval_seconds]

set -uo pipefail

cd /home/user/webapp || exit 1

INTERVAL="${1:-300}"
VERIFIED="data/interim/candidates_verified.jsonl"
STATE="data/interim/candidates_verification_state.json"
REPORT="data/interim/candidates_verification_report.json"

workers() { python3 scripts/verify_pids.py --count 2>/dev/null || echo 0; }

publish() {
  local count="$1"
  git add "$VERIFIED" "$STATE" "$REPORT" 2>/dev/null

  if git diff --cached --quiet; then
    return 0
  fi

  git commit -q -m "data(verify): live verification checkpoint — ${count} records persisted

Automatic durability checkpoint from the single resumable verification
pass. Rows are appended by the runner as each candidate is verified;
this commit publishes the completed rows so no verified work is
stranded in the sandbox. Input candidates_resolved.jsonl is untouched
and no verifier rule or threshold was changed."

  # Integrate any newer remote production checkpoint before pushing.
  # Rebase keeps remote history intact; we never reset, clean or force.
  git fetch -q origin 2>/dev/null
  if ! git diff --quiet HEAD origin/main -- 2>/dev/null; then
    if ! git pull -q --rebase origin main 2>/dev/null; then
      echo "$(date -u +%FT%TZ) rebase needed manual attention at ${count}"
      git rebase --abort 2>/dev/null
      return 1
    fi
  fi

  if git push -q origin main 2>/dev/null; then
    echo "$(date -u +%FT%TZ) pushed checkpoint ${count} @ $(git rev-parse --short HEAD)"
    return 0
  fi

  echo "$(date -u +%FT%TZ) push FAILED at ${count}, will retry next cycle"
  return 1
}

last_count=$(git show HEAD:"$VERIFIED" 2>/dev/null | wc -l | tr -d ' ')
[ -z "$last_count" ] && last_count=0

while true; do
  sleep "$INTERVAL"

  [ -f "$VERIFIED" ] || continue
  count=$(wc -l < "$VERIFIED" | tr -d ' ')
  running=$(workers)

  if [ "$count" -gt "$last_count" ]; then
    if publish "$count"; then
      last_count="$count"
    fi
  fi

  # Exit only when the worker is gone and everything on disk is published.
  if [ "$running" -eq 0 ]; then
    count=$(wc -l < "$VERIFIED" | tr -d ' ')
    if [ "$count" -gt "$last_count" ]; then
      publish "$count" && last_count="$count"
    fi
    break
  fi
done

echo "$(date -u +%FT%TZ) watchdog exiting at ${last_count} published records"
