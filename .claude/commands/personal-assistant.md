---
description: Personal-assistant routine ops — dispatches to metrics / freshness-check / harvest / write-back / live-writeback subcommands. Falls through to skill activation for free-form questions.
allowed-tools: Bash
---

The user invoked `/personal-assistant $ARGUMENTS` from the method-repo root. Parse the first token of `$ARGUMENTS` as the subcommand and forward the rest as flags. Dispatch table:

## `metrics` — refresh the dashboard

```bash
tools/metrics-aggregate.py <remaining-args>
tools/metrics-dashboard.py --serve
```

Default window in `metrics-aggregate.py` is 7 days; pass `--days 30` or `--since YYYY-MM-DD --until YYYY-MM-DD` through transparently. The dashboard step takes no args worth surfacing. Print the dashboard path in your final response so the user can re-open it later. If the aggregator reports zero events for the window, surface that explicitly (likely the routine hasn't fired or `PA_METRICS_DIR` is misconfigured).

## `freshness-check` — surface harvest health

```bash
tools/check-harvest-freshness.py <remaining-args>
```

The check exits 0 when the most recent harvest is `ok: true` and younger than 26h. Non-zero exits emit a banner on stderr with one of: `STALE`, `FAILED`, `STUCK`, `STUCK_AND_STALE`, `MISSING`, `CORRUPT`. Surface the banner to the user verbatim (don't paraphrase the error field). Pass-through flags: `--quiet`, `--json`, `--stuck-threshold N`.

## `harvest` — run the harvest orchestration on demand

The skill itself is the harvest orchestrator (per the "Harvest orchestration" section of `SKILL.md`). For `/personal-assistant harvest <args>`, treat `<args>` as the harvest scope (e.g. `since yesterday`, `last 90 days`, `slack only`) and follow the per-source procedures in SKILL.md. Do NOT invoke `tools/harvest.py` directly for live MCP work — those calls only succeed inside a Claude session.

If the user passes nothing (`/personal-assistant harvest`), default to `since yesterday`.

## `live-writeback` — fold accumulated live findings into memory

```bash
tools/live-writeback.py <remaining-args>
```

Walks `<content_root>/raw/live/<source>/`, runs `compress.py --provenance live` per file, moves processed files to `.processed/`. Pass-through flags: `--source <granola_note|slack_thread|gmail_thread>`, `--dry-run`. Useful after a session that fired multiple live calls, before the user closes their laptop. (Per #39-D — also runs as part of the daily harvest routine.)

## (no subcommand or unknown subcommand)

If `$ARGUMENTS` is empty or doesn't match any subcommand above:
- Empty: list the subcommands with one-line descriptions and stop.
- Unknown: tell the user the subcommand isn't recognized, list the valid ones, and stop. Do NOT silently fall through to skill activation — that would mask typos.

## Surface contract

Always print the actual shell command(s) you ran (so the user can re-run them by hand), and surface tool-side stderr (banners from freshness-check, summary lines from metrics). The skill's pre-flight harvest-freshness check from SKILL.md does NOT run for these routine ops — these are operator tasks, not user-question tasks, and re-running freshness-check on every dashboard refresh is noise.
