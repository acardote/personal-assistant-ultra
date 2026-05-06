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
    """Read all events files in the date window. Skips malformed lines.

    Filtering is by **filename only** (events-YYYY-MM-DD.jsonl). The library
    invariant in `_metrics.py:_today_path` is that an event's file date
    matches its UTC `ts` date — both are derived from the same `datetime.now`
    call microseconds apart at emit. Replayed/imported events with synthetic
    `ts` are NOT re-filtered against `ts`; they're trusted to be in the
    correct day-file. PR-D should NOT double-filter on `ts`.

    TODO (perf): for >1M events the read_text().splitlines() pattern doubles
    peak memory. Switch to streaming line iteration if that becomes a problem.
    """
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


def build_compress_source_index(events: list[dict]) -> dict[tuple, str]:
    """Index `compress_end` events by (session_id, ts) so `compress_result`
    events can be joined back to their source_kind.

    Per PR-B's design: compress_end carries source_kind + raw_chars +
    output_chars (timing-side data); compress_result carries kind +
    body_tokens + over_budget + cluster_role (post-validation outcome).
    Aggregator joins them by session_id + ts proximity (within ~5 seconds
    in normal operation).

    Returns {(session_id, approx_ts_minute): source_kind}. Approximate
    minute-bucketing is good enough — compress is per-item and far apart.
    """
    index: dict[tuple, str] = {}
    for e in events:
        if e.get("event") != "compress_end":
            continue
        sid = e.get("session_id")
        ts = e.get("ts")
        sk = (e.get("data") or {}).get("source_kind")
        if not (sid and ts and sk):
            continue
        # Bucket on first 16 chars (YYYY-MM-DDTHH:MM) so a same-session
        # compress_result emitted seconds after end joins to same source_kind.
        ts_bucket = ts[:16]
        index[(sid, ts_bucket)] = sk
    return index


def lookup_source_kind(idx: dict[tuple, str], session_id: str, ts: str) -> str:
    """Return source_kind for a compress_result by joining to compress_end.
    Falls back to "unknown" if no matching compress_end was indexed."""
    if not (session_id and ts):
        return "unknown"
    ts_bucket = ts[:16]
    # Try exact bucket; if miss, try previous minute (compress_result might
    # land 5-15s after compress_end at minute boundaries).
    for offset_min in (0, -1):
        b = ts_bucket
        if offset_min < 0:
            try:
                t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                t = t - _dt.timedelta(minutes=1)
                b = t.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                continue
        sk = idx.get((session_id, b))
        if sk:
            return sk
    return "unknown"


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
    """Time-to-response, abandonment proxy, query counts.

    Naming: `queries_per_session_*` is intentionally NOT called
    "iterations_to_resolution" because we don't have a satisfaction signal.
    A 12-query session may mean 12 successful answers OR 12 attempts at one
    question; metrics can't distinguish. PR-D dashboard should label these
    "queries per session", not "iterations".
    """
    query_ends = [e for e in events if e.get("event") == "query_end"]
    durations = [e["duration_ms"] for e in query_ends if isinstance(e.get("duration_ms"), (int, float))]
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    queries_per_session = defaultdict(int)
    for e in query_ends:
        sid = e.get("session_id")
        if sid:
            queries_per_session[sid] += 1
    qcounts = list(queries_per_session.values())

    # Abandonment proxy: sessions with query_start but no query_end.
    # Caveat: window-edge events (start in last day, end in next day) appear
    # abandoned. For 7-day windows the bias is small.
    started = {e.get("session_id") for e in events if e.get("event") == "query_start"}
    ended = {e.get("session_id") for e in events if e.get("event") == "query_end"}
    abandoned = started - ended
    abandonment_rate = len(abandoned) / max(len(started), 1)

    return {
        "queries_total": len(query_ends),
        "sessions_total": len(sessions),
        "time_to_response_ms_p50": int(percentile(durations, 50)) if durations else None,
        "time_to_response_ms_p95": int(percentile(durations, 95)) if durations else None,
        "queries_per_session_p50": percentile([float(x) for x in qcounts], 50) if qcounts else None,
        "queries_per_session_p95": percentile([float(x) for x in qcounts], 95) if qcounts else None,
        "query_abandonment_rate": round(abandonment_rate, 4),
    }


def aggregate_coverage(events: list[dict]) -> dict:
    """Memory hit rate, empty-handed rate, gap-discovery rate, live-call rate.

    Definitions:
    - memory_hit_rate: fraction of query_ends with memory_hits > 0.
    - empty_handed_rate: fraction with empty_handed=True (KB had data but
      memory was empty for this query).
    - gap_discovery_rate: fraction with memory_hits=0 AND topic_keywords
      non-empty. **Edge case**: a query whose extractor returns empty
      topic_keywords (short, all-stopwords) is invisible here regardless
      of memory_hits — neither in numerator nor denominator. Acceptable
      because no topic = nothing meaningful to chart as a gap.
    - live_calls_per_query: NOT a rate (can exceed 1.0 if a query triggers
      multiple live calls; e.g., one Slack + one Granola for the same
      question). Renamed from live_call_rate per round-1 review.
    """
    query_ends = [e for e in events if e.get("event") == "query_end"]
    if not query_ends:
        return {
            "memory_hit_rate": None,
            "empty_handed_rate": None,
            "gap_discovery_rate": None,
            "gap_detected_rate": None,
            "gap_by_reason": {},
            "live_calls_per_query": 0.0,
            "total_queries": 0,
        }
    with_memory = sum(1 for e in query_ends if (e.get("data") or {}).get("memory_hits", 0) > 0)
    empty = sum(1 for e in query_ends if (e.get("data") or {}).get("empty_handed") is True)
    gap = sum(
        1 for e in query_ends
        if (e.get("data") or {}).get("memory_hits", 0) == 0
        and (e.get("data") or {}).get("topic_keywords")
    )
    live = sum(1 for e in events if e.get("event") == "live_call_end")
    # gap_detected events from #39-A. Distinct from gap_discovery_rate
    # (which only counts zero_hit + topic_keywords>0). gap_detected_rate
    # is the operational signal: how often did the router decide live
    # would help, regardless of reason. Reasons split out in by_reason.
    gap_events = [e for e in events if e.get("event") == "gap_detected"]
    by_reason: dict[str, int] = {}
    for e in gap_events:
        r = (e.get("data") or {}).get("reason") or "unknown"
        by_reason[r] = by_reason.get(r, 0) + 1
    total = len(query_ends)
    return {
        "memory_hit_rate": round(with_memory / total, 4),
        "empty_handed_rate": round(empty / total, 4),
        "gap_discovery_rate": round(gap / total, 4),
        "gap_detected_rate": round(len(gap_events) / total, 4) if total else 0.0,
        "gap_by_reason": by_reason,
        "live_calls_per_query": round(live / total, 4) if total else 0.0,
        "total_queries": total,
    }


def _read_memory_created_at(path: Path) -> _dt.datetime | None:
    """Extract `created_at` from a memory file's YAML frontmatter, if present.

    Hand-parses the frontmatter rather than importing PyYAML — keeps the
    aggregator stdlib-only. Returns None if no frontmatter or no
    `created_at:` line.
    """
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
            if first != "---":
                return None
            for _ in range(50):  # cap frontmatter scan
                line = f.readline()
                if not line or line.strip() == "---":
                    break
                if line.startswith("created_at:"):
                    val = line.split(":", 1)[1].strip().strip("'\"")
                    return parse_iso(val)
    except OSError:
        return None
    return None


def aggregate_memory_quality(events: list[dict], memory_root: Path) -> dict:
    """Walk memory/ for current corpus state + use compress events for growth.

    Growth and topic_breadth come from events alone, so they're computed even
    when memory_root doesn't exist. Corpus-state fields (memory_objects_total,
    by_source_count, age distribution) require memory_root to be walkable.

    **Growth fix (per round-1 challenger)**: counts only `compress_result`
    events with `cluster_role=canonical`. Alternates and existing-canonical
    updates do NOT add net-new memory objects; counting them inflated
    growth by 30-50% on real workloads.

    **Memory age**: prefers `created_at` from frontmatter (truthful across
    cross-machine clones, restores, etc); falls back to file mtime when
    frontmatter is unparseable. Snapshot's `memory_age_source_distribution`
    field exposes the mix so dashboard can warn when mtime dominates
    (likely indicates a recent vault rehydration).
    """
    # Event-derived fields (independent of memory_root):
    growth = sum(
        1 for e in events
        if e.get("event") == "compress_result"
        and (e.get("data") or {}).get("cluster_role") == "canonical"
    )
    topics: set[str] = set()
    for e in events:
        kws = (e.get("data") or {}).get("topic_keywords") or []
        for k in kws:
            if isinstance(k, str):
                topics.add(k)

    # Corpus-walk fields:
    by_source: dict[str, int] = defaultdict(int)
    ages: list[float] = []
    age_source_counts: dict[str, int] = {"created_at": 0, "mtime": 0}
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
            # Prefer frontmatter created_at; fallback to mtime.
            created = _read_memory_created_at(f)
            if created is not None:
                ages.append((now - created).total_seconds() / 86400.0)
                age_source_counts["created_at"] += 1
            else:
                try:
                    mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime, tz=_dt.timezone.utc)
                    ages.append((now - mtime).total_seconds() / 86400.0)
                    age_source_counts["mtime"] += 1
                except OSError:
                    pass

    return {
        "memory_objects_total": file_count,
        "memory_growth_count_in_window": growth,
        "topic_coverage_breadth": len(topics),
        "by_source_count": dict(by_source),
        "memory_age_days_p50": round(percentile(ages, 50), 2) if ages else None,
        "memory_age_days_p95": round(percentile(ages, 95), 2) if ages else None,
        "memory_age_source_distribution": age_source_counts,
    }


def aggregate_source_economy(events: list[dict]) -> dict:
    """Per-source compression yield, bucketed by **source_kind**.

    Per round-1 challenger blocker: previous version bucketed by
    compress_result.data.kind (document type — thread / note / weekly), which
    was mislabeled as "per source." Now joins compress_result back to its
    matching compress_end via session_id + ts proximity to recover
    source_kind (slack_thread / gmail_thread / granola_note).

    Returns two top-level buckets so PR-D can chart both:
    - by_source_kind: keyed by source (slack_thread / gmail_thread / ...)
    - by_kind: keyed by document type (thread / note / weekly / ...)
    """
    src_index = build_compress_source_index(events)

    by_source_compress: dict[str, int] = defaultdict(int)
    by_source_over_budget: dict[str, int] = defaultdict(int)
    by_source_canonical: dict[str, int] = defaultdict(int)
    by_kind_compress: dict[str, int] = defaultdict(int)
    by_kind_over_budget: dict[str, int] = defaultdict(int)

    for e in events:
        if e.get("event") != "compress_result":
            continue
        d = e.get("data") or {}
        kind = d.get("kind") or "unknown"
        sid = e.get("session_id") or ""
        ts = e.get("ts") or ""
        source_kind = lookup_source_kind(src_index, sid, ts)

        by_kind_compress[kind] += 1
        by_source_compress[source_kind] += 1
        if d.get("over_budget"):
            by_kind_over_budget[kind] += 1
            by_source_over_budget[source_kind] += 1
        if d.get("cluster_role") == "canonical":
            by_source_canonical[source_kind] += 1

    def _bucketize(counts, over_budget, extra=None):
        out: dict = {}
        for k, count in counts.items():
            entry = {
                "compress_result_count": count,
                "over_budget_count": over_budget.get(k, 0),
                "over_budget_rate": round(over_budget.get(k, 0) / count, 4) if count else 0.0,
            }
            if extra and k in extra:
                entry["canonical_count"] = extra[k]
            out[k] = entry
        return out

    return {
        "by_source_kind": _bucketize(by_source_compress, by_source_over_budget, by_source_canonical),
        "by_kind": _bucketize(by_kind_compress, by_kind_over_budget),
    }


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
    """Build a snapshot for the given window. See `tools/metrics-aggregate.py`
    module docstring for schema details.

    Schema versioning policy: schema_version is bumped on field RENAMES or
    REMOVALS. Additive changes (new fields) do NOT bump the version —
    downstream consumers (PR-D dashboard) must tolerate missing fields.
    """
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
        "source_economy": aggregate_source_economy(events),
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
        # Include generation timestamp so re-running the same window doesn't
        # silently overwrite a prior snapshot. PR-D's "compare two snapshots"
        # workflow relies on prior versions being preserved.
        gen_compact = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = snapshots_dir / f"{start.isoformat()}_{end.isoformat()}_{gen_compact}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"[metrics-aggregate] window {start}..{end}: {snapshot['events_total']} events, "
          f"{snapshot['harvest_runs_total']} harvest runs → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
