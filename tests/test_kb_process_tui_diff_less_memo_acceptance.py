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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
