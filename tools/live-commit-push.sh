#!/usr/bin/env bash
# Commit + push vault changes (memory/, .harvest/, kb/, artefacts/) from
# <content_root> with rebase-retry on non-fast-forward.
#
# Per #74: per-query live-writeback writes new memory objects locally; this
# script commits + pushes them so the vault stays the single source of truth
# across machines. On non-fast-forward rejection (another machine pushed
# between our pull and push), retries once with `git pull --rebase`.
#
# Per #83: also stages kb/ + artefacts/ so the same helper covers the
# work-execution procedure's Phase 3 write-back path (KB updates and
# artefact files).
#
# Usage:
#   tools/live-commit-push.sh <content_root> "<commit-message>"
#
# Exit codes:
#   0 — committed + pushed (or nothing to commit, success no-op)
#   1 — bad args
#   2 — git operation failed (commit, fetch, etc); vault state unchanged or
#       partial; user must reconcile. Stderr carries the underlying error.
#   3 — push twice rejected (rebase-retry didn't resolve); local commits
#       remain unpushed. Working tree is clean (rebase aborted on conflict).
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <content_root> <commit-message>" >&2
    exit 1
fi

CONTENT_ROOT="$1"
COMMIT_MSG="$2"

if [[ ! -d "$CONTENT_ROOT/.git" ]]; then
    echo "[live-commit-push] $CONTENT_ROOT is not a git repo" >&2
    exit 1
fi

cd "$CONTENT_ROOT"

# Per-invocation tempfile, cleaned on exit (per #75 challenger C3 — shared
# /tmp/live-push.err raced when concurrent invocations stomped each other's
# stderr capture before grep classified the failure).
ERR_FILE=$(mktemp -t live-push.XXXXXX)
trap 'rm -f "$ERR_FILE"' EXIT

# Stage memory/, .harvest/, kb/, artefacts/ — never raw/ (gitignored anyway,
# but defensive). kb/ and artefacts/ added per #83 (work-execution procedure):
# Phase 3 write-back uses this helper for KB diffs and artefact files; without
# them in the stage list the helper exits 0 silently with nothing committed.
#
# Stage each path individually because `git add a b c` aborts on the FIRST
# nonexistent path and skips the rest (smoke-tested 2026-05-07 — passing
# .harvest/ when it doesn't exist made artefacts/ never reach the index).
# Per-path `|| true` tolerates nonexistent paths silently.
for path in memory/ .harvest/ kb/ artefacts/; do
    [[ -e "$path" ]] && git add "$path" 2>/dev/null || true
done

# Skip if nothing to commit. The `if` context disables set -e for the test,
# so this is the safe way to branch on commit-cleanliness.
if git diff --cached --quiet; then
    echo "[live-commit-push] no changes staged; skipping commit + push"
    exit 0
fi

# Per #75 challenger C2 — under set -e, a bare `git commit` failure (gpg
# signing, pre-commit hook, index lock) would exit the script with code 1,
# which docstring reserves for "bad args". Wrap in if so we can map to 2.
if ! git commit -m "$COMMIT_MSG" >&2 2>"$ERR_FILE"; then
    echo "[live-commit-push] git commit failed:" >&2
    cat "$ERR_FILE" >&2
    exit 2
fi

# First push attempt.
if git push 2>"$ERR_FILE"; then
    echo "[live-commit-push] pushed cleanly"
    exit 0
fi

# Inspect the failure. Non-fast-forward → retry with pull --rebase.
if grep -q "non-fast-forward\|fetch first" "$ERR_FILE"; then
    echo "[live-commit-push] non-ff rejection — pulling + rebasing" >&2
    if ! git pull --rebase 2>&1 >&2; then
        echo "[live-commit-push] rebase failed; aborting to leave working tree clean" >&2
        # Per #75 challenger C1 — without --abort, .git/rebase-merge persists
        # and the user's next git command sees a half-rebase state.
        git rebase --abort >&2 2>/dev/null || true
        exit 3
    fi
    if git push 2>&1 >&2; then
        echo "[live-commit-push] pushed after rebase"
        exit 0
    fi
    echo "[live-commit-push] second push failed after rebase" >&2
    exit 3
fi

# Some other push failure (auth, network, etc.) — surface it.
echo "[live-commit-push] push failed (not non-ff):" >&2
cat "$ERR_FILE" >&2
exit 2
