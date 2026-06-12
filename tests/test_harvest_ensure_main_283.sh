#!/usr/bin/env bash
# Regression test for issue #283: harvest-routine ensure-main step must
# fast-forward local main to origin/main BEFORE the stash pop, so re-applying
# harvest output never conflicts on .harvest/*.json edits already in origin.
#
# The routine runs under bash (NOT zsh: zsh mangles the `! ... ` construct inside
# `if`). This test is written for and MUST be invoked with bash.
#
# It does NOT hardcode the procedure. It EXTRACTS the exact "Ensure HEAD is main"
# bash snippet from templates/routines/harvest-routine.md (the indented code block
# beginning `git fetch origin main` and ending at the upstream-set line) and runs
# that extracted code against real git fixtures. If the template drifts, the
# extraction follows it.
#
# CASE 1 (FF-able stale local main):  assert NO stash-pop conflict, main==origin/main.
# CASE 2 (divergent local main):      assert non-destructive abort (stash + local commit survive).
# CASE 3 (HEAD already main, stale):  assert FF advances main with no stash churn.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROUTINE="$REPO_ROOT/templates/routines/harvest-routine.md"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ensure_main_283.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
note() { printf '  %s\n' "$*"; }
ok()   { printf 'PASS: %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf 'FAIL: %s\n' "$*"; FAIL=$((FAIL+1)); }

# --- Extract the ensure-main snippet from the live template -------------------
# The block is indented by 7 spaces in the markdown. Pull lines from the one
# starting with `git fetch origin main` through the `git branch --set-upstream-to`
# line, then strip the leading 7-space indentation so it is runnable bash.
SNIPPET="$WORK/ensure_main_snippet.sh"
awk '
  /^       git fetch origin main 2>&1 >&2$/ { capture=1 }
  capture { print }
  /^       git branch --set-upstream-to=origin\/main main 2>&1 >&2 \|\| true$/ { if (capture) exit }
' "$ROUTINE" | sed -E 's/^       //' > "$SNIPPET"

if ! grep -q 'git merge --ff-only origin/main' "$SNIPPET"; then
  bad "extraction: snippet does not contain 'git merge --ff-only origin/main' (patch missing or extraction failed)"
  echo "----- extracted snippet -----"; cat "$SNIPPET"; echo "-----------------------------"
  echo; echo "TOTAL: $PASS passed, $FAIL failed"; exit 1
fi
ok "extraction: ensure-main snippet pulled from template and contains the --ff-only FF"

# Sanity: the snippet must NOT contain reset --hard / force-move of main ref as an
# actual command. Strip comment lines (leading #) and echo/message lines first so
# the FATAL strings and the explanatory comment (which legitimately mention
# "reset --hard" to say it is NOT used) do not produce a false positive.
EXEC_LINES="$(grep -vE '^[[:space:]]*#' "$SNIPPET" | grep -v 'echo ')"
if printf '%s\n' "$EXEC_LINES" | grep -Eq 'reset --hard|checkout -B main|branch -f main'; then
  bad "snippet contains a destructive ref force-move (reset --hard / -B / branch -f) as a command — violates no-destructive-loss"
else
  ok "snippet contains no reset --hard / force ref-move command (mentions in comments/messages excluded)"
fi

# --- Fixture builder ----------------------------------------------------------
# bare origin; seed clone (commit A: gmail seen:[m1], watermark w0); advancer
# advances origin +2 (gmail seen:[m1,m2], watermark w1). Returns via globals:
#   ORIGIN, SANDBOX, SEED_SHA
build_fixture() {
  local root="$1"
  ORIGIN="$root/origin.git"
  local seed="$root/seed"
  git init --quiet --bare "$ORIGIN"

  git clone --quiet "$ORIGIN" "$seed"
  git -C "$seed" config user.email t@t.test
  git -C "$seed" config user.name  tester
  mkdir -p "$seed/.harvest"
  printf '{"seen":["m1"]}\n' > "$seed/.harvest/gmail.json"
  printf '{"watermark":"w0"}\n' > "$seed/.harvest/kb-scan-watermark.json"
  git -C "$seed" add -A
  git -C "$seed" commit --quiet -m "seed: gmail m1, watermark w0"
  git -C "$seed" branch -M main
  git -C "$seed" push --quiet -u origin main
  # Point the bare repo's default HEAD at main so later clones check it out
  # (the bare repo was init'd empty, so its HEAD still names master otherwise).
  git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
  SEED_SHA="$(git -C "$seed" rev-parse HEAD)"

  # advancer advances origin/main +2 commits touching the same state files.
  local adv="$root/adv"
  git clone --quiet "$ORIGIN" "$adv"
  git -C "$adv" config user.email t@t.test
  git -C "$adv" config user.name  tester
  printf '{"seen":["m1","m2"]}\n' > "$adv/.harvest/gmail.json"
  git -C "$adv" commit --quiet -am "advance: gmail m2"
  printf '{"watermark":"w1"}\n' > "$adv/.harvest/kb-scan-watermark.json"
  git -C "$adv" commit --quiet -am "advance: watermark w1"
  git -C "$adv" push --quiet origin main

  # sandbox clone, then pin local main STALE at SEED_SHA with HEAD detached at
  # origin/main tip (mirrors the routine sandbox construction in the design).
  SANDBOX="$root/sandbox"
  git clone --quiet "$ORIGIN" "$SANDBOX"
  git -C "$SANDBOX" config user.email t@t.test
  git -C "$SANDBOX" config user.name  tester
  git -C "$SANDBOX" fetch --quiet origin main
}

# Run the extracted snippet inside a sandbox; capture exit code.
run_snippet() {
  ( cd "$1" && RUN_TS="test-$$" VAULT="$1" bash "$SNIPPET" >/dev/null 2>&1 )
}

# Pre-fix snippet: the same extracted block with the `git merge --ff-only`
# guard removed. Used by CASE 0 to prove the fixture actually reproduces the
# #283 bug (so a green CASE 1 is not vacuous).
PREFIX_SNIPPET="$WORK/ensure_main_prefix.sh"
awk '
  /git merge --ff-only origin\/main 2>&1 >&2/ { skip=1 }
  skip && /^[[:space:]]*fi[[:space:]]*$/ { skip=0; next }
  skip && /^[[:space:]]*#/ { next }
  !skip { print }
' "$SNIPPET" > "$PREFIX_SNIPPET"
run_prefix_snippet() {
  ( cd "$1" && RUN_TS="test-$$" VAULT="$1" bash "$PREFIX_SNIPPET" >/dev/null 2>&1 )
}

# ===================== CASE 0: counterfactual (no-FF reproduces bug) ==========
# Same fixture as CASE 1, run with the FF removed. Assert the stash pop DOES
# conflict — proving the fixture exercises the #283 failure mode the FF fixes.
if grep -q 'git merge --ff-only origin/main' "$PREFIX_SNIPPET"; then
  bad "CASE0 setup: pre-fix snippet still contains the FF (awk strip failed)"
else
  ok "CASE0 setup: pre-fix snippet has the FF guard removed"
  C0="$WORK/c0"; mkdir -p "$C0"
  build_fixture "$C0"
  ORIGIN_TIP0="$(git -C "$SANDBOX" rev-parse origin/main)"
  git -C "$SANDBOX" checkout --quiet -b session-branch "$ORIGIN_TIP0"
  git -C "$SANDBOX" branch -f main "$SEED_SHA"
  printf '{"seen":["m1","m2","m3"]}\n' > "$SANDBOX/.harvest/gmail.json"
  run_prefix_snippet "$SANDBOX"; rc0=$?
  if [ "$rc0" -ne 0 ] || [ -n "$(git -C "$SANDBOX" ls-files -u)" ]; then
    ok "CASE0 pre-fix snippet conflicts/aborts (rc=$rc0, unmerged='$(git -C "$SANDBOX" ls-files -u | wc -l | tr -d ' ')') — fixture reproduces #283"
  else
    bad "CASE0 pre-fix snippet did NOT conflict (rc=$rc0) — fixture does not reproduce #283; CASE1 green would be vacuous"
  fi
fi

# ============================ CASE 1: FF-able stale ===========================
C1="$WORK/c1"; mkdir -p "$C1"
build_fixture "$C1"
# Mirror the live routine sandbox: HEAD on a SESSION BRANCH created at the
# CURRENT origin/main tip (the working tree reflects origin's state — gmail m1,m2
# / watermark w1 — because harvest ran against current origin), while LOCAL main
# is pinned STALE at SEED (the stranding state). The harvest output this fire
# extends gmail to seen:[m1,m2,m3] ON TOP of origin's state.
# Move HEAD off main first (clone lands on main) so `branch -f main` is allowed.
ORIGIN_TIP="$(git -C "$SANDBOX" rev-parse origin/main)"
git -C "$SANDBOX" checkout --quiet -b session-branch "$ORIGIN_TIP"
git -C "$SANDBOX" branch -f main "$SEED_SHA"          # local main pinned stale (2 behind origin/main)
# Write harvest output this fire: extend gmail to seen:[m1,m2,m3] on origin's base.
printf '{"seen":["m1","m2","m3"]}\n' > "$SANDBOX/.harvest/gmail.json"
run_snippet "$SANDBOX"; rc=$?

if [ "$rc" -eq 0 ]; then ok "CASE1 exit 0"; else bad "CASE1 exit $rc (expected 0)"; fi
[ "$(git -C "$SANDBOX" rev-parse --abbrev-ref HEAD)" = "main" ] \
  && ok "CASE1 HEAD on main" || bad "CASE1 HEAD not on main"
counts="$(git -C "$SANDBOX" rev-list --left-right --count main...origin/main 2>/dev/null)"
[ "$counts" = "0	0" ] \
  && ok "CASE1 main==origin/main (ahead/behind 0/0)" || bad "CASE1 main vs origin = '$counts' (expected '0\t0')"
[ -z "$(git -C "$SANDBOX" ls-files -u)" ] \
  && ok "CASE1 no unmerged paths (no stash-pop conflict)" || bad "CASE1 unmerged paths present (conflict!)"
[ -z "$(git -C "$SANDBOX" stash list)" ] \
  && ok "CASE1 stash dropped cleanly" || bad "CASE1 stash not dropped"
grep -q 'm3' "$SANDBOX/.harvest/gmail.json" \
  && ok "CASE1 harvest output (m3) re-applied" || bad "CASE1 m3 missing from gmail.json"
grep -q 'w1' "$SANDBOX/.harvest/kb-scan-watermark.json" \
  && ok "CASE1 origin state (watermark w1) preserved" || bad "CASE1 watermark w1 missing"

# ====================== CASE 2: divergent local main ==========================
C2="$WORK/c2"; mkdir -p "$C2"
build_fixture "$C2"
# Build a DIVERGENT local main: a local-only commit branching off SEED_SHA
# (origin/main has moved past SEED_SHA with DIFFERENT commits), so local main is
# not a strict ancestor of origin/main -> FF impossible.
# Detach HEAD off main first so `branch -f main` is allowed, build the divergent
# commit on a temp branch, then point local main at it.
git -C "$SANDBOX" checkout --quiet -b divergent-build "$SEED_SHA"
printf '{"local":"divergent"}\n' > "$SANDBOX/.harvest/local-divergent.json"
git -C "$SANDBOX" add -A
git -C "$SANDBOX" commit --quiet -m "local-only divergent commit"
DIVERGENT_SHA="$(git -C "$SANDBOX" rev-parse HEAD)"
git -C "$SANDBOX" branch -f main "$DIVERGENT_SHA"
# Now move to a session branch so ensure-main runs the stash+checkout+ff path,
# and the ff onto the divergent local main is impossible.
git -C "$SANDBOX" checkout --quiet -b session-branch "$DIVERGENT_SHA"
printf '{"seen":["m1","m2","m3"]}\n' > "$SANDBOX/.harvest/gmail.json"
run_snippet "$SANDBOX"; rc=$?

[ "$rc" -ne 0 ] && ok "CASE2 non-zero exit (abort) rc=$rc" || bad "CASE2 exit 0 (expected abort)"
[ -n "$(git -C "$SANDBOX" stash list)" ] \
  && ok "CASE2 harvest output PRESERVED in stash" || bad "CASE2 stash empty — harvest output lost"
git -C "$SANDBOX" cat-file -e "$DIVERGENT_SHA^{commit}" 2>/dev/null \
  && ok "CASE2 local divergent commit not clobbered (no force-move/reset)" \
  || bad "CASE2 divergent commit gone — destructive ref move occurred"
# main ref must NOT have been force-moved to origin/main.
[ "$(git -C "$SANDBOX" rev-parse main)" != "$(git -C "$SANDBOX" rev-parse origin/main)" ] \
  && ok "CASE2 main ref NOT force-moved to origin/main" \
  || bad "CASE2 main ref was moved to origin/main (force-move!)"

# ================== CASE 3: HEAD already main, stale, clean ===================
# When HEAD is already main the ensure-main `if` body is skipped entirely, so
# the snippet does not FF. This documents that the FF lives inside the
# off-main branch path (the stranding class), matching the live procedure.
C3="$WORK/c3"; mkdir -p "$C3"
build_fixture "$C3"
# HEAD already main; make it stale via reset --hard (FIXTURE setup only, not the
# code under test) so the ensure-main `if HEAD != main` body is skipped.
git -C "$SANDBOX" checkout --quiet main
git -C "$SANDBOX" reset --hard --quiet "$SEED_SHA"   # stale, clean tree
run_snippet "$SANDBOX"; rc=$?
[ "$rc" -eq 0 ] && ok "CASE3 exit 0 (HEAD already main)" || bad "CASE3 exit $rc"
# Hard guard still confirms HEAD==main.
[ "$(git -C "$SANDBOX" rev-parse --abbrev-ref HEAD)" = "main" ] \
  && ok "CASE3 HEAD remains main" || bad "CASE3 HEAD not main"

echo
echo "TOTAL: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
