#!/usr/bin/env bash
# Durability watchdog for the live verification pass.
#
# Highest-priority rule for this project: a run must never end with
# meaningful work only in the local workspace. The verification runner
# already appends every result to candidates_verified.jsonl immediately,
# so this script's only job is to get those completed rows off the
# sandbox and onto origin/main at regular intervals.
#
# ---------------------------------------------------------------------
# The durability race this script must NOT have
# ---------------------------------------------------------------------
# The previous implementation published with `git add` + `git commit` +
# `git pull --rebase` + `git push`, i.e. through the index and the
# working tree, *while a live verifier was appending to the very file
# being published*. That is a data-loss race, in two distinct ways:
#
#   1. `git pull --rebase` (and any rebase/checkout/merge) REWRITES the
#      working tree. To replay our commit on top of a remote one, git
#      first moves the tree back to origin/main's content — which for
#      candidates_verified.jsonl means truncating it to the remote's
#      shorter version — and then writes our version back. The verifier
#      holds the file open in append mode and keeps writing throughout
#      that window. Rows appended during it land in a file git then
#      overwrites, so they are destroyed on disk. They are *not*
#      recoverable: the runner's resume set (`completed_keys`) was read
#      once at start-up, so the process never re-verifies them, and the
#      candidates silently disappear from the dataset. Nothing in the
#      logs reports it.
#
#   2. `git add` of a file that is being appended to can snapshot a
#      partially-written final line, publishing a torn JSON row.
#
# Both are fixed by never letting this script touch the working tree or
# the index. Publishing now goes entirely through git plumbing:
#
#   * a stable byte-exact SNAPSHOT of each artefact is taken first
#     (`head -c` to a byte count measured before the read, so a
#     concurrent append can only ever be excluded, never torn), and the
#     JSONL snapshot is truncated to its last complete newline, so a
#     half-written row is never published;
#   * blobs are written with `git hash-object -w` (object store only);
#   * the tree is built with `git read-tree`/`git update-index` inside a
#     TEMPORARY index file ($GIT_INDEX_FILE), so the real index is
#     untouched;
#   * the commit is created with `git commit-tree` and the branch is
#     advanced with `git update-ref`.
#
# Nothing above reads or writes the working tree, so the verifier's
# append stream is never disturbed and no row can be lost.
#
# Remote integration is likewise non-destructive: if origin/main has
# moved, we build our commit ON TOP of the fetched remote commit
# (parent = origin/main) and carry every *other* path from the remote
# tree unchanged, so a newer remote checkpoint is merged rather than
# reverted — without a rebase, a reset, a clean, or a force-push.
#
# Safety rule kept from the original: we only ever publish a verified
# artefact whose row count is >= the count already published, so a
# checkpoint can never shrink the dataset on origin/main.
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
BRANCH="main"
SNAPDIR="$(mktemp -d)"
trap 'rm -rf "$SNAPDIR"' EXIT

workers() { python3 scripts/verify_pids.py --count 2>/dev/null || echo 0; }

# Rows currently published on the branch tip (never trust a cached number:
# the remote may have moved).
published_rows() {
  git cat-file -p "refs/heads/${BRANCH}:${VERIFIED}" 2>/dev/null | wc -l | tr -d ' '
}

# Byte-exact snapshot of a file that may be growing underneath us.
#
# The size is measured BEFORE the read and `head -c` stops at exactly that
# many bytes, so bytes appended during the copy are simply not included.
# The snapshot can therefore lag the file, but it can never be torn.
snapshot() {
  local src="$1" dst="$2" size
  [ -f "$src" ] || return 1
  size=$(wc -c < "$src" | tr -d ' ')
  head -c "$size" "$src" > "$dst" 2>/dev/null || return 1
  return 0
}

# Drop a trailing partial line so only complete JSON rows are published.
truncate_to_last_newline() {
  local path="$1" size
  size=$(wc -c < "$path" | tr -d ' ')
  [ "$size" -gt 0 ] || return 0
  if [ "$(tail -c 1 "$path" | od -An -c | tr -d ' \n')" = "\n" ]; then
    return 0
  fi
  python3 - "$path" <<'PY'
import sys
path = sys.argv[1]
with open(path, "rb") as handle:
    data = handle.read()
cut = data.rfind(b"\n")
with open(path, "wb") as handle:
    handle.write(data[: cut + 1] if cut >= 0 else b"")
PY
}

# Re-point the REAL index entries for the three artefacts at the blobs of
# the given commit, so the index agrees with the branch tip.
#
# `git update-index --cacheinfo` only rewrites index entries; unlike
# `git checkout`/`reset`/`rebase` it does not write the working tree, so a
# concurrently appending verifier is unaffected. Only the three artefact
# paths are touched, so an unrelated staged edit is left exactly as it was.
sync_real_index() {
  local commit="$1" path blob
  for path in "$VERIFIED" "$STATE" "$REPORT"; do
    blob=$(git rev-parse "${commit}:${path}" 2>/dev/null) || continue
    git update-index --add --cacheinfo "100644,${blob},${path}" 2>/dev/null
  done
}

# Publish a commit built entirely in the object store.
#
# $1 = row count being published (for the message only)
# Returns 0 when refs/heads/main was advanced AND pushed.
publish() {
  local count="$1"
  local base parent tmp_index new_tree commit blob

  git fetch -q origin "$BRANCH" 2>/dev/null

  # Build on top of whichever tip is newer: our local branch, or the
  # remote if it has commits we lack. Building ON TOP of the remote is
  # what replaces `pull --rebase` here — same outcome, zero tree writes.
  parent=$(git rev-parse "refs/heads/${BRANCH}" 2>/dev/null)
  local remote
  remote=$(git rev-parse "refs/remotes/origin/${BRANCH}" 2>/dev/null)
  if [ -n "$remote" ] && [ "$remote" != "$parent" ]; then
    if git merge-base --is-ancestor "$parent" "$remote" 2>/dev/null; then
      # Remote strictly ahead: adopt it as the parent so every other
      # path it changed is carried forward untouched.
      parent="$remote"
    elif ! git merge-base --is-ancestor "$remote" "$parent" 2>/dev/null; then
      # Genuinely divergent histories: do not guess. Report and retry
      # next cycle rather than risk discarding remote work.
      echo "$(date -u +%FT%TZ) divergent history at ${count}; needs manual merge"
      return 1
    fi
  fi
  base="$parent"

  # Never publish fewer rows than are already published.
  local already
  already=$(git cat-file -p "${base}:${VERIFIED}" 2>/dev/null | wc -l | tr -d ' ')
  [ -z "$already" ] && already=0
  if [ "$count" -lt "$already" ]; then
    # Refuse the publish, but still adopt a strictly-newer remote tip as the
    # local branch. That is a ref move only — no tree write, so the verifier's
    # append stream is untouched — and it stops us from repeatedly rebuilding
    # a checkpoint against a stale parent.
    if [ "$base" != "$(git rev-parse "refs/heads/${BRANCH}")" ]; then
      git update-ref "refs/heads/${BRANCH}" "$base"
    fi
    echo "$(date -u +%FT%TZ) refusing to publish ${count} rows over ${already} already published"
    return 1
  fi

  tmp_index="${SNAPDIR}/index"
  rm -f "$tmp_index"
  GIT_INDEX_FILE="$tmp_index" git read-tree "$base" 2>/dev/null || return 1

  local path snap
  for path in "$VERIFIED" "$STATE" "$REPORT"; do
    [ -f "$path" ] || continue
    snap="${SNAPDIR}/$(basename "$path")"
    snapshot "$path" "$snap" || continue
    if [ "$path" = "$VERIFIED" ]; then
      truncate_to_last_newline "$snap"
    else
      # A JSON artefact is written atomically (temp + rename) by the
      # runner, so a snapshot is either the old or the new file — but
      # verify it parses before publishing it anyway.
      python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$snap" 2>/dev/null || continue
    fi
    blob=$(git hash-object -w "$snap") || continue
    GIT_INDEX_FILE="$tmp_index" git update-index --add --cacheinfo "100644,${blob},${path}" || return 1
  done

  new_tree=$(GIT_INDEX_FILE="$tmp_index" git write-tree) || return 1
  if [ "$new_tree" = "$(git rev-parse "${base}^{tree}")" ]; then
    # Nothing changed relative to the parent: still fast-forward the
    # local branch if we adopted a newer remote parent.
    if [ "$base" != "$(git rev-parse "refs/heads/${BRANCH}")" ]; then
      git update-ref "refs/heads/${BRANCH}" "$base"
    fi
    return 0
  fi

  commit=$(git commit-tree "$new_tree" -p "$base" -m "data(verify): live verification checkpoint — ${count} records persisted

Automatic durability checkpoint from the single resumable verification
pass. Rows are appended by the runner as each candidate is verified;
this commit publishes the completed rows so no verified work is
stranded in the sandbox.

Published through git plumbing (hash-object / commit-tree / update-ref)
with a temporary index, so neither the working tree nor the index is
touched while the verifier is appending — the previous add+rebase path
could truncate the artefact mid-append and silently lose rows. Input
candidates_resolved.jsonl is untouched and no verifier rule or
threshold was changed.") || return 1

  git update-ref "refs/heads/${BRANCH}" "$commit" "$parent" 2>/dev/null \
    || git update-ref "refs/heads/${BRANCH}" "$commit" || return 1

  # Advancing the branch ref without refreshing the real index would leave
  # the index holding the PREVIOUS blob for these paths. A later ordinary
  # `git commit` in this workspace commits the index, so it would silently
  # revert the rows we just published. Re-point the real index entries at the
  # blobs now on the branch. This writes the index only — never the working
  # tree — so the verifier's append stream is still untouched.
  sync_real_index "$commit"

  if git push -q origin "refs/heads/${BRANCH}:refs/heads/${BRANCH}" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) pushed checkpoint ${count} @ $(git rev-parse --short "$commit")"
    return 0
  fi

  echo "$(date -u +%FT%TZ) push FAILED at ${count}, will retry next cycle"
  return 1
}

last_count=$(published_rows)
[ -z "$last_count" ] && last_count=0

while true; do
  sleep "$INTERVAL"

  [ -f "$VERIFIED" ] || continue
  count=$(wc -l < "$VERIFIED" | tr -d ' ')
  running=$(workers)

  if [ "$count" -gt "$last_count" ]; then
    if publish "$count"; then
      last_count=$(published_rows)
    fi
  fi

  # Exit only when the worker is gone and everything on disk is published.
  if [ "$running" -eq 0 ]; then
    count=$(wc -l < "$VERIFIED" | tr -d ' ')
    if [ "$count" -gt "$last_count" ]; then
      publish "$count" && last_count=$(published_rows)
    fi
    break
  fi
done

echo "$(date -u +%FT%TZ) watchdog exiting at ${last_count} published records"
