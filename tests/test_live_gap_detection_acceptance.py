#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/_live.py gap detection (#39-A).

Live-call augmentation itself comes in #39-B; this PR only adds the
decision module and wires its signal into route.py's metrics so we
can observe how often live would fire and why.

Tests:
  T1 — should_go_live triggers on zero memory hits (reason=zero_hit).
  T2 — should_go_live does NOT trigger when memory has hits and no pinned topic matches.
  T3 — should_go_live triggers on topic-pinned match even with hits (reason=topic_pinned).
  T4 — Topic match is case-insensitive substring.
  T5 — load_pinned_topics returns [] when config file is absent.
  T6 — load_pinned_topics ignores blank lines and # comments.
  T7 — Empty pinned-topics list does NOT cause a topic_pinned trigger.
  T8 — Zero-hit takes precedence over topic-pinned (reason=zero_hit, not topic_pinned).
  T9 — Injectable pinned_topics arg bypasses file I/O.
  T10 — LiveDecision is hashable / frozen.
"""

from __future__ import annotations

import importlib.util
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
    """T4: case-insensitive substring match."""
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


def test_zero_hit_wins_over_pin():
    """T8: when memory_hits=0 AND query matches a pin, reason=zero_hit (the broader signal)."""
    live = load_live()
    with tempfile.TemporaryDirectory() as td:
        root = make_root_with_pins(Path(td), ["Acko Projects Weekly Sync"])
        d = live.should_go_live("Acko Projects Weekly Sync update?", 0, content_root=root)
    assert d.should_go_live is True
    assert d.reason == "zero_hit", f"expected zero_hit, got {d.reason}"
    assert d.matched_topic is None
    print("  T8 PASS — zero_hit wins over topic_pinned.")


def test_injectable_pinned_topics():
    """T9: passing pinned_topics= explicitly bypasses file I/O."""
    live = load_live()
    # No vault dir at all — must not touch disk if topics provided.
    fake_root = Path("/nonexistent/vault")
    d = live.should_go_live(
        "status of Acko Projects Weekly Sync",
        2,
        content_root=fake_root,
        pinned_topics=["Acko Projects Weekly Sync"],
    )
    assert d.should_go_live is True
    assert d.reason == "topic_pinned"
    print("  T9 PASS — injectable pinned_topics bypasses file I/O.")


def test_live_decision_frozen():
    """T10: LiveDecision is a frozen dataclass (hashable, immutable)."""
    live = load_live()
    d = live.LiveDecision(should_go_live=False, reason=None, matched_topic=None)
    # Hashable
    _ = hash(d)
    # Immutable
    raised = False
    try:
        d.reason = "mutated"  # type: ignore[misc]
    except Exception:
        raised = True
    assert raised, "LiveDecision should be frozen (immutable)"
    print("  T10 PASS — LiveDecision is frozen.")


if __name__ == "__main__":
    print("Running test_live_gap_detection_acceptance.py...")
    test_zero_hit_triggers()
    test_hits_no_pin_no_trigger()
    test_topic_pinned_triggers_with_hits()
    test_topic_match_case_insensitive()
    test_load_pinned_topics_missing_file()
    test_load_pinned_topics_strips_comments_and_blanks()
    test_empty_pin_file_no_trigger()
    test_zero_hit_wins_over_pin()
    test_injectable_pinned_topics()
    test_live_decision_frozen()
    print("All live gap-detection tests passed.")
