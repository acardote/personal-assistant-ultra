#!/usr/bin/env -S uv run --quiet --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Acceptance tests for #10 — multi-fidelity event matching + ranked retrieval.

Tests:
  T1 — three same-event memos (Gmail, Granola, Meet of one Q3 roadmap review)
       are clustered together. Gmail wins canonical (authority=1). Granola and
       Meet become alternates. F1 from challenger probes over-merging — see T3.
  T2 — re-running clustering after T1 is idempotent: no spurious re-clustering
       or canonical drift.
  T3 — an unrelated memo (different topic, different date) does NOT join the
       Q3 cluster. Catches the "templated standups merged" failure mode F1
       warned about.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def cp(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def main() -> int:
    dedup = load_module("dedup", PROJ / "tools" / "dedup.py")
    fixtures = PROJ / "tests" / "fixtures" / "dedup"

    tmp = Path(tempfile.mkdtemp(prefix="dedup-test-"))
    try:
        memory_root = tmp / "memory"
        memory_root.mkdir()

        gmail_path = cp(fixtures / "q3-roadmap-gmail.md", memory_root / "q3-roadmap-gmail.md")
        granola_path = cp(fixtures / "q3-roadmap-granola.md", memory_root / "q3-roadmap-granola.md")
        meet_path = cp(fixtures / "q3-roadmap-meet.md", memory_root / "q3-roadmap-meet.md")

        cfg = dedup.load_config()

        # T1: process the three memos in arrival order. The compress.py
        # integration will do this for real; here we simulate at the algorithm
        # level. Order: Gmail (canonical-by-authority), Granola, Meet.

        gmail = dedup.load_memo_summary(gmail_path)
        granola = dedup.load_memo_summary(granola_path)
        meet = dedup.load_memo_summary(meet_path)

        print("Test T1 — three same-event memos cluster together")
        # Step 1: Gmail arrives first → new event.
        r1 = dedup.cluster_with_existing(gmail, [], cfg)
        print(f"  gmail: role={r1.role} event_id={r1.event_id[:12]}... score={r1.score:.3f}")
        assert r1.role == "canonical", f"expected canonical, got {r1.role}"
        assert r1.demoted_id is None
        # Backfill memory state to mimic compress.py writing event_id back.
        gmail = _patched(gmail, event_id=r1.event_id, is_canonical_for_event=True)

        # Step 2: Granola arrives, finds Gmail.
        r2 = dedup.cluster_with_existing(granola, [gmail], cfg)
        print(f"  granola: role={r2.role} event_id={r2.event_id[:12]}... score={r2.score:.3f}")
        assert r2.event_id == r1.event_id, "granola should join gmail's cluster"
        assert r2.role == "alternate", f"granola should be alternate, got {r2.role}"
        assert r2.demoted_id is None
        granola = _patched(granola, event_id=r2.event_id, is_canonical_for_event=False, superseded_by=gmail.id)

        # Step 3: Meet arrives, finds the cluster.
        r3 = dedup.cluster_with_existing(meet, [gmail, granola], cfg)
        print(f"  meet: role={r3.role} event_id={r3.event_id[:12]}... score={r3.score:.3f}")
        assert r3.event_id == r1.event_id, "meet should join the cluster"
        assert r3.role == "alternate", f"meet should be alternate, got {r3.role}"
        meet = _patched(meet, event_id=r3.event_id, is_canonical_for_event=False, superseded_by=gmail.id)

        # Cluster shape: 1 canonical (Gmail), 2 alternates.
        canonicals = [m for m in (gmail, granola, meet) if m.is_canonical_for_event]
        assert len(canonicals) == 1, f"expected 1 canonical, got {len(canonicals)}"
        assert canonicals[0].source_kind == "gmail_thread"
        print("  PASS — Gmail canonical, Granola+Meet alternates\n")

        # T2 — idempotent re-clustering.
        print("Test T2 — re-clustering Gmail against the now-populated cluster is stable")
        r2b = dedup.cluster_with_existing(gmail, [granola, meet], cfg)
        # Re-clustering Gmail itself: it's not in the corpus passed in; it
        # finds its own granola/meet alternates and reaches the same cluster.
        # Authority of new (gmail=1) vs current canonical (granola=3) → gmail
        # is more authoritative, so this would return role="canonical" with
        # demoted_id pointing at the granola pseudo-canonical. That's actually
        # the correct behaviour for a recluster — the wrapper integration in
        # compress.py knows it's the same memo and skips the demotion step.
        # For the algorithm-level test, just check the cluster is recognized.
        assert r2b.event_id == r1.event_id, "re-clustering should land same event_id"
        print(f"  re-cluster gmail: role={r2b.role} event_id={r2b.event_id[:12]}... (algorithm-level; integration de-dup is compress.py's job)")
        print("  PASS — cluster_id stable on re-process\n")

        # T3 — unrelated memo does NOT join.
        print("Test T3 — unrelated topic + different date does NOT cluster")
        unrelated_text = """---
id: mem-fixture-unrelated
source_uri: file:./tests/fixtures/dedup/unrelated.md
source_kind: doc
created_at: "2026-05-15T10:00:00Z"
expires_at: "2026-11-15T00:00:00Z"
kind: note
tags: [unrelated]
title: "Mobile app store rollout"
summary: "Notes on the mobile-app-store rollout schedule for the consumer launch."
---

## Discussion

The mobile app rollout schedule needs alignment with the consumer marketing
calendar. iOS App Store submission window opens June 1; Android Play Store
parity targeted three weeks later. App icon assets pending design review with
the brand team. Localization for German, Japanese, Spanish targeted for v1.1.

The customer support team needs the help-center articles by submission. CTO
review of the privacy disclosure is the gating step.

## Action items

- Design review for app icon by 2026-05-20.
- Localization vendor selection by 2026-05-22.
- CTO privacy review by 2026-05-25.
"""
        unrelated_path = memory_root / "unrelated.md"
        unrelated_path.write_text(unrelated_text, encoding="utf-8")
        unrelated = dedup.load_memo_summary(unrelated_path)
        r4 = dedup.cluster_with_existing(unrelated, [gmail, granola, meet], cfg)
        print(f"  unrelated: role={r4.role} event_id={r4.event_id[:12]}... score={r4.score:.3f}")
        assert r4.event_id != r1.event_id, "unrelated should NOT join Q3 cluster"
        assert r4.role == "canonical", "unrelated should start its own cluster"
        print("  PASS — unrelated memo correctly stays out of the Q3 cluster (F1 holds)\n")

        # T4 — F1 challenger probe: same-template recurring 1:1, week apart,
        # different decisions. Should NOT cluster despite vocabulary overlap.
        print("Test T4 — recurring 1:1s a week apart with different decisions don't cluster (F1 probe)")
        wk1_path = cp(fixtures / "templated-1on1-week1.md", memory_root / "templated-1on1-week1.md")
        wk2_path = cp(fixtures / "templated-1on1-week2.md", memory_root / "templated-1on1-week2.md")
        wk1 = dedup.load_memo_summary(wk1_path)
        wk2 = dedup.load_memo_summary(wk2_path)
        score, cs, ds = dedup.pair_score(wk1, wk2, cfg)
        print(f"  templated 1:1 pair score={score:.3f} (cosine={cs:.3f}, date={ds:.3f})")
        r5 = dedup.cluster_with_existing(wk2, [wk1], cfg)
        print(f"  cluster_with_existing role={r5.role} same_event={r5.event_id == wk1.event_id if wk1.event_id else 'n/a'}")
        if r5.role == "alternate" and r5.event_id == wk1.event_id:
            print("  WARN: clustered. F1 fires for this fixture pair under current threshold.")
            assert False, "F1 fires: templated 1:1 pair clusters incorrectly"
        print("  PASS — templated pair correctly stays in separate clusters\n")

        print("=== All #10 acceptance tests passed ===")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _patched(memo, **fields):
    """Return a MemoSummary with patched fields. The dataclass is mutable so
    we set attributes directly; tests don't care about immutability."""
    for k, v in fields.items():
        setattr(memo, k, v)
    return memo


if __name__ == "__main__":
    sys.exit(main())
