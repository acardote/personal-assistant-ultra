#!/usr/bin/env bash
# Commit + push memory/ from <content_root> with rebase-retry on non-fast-forward.
#
# Per #74: per-query live-writeback writes new memory objects locally; this
# script commits + pushes them so the vault stays the single source of truth
# across machines. On non-fast-forward rejection (another machine pushed
# between our pull and push), retries once with `git pull --rebase`.
#
# Usage:
#   tools/live-commit-push.sh <content_root> "<commit-message>"
#
# Exit codes:
#   0 — committed + pushed (or nothing to commit, success no-op)
#   1 — bad args
#   2 — git operation failed; vault is in unknown state, user must reconcile
#   3 — push failed twice (rebase didn't resolve); local has unpushed commits
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <content_root> <commit-message>" >&2
    exit 1
fi

CONTENT_ROOT="$1"
COMMIT_MSG="$2"

if [[ ! -d "$CONTENT_ROOT/.git" ]]; then
    echo "[live-commit-push] $CONTENT_ROOT is not a git repo" >&2
    exit 2
fi

cd "$CONTENT_ROOT"

# Stage only memory/ and .harvest/ — never raw/ (gitignored anyway, but defensive).
git add memory/ .harvest/ 2>/dev/null || true

# Skip if nothing to commit.
if git diff --cached --quiet; then
    echo "[live-commit-push] no changes staged; skipping commit + push"
    exit 0
fi

git commit -m "$COMMIT_MSG" >&2

# First push attempt.
if git push 2>/tmp/live-push.err; then
    echo "[live-commit-push] pushed cleanly"
    exit 0
fi

# Inspect the failure. Non-fast-forward → retry with pull --rebase.
if grep -q "non-fast-forward\|fetch first" /tmp/live-push.err; then
    echo "[live-commit-push] non-ff rejection — pulling + rebasing" >&2
    if ! git pull --rebase 2>&1 >&2; then
        echo "[live-commit-push] rebase failed; manual reconciliation needed" >&2
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
cat /tmp/live-push.err >&2
exit 2
