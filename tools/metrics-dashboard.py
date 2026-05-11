#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Render a local HTML dashboard from metrics snapshots (#41 PR-D).

Reads all snapshot JSONs in `<content_root>/.metrics/snapshots/`, sorts them
chronologically by `generated_at`, and renders an HTML page with:

- **Time-series charts** (one per key metric) showing trends across snapshots:
  query latency p50/p95, live_calls_per_query, memory_hit_rate,
  empty_handed_rate, gap_discovery_rate, harvest_success_rate, growth rate.
- **Current-state tables** from the latest snapshot: by_source_kind compress
  counts, memory by_source_count, freshness_check_states distribution.

Charts use Plotly.js loaded from a CDN — no Python plotly dependency. The
page is fully self-contained except for the CDN script tag.

Usage:
    tools/metrics-dashboard.py
    tools/metrics-dashboard.py --out ~/Desktop/pa-dashboard.html
    tools/metrics-dashboard.py --serve              # also opens local server

The default output is `<content_root>/.metrics/dashboard.html` (gitignored,
private). With `--serve`, opens the file in a browser via the OS-default
handler (no actual server — just the file:// URL).

Snapshot discovery: latest by mtime; multi-snapshot history by chronological
sort on `generated_at`. If no snapshots exist, renders a "no data yet" page
that's still well-formed HTML.

Exit codes:
  0 — dashboard written
  1 — config error or no snapshots found
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import importlib.util
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

# Avoid sys.path mutation; load _config via spec.
_CONFIG_PATH = Path(__file__).resolve().parent / "_config.py"
_spec = importlib.util.spec_from_file_location("_pa_dashboard_config", str(_CONFIG_PATH))
assert _spec is not None and _spec.loader is not None
_config_module = importlib.util.module_from_spec(_spec)
sys.modules["_pa_dashboard_config"] = _config_module
_spec.loader.exec_module(_config_module)
load_config = _config_module.load_config


# Plotly.js loaded via CDN. **Security trade-off acknowledged**: there is
# no SubResource Integrity (SRI) hash here because computing one for each
# Plotly version requires a build step we don't currently run, and shipping
# an unverified hash would block all loads if wrong (worse than no SRI).
# Mitigations available:
#   - Vendor plotly.js under tools/vendor/ (~3.7MB minified).
#   - Compute SRI with `cat plotly.min.js | openssl dgst -sha384 -binary | base64`
#     and pin to the version, then auto-bump via Renovate.
#   - Use --offline flag (future) to skip charts entirely.
# For a personal-use local dashboard rendering against private vault data,
# the CDN supply-chain risk is real but bounded (private machine, infrequent
# render). Caveat documented.
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"


def load_snapshots(snapshots_dir: Path) -> list[dict]:
    """Read all snapshot JSONs, sort by generated_at ascending."""
    if not snapshots_dir.is_dir():
        return []
    snapshots: list[dict] = []
    for p in snapshots_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        # Stamp the source path so the dashboard can show provenance.
        data["_source_path"] = str(p)
        snapshots.append(data)
    snapshots.sort(key=lambda s: s.get("generated_at", ""))
    return snapshots


def time_series_chart(
    title: str, xs: list[str], series: dict[str, list], *, yaxis_title: str = "",
) -> dict:
    """Build a Plotly figure spec dict for a time-series chart with multiple
    series over the same x-axis (snapshot generated_at)."""
    data = []
    for label, ys in series.items():
        data.append({
            "type": "scatter",
            "mode": "lines+markers",
            "name": label,
            "x": xs,
            "y": ys,
        })
    layout = {
        "title": title,
        "xaxis": {"title": "Snapshot date"},
        "yaxis": {"title": yaxis_title},
        "margin": {"l": 60, "r": 30, "t": 50, "b": 50},
        "height": 320,
    }
    return {"data": data, "layout": layout}


def _esc(value: Any) -> str:
    """HTML-escape a value, coercing to string. Defense-in-depth even though
    snapshot data comes from trusted aggregator — future schema fields or
    user-provided event data could leak HTML/script into the dashboard."""
    return html.escape(str(value), quote=True)


def render_table(rows: list[tuple], headers: list[str]) -> str:
    """Tiny HTML table renderer. All values escaped."""
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def safe_get(d: dict, *keys: str, default=None):
    """Walk nested dict with default fallback."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
        if d is default:
            return default
    return d


def build_charts(snapshots: list[dict]) -> list[dict]:
    """Build the list of Plotly figure specs from snapshot history.

    Requires at least 2 snapshots for trends to be meaningful — a one-point
    line chart is misleading. Caller is expected to render an explicit
    "need ≥2 snapshots" message when this returns empty due to the gate.
    """
    if len(snapshots) < 2:
        return []
    xs = [s.get("generated_at", "") for s in snapshots]

    charts: list[dict] = []

    # Query latency p50/p95
    p50 = [safe_get(s, "user_experience", "time_to_response_ms_p50") for s in snapshots]
    p95 = [safe_get(s, "user_experience", "time_to_response_ms_p95") for s in snapshots]
    charts.append(time_series_chart(
        "Query latency (ms)",
        xs,
        {"p50": [v if v is not None else None for v in p50],
         "p95": [v if v is not None else None for v in p95]},
        yaxis_title="ms",
    ))

    # Coverage: hit rate, empty-handed, gap discovery
    hit_rate = [safe_get(s, "coverage", "memory_hit_rate") for s in snapshots]
    empty_rate = [safe_get(s, "coverage", "empty_handed_rate") for s in snapshots]
    gap_rate = [safe_get(s, "coverage", "gap_discovery_rate") for s in snapshots]
    charts.append(time_series_chart(
        "Coverage rates",
        xs,
        {"memory_hit_rate": hit_rate,
         "empty_handed_rate": empty_rate,
         "gap_discovery_rate": gap_rate},
        yaxis_title="rate (0-1)",
    ))

    # Live calls per query (will be 0 until #39 lands)
    live = [safe_get(s, "coverage", "live_calls_per_query") for s in snapshots]
    charts.append(time_series_chart(
        "Live calls per query",
        xs,
        {"live_calls_per_query": live},
        yaxis_title="calls/query",
    ))

    # Memory growth + corpus
    growth = [safe_get(s, "memory_quality", "memory_growth_count_in_window") for s in snapshots]
    total = [safe_get(s, "memory_quality", "memory_objects_total") for s in snapshots]
    charts.append(time_series_chart(
        "Memory corpus over time",
        xs,
        {"new in window (canonical)": growth,
         "total objects": total},
        yaxis_title="count",
    ))

    # System health: harvest success rate
    hs = [safe_get(s, "system_health", "harvest_success_rate") for s in snapshots]
    charts.append(time_series_chart(
        "Harvest success rate",
        xs,
        {"harvest_success_rate": hs},
        yaxis_title="rate (0-1)",
    ))

    # Token budget violations
    tv = [safe_get(s, "system_health", "token_budget_violations") for s in snapshots]
    charts.append(time_series_chart(
        "Token budget violations (count)",
        xs,
        {"token_budget_violations": tv},
        yaxis_title="count",
    ))

    return charts


def render_current_state(latest: dict) -> str:
    """Render latest-snapshot tables (current state)."""
    if not latest:
        return "<p><em>No snapshot data yet — run <code>tools/metrics-aggregate.py</code> at least once.</em></p>"

    sections: list[str] = []

    # Header — escape all snapshot-derived values
    gen_at = _esc(latest.get("generated_at", "?"))
    win = _esc(f"{latest.get('window_start', '?')} → {latest.get('window_end', '?')}")
    events = _esc(latest.get("events_total", 0))
    runs = _esc(latest.get("harvest_runs_total", 0))
    sections.append(
        f"<p>Latest snapshot: <strong>{gen_at}</strong> · "
        f"window <strong>{win}</strong> · "
        f"<strong>{events}</strong> events · "
        f"<strong>{runs}</strong> harvest runs</p>"
    )

    # Staleness warning if latest snapshot is stale (>36h old).
    parsed_gen = None
    try:
        gen_str = latest.get("generated_at", "")
        if isinstance(gen_str, str):
            parsed_gen = _dt.datetime.fromisoformat(gen_str.replace("Z", "+00:00"))
    except ValueError:
        pass
    if parsed_gen is not None:
        age_h = (_dt.datetime.now(_dt.timezone.utc) - parsed_gen).total_seconds() / 3600.0
        if age_h > 36:
            sections.append(
                f'<p style="color:#c00;background:#fee;padding:6px 12px;border-left:4px solid #c00;">'
                f'⚠️ Latest snapshot is {age_h:.1f}h old. Run '
                f'<code>tools/metrics-aggregate.py</code> to refresh.</p>'
            )

    # User-experience summary
    ux = latest.get("user_experience") or {}
    if ux:
        rows = [
            ("Queries total", ux.get("queries_total", 0)),
            ("Sessions total", ux.get("sessions_total", 0)),
            ("Latency p50 (ms)", ux.get("time_to_response_ms_p50") or "—"),
            ("Latency p95 (ms)", ux.get("time_to_response_ms_p95") or "—"),
            ("Queries/session p50", ux.get("queries_per_session_p50") or "—"),
            ("Abandonment rate", ux.get("query_abandonment_rate") or 0),
        ]
        sections.append("<h3>User experience</h3>" + render_table(rows, ["metric", "value"]))

    # Coverage
    cov = latest.get("coverage") or {}
    if cov:
        rows = [
            ("Memory hit rate", cov.get("memory_hit_rate") or "—"),
            ("Empty-handed rate", cov.get("empty_handed_rate") or "—"),
            ("Gap-discovery rate", cov.get("gap_discovery_rate") or "—"),
            ("Gap-detected rate (#39-A)", cov.get("gap_detected_rate") or "—"),
            ("Live calls per query", cov.get("live_calls_per_query") or 0),
            ("Total queries", cov.get("total_queries", 0)),
        ]
        sections.append("<h3>Coverage</h3>" + render_table(rows, ["metric", "value"]))
        # Gap reason breakdown
        by_reason = cov.get("gap_by_reason") or {}
        if by_reason:
            reason_rows = sorted(by_reason.items(), key=lambda kv: -kv[1])
            sections.append("<h4>Gap reasons</h4>" + render_table(reason_rows, ["reason", "count"]))
        # Live-call status breakdown (#39-B). Surfaces the success/empty/error/timeout
        # mix per source so an operator can spot MCP auth issues, over-firing, or
        # latency problems at a glance.
        by_status = cov.get("live_by_status") or {}
        if by_status:
            status_rows = sorted(by_status.items(), key=lambda kv: -kv[1])
            sections.append("<h4>Live-call status</h4>" + render_table(status_rows, ["status", "count"]))
        by_live_src = cov.get("live_by_source") or {}
        if by_live_src:
            src_rows = sorted(by_live_src.items(), key=lambda kv: -kv[1])
            sections.append("<h4>Live calls by source</h4>" + render_table(src_rows, ["source", "count"]))
        truncated_count = cov.get("live_body_truncated_count", 0)
        if truncated_count:
            sections.append(
                f"<p><strong>Live-call body truncations:</strong> {_esc(truncated_count)} "
                "— check if MAX_BODY_CHARS=65536 needs raising for your typical thread sizes.</p>"
            )
        # Write-back pipeline (#39-D) — surfaces per-source success/error
        # so an operator can spot live-writeback.py falling behind.
        wb = cov.get("writeback_by_source") or {}
        if wb:
            wb_rows = []
            for src in sorted(wb.keys()):
                d = wb[src]
                wb_rows.append((src, d.get("success", 0), d.get("error", 0), d.get("total", 0)))
            sections.append("<h4>Write-back items (#39-D)</h4>" + render_table(
                wb_rows, ["source", "success", "error", "total"]
            ))

    # Source economy
    se = latest.get("source_economy") or {}
    by_source = se.get("by_source_kind") or {}
    if by_source:
        rows = []
        for src, stats in sorted(by_source.items()):
            rows.append((
                src,
                stats.get("compress_result_count", 0),
                stats.get("over_budget_count", 0),
                stats.get("over_budget_rate", 0),
                stats.get("canonical_count", "—"),
            ))
        sections.append("<h3>Source economy (by source kind)</h3>" + render_table(
            rows, ["source", "compresses", "over budget", "over budget rate", "canonical (new)"]
        ))

    # Memory by source (current corpus)
    mq = latest.get("memory_quality") or {}
    by_src_count = mq.get("by_source_count") or {}
    if by_src_count:
        rows = sorted(by_src_count.items())
        sections.append("<h3>Memory corpus by source</h3>" + render_table(rows, ["source", "memory objects"]))
        # Age provenance — warn if mtime DOMINATES (>2x created_at) per round-1
        # challenger: 51/49 fired identically to 99/1 with the old binary check.
        agedist = mq.get("memory_age_source_distribution") or {}
        if agedist:
            ca = agedist.get("created_at", 0) or 0
            mt = agedist.get("mtime", 0) or 0
            ratio_warn = mt > 2 * max(ca, 1)
            warn = (
                f' ⚠️ <em>mtime dominates ({mt}:{ca} = {mt / max(ca, 1):.1f}x) — '
                f'likely vault rehydration; ages are not truthful.</em>'
                if ratio_warn else ""
            )
            sections.append(
                f"<p>Memory age source: <strong>{_esc(ca)}</strong> from <code>created_at</code> (frontmatter), "
                f"<strong>{_esc(mt)}</strong> from <code>mtime</code>.{warn}</p>"
            )

    # System health
    sh = latest.get("system_health") or {}
    if sh:
        rows = [
            ("Harvest runs", sh.get("harvest_runs_total", 0)),
            ("Harvest success", sh.get("harvest_success_count", 0)),
            ("Harvest failed", sh.get("harvest_failed_count", 0)),
            ("Harvest success rate", sh.get("harvest_success_rate") or "—"),
            ("Token budget violations", sh.get("token_budget_violations", 0)),
        ]
        sections.append("<h3>System health</h3>" + render_table(rows, ["metric", "value"]))

        states = sh.get("freshness_check_states") or {}
        if states:
            rows = sorted(states.items(), key=lambda x: -x[1])
            sections.append("<h4>Freshness check states</h4>" + render_table(rows, ["state", "count"]))

        mcp = sh.get("mcp_errors_by_source") or {}
        if mcp:
            rows = sorted(mcp.items(), key=lambda x: -x[1])
            sections.append("<h4>MCP errors by source</h4>" + render_table(rows, ["source", "errors"]))

    return "\n".join(sections)


def _json_for_script(obj: Any) -> str:
    """Serialize `obj` for embedding inside a `<script>` tag.

    `json.dumps` does NOT escape `</` — a snapshot value containing the literal
    `</script>` would break out of the script block. Today only `generated_at`
    (aggregator-side timestamp) flows into Plotly data, so the path is
    theoretically unreachable; defense-in-depth so it stays that way when
    future chart fields source from `by_source_kind` keys or other
    snapshot-derived strings.
    """
    return json.dumps(obj).replace("</", "<\\/")


def render_html(snapshots: list[dict]) -> str:
    """Render the full HTML dashboard."""
    latest = snapshots[-1] if snapshots else {}
    charts = build_charts(snapshots)

    chart_divs = "\n".join(f"<div id='chart-{i}' class='chart'></div>" for i in range(len(charts)))
    chart_scripts = "\n".join(
        f"Plotly.newPlot('chart-{i}', {_json_for_script(c['data'])}, {_json_for_script(c['layout'])}, {{responsive: true}});"
        for i, c in enumerate(charts)
    )

    current_state = render_current_state(latest)
    n_snapshots = len(snapshots)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Personal-assistant metrics dashboard</title>
  <script src="{PLOTLY_CDN}"></script>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
            margin: 24px; max-width: 1100px; color: #222; }}
    h1 {{ margin: 0 0 4px; }}
    h2 {{ margin-top: 32px; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
    h3 {{ margin-top: 20px; }}
    .meta {{ color: #666; font-size: 13px; }}
    .chart {{ margin: 16px 0 32px; }}
    table {{ border-collapse: collapse; margin: 8px 0 16px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 12px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    code {{ background: #f5f5f5; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  </style>
</head>
<body>
  <h1>Personal-assistant metrics</h1>
  <p class="meta">{n_snapshots} snapshot{'s' if n_snapshots != 1 else ''} · generated by
     <code>tools/metrics-dashboard.py</code> · per
     <a href="https://github.com/acardote/personal-assistant-ultra/issues/41">issue #41</a></p>

  <h2>Current state (latest snapshot)</h2>
  {current_state}

  <h2>Trends across snapshots</h2>
  {'<p><em>Need at least 2 snapshots to show trends — a one-point line is meaningless. Run <code>tools/metrics-aggregate.py</code> daily/weekly to build history.</em></p>' if not charts else chart_divs}

  <script>
{chart_scripts}
  </script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render personal-assistant metrics dashboard.")
    parser.add_argument("--out", help="Output HTML path (default: <content_root>/.metrics/dashboard.html).")
    parser.add_argument("--serve", action="store_true", help="Open the dashboard in your default browser after writing.")
    args = parser.parse_args(argv[1:])

    try:
        cfg = load_config(require_explicit_content_root=False)
    except RuntimeError as exc:
        print(f"[metrics-dashboard] config error: {exc}", file=sys.stderr)
        return 1

    metrics_dir = cfg.harvest_state_root.parent / ".metrics"
    snapshots_dir = metrics_dir / "snapshots"
    snapshots = load_snapshots(snapshots_dir)

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        out_path = metrics_dir / "dashboard.html"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_out = render_html(snapshots)  # local name avoids shadowing the `html` stdlib import
    out_path.write_text(html_out, encoding="utf-8")
    print(f"[metrics-dashboard] {len(snapshots)} snapshot(s) → {out_path}", file=sys.stderr)

    if args.serve:
        try:
            webbrowser.open(f"file://{out_path}")
        except Exception as exc:
            print(f"[metrics-dashboard] webbrowser.open failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
