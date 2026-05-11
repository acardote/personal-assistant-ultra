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
    # synthesize.md template references <SPECIALIST> in instruction text;
    # what we want to verify is that the DATA BLOCK (with closing tag) isn't
    # appended when no specialist response was passed. A data block has the
    # shape: "<SPECIALIST>\n...\n</SPECIALIST>" with actual content. Check
    # for a closing tag, which only appears when run_synthesizer appended it.
    assert "</SPECIALIST>" not in p, "SPECIALIST data block leaked when none was passed"
    print("  T6 PASS — run_synthesizer builds prompt with DRAFT + CRITIQUE blocks (no SPECIALIST data).")


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


def test_batch_mode_starts_fresh_session_per_case():
    """T8: --batch loop mints a fresh PA_SESSION_ID per case.

    Without this, route()'s inherit_or_start() reads the prior case's
    session id from os.environ and every batch case collapses into one
    session_id in .metrics/events-*.jsonl — breaking per-query aggregation
    in eval runs. The fix imports start_session and calls it before each
    route() invocation in the batch loop."""
    import json as _json
    import os as _os
    import tempfile

    route = load_route()

    # Source-level check: start_session is imported.
    src = (PROJ / "tools" / "route.py").read_text(encoding="utf-8")
    assert "start_session" in src, "route.py must import start_session for batch session fix"

    # Behavioural check: each case sees a different session id.
    captured: list[str] = []

    def fake_route(query, *, no_critic=False, no_specialist=False):
        captured.append(_os.environ.get("PA_SESSION_ID", ""))
        return route.RouteResult(
            query=query, kb_tokens=0, memory_tokens=0,
            memory_files=[], specialist=None,
        )

    original_route = route.route
    route.route = fake_route
    try:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            cases_path = td_p / "cases.json"
            report_path = td_p / "report.json"
            cases_path.write_text(_json.dumps([
                {"id": "case-1", "query": "q1"},
                {"id": "case-2", "query": "q2"},
                {"id": "case-3", "query": "q3"},
            ]))
            # Pre-seed env so we can prove start_session() overwrites it.
            _os.environ["PA_SESSION_ID"] = "deadbeef"
            rc = route.main([
                "route.py", "--batch", str(cases_path),
                "--report-out", str(report_path),
            ])
            assert rc == 0
            assert len(captured) == 3, f"expected 3 cases captured, got {len(captured)}"
            assert len(set(captured)) == 3, (
                f"all batch cases share session id — fix not in place. Captured: {captured}"
            )
            # None should equal the pre-seeded value either.
            assert "deadbeef" not in captured, (
                "first case inherited the pre-seeded env var; start_session() not called per case"
            )
    finally:
        route.route = original_route
        _os.environ.pop("PA_SESSION_ID", None)

    print("  T8 PASS — batch mode mints a fresh PA_SESSION_ID per case (no session bleed).")


if __name__ == "__main__":
    print("Running test_router_synthesis_acceptance.py...")
    test_route_result_has_synthesized_field()
    test_render_uses_synthesized()
    test_render_falls_back_to_advisor()
    test_no_critic_header_in_render()
    test_synthesize_prompt_exists()
    test_run_synthesizer_prompt_includes_blocks()
    test_run_synthesizer_includes_specialist_when_present()
    test_batch_mode_starts_fresh_session_per_case()
    print("All router synthesis tests passed.")
