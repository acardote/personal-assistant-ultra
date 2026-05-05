#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check whether the scheduled harvest is firing on cadence.

This is the F1 silent-failure detector for the routine path (#27).

Reads `<content_root>/.harvest/runs/*.json`, finds the newest entry by
mtime, and decides:
  - PASS:  newest run is `ok: true` AND younger than the staleness threshold
  - STALE: newest run is older than the threshold (no successful run lately)
  - FAILED: newest run is `ok: false` (most recent fire errored out)
  - MISSING: no runs/ directory or no .json files in it

The staleness threshold defaults to 26 hours (covers a daily-cadence routine
plus 2h slack for clock skew or one slow run). Override with --max-age-hours.

By design this works against runs/ files written by EITHER scheduler:
  - launchd path (`tools/scheduled-harvest.py`) writes `"scheduler": "launchd"`
  - routine path writes `"scheduler": "routine"`
The check is scheduler-agnostic — it cares about freshness, not provenance.

Exit codes:
  0 — PASS
  1 — STALE / FAILED / MISSING (any reason the user should investigate)
  2 — config error (couldn't load .assistant.local.json)

Output formats:
  - default: human-readable banner suitable for skill startup or terminal use
  - --json: structured JSON suitable for programmatic consumption

Usage:
    tools/check-harvest-freshness.py
    tools/check-harvest-freshness.py --max-age-hours 48
    tools/check-harvest-freshness.py --json
    tools/check-harvest-freshness.py --quiet           # PASS prints nothing
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

DEFAULT_MAX_AGE_HOURS = 26


@dataclass(frozen=True)
class FreshnessResult:
    state: str  # "PASS" | "STALE" | "FAILED" | "MISSING"
    newest_path: Path | None
    age_hours: float | None
    scheduler: str | None  # "routine" | "launchd" | None
    payload_ok: bool | None
    error: str | None
    summary: str

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "newest_path": str(self.newest_path) if self.newest_path else None,
            "age_hours": round(self.age_hours, 2) if self.age_hours is not None else None,
            "scheduler": self.scheduler,
            "payload_ok": self.payload_ok,
            "error": self.error,
            "summary": self.summary,
        }


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def assess_freshness(runs_dir: Path, max_age_hours: float) -> FreshnessResult:
    if not runs_dir.is_dir():
        return FreshnessResult(
            state="MISSING",
            newest_path=None,
            age_hours=None,
            scheduler=None,
            payload_ok=None,
            error=None,
            summary=(
                f"runs/ directory not found at {runs_dir}. The scheduled harvest "
                f"has never written a run-status file at this content root."
            ),
        )
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return FreshnessResult(
            state="MISSING",
            newest_path=None,
            age_hours=None,
            scheduler=None,
            payload_ok=None,
            error=None,
            summary=(
                f"runs/ directory exists at {runs_dir} but contains no .json status "
                f"files. Has the scheduled harvest ever fired?"
            ),
        )

    newest = files[0]
    mtime = _dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=_dt.timezone.utc)
    age_hours = (_utcnow() - mtime).total_seconds() / 3600.0

    payload: dict | None
    try:
        payload = json.loads(newest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = None
    except (json.JSONDecodeError, OSError):
        payload = None

    scheduler = payload.get("scheduler") if payload else None
    payload_ok = payload.get("ok") if payload else None
    payload_error = payload.get("error") if payload else None

    if age_hours > max_age_hours:
        return FreshnessResult(
            state="STALE",
            newest_path=newest,
            age_hours=age_hours,
            scheduler=scheduler,
            payload_ok=payload_ok,
            error=payload_error,
            summary=(
                f"Most recent harvest run is {age_hours:.1f}h old (threshold "
                f"{max_age_hours}h). The scheduled harvest may have stopped "
                f"firing — check the routine status at "
                f"https://claude.ai/code/routines, or the launchd plist if "
                f"you're on the alternative scheduler."
            ),
        )

    if payload_ok is False:
        return FreshnessResult(
            state="FAILED",
            newest_path=newest,
            age_hours=age_hours,
            scheduler=scheduler,
            payload_ok=False,
            error=payload_error,
            summary=(
                f"Most recent harvest run ({age_hours:.1f}h ago, scheduler="
                f"{scheduler or 'unknown'}) reported ok=false: "
                f"{payload_error or '<no error message>'}. Investigate before "
                f"the next fire."
            ),
        )

    return FreshnessResult(
        state="PASS",
        newest_path=newest,
        age_hours=age_hours,
        scheduler=scheduler,
        payload_ok=payload_ok,
        error=None,
        summary=(
            f"Most recent harvest run is {age_hours:.1f}h old (scheduler="
            f"{scheduler or 'unknown'}, ok={payload_ok}). Healthy."
        ),
    )


def _format_human_banner(result: FreshnessResult) -> str:
    if result.state == "PASS":
        return f"[harvest-freshness] OK — {result.summary}"
    bar = "=" * 78
    icon = {"STALE": "⚠️", "FAILED": "❌", "MISSING": "❓"}[result.state]
    lines = [
        bar,
        f"{icon}  HARVEST FRESHNESS: {result.state}",
        "",
        result.summary,
    ]
    if result.newest_path:
        lines.append(f"  newest run-status: {result.newest_path}")
    lines.append(bar)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check whether scheduled harvest is firing on cadence.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"Staleness threshold in hours (default: {DEFAULT_MAX_AGE_HOURS}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of a human banner.")
    parser.add_argument("--quiet", action="store_true", help="On PASS, suppress output (still exits 0).")
    args = parser.parse_args(argv[1:])

    try:
        cfg = load_config(require_explicit_content_root=False)
    except RuntimeError as exc:
        # require_explicit_content_root=False shouldn't raise, but catch defensively.
        print(f"[harvest-freshness] config error: {exc}", file=sys.stderr)
        return 2

    runs_dir = cfg.harvest_state_root / "runs"
    result = assess_freshness(runs_dir, max_age_hours=args.max_age_hours)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif args.quiet and result.state == "PASS":
        pass
    else:
        # Banner goes to stderr for STALE/FAILED/MISSING (so it surfaces in shell
        # wrappers that ignore stdout); to stdout for PASS so --quiet can suppress
        # cleanly without redirection gymnastics.
        stream = sys.stdout if result.state == "PASS" else sys.stderr
        print(_format_human_banner(result), file=stream)

    return 0 if result.state == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
