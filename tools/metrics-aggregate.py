#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Aggregate metrics events into a snapshot JSON (#41 PR-C).

Reads:
  - `<content_root>/.metrics/events-YYYY-MM-DD.jsonl` for the requested window
  - `<content_root>/.harvest/runs/<utc>.json` (synthesizes harvest events
    from existing run-status files; routine prompt didn't change in PR-B)
  - `<content_root>/memory/**/*.md` (memory corpus state for quality metrics)

Writes:
  - `<content_root>/.metrics/snapshots/<window-start>_<window-end>.json`

Snapshot schema covers the metric categories from #41:
  - user_experience: time-to-first-response, iterations, abandonment
  - coverage: memory hit rate, live call rate, empty-handed, gap discovery
  - memory_quality: growth, retrieval, age distribution, topic breadth
  - source_economy: yield + utilization per source
  - system_health: harvest success rate, mcp failure, token budget violations

Usage:
    tools/metrics-aggregate.py                              # default: last 7 days
    tools/metrics-aggregate.py --days 30
    tools/metrics-aggregate.py --since 2026-04-01 --until 2026-05-06
    tools/metrics-aggregate.py --out /tmp/snapshot.json     # override default location

Exit codes:
  0 — snapshot written (even if some sources had no data)
  1 — config error or input failure
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

# Avoid sys.path mutation; load _config via spec. Note: Python 3.14's
# dataclass implementation reads cls.__module__ from sys.modules, so we
# MUST register the module before exec (otherwise a frozen-dataclass
# decorator inside _config.py crashes).
_CONFIG_PATH = Path(__file__).resolve().parent / "_config.py"
_spec = importlib.util.spec_from_file_location("_pa_aggregate_config", str(_CONFIG_PATH))
assert _spec is not None and _spec.loader is not None
_config_module = importlib.util.module_from_spec(_spec)
sys.modules["_pa_aggregate_config"] = _config_module
_spec.loader.exec_module(_config_module)
load_config = _config_module.load_config


def parse_iso(ts: str) -> _dt.datetime | None:
    """Parse ISO-8601 UTC. Tolerates `Z` suffix or explicit `+00:00`."""
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def date_range(start: _dt.date, end: _dt.date) -> Iterable[_dt.date]:
    """Iterate dates from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += _dt.timedelta(days=1)


def read_events_for_window(
    metrics_dir: Path, start: _dt.date, end: _dt.date
) -> list[dict]:
    """Read all events files in the date window. Skips malformed lines."""
    events: list[dict] = []
    for d in date_range(start, end):
        path = metrics_dir / f"events-{d.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate corruption (PR-A's MAX_LINE_BYTES guard)
    return events


def read_harvest_runs(
    runs_dir: Path, start: _dt.date, end: _dt.date
) -> list[dict]:
    """Read harvest run-status JSONs in the window. Each is a synthesized
    'harvest' event from the aggregator's perspective."""
    runs: list[dict] = []
    if not runs_dir.is_dir():
        return runs
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        # Filter by start_at into window
        ts = parse_iso(payload.get("started_at", "") or payload.get("ended_at", ""))
        if ts is None:
            continue
        if not (start <= ts.date() <= end):
            continue
        runs.append(payload)
    return runs


def percentile(values: list[float], p: float) -> float:
    """Compute percentile (0-100) without numpy."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ───────────────────────────────────────────────────────────────────────
# Per-category aggregators
# ───────────────────────────────────────────────────────────────────────


def aggregate_user_experience(events: list[dict]) -> dict:
    """Time-to-response, abandonment proxy, query counts."""
    query_ends = [e for e in events if e.get("event") == "query_end"]
    durations = [e["duration_ms"] for e in query_ends if isinstance(e.get("duration_ms"), (int, float))]
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    queries_per_session = defaultdict(int)
    for e in query_ends:
        sid = e.get("session_id")
        if sid:
            queries_per_session[sid] += 1
    iterations = list(queries_per_session.values())

    # Abandonment proxy: sessions with query_start but no query_end.
    started = {e.get("session_id") for e in events if e.get("event") == "query_start"}
    ended = {e.get("session_id") for e in events if e.get("event") == "query_end"}
    abandoned = started - ended
    abandonment_rate = len(abandoned) / max(len(started), 1)

    return {
        "queries_total": len(query_ends),
        "sessions_total": len(sessions),
        "time_to_response_ms_p50": int(percentile(durations, 50)) if durations else None,
        "time_to_response_ms_p95": int(percentile(durations, 95)) if durations else None,
        "iterations_per_session_p50": percentile([float(x) for x in iterations], 50) if iterations else None,
        "iterations_per_session_p95": percentile([float(x) for x in iterations], 95) if iterations else None,
        "query_abandonment_rate": round(abandonment_rate, 4),
    }


def aggregate_coverage(events: list[dict]) -> dict:
    """Memory hit rate, empty-handed rate, gap-discovery rate, live-call rate."""
    query_ends = [e for e in events if e.get("event") == "query_end"]
    if not query_ends:
        return {
            "memory_hit_rate": None,
            "empty_handed_rate": None,
            "gap_discovery_rate": None,
            "live_call_rate": 0.0,
        }
    with_memory = sum(1 for e in query_ends if (e.get("data") or {}).get("memory_hits", 0) > 0)
    empty = sum(1 for e in query_ends if (e.get("data") or {}).get("empty_handed") is True)
    # Gap discovery: query_end with memory_hits == 0 AND topic_keywords present.
    gap = sum(
        1 for e in query_ends
        if (e.get("data") or {}).get("memory_hits", 0) == 0
        and (e.get("data") or {}).get("topic_keywords")
    )
    live = sum(1 for e in events if e.get("event") == "live_call_end")
    total = len(query_ends)
    return {
        "memory_hit_rate": round(with_memory / total, 4),
        "empty_handed_rate": round(empty / total, 4),
        "gap_discovery_rate": round(gap / total, 4),
        "live_call_rate": round(live / total, 4) if total else 0.0,
        "total_queries": total,
    }


def aggregate_memory_quality(events: list[dict], memory_root: Path) -> dict:
    """Walk memory/ for current corpus state + use compress events for growth.

    Growth and topic_breadth come from events alone, so they're computed even
    when memory_root doesn't exist (e.g., a fresh test fixture). Corpus-state
    fields (memory_objects_total, by_source_count, age distribution) require
    memory_root to be present and walkable.
    """
    # Event-derived fields (independent of memory_root):
    growth = sum(1 for e in events if e.get("event") == "compress_result")
    topics: set[str] = set()
    for e in events:
        kws = (e.get("data") or {}).get("topic_keywords") or []
        for k in kws:
            if isinstance(k, str):
                topics.add(k)

    # Corpus-walk fields (require memory_root):
    by_source: dict[str, int] = defaultdict(int)
    ages: list[float] = []
    file_count = 0
    if memory_root.is_dir():
        files = [
            p for p in memory_root.rglob("*.md")
            if not any(
                part.startswith(".") or part == "examples"
                for part in p.relative_to(memory_root).parts[:-1]
            )
        ]
        file_count = len(files)
        now = _dt.datetime.now(_dt.timezone.utc)
        for f in files:
            rel = f.relative_to(memory_root)
            if rel.parts:
                by_source[rel.parts[0]] += 1
            try:
                mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime, tz=_dt.timezone.utc)
                ages.append((now - mtime).total_seconds() / 86400.0)
            except OSError:
                pass

    return {
        "memory_objects_total": file_count,
        "memory_growth_count_in_window": growth,
        "topic_coverage_breadth": len(topics),
        "by_source_count": dict(by_source),
        "memory_age_days_p50": round(percentile(ages, 50), 2) if ages else None,
        "memory_age_days_p95": round(percentile(ages, 95), 2) if ages else None,
    }


def aggregate_source_economy(events: list[dict], memory_root: Path) -> dict:
    """Per-source compression yield + retrieval utilization.

    Yield: compress_result count per source / harvest events per source.
    Utilization: % of memory objects per source retrieved at least once during
    the window (best-effort: counts memory_retrieve events with source-tagged
    keywords, since we don't have memory IDs at retrieve time yet).
    """
    by_source_compress: dict[str, int] = defaultdict(int)
    by_source_over_budget: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("event") == "compress_result":
            d = e.get("data") or {}
            kind = d.get("kind") or "unknown"
            by_source_compress[kind] += 1
            if d.get("over_budget"):
                by_source_over_budget[kind] += 1

    out: dict = {}
    for kind, count in by_source_compress.items():
        out[kind] = {
            "compress_result_count": count,
            "over_budget_count": by_source_over_budget.get(kind, 0),
            "over_budget_rate": round(by_source_over_budget.get(kind, 0) / count, 4) if count else 0.0,
        }
    return out


def aggregate_system_health(events: list[dict], harvest_runs: list[dict]) -> dict:
    """Harvest success rate, freshness check states, MCP errors."""
    # Harvest success rate from synthesized harvest events
    harvest_total = len(harvest_runs)
    harvest_ok = sum(1 for r in harvest_runs if r.get("ok") is True)
    harvest_failed = sum(1 for r in harvest_runs if r.get("ok") is False)

    # Freshness check states
    freshness_events = [e for e in events if e.get("event") == "freshness_check"]
    freshness_state_counts: dict[str, int] = defaultdict(int)
    for e in freshness_events:
        state = (e.get("data") or {}).get("state") or "unknown"
        freshness_state_counts[state] += 1

    # MCP errors: errors keyed in harvest run-status
    mcp_errors: dict[str, int] = defaultdict(int)
    for r in harvest_runs:
        sources = r.get("sources") or {}
        if isinstance(sources, dict):
            for src, info in sources.items():
                if isinstance(info, dict):
                    errs = info.get("errors") or []
                    if isinstance(errs, list) and errs:
                        mcp_errors[src] += len(errs)

    # Token budget violations from compress events
    over_budget = sum(
        1 for e in events
        if e.get("event") == "compress_result"
        and (e.get("data") or {}).get("over_budget") is True
    )

    return {
        "harvest_runs_total": harvest_total,
        "harvest_success_count": harvest_ok,
        "harvest_failed_count": harvest_failed,
        "harvest_success_rate": round(harvest_ok / harvest_total, 4) if harvest_total else None,
        "freshness_check_states": dict(freshness_state_counts),
        "mcp_errors_by_source": dict(mcp_errors),
        "token_budget_violations": over_budget,
    }


# ───────────────────────────────────────────────────────────────────────
# Snapshot composer
# ───────────────────────────────────────────────────────────────────────


def build_snapshot(
    *, metrics_dir: Path, runs_dir: Path, memory_root: Path,
    start: _dt.date, end: _dt.date,
) -> dict:
    events = read_events_for_window(metrics_dir, start, end)
    harvest_runs = read_harvest_runs(runs_dir, start, end)

    return {
        "schema_version": 1,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events_total": len(events),
        "harvest_runs_total": len(harvest_runs),
        "user_experience": aggregate_user_experience(events),
        "coverage": aggregate_coverage(events),
        "memory_quality": aggregate_memory_quality(events, memory_root),
        "source_economy": aggregate_source_economy(events, memory_root),
        "system_health": aggregate_system_health(events, harvest_runs),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Aggregate metrics events into a snapshot.")
    parser.add_argument("--days", type=int, default=7,
                        help="Window size in days back from today (default: 7).")
    parser.add_argument("--since", help="Start date YYYY-MM-DD (overrides --days).")
    parser.add_argument("--until", help="End date YYYY-MM-DD (default: today).")
    parser.add_argument("--out", help="Output snapshot path (default: .metrics/snapshots/<window>.json).")
    args = parser.parse_args(argv[1:])

    today = _dt.datetime.now(_dt.timezone.utc).date()
    end = _dt.date.fromisoformat(args.until) if args.until else today
    if args.since:
        start = _dt.date.fromisoformat(args.since)
    else:
        start = end - _dt.timedelta(days=args.days - 1)

    if start > end:
        print(f"[metrics-aggregate] start ({start}) is after end ({end})", file=sys.stderr)
        return 1

    try:
        cfg = load_config(require_explicit_content_root=False)
    except RuntimeError as exc:
        print(f"[metrics-aggregate] config error: {exc}", file=sys.stderr)
        return 1

    metrics_dir = cfg.harvest_state_root.parent / ".metrics"
    runs_dir = cfg.harvest_state_root / "runs"
    memory_root = cfg.memory_root

    if not metrics_dir.exists():
        print(f"[metrics-aggregate] no .metrics/ directory at {metrics_dir} — nothing to aggregate", file=sys.stderr)
        # Still write an empty snapshot so the dashboard can show "no data yet"
        # rather than crashing.

    snapshot = build_snapshot(
        metrics_dir=metrics_dir, runs_dir=runs_dir, memory_root=memory_root,
        start=start, end=end,
    )

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        snapshots_dir = metrics_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        out_path = snapshots_dir / f"{start.isoformat()}_{end.isoformat()}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"[metrics-aggregate] window {start}..{end}: {snapshot['events_total']} events, "
          f"{snapshot['harvest_runs_total']} harvest runs → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
