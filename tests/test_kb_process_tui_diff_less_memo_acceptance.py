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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
