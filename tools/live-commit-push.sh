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
#   4 — provenance lint refused (per #85); nothing committed. Fix the
#       malformed entry and retry.
#   5 — content_root arg disagrees with .assistant.local.json's configured
#       vault path (per #87); refuses to lint the wrong tree silently.
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

# Per #87: the lint resolves the vault via .assistant.local.json, NOT via
# our $CONTENT_ROOT arg. If the two disagree, the lint scans the wrong tree
# and the gate silently misses. Refuse loudly. Single-vault usage by design;
# multi-vault would need a separate config story.
#
# Use python via env-var transport (NOT string interpolation) so paths with
# quotes / unusual chars don't crash the comparison. samefile handles
# symlinks AND case-insensitive filesystems (macOS HFS+) in one call.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
METHOD_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
SCOPE_CHECK=$(LCP_METHOD_ROOT="$METHOD_ROOT" LCP_ARG_ROOT="$CONTENT_ROOT" python3 -c '
import json, os, sys
mr = os.environ["LCP_METHOD_ROOT"]
arg = os.environ["LCP_ARG_ROOT"]
cfg = os.path.join(mr, ".assistant.local.json")
if not os.path.isfile(cfg):
    print("no-config")
    sys.exit(0)
try:
    cr = json.load(open(cfg)).get("paths", {}).get("content_root")
except Exception:
    print("warn:corrupt-config")
    sys.exit(0)
if not cr:
    print("warn:empty-content-root")
    sys.exit(0)
configured = os.path.realpath(os.path.expanduser(cr))
try:
    if os.path.samefile(arg, configured):
        print("ok")
    else:
        print(f"mismatch:{configured}")
except FileNotFoundError:
    # configured path no longer exists — treat as no-enforcement; the lint
    # will surface that separately via its own fallback warning.
    print("warn:configured-missing")
' 2>/dev/null || echo "warn:python-failed")

case "$SCOPE_CHECK" in
    ok|no-config|warn:*)
        # warn:* cases fall through with no enforcement; the lint's own
        # fallback warning surfaces config issues separately.
        if [[ "$SCOPE_CHECK" == warn:* ]]; then
            echo "[live-commit-push] $SCOPE_CHECK — proceeding without scope check" >&2
        fi
        ;;
    mismatch:*)
        configured="${SCOPE_CHECK#mismatch:}"
        echo "[live-commit-push] content_root arg ($CONTENT_ROOT) disagrees with" >&2
        echo "  .assistant.local.json's configured vault ($configured)." >&2
        echo "  The lint would scan the configured vault, not your arg —" >&2
        echo "  refusing to commit with mismatched scope. Either point" >&2
        echo "  .assistant.local.json at the arg, or pass the configured path." >&2
        exit 5
        ;;
esac

cd "$CONTENT_ROOT"

# Per-invocation tempfile, cleaned on exit (per #75 challenger C3 — shared
# /tmp/live-push.err raced when concurrent invocations stomped each other's
# stderr capture before grep classified the failure).
ERR_FILE=$(mktemp -t live-push.XXXXXX)
trap 'rm -f "$ERR_FILE"' EXIT

# Provenance lint gate (per #85, B4 fixup): refuse to commit malformed
# agent-produced KB / artefact entries. The lint lives next to this script
# in <method_root>/tools/. Running --require-vault keeps method-only mode
# from silently passing here. Failures are LOUD and abort.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LINT="$SCRIPT_DIR/lint-provenance.py"
if [[ -x "$LINT" ]]; then
    if ! "$LINT" --require-vault >&2; then
        echo "[live-commit-push] provenance lint refused — fix violations and retry" >&2
        exit 4
    fi
fi

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
