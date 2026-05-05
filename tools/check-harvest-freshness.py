#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check whether the scheduled harvest is firing on cadence.

This is the F1 silent-failure detector for the routine path (#27). Note that
this is not a true SLA on detection time — it's a check that surfaces a
warning when the user invokes `/personal-assistant`. The window is bounded
by user-invocation cadence, not by the 26h threshold below. Out-of-band
alerting (Slack self-DM, daily-digest entry) is tracked separately as
follow-up work.

Reads `<content_root>/.harvest/runs/*.json`, finds the newest entry, and
decides:
  - PASS:             newest run is `ok: true` AND younger than threshold
  - STALE:            newest run is older than threshold (no successful run lately)
  - FAILED:           newest run is `ok: false` (most recent fire errored out)
  - STUCK:            N consecutive `ok: false` runs with the same error
                      (chronic, not transient — different remediation)
  - STUCK_AND_STALE:  STUCK conditions hold AND newest run also exceeds the
                      staleness threshold (two distinct problems, both surfaced)
  - MISSING:          no runs/ directory or no .json files in it
  - CORRUPT:          newest .json file is unparseable (truncated write or
                      manual corruption — likely the last run crashed mid-write)

Age clock: prefers the `started_at` field from the run-status JSON payload,
falling back to filesystem mtime if the payload is absent or unparseable.
This matters because `git clone`, `cp -r`, and the backup/restore tooling
all reset mtime — using mtime alone would silently report PASS for a
months-old vault snapshot.

By design this works against runs/ files written by EITHER scheduler:
  - launchd path (`tools/scheduled-harvest.py`) writes `"scheduler": "launchd"`
  - routine path writes `"scheduler": "routine"`
The check is scheduler-agnostic — it cares about freshness, not provenance.

Exit codes:
  0 — PASS
  1 — STALE / FAILED / STUCK / STUCK_AND_STALE / MISSING / CORRUPT
      (any reason to investigate)
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
STUCK_CONSECUTIVE_THRESHOLD = 3  # N consecutive same-error failures → STUCK


@dataclass(frozen=True)
class FreshnessResult:
    state: str  # "PASS" | "STALE" | "FAILED" | "STUCK" | "MISSING" | "CORRUPT"
    newest_path: Path | None
    age_hours: float | None
    age_source: str | None  # "started_at" | "mtime" | None
    scheduler: str | None  # "routine" | "launchd" | None
    payload_ok: bool | None
    error: str | None
    consecutive_failures: int | None
    summary: str

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "newest_path": str(self.newest_path) if self.newest_path else None,
            "age_hours": round(self.age_hours, 2) if self.age_hours is not None else None,
            "age_source": self.age_source,
            "scheduler": self.scheduler,
            "payload_ok": self.payload_ok,
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
            "summary": self.summary,
        }


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Parse an ISO-8601 UTC timestamp. Tolerates both `Z` suffix and explicit `+00:00`."""
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


def _read_payload(path: Path) -> tuple[dict | None, bool]:
    """Returns (payload_dict_or_None, parse_ok). parse_ok=False indicates the file
    exists but is unparseable — distinct from "successfully parsed but not a dict",
    which still returns parse_ok=True with payload=None."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, dict):
        return None, True
    return payload, True


def _count_consecutive_failures(files: list[Path]) -> tuple[int, str | None]:
    """Walk newest-to-oldest, count how many consecutive runs reported ok=false
    with the same `error` text. Returns (count, error_text) — count is 0 if the
    newest run was ok=true; count is 1 for an isolated failure.

    Unparseable (corrupt) files in the middle of the walk are SKIPPED, not
    treated as a break condition. Rationale: a corrupt status file in the
    middle of a streak of real failures shouldn't mask the real failure
    pattern. Treating corrupt as a break would silently under-report STUCK.

    The walk DOES break on the first ok=true run, on a different error,
    or on a payload that successfully parsed but isn't a dict."""
    error_text: str | None = None
    count = 0
    for path in files:
        payload, parse_ok = _read_payload(path)
        if not parse_ok:
            # Skip corrupt files — they don't reset the counter, they don't
            # extend it either. Just keep walking.
            continue
        if not payload:
            # Successfully parsed but not a dict — break.
            break
        if payload.get("ok") is False:
            this_error = payload.get("error") or ""
            if count == 0:
                error_text = this_error
                count = 1
            elif this_error == error_text:
                count += 1
            else:
                break
        else:
            break
    return count, error_text


def assess_freshness(
    runs_dir: Path, max_age_hours: float, stuck_threshold: int = STUCK_CONSECUTIVE_THRESHOLD
) -> FreshnessResult:
    if not runs_dir.is_dir():
        return FreshnessResult(
            state="MISSING", newest_path=None, age_hours=None, age_source=None,
            scheduler=None, payload_ok=None, error=None, consecutive_failures=None,
            summary=(
                f"No harvest run-status files yet at {runs_dir}. If you've configured "
                f"the scheduled routine and are waiting for the first fire, this is "
                f"expected. Otherwise, configure the routine per "
                f"`templates/routines/harvest-routine.md`."
            ),
        )
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return FreshnessResult(
            state="MISSING", newest_path=None, age_hours=None, age_source=None,
            scheduler=None, payload_ok=None, error=None, consecutive_failures=None,
            summary=(
                f"runs/ directory exists at {runs_dir} but contains no .json status "
                f"files. Configure the routine per `templates/routines/harvest-routine.md` "
                f"if you haven't already, or wait for its first fire."
            ),
        )

    newest = files[0]
    payload, parse_ok = _read_payload(newest)

    if not parse_ok:
        # Truncated write / manual corruption — distinct signal that the last run
        # crashed mid-write or someone edited the file. Don't silently PASS.
        return FreshnessResult(
            state="CORRUPT", newest_path=newest, age_hours=None, age_source=None,
            scheduler=None, payload_ok=None, error=None, consecutive_failures=None,
            summary=(
                f"Newest harvest run-status file at {newest} is unparseable. "
                f"Either the most recent run crashed mid-write, or the file was "
                f"manually edited. Investigate the file directly."
            ),
        )

    # Determine age — prefer started_at from payload (resists mtime reset by
    # git clone / cp / backup-restore); fallback to mtime.
    age_hours: float
    age_source: str
    started_at = payload.get("started_at") if payload else None
    parsed_started = _parse_iso(started_at) if started_at else None
    if parsed_started is not None:
        age_hours = (_utcnow() - parsed_started).total_seconds() / 3600.0
        age_source = "started_at"
    else:
        mtime = _dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=_dt.timezone.utc)
        age_hours = (_utcnow() - mtime).total_seconds() / 3600.0
        age_source = "mtime"

    scheduler = payload.get("scheduler") if payload else None
    payload_ok = payload.get("ok") if payload else None
    payload_error = payload.get("error") if payload else None

    # STUCK takes precedence over FAILED if N consecutive same-error failures.
    # If both STUCK and STALE conditions hold, surface both signals — the user
    # has two distinct problems (chronic error AND no successful run lately).
    consecutive_failures, stuck_error = _count_consecutive_failures(files)
    if consecutive_failures >= stuck_threshold:
        also_stale = age_hours > max_age_hours
        # When both STUCK and STALE hold, surface both in the headline state so
        # a user scanning the banner doesn't miss the staleness signal.
        state_label = "STUCK_AND_STALE" if also_stale else "STUCK"
        headline = (
            f"BOTH STUCK AND STALE: last {consecutive_failures} consecutive runs "
            f"failed with the same error AND it has been {age_hours:.1f}h since "
            f"the most recent fire (threshold {max_age_hours}h)."
            if also_stale else
            f"STUCK: last {consecutive_failures} consecutive runs failed with the same error."
        )
        return FreshnessResult(
            state=state_label, newest_path=newest, age_hours=age_hours, age_source=age_source,
            scheduler=scheduler, payload_ok=payload_ok, error=stuck_error,
            consecutive_failures=consecutive_failures,
            summary=(
                f"{headline} Error: '{stuck_error or '<no error message>'}'. "
                f"Chronic, not transient — fix the underlying issue (often a "
                f"connector that needs re-authentication) before the next fire."
            ),
        )

    if age_hours > max_age_hours:
        # Include payload error in summary if last run was also a failure (reviewer
        # round-2 suggestion #1).
        suffix = ""
        if payload_ok is False and payload_error:
            suffix = f" Last run also reported error: '{payload_error}'."
        return FreshnessResult(
            state="STALE", newest_path=newest, age_hours=age_hours, age_source=age_source,
            scheduler=scheduler, payload_ok=payload_ok, error=payload_error,
            consecutive_failures=consecutive_failures or None,
            summary=(
                f"Most recent harvest run is {age_hours:.1f}h old (threshold "
                f"{max_age_hours}h, age_source={age_source}). The scheduled harvest "
                f"may have stopped firing — check routine status at "
                f"https://claude.ai/code/routines, or the launchd plist if you're "
                f"on the alternative scheduler.{suffix}"
            ),
        )

    if payload_ok is False:
        return FreshnessResult(
            state="FAILED", newest_path=newest, age_hours=age_hours, age_source=age_source,
            scheduler=scheduler, payload_ok=False, error=payload_error,
            consecutive_failures=consecutive_failures or None,
            summary=(
                f"Most recent harvest run ({age_hours:.1f}h ago, scheduler="
                f"{scheduler or 'unknown'}) reported ok=false: "
                f"'{payload_error or '<no error message>'}'. Investigate before "
                f"the next fire. (If this error persists across "
                f"{STUCK_CONSECUTIVE_THRESHOLD} consecutive runs the state will "
                f"escalate to STUCK.)"
            ),
        )

    return FreshnessResult(
        state="PASS", newest_path=newest, age_hours=age_hours, age_source=age_source,
        scheduler=scheduler, payload_ok=payload_ok, error=None,
        consecutive_failures=None,
        summary=(
            f"Most recent harvest run is {age_hours:.1f}h old (scheduler="
            f"{scheduler or 'unknown'}, ok={payload_ok}, age_source={age_source}). Healthy."
        ),
    )


def _format_human_banner(result: FreshnessResult) -> str:
    if result.state == "PASS":
        return f"[harvest-freshness] OK — {result.summary}"
    bar = "=" * 78
    icon = {
        "STALE": "⚠️",
        "FAILED": "❌",
        "STUCK": "🔁",
        "STUCK_AND_STALE": "🔁⚠️",
        "MISSING": "❓",
        "CORRUPT": "⚠️",
    }.get(result.state, "⚠️")
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
    parser.add_argument(
        "--stuck-threshold",
        type=int,
        default=STUCK_CONSECUTIVE_THRESHOLD,
        help=(
            f"Number of consecutive same-error failures before STUCK fires "
            f"(default: {STUCK_CONSECUTIVE_THRESHOLD}, minimum: 1). For sub-daily routines "
            f"(e.g. hourly) you may want a higher value; for monthly cadences, lower."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of a human banner.")
    parser.add_argument("--quiet", action="store_true", help="On PASS, suppress output (still exits 0).")
    args = parser.parse_args(argv[1:])

    if args.stuck_threshold < 1:
        parser.error(
            f"--stuck-threshold must be ≥ 1 (got {args.stuck_threshold}); "
            f"a threshold of 0 or negative would fire STUCK on every healthy run."
        )

    try:
        cfg = load_config(require_explicit_content_root=False)
    except RuntimeError as exc:
        # require_explicit_content_root=False shouldn't raise, but catch defensively.
        print(f"[harvest-freshness] config error: {exc}", file=sys.stderr)
        return 2

    runs_dir = cfg.harvest_state_root / "runs"
    result = assess_freshness(
        runs_dir, max_age_hours=args.max_age_hours, stuck_threshold=args.stuck_threshold
    )

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
