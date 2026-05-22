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


def test_prefix_diff_body_idempotent_on_already_prefixed():
    """F3: already-prefixed body must NOT be double-prefixed.
    Defends against renderer composition (passing pre-rendered output back in)."""
    already = "+ a\n+ b"
    assert scan._prefix_diff_body(already) == already


def test_prefix_diff_body_idempotent_with_bare_plus_blank():
    already = "+ a\n+\n+ b"
    assert scan._prefix_diff_body(already) == already


def test_prefix_diff_body_mixed_input_treated_as_raw():
    """If only SOME lines are prefixed, treat as raw (the idempotency check
    requires ALL non-empty lines to be prefixed). This makes the helper
    conservative — the caller has a slightly mixed input → prefix everything."""
    mixed = "+ a\nb"
    assert scan._prefix_diff_body(mixed) == "+ + a\n+ b"


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
