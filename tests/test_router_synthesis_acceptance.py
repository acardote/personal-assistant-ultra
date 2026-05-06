#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/route.py synthesis (#40).

Validates the multi-agent router's NEW user-facing format (single
synthesized response) without advisor/critic/specialist sections exposed.
The internal critic still runs; only its output is no longer surfaced
to the user.

Tests:
  T1 — RouteResult has the new `synthesized_response` field.
  T2 — render_human_output uses synthesized_response when present.
  T3 — render_human_output falls back to advisor_response when synthesis skipped.
  T4 — render_human_output does NOT contain "## Adversarial critic" header (the
        load-bearing UX fix per #40).
  T5 — synthesize prompt file exists at tools/prompts/synthesize.md.
  T6 — run_synthesizer prompt includes the draft + critique blocks.
  T7 — run_synthesizer omits SPECIALIST block when no specialist response.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_route():
    sys.modules.pop("route_test", None)
    spec = importlib.util.spec_from_file_location("route_test", str(PROJ / "tools" / "route.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["route_test"] = m
    spec.loader.exec_module(m)
    return m


def make_result(**kw):
    """Build a RouteResult with sensible defaults."""
    route = load_route()
    return route.RouteResult(
        query=kw.get("query", "test question"),
        kb_tokens=kw.get("kb_tokens", 100),
        memory_tokens=kw.get("memory_tokens", 200),
        memory_files=kw.get("memory_files", ["m1.md"]),
        specialist=kw.get("specialist"),
        advisor_response=kw.get("advisor_response", ""),
        critic_response=kw.get("critic_response", ""),
        specialist_response=kw.get("specialist_response", ""),
        synthesized_response=kw.get("synthesized_response", ""),
    )


def test_route_result_has_synthesized_field():
    """T1: RouteResult dataclass has `synthesized_response`."""
    r = make_result()
    assert hasattr(r, "synthesized_response")
    assert r.synthesized_response == ""
    print("  T1 PASS — RouteResult has synthesized_response field.")


def test_render_uses_synthesized():
    """T2: render_human_output uses synthesized when present."""
    route = load_route()
    r = make_result(
        advisor_response="DRAFT TEXT FROM ADVISOR",
        critic_response="CRITIQUE TEXT",
        synthesized_response="UNIFIED SYNTHESIS RESPONSE",
    )
    out = route.render_human_output(r)
    assert "UNIFIED SYNTHESIS RESPONSE" in out
    # Advisor draft + critique should NOT appear in user-facing output
    assert "DRAFT TEXT FROM ADVISOR" not in out
    assert "CRITIQUE TEXT" not in out
    print("  T2 PASS — render shows synthesized; hides advisor draft + critique.")


def test_render_falls_back_to_advisor():
    """T3: when synthesis didn't run, render shows advisor draft."""
    route = load_route()
    r = make_result(
        advisor_response="ADVISOR DRAFT (no synthesis)",
        critic_response="",  # --no-critic case
        synthesized_response="",
    )
    out = route.render_human_output(r)
    assert "ADVISOR DRAFT (no synthesis)" in out
    print("  T3 PASS — render falls back to advisor when synthesis skipped.")


def test_no_critic_header_in_render():
    """T4: render does NOT include '## Adversarial critic' header (the round-1 UX fix)."""
    route = load_route()
    r = make_result(
        advisor_response="advisor",
        critic_response="critic findings",
        synthesized_response="synthesized response",
    )
    out = route.render_human_output(r)
    # The load-bearing UX requirement from #40
    assert "## Adversarial critic" not in out
    assert "Adversarial critic" not in out
    print("  T4 PASS — '## Adversarial critic' header is gone from user-facing render.")


def test_synthesize_prompt_exists():
    """T5: tools/prompts/synthesize.md exists and is non-empty."""
    p = PROJ / "tools" / "prompts" / "synthesize.md"
    assert p.exists(), f"synthesize.md not found at {p}"
    content = p.read_text(encoding="utf-8")
    assert len(content) > 200
    assert "synthesizer" in content.lower()
    assert "<DRAFT>" in content
    assert "<CRITIQUE>" in content
    print("  T5 PASS — synthesize.md exists with required structure.")


def test_run_synthesizer_prompt_includes_blocks():
    """T6: run_synthesizer constructs a prompt with both <DRAFT> and <CRITIQUE>."""
    route = load_route()
    # Stub call_claude to capture the prompt rather than actually invoke it.
    captured = {}

    def stub(prompt: str) -> str:
        captured["prompt"] = prompt
        return "synthesized"

    original = route.call_claude
    route.call_claude = stub
    try:
        route.run_synthesizer(
            query="What's up with X?",
            context="<KB>kb</KB>\n<MEMORY>m</MEMORY>\n<QUESTION>q</QUESTION>",
            draft="advisor draft text",
            critique="critique text",
            specialist_response="",
        )
    finally:
        route.call_claude = original

    p = captured["prompt"]
    assert "<DRAFT>" in p and "advisor draft text" in p
    assert "<CRITIQUE>" in p and "critique text" in p
    # Synthesize.md template mentions <SPECIALIST> in instructions, but the
    # data block (with content) shouldn't be appended when there's no specialist.
    # Count <SPECIALIST> occurrences: should be ≤1 (instruction reference only).
    assert p.count("<SPECIALIST>") <= 1, (
        f"unexpected SPECIALIST data block when none passed; count={p.count('<SPECIALIST>')}"
    )
    print("  T6 PASS — run_synthesizer builds prompt with DRAFT + CRITIQUE blocks.")


def test_run_synthesizer_includes_specialist_when_present():
    """T7: SPECIALIST block appears when specialist_response is set."""
    route = load_route()
    captured = {}

    def stub(prompt: str) -> str:
        captured["prompt"] = prompt
        return "synthesized"

    original = route.call_claude
    route.call_claude = stub
    try:
        route.run_synthesizer(
            query="incident",
            context="<KB>k</KB>\n<MEMORY>m</MEMORY>\n<QUESTION>q</QUESTION>",
            draft="draft",
            critique="critique",
            specialist_response="SPECIALIST INCIDENT INPUT",
        )
    finally:
        route.call_claude = original

    p = captured["prompt"]
    assert "<SPECIALIST>" in p and "SPECIALIST INCIDENT INPUT" in p
    print("  T7 PASS — SPECIALIST block included when specialist_response is set.")


if __name__ == "__main__":
    print("Running test_router_synthesis_acceptance.py...")
    test_route_result_has_synthesized_field()
    test_render_uses_synthesized()
    test_render_falls_back_to_advisor()
    test_no_critic_header_in_render()
    test_synthesize_prompt_exists()
    test_run_synthesizer_prompt_includes_blocks()
    test_run_synthesizer_includes_specialist_when_present()
    print("All router synthesis tests passed.")
