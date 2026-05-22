#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["PyYAML"]
# ///
"""Acceptance tests for #229 Child 3b / #233 — kb-scan continuation-line prefix.

Tests the contract that `tools/kb-scan.py`'s diff-renderers emit `+ ` on
every non-empty line of a wrapped body / summary, and `+` on every blank
line — matching the format `extract_diff_block_content` (kb-process-tui.py)
accepts and `kb-process apply` round-trips.

Pre-fix Mode-B failure: `render_decision_diff(body="line1\\nline2")` produced
`+ line1\\nline2` (continuation `line2` un-prefixed), tripping the TUI's
shape-contract check.
"""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = spec_from_file_location(name, PROJ / "tools" / filename)
    mod = module_from_spec(spec)
    assert spec and spec.loader
    # Register the module BEFORE exec_module so `dataclasses.dataclass` can
    # resolve `cls.__module__` while introspecting frozen dataclasses defined
    # in the module — see existing pattern in test_kb_scan_acceptance.py.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load("kb_scan", "kb-scan.py")
tui = _load("kb_process_tui", "kb-process-tui.py")
process = _load("kb_process", "kb-process.py")


# ---------- T1: _prefix_diff_body unit ----------


def test_prefix_diff_body_single_line():
    assert scan._prefix_diff_body("hello") == "+ hello"


def test_prefix_diff_body_multi_line():
    assert scan._prefix_diff_body("a\nb\nc") == "+ a\n+ b\n+ c"


def test_prefix_diff_body_blank_lines_become_bare_plus():
    """F2: blank lines must be `+`, not `+ ` (trailing space) and not `+ +`."""
    assert scan._prefix_diff_body("a\n\nb") == "+ a\n+\n+ b"


def test_prefix_diff_body_empty_input_returns_empty():
    assert scan._prefix_diff_body("") == ""


def test_prefix_diff_body_none_input_returns_empty():
    """The synthesis dict can carry `null` if the LLM omits a field; the
    helper must handle None without crashing (pr-challenger M1 on #236)."""
    assert scan._prefix_diff_body(None) == ""


def test_prefix_diff_body_commonmark_plus_bullets_get_double_prefixed():
    """**Load-bearing** (pr-challenger F3-on-#236): if an LLM emits a body
    using CommonMark `+`-bulleted items, the helper MUST treat them as raw
    content and prefix again — anything else silently destroys the bullets
    on extractor round-trip.

    Before the F3-fix on #236, the helper detected "all lines start with
    `+ `" as "already prefixed" and passed through. That meant the rendered
    diff carried raw `+ point A` lines; both extractors strip the leading
    `+ ` and the kb file lost the bullet markers. Always-prefix avoids the
    footgun — at the cost of a one-time round-trip mismatch for any caller
    that ACTUALLY pre-prefixed (currently zero callers do this)."""
    bullets = "+ point A\n+ point B"
    assert scan._prefix_diff_body(bullets) == "+ + point A\n+ + point B"


# ---------- T2: round-trip through extract_diff_block_content ----------


def test_render_decision_diff_round_trips_multi_line_body():
    """The pre-fix Mode-B scenario: multi-line body produces a diff that
    extract_diff_block_content can read end-to-end."""
    dec = {"title": "Decision X", "body": "line one\nline two wraps here\nline three"}
    # Minimal MemoryObject stand-in — render_decision_diff only reads memory_id + source_kind
    class _Mo:
        memory_id = "mem-test"
        source_kind = "test"
    diff_text = scan.render_decision_diff(dec, _Mo())
    extracted = tui.extract_diff_block_content(diff_text)
    assert extracted is not None, (
        "extract_diff_block_content returned None — Mode-B regression on multi-line body"
    )
    # Body content must appear verbatim in the extracted result.
    assert "line one" in extracted
    assert "line two wraps here" in extracted
    assert "line three" in extracted


def test_render_person_org_diff_round_trips_multi_line_summary():
    syn = {
        "title": "Person Y",
        "role_or_relation": "Engineer",
        "summary": "first paragraph line\nsecond line\n\nsecond paragraph",
    }
    diff_text = scan.render_person_org_diff(syn)
    extracted = tui.extract_diff_block_content(diff_text)
    assert extracted is not None, (
        "extract_diff_block_content returned None on multi-line summary — Mode-B regression"
    )
    assert "first paragraph line" in extracted
    assert "second line" in extracted
    assert "second paragraph" in extracted


def test_render_decision_diff_preserves_blank_lines():
    """F4: a body with intentional blank lines must round-trip with the
    blank-line shape preserved (each blank line emerges as a bare blank in
    the extracted content, not collapsed and not a `+` artifact)."""
    dec = {"title": "Decision Z", "body": "paragraph one\n\nparagraph two"}
    class _Mo:
        memory_id = "mem-test"
        source_kind = "test"
    diff_text = scan.render_decision_diff(dec, _Mo())
    extracted = tui.extract_diff_block_content(diff_text)
    assert extracted is not None
    # Find the body section (after the header bullets) and verify the blank line.
    lines = extracted.split("\n")
    # Locate "paragraph one"; the next non-trailing line should be blank, then "paragraph two".
    idx = lines.index("paragraph one")
    assert lines[idx + 1] == "", (
        f"blank line collapsed; got `{lines[idx + 1]!r}` after `paragraph one`"
    )
    assert "paragraph two" in lines[idx + 2:]


# ---------- T3: pre-fix shape was actually broken (regression guard) ----------


def test_pre_fix_shape_would_have_tripped_mode_b():
    """Sanity check: the unfixed shape — `+ {body}` with a multi-line body —
    DOES trip extract_diff_block_content. This guards against the helper being
    silently bypassed (e.g., by a future refactor that re-inlines the format string)."""
    naive_unfixed = (
        "```diff\n"
        "+ ## Title\n"
        "+ - **Source:** test\n"
        "+ \n"
        "+ line one\n"
        "line two no prefix\n"  # <-- this is the bug
        "```"
    )
    assert tui.extract_diff_block_content(naive_unfixed) is None, (
        "naive multi-line body should trip Mode B; if this assert fails, the "
        "extractor's shape contract has changed and #233's premise is stale"
    )


# ---------- T4: apply-path round-trip (pr-challenger #236 critical finding) ----------


def test_render_decision_diff_round_trips_through_extract_proposed_diff():
    """The amend path uses `kb-process-tui.py:extract_diff_block_content`; the
    apply path uses `kb-process.py:extract_proposed_diff` — a SEPARATE, more
    strict extractor (raises ValueError instead of returning None). If the two
    extractors drift, a body the TUI accepts but apply rejects (or vice versa)
    lands as a silent class of bugs. Verify the renderer's output round-trips
    through BOTH extractors on a multi-line body — that's the only contract
    that holds the full pipeline together. pr-challenger N1 on #236."""
    dec = {"title": "Decision X", "body": "line one\nline two wraps here\nline three"}
    class _Mo:
        memory_id = "mem-test"
        source_kind = "test"
    diff_text = scan.render_decision_diff(dec, _Mo())
    # Apply-side extractor — raises ValueError on shape violation; returning
    # at all means the diff parsed cleanly.
    applied_text = process.extract_proposed_diff(diff_text)
    assert "line one" in applied_text
    assert "line two wraps here" in applied_text
    assert "line three" in applied_text


def test_render_person_org_diff_round_trips_through_extract_proposed_diff():
    syn = {
        "title": "Person Y",
        "role_or_relation": "Engineer",
        "summary": "first paragraph line\nsecond line\n\nsecond paragraph",
    }
    diff_text = scan.render_person_org_diff(syn)
    applied_text = process.extract_proposed_diff(diff_text)
    assert "first paragraph line" in applied_text
    assert "second line" in applied_text
    assert "second paragraph" in applied_text


def test_commonmark_plus_bullet_body_extracts_cleanly_on_both_extractors():
    """**Load-bearing**: an LLM emitting a body of `+`-bulleted CommonMark items
    must produce a rendered diff that BOTH the TUI extractor AND the apply
    extractor parse without dropping the bullets. The always-prefix design
    guarantees this: `+ point A` in the body becomes `+ + point A` in the diff;
    extractor strips `+ ` → user sees `+ point A` (the bullet) in the kb file.
    pr-challenger F3 on #236 — silently destroying bullets via incorrect
    idempotency-detection is the failure mode this test pins."""
    dec = {"title": "Decision with bullets", "body": "Key points:\n+ point A\n+ point B"}
    class _Mo:
        memory_id = "mem-test"
        source_kind = "test"
    diff_text = scan.render_decision_diff(dec, _Mo())
    # TUI side
    tui_extracted = tui.extract_diff_block_content(diff_text)
    assert tui_extracted is not None
    assert "+ point A" in tui_extracted
    assert "+ point B" in tui_extracted
    # Apply side
    apply_extracted = process.extract_proposed_diff(diff_text)
    assert "+ point A" in apply_extracted
    assert "+ point B" in apply_extracted


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
