#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Generate a system-health recommendations report from the latest metrics
snapshot (#41 PR-E).

Reads the most recent snapshot in `<content_root>/.metrics/snapshots/`,
applies a set of threshold-based rules, and writes a markdown report to
`<content_root>/.metrics/reviews/<utc-date>.md`. The report flags issues
the user should investigate and proposes concrete adjustments to the
system (harvest scope, KB content, routing logic, etc.).

This is a **local tool**, not a Claude Code routine. Events (and snapshots)
live under `.metrics/` which is gitignored — a routine workspace would clone
the vault without any history, defeating the point. Run this on the user's
Mac (e.g., as a weekly launchd job, or manually).

Usage:
    tools/metrics-self-review.py                    # write to default path
    tools/metrics-self-review.py --print            # also print to stdout
    tools/metrics-self-review.py --aggregate-first  # run aggregate before review
    tools/metrics-self-review.py --out /tmp/review.md

Recommendation rules (deterministic, threshold-based):

  - empty_handed_rate > 0.30 → coverage gap; suggest broadening harvest
  - gap_discovery_rate > 0.40 → many queries hit topics not in memory
  - memory_hit_rate < 0.50 → memory misses >50% of queries
  - query_abandonment_rate > 0.20 → 20%+ queries abandoned
  - harvest_success_rate < 0.95 → harvest failing more than rarely
  - token_budget_violations > weekly_threshold → compression bloating
  - mtime dominates age (>2x) → vault rehydration; ages not truthful
  - any source with 0 retrievals → scope mismatch; consider trimming

Each rule produces a recommendation with severity + suggested next action.
The user reviews the report and decides whether to act.

Exit codes:
  0 — review written
  1 — config error / no snapshots
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Avoid sys.path mutation; load _config via spec.
_CONFIG_PATH = Path(__file__).resolve().parent / "_config.py"
_spec = importlib.util.spec_from_file_location("_pa_review_config", str(_CONFIG_PATH))
assert _spec is not None and _spec.loader is not None
_config_module = importlib.util.module_from_spec(_spec)
sys.modules["_pa_review_config"] = _config_module
_spec.loader.exec_module(_config_module)
load_config = _config_module.load_config

METHOD_ROOT = Path(__file__).resolve().parent.parent
AGGREGATE_TOOL = METHOD_ROOT / "tools" / "metrics-aggregate.py"

# Producer/consumer schema contract. Aggregator bumps schema_version on field
# RENAMES or REMOVALS (additive changes don't bump). A mismatch means rules
# below may silently degrade to "key missing → no finding" — surface it loudly
# rather than producing a clean-looking but stale report.
EXPECTED_SCHEMA_VERSION = 1

# Thresholds. Hoisted to module level so calibration is one edit; tests
# reference these constants instead of hardcoded literals. Expect to re-tune
# after 2-4 weeks of real production data.
THRESHOLDS = {
    "empty_handed_rate": 0.30,           # high: many queries find no useful memory
    "gap_discovery_rate": 0.40,          # high: many queries surface untracked topics
    "memory_hit_rate": 0.50,             # medium: memory misses on >50% of queries (low side)
    "query_abandonment_rate": 0.20,      # medium: 20%+ of queries have no end event
    "p95_latency_ms": 60_000,            # low: slow tail >1 minute
    "harvest_success_rate": 0.95,        # high: harvest failing more than rarely
    "token_budget_violations": 10,       # medium: >10/window suggests bloat
    "mtime_to_created_at_ratio": 2.0,    # medium: vault rehydration likely
    "min_created_at_for_mtime_rule": 3,  # need ≥3 created_at-dated objects for the ratio to be meaningful — under that, fresh-clone vaults trip the rule every run forever
    "live_calls_per_query": 0.50,        # medium: half+ of queries need live (#39 not fully closed)
    "live_call_error_rate": 0.10,        # high (provisional pre-data): >10% of live calls erroring → MCP auth/latency
    "live_call_empty_rate": 0.40,        # medium (provisional pre-data): >40% empty → over-firing or wrong scope
    "min_live_calls_to_flag": 5,         # gate live_call rules on ≥5 calls — avoids 1-of-3 = 33% noise
    "min_mcp_errors_to_flag": 1,         # low: any MCP error worth surfacing
    "min_memory_for_orphan_check": 5,    # low: source needs ≥5 memory objects to flag
}


def latest_snapshot(snapshots_dir: Path) -> dict | None:
    """Return the most recent snapshot by generated_at, or None if no snapshots."""
    if not snapshots_dir.is_dir():
        return None
    best: dict | None = None
    best_ts = ""
    for p in snapshots_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        gen = data.get("generated_at", "")
        if isinstance(gen, str) and gen > best_ts:
            best_ts = gen
            best = data
            best["_source_path"] = str(p)
    return best


def safe_get(d: dict, *keys: str, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is default:
            return default
    return cur


def evaluate_rules(snapshot: dict) -> list[dict]:
    """Apply deterministic threshold rules to the snapshot. Returns a list of
    recommendations: [{severity, category, finding, suggested_action}, ...]."""
    recs: list[dict] = []

    # Coverage gaps
    eh = safe_get(snapshot, "coverage", "empty_handed_rate")
    if isinstance(eh, (int, float)) and eh > THRESHOLDS["empty_handed_rate"]:
        recs.append({
            "severity": "high",
            "category": "coverage",
            "finding": f"empty_handed_rate is {eh:.0%} — more than {THRESHOLDS['empty_handed_rate']:.0%} of queries find no useful memory.",
            "suggested_action": "Broaden harvest scope (add Slack channels to allow-list, lower Gmail label filter, add Granola meeting series). Consider live-call augmentation per #39.",
        })

    gap = safe_get(snapshot, "coverage", "gap_discovery_rate")
    if isinstance(gap, (int, float)) and gap > THRESHOLDS["gap_discovery_rate"]:
        recs.append({
            "severity": "high",
            "category": "coverage",
            "finding": f"gap_discovery_rate is {gap:.0%} — most queries surface topics not in memory.",
            "suggested_action": "Inspect events log for top topic_keywords with memory_hits=0; add corresponding sources to harvest scope or topic-pin them for live calls (#39).",
        })

    hit = safe_get(snapshot, "coverage", "memory_hit_rate")
    if isinstance(hit, (int, float)) and hit < THRESHOLDS["memory_hit_rate"]:
        recs.append({
            "severity": "medium",
            "category": "coverage",
            "finding": f"memory_hit_rate is {hit:.0%} — memory misses on more than half of queries.",
            "suggested_action": "Either coverage is too narrow (broaden harvest) or retrieval is broken (check tools/route.py:load_memory_objects keyword scoring).",
        })

    # Live calls per query: tracked by #39; high rate signals harvest can't keep up.
    lcq = safe_get(snapshot, "coverage", "live_calls_per_query")
    if isinstance(lcq, (int, float)) and lcq > THRESHOLDS["live_calls_per_query"]:
        recs.append({
            "severity": "medium",
            "category": "coverage",
            "finding": f"live_calls_per_query is {lcq:.2f} — over half of queries needed live MCP calls.",
            "suggested_action": "Live-call layer (#39) is a gap-filler, not a substitute for harvest. Inspect which topics consistently miss memory and add them to scheduled-harvest scope.",
        })

    # Live-call status breakdown (#39-B). Use rates relative to total live calls,
    # not total queries — a high error rate among 5 live calls is meaningful even
    # if live_calls_per_query is low. Gated on min_live_calls_to_flag so 1-of-3
    # noise doesn't trigger high-severity findings (per pr-challenger #58).
    by_status = safe_get(snapshot, "coverage", "live_by_status") or {}
    total_live = sum(by_status.values()) if isinstance(by_status, dict) else 0
    if total_live >= THRESHOLDS["min_live_calls_to_flag"]:
        errors = (by_status.get("error", 0) + by_status.get("timeout", 0))
        empties = by_status.get("empty", 0)
        err_rate = errors / total_live
        emp_rate = empties / total_live
        if err_rate > THRESHOLDS["live_call_error_rate"]:
            recs.append({
                "severity": "high",
                "category": "live_calls",
                "finding": f"live_call error+timeout rate is {err_rate:.0%} ({errors}/{total_live}) — MCP calls failing more than rarely.",
                "suggested_action": "Check MCP auth status (most common cause). Run `nap diagnose` if the connector is internal. Inspect the per-source breakdown (live_by_source) to see which connector is failing.",
            })
        if emp_rate > THRESHOLDS["live_call_empty_rate"]:
            recs.append({
                "severity": "medium",
                "category": "live_calls",
                "finding": f"live_call empty rate is {emp_rate:.0%} ({empties}/{total_live}) — live searches consistently return nothing.",
                "suggested_action": "Either the gap-detection trigger is over-firing (signal: high live_calls_per_query AND low success rate) or the search scope is wrong (e.g. label:important not populated). Inspect SKILL.md procedure scoping for the affected source.",
            })

    # User experience
    ab = safe_get(snapshot, "user_experience", "query_abandonment_rate")
    if isinstance(ab, (int, float)) and ab > THRESHOLDS["query_abandonment_rate"]:
        recs.append({
            "severity": "medium",
            "category": "user_experience",
            "finding": f"query_abandonment_rate is {ab:.0%} — more than 1 in 5 queries had no completion event.",
            "suggested_action": "Likely either (a) latency is making users give up, or (b) sessions are starting without a query_end emit (instrumentation gap). Check median latency p95 first.",
        })

    p95 = safe_get(snapshot, "user_experience", "time_to_response_ms_p95")
    if isinstance(p95, (int, float)) and p95 > THRESHOLDS["p95_latency_ms"]:
        recs.append({
            "severity": "low",
            "category": "user_experience",
            "finding": f"latency p95 is {p95 / 1000:.0f}s — most queries complete fast but the slow tail is >1 minute.",
            "suggested_action": "Profile route.py call breakdown via per-stage events (advisor_call, critic_call, specialist_call). Consider parallelizing critic + advisor.",
        })

    # System health
    hs = safe_get(snapshot, "system_health", "harvest_success_rate")
    if isinstance(hs, (int, float)) and hs < THRESHOLDS["harvest_success_rate"]:
        recs.append({
            "severity": "high",
            "category": "system_health",
            "finding": f"harvest_success_rate is {hs:.0%} — harvest is failing more than rarely.",
            "suggested_action": "Check the runs/ directory for recent ok=false entries. Common causes: Granola enumeration timeouts (per #34's weekly fallback), MCP auth lapse, hard-floor failures.",
        })

    tv = safe_get(snapshot, "system_health", "token_budget_violations")
    if isinstance(tv, (int, float)) and tv > THRESHOLDS["token_budget_violations"]:
        recs.append({
            "severity": "medium",
            "category": "system_health",
            "finding": f"token_budget_violations is {tv} in this window — compression is bloating.",
            "suggested_action": "Tighten the compress.py prompt or raise the soft budget; investigate which kinds are over-budget (see source_economy.by_kind.over_budget_rate).",
        })

    # MCP errors per source — any non-zero is signal worth surfacing.
    mcp = safe_get(snapshot, "system_health", "mcp_errors_by_source") or {}
    if isinstance(mcp, dict) and mcp:
        sources_with_errors = ", ".join(f"{src}={n}" for src, n in sorted(mcp.items()) if n >= THRESHOLDS["min_mcp_errors_to_flag"])
        if sources_with_errors:
            recs.append({
                "severity": "low",
                "category": "system_health",
                "finding": f"MCP errors observed: {sources_with_errors}.",
                "suggested_action": "Inspect harvest run-status JSONs for the failing source(s). If recurring, check connector authentication state in claude.ai.",
            })

    # Freshness check non-PASS states surface here too — a STALE/FAILED/STUCK
    # check at skill startup is already user-visible (per #27 banner) and
    # routine watchdog DM (per #32). Aggregating them here helps identify
    # whether the issue is acute (one-off) or chronic (every fire).
    fc_states = safe_get(snapshot, "system_health", "freshness_check_states") or {}
    if isinstance(fc_states, dict):
        non_pass = {k: v for k, v in fc_states.items() if k != "PASS" and v > 0}
        if non_pass:
            states_str = ", ".join(f"{k}={v}" for k, v in sorted(non_pass.items()))
            recs.append({
                "severity": "medium",
                "category": "system_health",
                "finding": f"Freshness check fired non-PASS states this window: {states_str}.",
                "suggested_action": "Cross-reference the watchdog Slack DM history (#32). If STALE or STUCK appears repeatedly, the routine isn't firing reliably; investigate the routine config.",
            })

    # Memory quality
    ad = safe_get(snapshot, "memory_quality", "memory_age_source_distribution") or {}
    ca = ad.get("created_at", 0) or 0
    mt = ad.get("mtime", 0) or 0
    # Skip when ca is below the floor: the ratio is undefined / dominated by
    # the +1 in max(ca, 1) and fires every run on fresh-clone vaults until
    # backfill, which is exactly the F4 staleness pattern the parent (#41)
    # explicitly tries to avoid.
    if ca >= THRESHOLDS["min_created_at_for_mtime_rule"] and mt > THRESHOLDS["mtime_to_created_at_ratio"] * ca:
        recs.append({
            "severity": "medium",
            "category": "memory_quality",
            "finding": f"mtime dominates age distribution ({mt}:{ca} = {mt / max(ca, 1):.1f}x). Memory ages are not truthful.",
            "suggested_action": "Likely a recent vault rehydration (git clone, restore from backup). Either accept that age metrics are now since-checkout, or backfill `created_at` frontmatter from harvest run-status timestamps.",
        })

    # Source economy: any source with non-trivial memory but zero retrievals?
    se = safe_get(snapshot, "source_economy", "by_source_kind") or {}
    by_count = safe_get(snapshot, "memory_quality", "by_source_count") or {}
    for src, count in by_count.items():
        if count >= THRESHOLDS["min_memory_for_orphan_check"] and src not in se:
            recs.append({
                "severity": "low",
                "category": "source_economy",
                "finding": f"Source `{src}` has {count} memory objects but no compress activity in this window.",
                "suggested_action": "Either the source has stopped harvesting (check #34 floors) or queries aren't pulling from it. Check retrieval keyword scoring in tools/route.py.",
            })

    return recs


def render_review(snapshot: dict, recs: list[dict]) -> str:
    """Render the markdown review report."""
    gen_at = snapshot.get("generated_at", "?")
    win = f"{snapshot.get('window_start', '?')} → {snapshot.get('window_end', '?')}"
    events = snapshot.get("events_total", 0)

    sections: list[str] = []
    sections.append(f"# Self-review: {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}")
    sections.append("")
    sections.append(f"Snapshot generated: **{gen_at}** · window **{win}** · {events} events.")
    sections.append("")

    if not recs:
        sections.append("## ✅ No issues detected against current thresholds")
        sections.append("")
        sections.append("All metrics are within acceptable ranges. System is operating as expected.")
        return "\n".join(sections)

    # Group by severity
    by_sev: dict[str, list[dict]] = {"high": [], "medium": [], "low": []}
    for r in recs:
        by_sev.setdefault(r.get("severity", "low"), []).append(r)

    sections.append(f"## Summary")
    sections.append("")
    sections.append(f"- **High severity**: {len(by_sev.get('high', []))} finding(s)")
    sections.append(f"- **Medium severity**: {len(by_sev.get('medium', []))} finding(s)")
    sections.append(f"- **Low severity**: {len(by_sev.get('low', []))} finding(s)")
    sections.append("")

    for sev in ("high", "medium", "low"):
        items = by_sev.get(sev, [])
        if not items:
            continue
        emoji = {"high": "🔴", "medium": "🟡", "low": "🔵"}[sev]
        sections.append(f"## {emoji} {sev.title()} severity")
        sections.append("")
        for r in items:
            sections.append(f"### [{r['category']}] {r['finding']}")
            sections.append("")
            sections.append(f"**Suggested action**: {r['suggested_action']}")
            sections.append("")

    # Raw snapshot reference
    sections.append("---")
    sections.append("")
    sections.append(f"*Snapshot source: `{snapshot.get('_source_path', '?')}`*")
    sections.append("")

    return "\n".join(sections)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate self-review report from latest metrics snapshot.")
    parser.add_argument("--print", dest="print_too", action="store_true",
                        help="Also print the review to stdout (default: write only).")
    parser.add_argument("--aggregate-first", action="store_true",
                        help="Run tools/metrics-aggregate.py before reading the latest snapshot.")
    parser.add_argument("--out", help="Output path (default: <content_root>/.metrics/reviews/<utc-date>.md).")
    args = parser.parse_args(argv[1:])

    try:
        cfg = load_config(require_explicit_content_root=False)
    except RuntimeError as exc:
        print(f"[metrics-self-review] config error: {exc}", file=sys.stderr)
        return 1

    metrics_dir = cfg.harvest_state_root.parent / ".metrics"
    snapshots_dir = metrics_dir / "snapshots"

    if args.aggregate_first:
        print("[metrics-self-review] running aggregator first...", file=sys.stderr)
        result = subprocess.run([str(AGGREGATE_TOOL)], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[metrics-self-review] aggregate failed: {result.stderr.strip()}", file=sys.stderr)
            return 1

    snap = latest_snapshot(snapshots_dir)
    if snap is None:
        print("[metrics-self-review] no snapshot found. Run tools/metrics-aggregate.py first or use --aggregate-first.", file=sys.stderr)
        return 1

    snap_version = snap.get("schema_version")
    if snap_version != EXPECTED_SCHEMA_VERSION:
        print(
            f"[metrics-self-review] WARNING: snapshot schema_version={snap_version!r} "
            f"but this tool expects {EXPECTED_SCHEMA_VERSION}. Rules may silently misfire "
            f"on renamed/removed keys. Update this tool or re-run metrics-aggregate.py.",
            file=sys.stderr,
        )

    recs = evaluate_rules(snap)
    review = render_review(snap, recs)

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        reviews_dir = metrics_dir / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        out_path = reviews_dir / f"{today}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(review, encoding="utf-8")
    print(f"[metrics-self-review] {len(recs)} finding(s) → {out_path}", file=sys.stderr)

    if args.print_too:
        print(review)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
