#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for #229 / #231 — kb-process TUI diff-less memo handling.

Tests the contract that:
  - `memo_has_diff_block` correctly distinguishes Mode-A (no ```diff fence)
    from Mode-B (fence present, shape violation) and from happy-path memos.
  - `run_amend_flow` asserts loudly if a Mode-A memo reaches it (the m-handler
    is supposed to guard upstream, so reaching the assertion is a regression).
  - `extract_diff_block_content` still returns None on Mode-A (the helper and
    the extractor agree on the predicate).

Fixtures are sanitized from the user's vault (per #229 Repro evidence) —
structural shape preserved, identifying content redacted.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent


def _load_tui_module():
    spec = spec_from_file_location(
        "kb_process_tui",
        PROJ / "tools" / "kb-process-tui.py",
    )
    mod = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


tui = _load_tui_module()


# ---------- Fixtures: structural shapes drawn from the user's vault ----------

MODE_A_MEMO = """---
id: art-00000000-0000-0000-0000-000000000001
kind: memo
created_at: '2026-05-20T06:29:23Z'
title: 'Candidate decision: <redacted>'
produced_by:
  session_id: deadbeef
  query: kb-scan decision-extract from mem-<redacted>
  model: claude-opus-4-7
  sources_cited:
  - mem://mem-<redacted>
---

## Candidate decision: <redacted>

<one-paragraph body in plain prose — no diff block, no Proposed-diff section>

## Sources
- mem://mem-<redacted>
"""

MODE_B_MEMO = """---
id: art-00000000-0000-0000-0000-000000000002
kind: memo
title: 'Candidate person: <redacted>'
---

## Proposed diff

```diff
+ ## <redacted>
+ - **Role / relation:** <redacted>
+ - **Last verified:** 2026-05-12
+
+ <first paragraph line>
this is a continuation line without the + prefix and trips Mode B.
```

## Sources
- mem://mem-<redacted>
"""

HAPPY_PATH_MEMO = """---
id: art-00000000-0000-0000-0000-000000000003
kind: memo
title: 'Candidate decision: <redacted>'
---

## Proposed diff

```diff
+ ## <redacted>
+ - **Date:** 2026-05-22
+ - **Status:** decided
+
+ Single-line body, properly prefixed.
```

## Sources
- mem://mem-<redacted>
"""


# ---------- T1: memo_has_diff_block ----------


def test_memo_has_diff_block_returns_false_on_mode_a():
    """Mode-A memo (kb-scan decision-extract emission with no diff fence)
    must return False — that's the guard the m-handler relies on."""
    assert tui.memo_has_diff_block(MODE_A_MEMO) is False


def test_memo_has_diff_block_returns_true_on_mode_b():
    """Mode-B memo HAS a ```diff fence; the shape violation is on a line
    INSIDE the block. memo_has_diff_block is a cheap fence-presence check
    — it must NOT eagerly inspect line shape (that's `extract_diff_block_content`'s
    job, and Mode-B is a real emitter bug worth surfacing via the existing
    red-error path)."""
    assert tui.memo_has_diff_block(MODE_B_MEMO) is True


def test_memo_has_diff_block_returns_true_on_happy_path():
    """A properly-formed diff-bearing memo must return True so the m-handler
    routes through `run_amend_flow` (NL amend) and does NOT regress to $EDITOR."""
    assert tui.memo_has_diff_block(HAPPY_PATH_MEMO) is True


# ---------- T2: extractor agreement (cross-check the helper against the extractor) ----------


def test_extractor_returns_none_on_mode_a():
    """Cross-check: the predicate the helper uses MUST be the same predicate
    the extractor uses. If these diverge, the m-handler could route a memo
    one way while `extract_diff_block_content` routes it the other — which
    is the exact root cause #231 is fixing."""
    assert tui.extract_diff_block_content(MODE_A_MEMO) is None


def test_extractor_returns_none_on_mode_b():
    """Mode B is the shape-violation case — extract returns None, but for a
    different reason than Mode A. The helper still returns True (fence
    present); the m-handler routes to `run_amend_flow`, which falls into the
    existing red-error path (no assertion fires because the assertion is
    Mode-A-specific — `_DIFF_BLOCK_RE.search` matches on Mode-B input)."""
    assert tui.extract_diff_block_content(MODE_B_MEMO) is None


def test_extractor_succeeds_on_happy_path():
    extracted = tui.extract_diff_block_content(HAPPY_PATH_MEMO)
    assert extracted is not None
    assert "## <redacted>" in extracted
    assert "Single-line body, properly prefixed." in extracted


# ---------- T3: run_amend_flow guard (Mode-A must NOT silently fail) ----------


def test_run_amend_flow_asserts_on_mode_a(tmp_path):
    """If a Mode-A memo somehow reaches `run_amend_flow` (e.g., a future
    caller forgets to gate via `memo_has_diff_block`), the function must
    raise AssertionError loudly rather than silently returning ("failed",
    0, ""). The assertion is the trail-marker that surfaces a regression
    in the upstream guard contract — without it, a regression would be
    invisible (the legacy red-error path would just fire again)."""
    memo_path = tmp_path / "art-mode-a.md"
    memo_path.write_text(MODE_A_MEMO, encoding="utf-8")
    with pytest.raises(AssertionError, match="memo_has_diff_block"):
        tui.run_amend_flow(memo_path)


def test_run_amend_flow_red_error_path_intact_on_mode_b(tmp_path, capsys, monkeypatch):
    """Mode-B input has a ```diff fence (so the assertion does NOT fire) but
    its content fails the shape contract — `extract_diff_block_content` returns
    None for a different reason. The existing red-error + ("failed", 0, "")
    behavior must be preserved so Child 3b's emitter bug stays visible to
    operators (and to the harvest dashboard) without changing the existing
    return contract.

    Robustness note: run_amend_flow opens an interactive prompt on the happy
    path; we don't reach it here because the Mode-B input fails out at
    extraction. If a future refactor makes this test reach the prompt, that
    itself is a regression worth catching."""
    memo_path = tmp_path / "art-mode-b.md"
    memo_path.write_text(MODE_B_MEMO, encoding="utf-8")

    status, rounds, instr = tui.run_amend_flow(memo_path)
    assert status == "failed"
    assert rounds == 0
    assert instr == ""
    captured = capsys.readouterr()
    assert "Couldn't extract diff block" in captured.out


# ---------- T4: Mode-B error-surface improvements (#234) ----------


def test_mode_b_violation_lines_finds_all_offenders():
    """`_mode_b_violation_lines` reports EVERY shape-violating file line
    inside the first ```diff block — not just the first (pr-challenger F3
    on #234). Operator needs to see the full scope of the fix so they don't
    fix-and-retry one line at a time."""
    memo_with_two_violations = (
        "---\n"
        "id: art-test\n"
        "---\n"
        "\n"
        "## Heading\n"
        "\n"
        "```diff\n"        # file line 7
        "+ ## Title\n"     # file line 8 — OK
        "+ - item\n"       # file line 9 — OK
        "raw bad line 1\n" # file line 10 — VIOLATION
        "+ ok again\n"     # file line 11 — OK
        "another raw bad\n"# file line 12 — VIOLATION
        "```\n"
    )
    violations = tui._mode_b_violation_lines(memo_with_two_violations)
    assert violations == [10, 12], (
        f"Expected violations at file lines [10, 12]; got {violations}. "
        "If this assert fails, the file-line arithmetic in "
        "_mode_b_violation_lines drifted from the docstring contract."
    )


def test_mode_b_violation_lines_empty_when_no_violations():
    """A clean diff block returns empty list — no false positives."""
    clean = (
        "## Heading\n\n"
        "```diff\n"
        "+ a\n"
        "+ b\n"
        "+\n"
        "+ c\n"
        "```\n"
    )
    assert tui._mode_b_violation_lines(clean) == []


def test_mode_b_violation_lines_empty_when_no_diff_block():
    """No ```diff fence → empty list. This function is a Mode-B classifier;
    Mode A is `memo_has_diff_block`'s domain."""
    assert tui._mode_b_violation_lines("just prose no diff fence") == []


def test_run_amend_flow_mode_b_message_names_mode_and_file_line(tmp_path, capsys):
    """The Mode-B error surface (#234) must include the string 'Mode B' and
    the file line number(s) of the offending line(s). It must NOT echo the
    offending line CONTENT (pr-challenger F2 — privacy)."""
    memo = (
        "---\n"
        "id: art-mode-b-1\n"
        "---\n"
        "\n"
        "## Heading\n"
        "\n"
        "```diff\n"
        "+ ## Title\n"
        "+ - **Source:** secret_token_AKIAIOSFODNN7EXAMPLE\n"
        "secret_token_AKIAIOSFODNN7EXAMPLE_continued_on_unprefixed_line\n"
        "```\n"
    )
    memo_path = tmp_path / "art-mode-b-1.md"
    memo_path.write_text(memo, encoding="utf-8")

    status, rounds, instr = tui.run_amend_flow(memo_path)
    assert status == "failed"
    captured = capsys.readouterr()
    assert "Mode B" in captured.out
    # File line 10 is the violation (1: ---, 2: id, 3: ---, 4: blank, 5: ## Heading,
    # 6: blank, 7: ```diff, 8: + ## Title, 9: + - **Source:**, 10: secret_token_...)
    assert "line 10" in captured.out
    # Memo path is repr'd for terminal-safety (#238 pr-challenger F8); check
    # via the file's basename (a substring of both raw and repr'd forms).
    assert memo_path.name in captured.out
    # F2 + F11 (pr-challenger on #234, hardened on #238) — content NOT echoed.
    # The fake secret string must not leak; and no ≥6-char substring of it
    # should appear either (defends against partial-leak via column-truncation
    # or base64-style chunking).
    secret = "AKIAIOSFODNN7EXAMPLE"
    assert secret not in captured.out
    for i in range(len(secret) - 6 + 1):
        chunk = secret[i:i + 6]
        assert chunk not in captured.out, (
            f"6-char substring `{chunk}` of the secret leaked into the message"
        )


def test_run_amend_flow_mode_b_message_lists_all_violations(tmp_path, capsys):
    """Multiple violations are all reported (pr-challenger F3) — the operator
    sees the full scope, not just the first offender."""
    memo = (
        "---\n"
        "id: art-mode-b-multi\n"
        "---\n"
        "\n"
        "```diff\n"        # file line 5
        "+ ## Title\n"     # file line 6 — OK
        "raw line A\n"     # file line 7 — VIOLATION
        "+ ok\n"           # file line 8 — OK
        "raw line B\n"     # file line 9 — VIOLATION
        "+ also ok\n"      # file line 10 — OK
        "raw line C\n"     # file line 11 — VIOLATION
        "```\n"
    )
    memo_path = tmp_path / "art-mode-b-multi.md"
    memo_path.write_text(memo, encoding="utf-8")

    status, _, _ = tui.run_amend_flow(memo_path)
    assert status == "failed"
    captured = capsys.readouterr()
    assert "Mode B" in captured.out
    assert "7" in captured.out
    assert "9" in captured.out
    assert "11" in captured.out


def test_run_amend_flow_mode_b_message_truncates_when_many_violations(tmp_path, capsys):
    """For ≥6 violations, the message summarizes ('N violations starting at
    line K (next: …)') rather than listing all — keeps the terminal line
    readable while preserving the total count."""
    diff_lines = ["+ ## T"]
    for i in range(8):
        diff_lines.append(f"bad line {i}")  # 8 violations
    memo = "---\nid: x\n---\n\n```diff\n" + "\n".join(diff_lines) + "\n```\n"
    memo_path = tmp_path / "art-mode-b-many.md"
    memo_path.write_text(memo, encoding="utf-8")

    status, _, _ = tui.run_amend_flow(memo_path)
    assert status == "failed"
    captured = capsys.readouterr()
    assert "8 violations" in captured.out
    assert "Mode B" in captured.out
    # F7 (pr-challenger #238) — ellipsis only appears when MORE remain after
    # the preview. For n=8: preview shows 3 ("next: X, Y, Z"), 4 remain
    # afterward → ellipsis present.
    assert "…" in captured.out


def test_mode_b_message_ellipsis_only_when_items_remain_beyond_preview(tmp_path, capsys):
    """F7 (pr-challenger #238) — ellipsis must be truthful. The previous shape
    showed `, …` unconditionally for n≥6, but the right rule is "ellipsis
    appears IFF there are items beyond the preview".

    Boundary trace (preview is `violations[1:4]` = up to 3 items):
    - n=6: first shown + preview shown (3) = 4 items. Remaining = 2. Ellipsis ✓
    - n=7: 4 shown, 3 remain. Ellipsis ✓
    - n=4 / n=5: handled by the `n <= 5` enumerate-all branch — no ellipsis ever.

    The condition `n > 1 + len(preview) + 1` says "ellipsis if at least one
    item exists beyond the [first + preview] block". For n=6, 6 > 1+3+1=5 →
    ellipsis. Correct — 2 more do exist."""
    # Verify n=6 case: ellipsis IS present (2 items remain beyond preview).
    diff_lines = ["+ ## T"]
    for i in range(6):
        diff_lines.append(f"bad line {i}")
    memo = "---\nid: x\n---\n\n```diff\n" + "\n".join(diff_lines) + "\n```\n"
    memo_path = tmp_path / "art-mode-b-six.md"
    memo_path.write_text(memo, encoding="utf-8")

    status, _, _ = tui.run_amend_flow(memo_path)
    assert status == "failed"
    captured = capsys.readouterr()
    assert "6 violations" in captured.out
    # 2 items remain after the 4-item header; ellipsis correctly says "more".
    assert "…" in captured.out, (
        "ellipsis should appear at n=6 — 2 violations exist beyond the preview"
    )


def test_mode_b_helper_consistent_with_extractor_on_crlf(tmp_path):
    """F5 (pr-challenger #238) — CRLF caveat. `_mode_b_violation_lines` and
    `extract_diff_block_content` BOTH use `m.group(1).split('\\n')` without
    stripping `\\r`. The bare-`+` blank line ends up as `+\\r`, which fails
    the shape contract in BOTH functions consistently:
      - extractor: returns None (bails on the first `+\\r`).
      - helper: reports every `+\\r` line as a violation.
    The two functions AGREE that the memo trips Mode B. This test pins the
    consistency — if a future change strips `\\r` in one but not the other,
    the message would say "Mode B violations: <empty>" while the extractor
    correctly bailed (or vice versa). Fixing CRLF support requires
    coordinating BOTH functions; tracked as future work."""
    crlf_memo = (
        "---\r\n"
        "id: art-crlf\r\n"
        "---\r\n"
        "\r\n"
        "```diff\r\n"
        "+ ## Title\r\n"
        "+ - **Source:** ok\r\n"
        "+\r\n"  # ← bare-`+` blank line in CRLF; both functions see `+\r`
        "+ body\r\n"
        "```\r\n"
    )
    memo_path = tmp_path / "art-crlf.md"
    memo_path.write_text(crlf_memo, encoding="utf-8", newline="")
    # Extractor and helper must agree that the memo is malformed:
    extracted = tui.extract_diff_block_content(crlf_memo)
    violations = tui._mode_b_violation_lines(crlf_memo)
    # Either both signal failure (extractor returns None AND helper finds
    # violations) OR both signal success (extractor returns content AND
    # helper finds none). Anything else is a contract drift.
    extractor_failed = extracted is None
    helper_found_violations = len(violations) > 0
    assert extractor_failed == helper_found_violations, (
        f"extractor and helper disagree on CRLF input: "
        f"extractor returned None? {extractor_failed}; "
        f"helper found violations? {helper_found_violations} ({violations})"
    )


# ---------- T5: a / M handler gates on diff-less memos (#241 / #242) ----------
#
# The `a` (approve) and `M` (direct-$EDITOR amend) handlers used to drive
# diff-less memos through scope-inject + apply chains that fail downstream.
# The fix gates BOTH handlers on `memo_has_diff_block` BEFORE any of that.
# These tests exercise the gate via source-level invariants — the handlers
# themselves live inside the TUI's giant per-memo loop and aren't unit-
# testable directly, so we pin the gate's PRESENCE (the keystroke handler
# block contains a `memo_has_diff_block` check) and ABSENCE of the
# misleading help text the gate replaces.


def _read_tui_source() -> str:
    return (PROJ / "tools" / "kb-process-tui.py").read_text(encoding="utf-8")


def test_all_apply_paths_gate_on_memo_has_diff_block():
    """The `m`, `a`, and `M` handlers must EACH gate on `memo_has_diff_block`
    before any code that would reach `kb-process.py apply` (whose
    `extract_proposed_diff` raises ValueError without a ```diff fence).

    Post-#243 F4 fix, the m and M handlers ALSO re-check after the amend
    session — claude or hand-edit could have deleted the ```diff fence.
    So the expected count of `memo_has_diff_block(memo_path…` calls is:
    - `m` handler: entry gate (#235) + post-amend re-check (#243 F4) = 2
    - `a` handler: entry gate (#242)                                  = 1
    - `M` handler: entry gate (#242) + post-edit re-check (#243 F4)   = 2
    Total = 5."""
    src = _read_tui_source()
    n = src.count("memo_has_diff_block(memo_path")
    assert n == 5, (
        f"Expected 5 `memo_has_diff_block(memo_path` call sites — entry "
        f"gates for m/a/M (3) plus post-amend re-checks for m/M (2); "
        f"found {n}. If you removed or added a gate / re-check, update "
        f"this test deliberately. See #235 (m entry), #242 (a/M entry), "
        f"#243 F4 (m/M post-amend)."
    )


def test_handlers_gate_before_inject_scope_into_memo_call_sites():
    """Every CALL SITE of `inject_scope_into_memo` (not the def itself, not
    docstring mentions) must be preceded — within the same handler block —
    by a `memo_has_diff_block` check. Pin the locality so a future refactor
    that moves the gate too far or removes it fails this test.

    Match pattern: `injected = inject_scope_into_memo(memo_path,` — that's
    the specific call shape used in both the `a` and `M` handlers."""
    src = _read_tui_source()
    call_pattern = "injected = inject_scope_into_memo(memo_path,"
    call_positions = [i for i in range(len(src)) if src.startswith(call_pattern, i)]
    assert len(call_positions) == 3, (
        f"expected exactly 3 inject_scope_into_memo CALL sites — `a` "
        f"handler, `m` handler (post-claude-amend success), and `M` "
        f"handler; found {len(call_positions)}. If you added or removed "
        f"a call site, update this test deliberately."
    )
    for pos in call_positions:
        # Find the immediately-preceding handler header (`if key == "X":` or
        # `elif key == "X":` in the main walk loop). The handler block between
        # that header and this call site must contain the gate.
        before = src[:pos]
        # Match the LAST `if key ==` / `elif key ==` before this position.
        header_pos = max(
            before.rfind('if key == "a":'),
            before.rfind('elif key == "m":'),
            before.rfind('elif key == "M":'),
        )
        assert header_pos >= 0, (
            f"inject_scope_into_memo call at position {pos} has no preceding "
            f"a/m/M handler header — unexpected structure."
        )
        handler_block = src[header_pos:pos]
        assert "memo_has_diff_block(memo_path" in handler_block, (
            f"inject_scope_into_memo call at position {pos} is inside a "
            f"handler block (starting at {header_pos}) that does NOT contain "
            f"a memo_has_diff_block gate — diff-less memos could reach this "
            f"call site. See #242."
        )


def test_apply_memo_after_amend_is_preceded_by_diff_block_recheck():
    """F4 (per #243 pr-challenger) — the entry gates in the `m` and `M`
    handlers read the memo on ENTRY, but the amend session can DELETE the
    ```diff fence (claude instruction like "rewrite as prose" / hand-edit).
    If `apply_memo` runs unconditionally after that, the operator gets a
    "✗ apply failed rc=1" cascade.

    Invariant (only for handlers WITH an amend session — `m` and `M`,
    not `a`): between the amend call (`run_amend_flow` for `m`, the post-
    gate `amend_in_editor` for `M`) and the subsequent `apply_memo` call,
    there must be a `memo_has_diff_block` re-check."""
    src = _read_tui_source()
    apply_pattern = "rc, out = apply_memo(method_root, art_id)"
    apply_positions = [i for i in range(len(src)) if src.startswith(apply_pattern, i)]
    # 3 apply call sites (a, m, M). Check the two with amend sessions.
    # We identify those by looking for `run_amend_flow(memo_path)` or
    # `changed = amend_in_editor(memo_path)` between the handler header
    # and the apply call.
    amend_markers = (
        "status, amend_rounds, amend_instr = run_amend_flow(memo_path)",
        "changed = amend_in_editor(memo_path)",
    )
    apply_after_amend_positions = []
    for pos in apply_positions:
        # Look at the 6000 chars before the apply for an amend marker. The
        # m-handler block is large (~5000 chars from `run_amend_flow` call to
        # `apply_memo` due to scope-prompt, accuracy log, etc.).
        before = src[max(0, pos - 6000):pos]
        if any(m in before for m in amend_markers):
            apply_after_amend_positions.append(pos)
    assert len(apply_after_amend_positions) == 2, (
        f"expected exactly 2 apply_memo call sites downstream of an amend "
        f"session (m + M handlers); found {len(apply_after_amend_positions)}"
    )
    for pos in apply_after_amend_positions:
        # The re-check must appear AFTER the amend marker and BEFORE the
        # apply call. Find the latest amend marker before this apply, then
        # check the slice in between contains memo_has_diff_block.
        before = src[max(0, pos - 6000):pos]
        latest_amend_idx = max(before.rfind(m) for m in amend_markers if m in before)
        between = before[latest_amend_idx:]
        assert "memo_has_diff_block(memo_path" in between, (
            f"apply_memo at position {pos} is downstream of an amend call "
            f"but lacks a memo_has_diff_block re-check between the amend "
            f"and the apply. F4 on #243: amend can delete the diff fence; "
            f"apply without a re-check cascades through ValueError."
        )


def test_diff_less_gate_blocks_do_not_misdirect_to_apply_via_m():
    """F3 (per #242 falsifier, hardened per #243 pr-challenger F6) — the
    misleading "Use `m` to edit manually, then apply" phrase must NOT
    appear inside ANY diff-less gate block, regardless of which issue
    number the block was added under.

    Pre-#243 version of this test keyed on the "#241 / #242" comment
    marker, which would silently NOT cover a future gate block added
    under a different issue. The hardened version delimits each gate
    block by `if not memo_has_diff_block(memo_path` (entry) and the next
    `continue` keyword — independent of any issue-number marker.

    Each `if not memo_has_diff_block(memo_path...` defines a gate block.
    For each block, the misleading phrase MUST NOT appear before the
    block's terminating `continue`."""
    src = _read_tui_source()
    gate_start_pattern = "if not memo_has_diff_block(memo_path"
    bad_phrase = "Use `m` to edit manually, then apply"
    gate_starts = [i for i in range(len(src)) if src.startswith(gate_start_pattern, i)]
    # Expect 5 occurrences post-#243 F4: 3 entry gates (m/a/M) + 2 post-amend
    # re-checks (m/M). See the count-invariant test above for the breakdown.
    assert len(gate_starts) == 5, (
        f"expected exactly 5 `if not memo_has_diff_block(memo_path…` blocks "
        f"(3 entry gates + 2 post-amend re-checks); found {len(gate_starts)}. "
        f"If you added or removed a gate, update the count here deliberately."
    )
    for start in gate_starts:
        # Find the next `continue` statement after this gate's body. The
        # bounce body is indented 16 spaces inside the `if not memo_has_diff_block(...)`
        # block (which itself is at 12 spaces inside the handler).
        block_end = src.find("\n                continue", start)
        assert block_end > start, (
            f"gate block starting at {start} has no terminating `continue` "
            f"at the expected 16-space indent"
        )
        block = src[start:block_end]
        assert bad_phrase not in block, (
            f"The misleading phrase {bad_phrase!r} appears inside a "
            f"diff-less gate block (start={start}). #242 F3 / #243 F6: "
            f"`m` cannot apply diff-less memos post-#235, so suggesting "
            f"'use `m`... then apply' would mislead the operator."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
