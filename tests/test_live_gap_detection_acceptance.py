#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/_live.py gap detection (#39-A).

Live-call augmentation itself comes in #39-B; this PR only adds the
decision module and wires its signal into route.py's metrics so we
can observe how often live would fire and why.

Tests (revised after PR #49 adversarial review):
  T1  — should_go_live triggers on zero memory hits (reason=zero_hit).
  T2  — should_go_live does NOT trigger when memory has hits and no pinned topic matches.
  T3  — should_go_live triggers on topic-pinned match even with hits (reason=topic_pinned).
  T4  — Topic match is case-insensitive.
  T5  — load_pinned_topics returns [] when config file is absent.
  T6  — load_pinned_topics ignores blank lines and # comments.
  T7  — Empty pinned-topics list does NOT cause a topic_pinned trigger.
  T8  — Zero-hit takes precedence (reason=zero_hit) but matched_topic is PRESERVED when a pin also matches.
  T9  — Injectable _pinned_topics_override bypasses file I/O.
  T10 — LiveDecision is hashable / frozen.
  T11 — Word-boundary match — pinned 'sync' does NOT match 'asynchronous'.
  T12 — matched_topic is bounded to MATCHED_TOPIC_MAX_CHARS in the LiveDecision.
  T13 — Integration: route.py emits gap_detected event with expected shape.
  T14 — Integration: gap event payload carries no_critic / no_specialist flags.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_live():
    sys.modules.pop("live_test", None)
    spec = importlib.util.spec_from_file_location("live_test", str(PROJ / "tools" / "_live.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["live_test"] = m
    spec.loader.exec_module(m)
    return m


def make_root_with_pins(td: Path, lines: list[str] | None) -> Path:
    """Create a synthetic content_root. If `lines is None`, no pin file is written."""
    root = td / "vault"
    (root / ".harvest").mkdir(parents=True)
    if lines is not None:
        (root / ".harvest" / "live-pinned.txt").write_text("\n".join(lines), encoding="utf-8")
    return root


def test_zero_hit_triggers():
    """T1: 0 memory hits → live fires with zero_hit reason."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), None)
        d = live.should_go_live("anything", 0, content_root=root)
    assert d.should_go_live is True
    assert d.reason == "zero_hit"
    assert d.matched_topic is None
    print("  T1 PASS — zero-hit triggers live with reason=zero_hit.")


def test_hits_no_pin_no_trigger():
    """T2: hits>0 + no pin file → no live trigger."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), None)
        d = live.should_go_live("what's up with q3 planning", 5, content_root=root)
    assert d.should_go_live is False
    assert d.reason is None
    assert d.matched_topic is None
    print("  T2 PASS — hits + no pin file means no live trigger.")


def test_topic_pinned_triggers_with_hits():
    """T3: pinned topic in query → live fires even when memory has hits."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["Acko Projects Weekly Sync"])
        d = live.should_go_live("status of Acko Projects Weekly Sync", 8, content_root=root)
    assert d.should_go_live is True
    assert d.reason == "topic_pinned"
    assert d.matched_topic == "Acko Projects Weekly Sync"
    print("  T3 PASS — topic_pinned triggers with hits>0.")


def test_topic_match_case_insensitive():
    """T4: case-insensitive match."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["BADAS Weekly"])
        d = live.should_go_live("what was discussed in badas weekly?", 3, content_root=root)
    assert d.should_go_live is True
    assert d.reason == "topic_pinned"
    assert d.matched_topic == "BADAS Weekly"
    print("  T4 PASS — topic match is case-insensitive.")


def test_load_pinned_topics_missing_file():
    """T5: missing live-pinned.txt → empty list (safe default)."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), None)
        topics = live.load_pinned_topics(root)
    assert topics == []
    print("  T5 PASS — missing pin file returns [].")


def test_load_pinned_topics_strips_comments_and_blanks():
    """T6: blank lines + '#' comments are skipped, leading/trailing whitespace trimmed."""
    live = load_live()
    body = [
        "# topics that need live reads",
        "",
        "  Acko Projects Weekly Sync  ",
        "",
        "# another comment",
        "BADAS Weekly",
        "",
    ]
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), body)
        topics = live.load_pinned_topics(root)
    assert topics == ["Acko Projects Weekly Sync", "BADAS Weekly"]
    print("  T6 PASS — comments + blank lines stripped, whitespace trimmed.")


def test_empty_pin_file_no_trigger():
    """T7: pin file present but only comments → no topic_pinned trigger."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["# nothing pinned yet"])
        d = live.should_go_live("status of weekly sync", 4, content_root=root)
    assert d.should_go_live is False
    print("  T7 PASS — empty (comments-only) pin file does not trigger.")


def test_zero_hit_preserves_matched_topic():
    """T8 (revised after challenger F5): when memory_hits=0 AND a pin also matches,
    reason=zero_hit (broader signal) BUT matched_topic is preserved so #39-B can
    route to the pinned source preferentially. Throwing it away was a design loss."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["Acko Projects Weekly Sync"])
        d = live.should_go_live("Acko Projects Weekly Sync update?", 0, content_root=root)
    assert d.should_go_live is True
    assert d.reason == "zero_hit", f"expected zero_hit, got {d.reason}"
    assert d.matched_topic == "Acko Projects Weekly Sync", (
        f"matched_topic should be preserved on zero_hit (got {d.matched_topic!r})"
    )
    print("  T8 PASS — zero_hit wins on reason but matched_topic preserved.")


def test_injectable_override_arg():
    """T9: passing _pinned_topics_override= explicitly bypasses file I/O. The
    leading-underscore name signals 'test seam' (per challenger F3)."""
    live = load_live()
    fake_root = Path("/nonexistent/vault")
    d = live.should_go_live(
        "status of Acko Projects Weekly Sync",
        2,
        content_root=fake_root,
        _pinned_topics_override=["Acko Projects Weekly Sync"],
    )
    assert d.should_go_live is True
    assert d.reason == "topic_pinned"
    print("  T9 PASS — _pinned_topics_override bypasses file I/O.")


def test_live_decision_frozen():
    """T10: LiveDecision is a frozen dataclass (hashable, immutable)."""
    live = load_live()
    d = live.LiveDecision(should_go_live=False, reason=None, matched_topic=None)
    _ = hash(d)
    raised = False
    try:
        d.reason = "mutated"  # type: ignore[misc]
    except Exception:
        raised = True
    assert raised, "LiveDecision should be frozen (immutable)"
    print("  T10 PASS — LiveDecision is frozen.")


def test_word_boundary_match():
    """T11 (challenger F2): word-boundary regex match — pinned 'sync' must NOT
    match 'asynchronous' or 'syncretism'. Substring-contains was a false-positive
    minefield for short pin entries inside larger words."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["sync"])
        # Pinned word inside another word: must NOT match
        d1 = live.should_go_live("how do I write an asynchronous handler", 3, content_root=root)
        d2 = live.should_go_live("the syncretism of two religions", 2, content_root=root)
        # Standalone word: SHOULD match
        d3 = live.should_go_live("what time is the sync today?", 4, content_root=root)
    assert d1.should_go_live is False, "asynchronous must not match pinned 'sync'"
    assert d2.should_go_live is False, "syncretism must not match pinned 'sync'"
    assert d3.should_go_live is True and d3.reason == "topic_pinned"
    print("  T11 PASS — word-boundary match (no false-positive on substring).")


def test_matched_topic_bounded():
    """T12: matched_topic exposed in LiveDecision is bounded to
    MATCHED_TOPIC_MAX_CHARS so unbounded user pin entries don't bloat
    metrics events (per pr-reviewer + challenger F1)."""
    live = load_live()
    long_topic = "X" * 200
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), [long_topic])
        d = live.should_go_live(long_topic.lower(), 2, content_root=root)
    assert d.should_go_live is True
    assert d.matched_topic is not None
    assert len(d.matched_topic) == live.MATCHED_TOPIC_MAX_CHARS, (
        f"matched_topic should be bounded to {live.MATCHED_TOPIC_MAX_CHARS}, got {len(d.matched_topic)}"
    )
    print(f"  T12 PASS — matched_topic bounded to {live.MATCHED_TOPIC_MAX_CHARS} chars.")


def _load_route():
    """Load the route module fresh and invalidate the cached _METRICS_DIR
    (which caches first-call resolution of PA_METRICS_DIR — without this
    reset, two tests setting different env vars would share a dir)."""
    sys.path.insert(0, str(PROJ / "tools"))
    sys.modules.pop("route_test_int", None)
    spec = importlib.util.spec_from_file_location("route_test_int", str(PROJ / "tools" / "route.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["route_test_int"] = m
    spec.loader.exec_module(m)
    # Invalidate the cached metrics dir on the (already-imported) _metrics module.
    metrics_mod = sys.modules.get("_metrics")
    if metrics_mod is not None:
        metrics_mod._METRICS_DIR = None  # type: ignore[attr-defined]
    return m


def test_route_emits_gap_detected_event():
    """T13 (challenger F6): integration — route.py actually emits a gap_detected
    event with the expected keys when memory_hits=0. Stubs the LLM + retrieval
    so the test is fast and deterministic."""
    with tempfile.TemporaryDirectory() as td:
        # Point metrics at a temp dir so we can read events back deterministically.
        os.environ["PA_METRICS_DIR"] = td
        try:
            route = _load_route()

            # Stub heavy I/O paths.
            route.call_claude = lambda prompt: "stubbed-response"  # type: ignore[assignment]
            route.assemble_kb_text = lambda: ("KB text", 5)  # type: ignore[assignment]
            route.load_memory_objects = lambda q, *, max_items=12: ("", 0, [])  # type: ignore[assignment]

            # Force should_go_live's content_root to a clean temp (no pin file).
            content_root = Path(td) / "vault"
            (content_root / ".harvest").mkdir(parents=True)
            route._CFG = type(route._CFG)(  # rebuild Config with new content_root
                method_root=route._CFG.method_root,
                content_root=content_root,
                config_source=route._CFG.config_source,
                config_path=route._CFG.config_path,
            )

            r = route.route("any question with no memory", no_critic=True, no_specialist=True)
            assert r.advisor_response == "stubbed-response"

            # Read the events file and look for gap_detected.
            events_files = list(Path(td).glob("events-*.jsonl"))
            assert events_files, "no events file written"
            events = [json.loads(line) for line in events_files[0].read_text().splitlines() if line.strip()]
            gap_events = [e for e in events if e["event"] == "gap_detected"]
            assert len(gap_events) == 1, f"expected exactly 1 gap_detected event, got {len(gap_events)}"
            payload = gap_events[0]["data"]
            assert payload["reason"] == "zero_hit"
            assert payload["memory_hits"] == 0
            print("  T13 PASS — route.py emits gap_detected event with expected shape.")
        finally:
            os.environ.pop("PA_METRICS_DIR", None)


def test_route_event_carries_flags():
    """T14: integration — gap_detected event carries no_critic + no_specialist
    so #39-B can route accordingly even when caller degraded the pipeline."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["PA_METRICS_DIR"] = td
        try:
            route = _load_route()
            route.call_claude = lambda prompt: "stub"  # type: ignore[assignment]
            route.assemble_kb_text = lambda: ("KB", 3)  # type: ignore[assignment]
            route.load_memory_objects = lambda q, *, max_items=12: ("", 0, [])  # type: ignore[assignment]

            content_root = Path(td) / "vault"
            (content_root / ".harvest").mkdir(parents=True)
            route._CFG = type(route._CFG)(
                method_root=route._CFG.method_root,
                content_root=content_root,
                config_source=route._CFG.config_source,
                config_path=route._CFG.config_path,
            )

            route.route("anything", no_critic=True, no_specialist=False)

            events_files = list(Path(td).glob("events-*.jsonl"))
            events = [json.loads(line) for line in events_files[0].read_text().splitlines() if line.strip()]
            gap = [e for e in events if e["event"] == "gap_detected"][0]
            assert gap["data"]["no_critic"] is True
            assert gap["data"]["no_specialist"] is False
            print("  T14 PASS — gap_detected event carries no_critic / no_specialist flags.")
        finally:
            os.environ.pop("PA_METRICS_DIR", None)


if __name__ == "__main__":
    print("Running test_live_gap_detection_acceptance.py...")
    test_zero_hit_triggers()
    test_hits_no_pin_no_trigger()
    test_topic_pinned_triggers_with_hits()
    test_topic_match_case_insensitive()
    test_load_pinned_topics_missing_file()
    test_load_pinned_topics_strips_comments_and_blanks()
    test_empty_pin_file_no_trigger()
    test_zero_hit_preserves_matched_topic()
    test_injectable_override_arg()
    test_live_decision_frozen()
    test_word_boundary_match()
    test_matched_topic_bounded()
    test_route_emits_gap_detected_event()
    test_route_event_carries_flags()
    print("All live gap-detection tests passed.")
