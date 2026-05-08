---
description: Personal-assistant routine ops — dispatches metrics / freshness-check / harvest / live-writeback / project subcommands.
---

The user invoked `/personal-assistant $ARGUMENTS` from the method-repo root.

## Dispatch contract

1. **Parse the FIRST whitespace-separated token of `$ARGUMENTS` as the subcommand.** Tokens with no whitespace (e.g. `metrics--days30`) are NOT subcommands — treat them as unknown.
2. **Match the token EXACTLY against the table below.** No fuzzy matching, no inference of intent on near-misses (typos like `metrcs` are unknown, not `metrics`).
3. **On match**: forward `$ARGUMENTS` minus the first token as flags to the underlying tool, follow the subcommand's instructions, surface the actual shell command(s) you ran in your final response.
4. **On empty `$ARGUMENTS` OR unknown subcommand**: print the valid subcommand list with one-line descriptions and STOP. Do NOT activate the personal-assistant skill, do NOT ask a clarifying question, do NOT infer intent. Empty/unknown is a deterministic refusal — that's the contract.

## Subcommand table

| Subcommand | Tool dispatch |
|---|---|
| `metrics` | `tools/metrics-aggregate.py <flags>` then `tools/metrics-dashboard.py --serve` |
| `freshness-check` | `tools/check-harvest-freshness.py <flags>` |
| `harvest` | follow harvest orchestration in SKILL.md (uses MCP tools, can't go through Bash alone) |
| `live-writeback` | `tools/live-writeback.py <flags>` |
| `project` | nested router (see "Subcommand: project" below) — dispatches to `tools/project.py <subcmd>` |
| `kb-process` | nested router (see "Subcommand: kb-process" below) — dispatches to `tools/kb-process.py <subcmd>` |

## Per-subcommand notes

### `metrics`
Default window is 7 days; pass `--days N` or `--since YYYY-MM-DD --until YYYY-MM-DD` through transparently. Print the dashboard path. If aggregator reports zero events for the window, surface that explicitly (likely the routine hasn't fired or `PA_METRICS_DIR` is misconfigured).

### `freshness-check`
The check exits 0 when the most recent harvest is `ok: true` AND younger than 26h. Non-zero exits emit a banner on stderr with one of: `STALE`, `FAILED`, `STUCK`, `STUCK_AND_STALE`, `MISSING`, `CORRUPT`. Surface the banner verbatim. Pass-through: `--quiet`, `--json`, `--stuck-threshold N`.

### `harvest`
**Run `tools/check-harvest-freshness.py --quiet` FIRST** (per pr-challenger B2 on #66 — on-demand harvest without pre-flight produces false-positive successes when auth/MCP is broken). If freshness exits non-zero with `FAILED` / `STUCK` / `STUCK_AND_STALE` / `CORRUPT`, surface the banner and ASK the user before proceeding (the user may want to fix the upstream issue first; running harvest over a broken auth wastes time and pollutes the trail). For `MISSING` / `STALE`, proceed — those are the cases on-demand harvest exists to fix.

Then follow the per-source procedures in SKILL.md ("Harvest orchestration"). Treat `<args>` as the harvest scope: `since yesterday` (default if no args), `last 90 days`, `slack only`, etc.

### `live-writeback`
Walks `<content_root>/raw/live/<source>/`, runs `compress.py --provenance live` per file, moves processed files to `.processed/`. Pass-through: `--source <granola_note|slack_thread|gmail_thread>`, `--dry-run`. Useful after a session that fired multiple live calls.

### `project`

PA project tier (per [ADR-0003 Amendment 1](../../docs/adr/0003-agent-output-taxonomy.md#amendment-1--project-tier-2026-05-07)) — multi-session containers for agent-executed work, with start/resume/promote/copy mechanics.

Parse the SECOND whitespace-separated token of `$ARGUMENTS` as the project subcommand:

| Project subcommand | Dispatch | Notes |
|---|---|---|
| `new <short-name> "<intent>"` | `tools/project.py new <short> "<intent>"` | Generates slug `YYYYMMDD-<short>-<4hex>`, scaffolds folder, sets active state. |
| `resume <slug-or-shortname>` | `tools/project.py resume <ref>` | Sets active state. The tool prints `project.md` + manifest + `notes.md` to stdout — read the output to load the project's context into your working memory. |
| `list [--include-archived]` | `tools/project.py list [<flag>]` | Active by default; flag adds archived. |
| `archive <slug>` | `tools/project.py archive <slug>` | Flips status. Then `tools/live-commit-push.sh <content_root> "project: archive <slug>"`. |
| `promote <art-uuid> <slug>` | `tools/project.py promote <uuid> <slug>` | Moves a flat artefact + sidecars into a project. Then `tools/live-commit-push.sh ... "project: promote <art-uuid> -> <slug>"`. |
| `copy-artefact <art-uuid> <dest-slug>` | `tools/project.py copy-artefact <uuid> <dest>` | Copies (fresh id, derived_from). Then commit-push. |
| `clear` | `tools/project.py clear` | Removes the active-project state file. |
| `status` | `tools/project.py status` | Prints active slug + age + frontmatter scalars. If age > 4h, the tool flags STALE — surface that to the user. |
| `touch <slug>` | `tools/project.py touch <slug>` | Updates `last_active` on the project's frontmatter using the surgical updater (preserves nested blocks). Used by the SKILL's Phase 3 after a project-scoped write. |
| `sweep [--days N] [--json]` | `tools/project.py sweep [<flags>]` | Lists active projects whose `last_active` is older than N days (default 30). Read-only — does NOT auto-archive (per ADR-0003 Amendment 1's diff-and-approve default). Run `archive <slug>` per candidate to archive. |

**Active-project state**: lives at `<content_root>/.pa-active-project.json`. The 4-hour staleness threshold (per ADR-0003 Amendment 1) means: if `status` reports STALE, treat the slug as cleared and prompt the user to explicitly `project resume <slug>` if they want to continue. Do NOT silently inherit a stale project's context.

**On `project new` / `project resume`**: after the tool runs, remember the active slug in your conversation context and prepend `export PA_PROJECT_ID=<slug> &&` to subsequent project-relevant Bash calls in this session — this is the env-var bridge across the assistant's separate Bash invocations (each is a new shell).

**On unknown `<project-subcmd>`**: print the valid project subcommand list and stop. Do NOT activate the skill or infer intent.

### `kb-process`

KB candidate-memo consumer (per parent [#116](https://github.com/acardote/personal-assistant-ultra/issues/116), child [#121](https://github.com/acardote/personal-assistant-ultra/issues/121)) — interactively walks the unprocessed memos that `tools/kb-scan.py` (slice 2 / #119) emitted and applies approved diffs to the right kb file under the standard diff-and-approve gate.

Parse the SECOND whitespace-separated token of `$ARGUMENTS` as the kb-process subcommand:

| kb-process subcommand | Dispatch | Notes |
|---|---|---|
| `list [--json]` | `tools/kb-process.py list [<flag>]` | Prints `art-id [kind] referent` rows for `<vault>/artefacts/memo/.unprocessed/`. |
| `show <art-id>` | `tools/kb-process.py show <art-id>` | Prints a memo body (the proposed diff lives inside). |
| `apply <art-id>` | `tools/kb-process.py apply <art-id>` | Appends the diff to the right kb file with inline `<!-- produced_by: session=<current> ... via=<art-id> -->`. Runs lint-provenance — refuses + rolls back on failure. Moves memo to `.processed/`. |
| `reject <art-id> [--reason TEXT]` | `tools/kb-process.py reject <art-id> [--reason TEXT]` | Moves memo to `.rejected/` without touching kb. |

**Walk pattern (the assistant's job)**: after the user runs `/personal-assistant kb-process`, the assistant calls `kb-process list` to see candidates, then for each:
1. Call `kb-process show <art-id>` and surface the memo body to the user.
2. Ask the user "approve / skip (review later) / reject?".
3. On approve → `kb-process apply <art-id>` (current `PA_SESSION_ID` is automatically used as the inline session_id).
4. On skip → leave in `.unprocessed/` for the next run.
5. On reject → `kb-process reject <art-id>` (optionally with `--reason`).

After all candidates processed, run `tools/live-commit-push.sh <content_root> "kb: <summary of approved candidates>"` to land the kb edits.

**Glossary candidates**: `kb-process apply` refuses these — glossary uses PR-only provenance against the method repo. Surface the proposed diff to the user and tell them to open a PR manually.

**On unknown `<kb-process-subcmd>`**: print the valid list and stop.

## Empty / unknown response template

When `$ARGUMENTS` is empty or unknown, respond with EXACTLY this shape (substituting the literal subcommand list):

> Available subcommands: `metrics`, `freshness-check`, `harvest`, `live-writeback`, `project`, `kb-process`. Pass one as the first token of `/personal-assistant <subcommand> [flags]`.

No skill activation, no clarifying question, no inference.
