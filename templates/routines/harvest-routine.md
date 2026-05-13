# Harvest routine — canonical configuration

This is the artifact-of-record for the production scheduled harvest, per [#25](https://github.com/acardote/personal-assistant-ultra/issues/25). The actual routine is created by the user via `/schedule` in Claude Code or at https://claude.ai/code/routines; this file documents what the user should configure so a fresh-clone setup is reproducible. Architectural rationale (why routines, why not launchd, what trade-offs were accepted) is captured in [ADR-0002](../../docs/adr/0002-scheduled-harvest-trigger.md).

## Why routines (not launchd)

Claude Code routines run on Anthropic's web infrastructure (verified end-to-end by probes 1–6 on 2026-05-05; canonical reference: probe `trig_012bbTLE2G6RYFsncQH89Ysy` for MCP discovery + `trig_01E63nUVn7TsfKVdCTnZbjHJ` for Granola body extraction). They:

- Fire on schedule even when your laptop is closed or off.
- Push commits to the vault via **`git push` against a per-fire feature branch + `gh pr create` for merge** — restored as the primary write transport in [#178](https://github.com/acardote/personal-assistant-ultra/issues/178) (slice 1 in [#179](https://github.com/acardote/personal-assistant-ultra/issues/179)) after the 2026-05-08→2026-05-11 sandbox proxy GitHub-identity swap blocked direct push to `main` but left feature-branch push intact for the `acardote` principal. The earlier MCP `push_files` transport (per [#153](https://github.com/acardote/personal-assistant-ultra/issues/153)) remains as the documented fallback when `git push` or `gh pr create` fail authentication.
- Auto-attach your account-level MCP connectors (Slack, Gmail, Granola, GitHub all confirmed reachable from the routine sandbox).
- Draw down the same Claude subscription as interactive sessions (no separate billing).
- Are subject to per-tier daily limits (Pro: 5/day, Max: 15/day, Team/Enterprise: 25/day).

**Choose ONE scheduler** — do not run routines and launchd against the same vault simultaneously. They will race on writes to the vault's `main` branch (launchd via direct `git push` to `main`; routine via `git push` to a feature branch + PR merge into `main`) and the dedup state files (`.harvest/<source>.json`) are last-writer-wins JSON. The launchd-based path (`templates/launchd/`) remains as an **alternative** for users without routine eligibility (lower tiers, certain enterprise restrictions) or who prefer strictly local execution. If you switch, disable the previous scheduler before enabling the new one.

**Sources NOT covered by the routine path**: Google Meet folder watch and generic transcript drop. These are file-system-based sources that need either local Meet-export sync or a folder you drop transcripts into — neither exists in the routine sandbox. If you depend on those sources, either keep launchd active alongside routines for those sources only (with the same vault — but bear in mind the lock-and-race caveat above), or run them ad-hoc via `tools/harvest.py --source gmeet|transcripts --folder <path>` from a Mac session.

### Architectural caveat: §11 trade-off accepted

The routine path is, by construction, an LLM session executing the prompt below. There is no shell harness wrapping the LLM that could enforce gates *before* it runs. As a result, the in-prompt PREFLIGHT block is an instruction the LLM is expected to follow, not a deterministic gate that prevents harvest if it fails. This is a structural §11 dependency on LLM compliance — flagged explicitly by adversarial review on PR #26 (round 3), and accepted as a property of the routine architecture rather than a fixable issue. Mitigations:

- The launchd alternative path (`templates/launchd/`) does have a deterministic Python wrapper (`tools/scheduled-harvest.py`) that can grow real preflight gates if needed; users who require enforced gates should use that path.
- The routine prompt's preflight is structured to make the abort decision binary on the model's side: probe-call returns either a normal response or a discovery-error, with explicit "do not exercise judgment" framing — minimizing the surface where model latitude could mis-classify a missing connector as "fine to skip."
- If the routine ever produces `ok: true` with a critical connector missing (the falsifier for "the LLM follows preflight reliably"), that is observable in the run-status JSON and constitutes evidence that the §11 trade-off has bitten us — and at that point the principled response is to move the production trigger back to launchd.

## Configuration

When you create the routine via `/schedule`, configure it as below.

### Schedule

- **Recurring cron**: `7 7 * * *` (daily at 7:07 UTC). The off-the-hour minute is the schedule skill's anti-stampede convention. Adjust to your timezone — the cron expression is in UTC, so for 7am Europe/Lisbon use `7 6 * * *` in summer (DST) or `7 7 * * *` in winter. The routine confirmation flow will echo the converted local time before saving.
- Routines have a 1-hour minimum interval; daily-or-coarser is the sweet spot for harvest cadence.

### Linked repos

Two `git_repository` sources, in this order:

1. `https://github.com/acardote/personal-assistant-ultra` (method repo — contains `.claude/skills/personal-assistant/SKILL.md`, `tools/compress.py`, schemas, prompts). Same for every user.
2. `https://github.com/<your-org>/<your-vault>` (content vault — destination for memory objects, KB, harvest state). **This is your private vault — replace with your own.** The author's example for reference: `https://github.com/getnexar/acardote-pa-vault`.

The routine workspace clones both. The vault is the git-write target.

### MCP connectors

Auto-attached at routine create time (no manual config in the create body needed) — verified by probes 2/3/5 against `trig_012bbTLE2G6RYFsncQH89Ysy` on 2026-05-05:

- Slack (account-level connector at https://mcp.slack.com/mcp).
- Gmail (account-level connector at https://gmailmcp.googleapis.com/mcp/v1).
- Granola (account-level connector at https://mcp.granola.ai/mcp). Exposes `query_granola_meetings` (required `query` string; optional `document_ids` UUID array). Connector must be **authenticated** in claude.ai (not just enabled) — probes 3 and 4 showed Granola absent from the auto-attach list when only enabled but unauthenticated, and present once authenticated. Probe 6 confirmed the tool returns full meeting bodies (sections, discussion, action items), not just metadata. Recommended invocations: `{"query": "Show me the full notes from my most recent meeting"}` for cold-start cycling through recent meetings; `{"query": "Show me all notes and action items from <title>"}` for title-targeted; `{"document_ids": ["<uuid>"]}` once IDs are known. Beware a ~60s timeout on long natural-language queries — back off and retry once at most.

If you have additional connectors, the API attaches them automatically based on your account's connected (and authenticated) list. Verify the create response's `mcp_connections` array matches expectations before declaring the routine ready.

### Allowed tools

`Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep` — covers the orchestration's needs (filesystem, shell, structured edits).

### Model

`claude-opus-4-7` (default). The harvest's multi-step orchestration (preflight, per-source enumeration, compress, live-writeback, kb-scan, kb-drift-scan, batched MCP push) benefits from Opus's stronger prompt-following discipline — see [#165](https://github.com/acardote/personal-assistant-ultra/issues/165) for the assumption ledger and validation.

**Cost note**: Opus is roughly 5× Sonnet per token (both input and output) at current pricing. A daily steady-state harvest is small (single digits of LLM calls outside kb-drift-scan), but `tools/kb-drift-scan.py` caps at 100 LLM calls per fire by default — that cap dominates per-fire cost. Sonnet 4.6 / Haiku 4.5 are valid overrides if you want the cheaper path and accept the discipline tradeoff. The watchdog routine (`templates/routines/watchdog-routine.md`) appropriately runs on Haiku 4.5 — model choice should match workload, not just default everywhere.

**Runtime override**: to swap models on an already-created routine without re-creating it, partial-update via `RemoteTrigger` with `{"job_config": {"ccr": {"session_context": {"model": "<id>", ...}}}}`. The `session_context` block needs the full set (`allowed_tools`, `model`, `sources`) — partials of that subobject are rejected.

### Routine prompt

Copy this verbatim into the routine's prompt field. It's self-contained — the routine starts with zero conversational context, so the prompt has to carry everything.

```
Run the personal-assistant scheduled harvest.

Two repos are linked to this routine:
- METHOD: acardote/personal-assistant-ultra (cloned to your workspace)
- VAULT:  getnexar/acardote-pa-vault (cloned to your workspace)

Identify the workspace paths. Use `find` constrained to known workspace roots, NOT the whole filesystem:

  METHOD=""
  VAULT=""
  for root in /root /workspace /home/user; do
    [ -d "$root" ] || continue
    [ -z "$METHOD" ] && METHOD=$(find "$root" -maxdepth 4 -name personal-assistant-ultra -type d 2>/dev/null | head -1)
    [ -z "$VAULT" ]  && VAULT=$(find "$root" -maxdepth 4 -name '*-pa-vault' -type d 2>/dev/null | head -1)
  done
  if [ -z "$METHOD" ] || [ -z "$VAULT" ]; then
    echo "FATAL: could not resolve METHOD ($METHOD) or VAULT ($VAULT) — aborting" >&2
    exit 1
  fi
  if [ "$METHOD" = "$VAULT" ] || [[ "$VAULT" == "$METHOD"/* ]]; then
    echo "FATAL: VAULT must be distinct from METHOD (got METHOD=$METHOD VAULT=$VAULT)" >&2
    exit 1
  fi
  export METHOD VAULT

  # Mint a session id for this routine run (per #116 slice 5). Tools that
  # emit metrics events or kind=memo artefacts (kb-scan) use this so the
  # routine's outputs share one session_id. The user's interactive
  # session_id is what later carries through to kb edits when candidates
  # are approved via kb-process — that's the F3 closer from #121.
  export PA_SESSION_ID=$(openssl rand -hex 4)

If either resolution fails, exit immediately — do NOT continue with empty paths. The fail-fast is essential: a silent fallback to cwd would write a runaway `.assistant.local.json` and produce a successful-looking run with zero real output.

Write a per-checkout config so the method-repo tools resolve content paths against the vault:

  cat > "$METHOD/.assistant.local.json" <<EOF
  {"paths": {"content_root": "$VAULT"}}
  EOF

**PREFLIGHT — run BEFORE any harvest work. This is a binary gate, not a soft check.**

Apply these rules mechanically. Do not exercise judgment about whether a failure is "probably fine to skip."

For each critical connector (Slack and Granola — the two highest-volume sources), attempt one cheap probe call:

- Slack probe: invoke `slack_search_users` with `{"query": "preflight"}` (any user query — return result is irrelevant).
- Granola probe: invoke `query_granola_meetings` with `{"query": "preflight"}`.

For Gmail (medium volume), make a probe call as well but treat failure as warning-only (skip Gmail with note, continue with others).

Decision rules:

1. If a probe call errors with "tool not found" / "no such tool" / "not in tool list" / a discovery-level error, treat the connector as missing.
2. If a probe call returns a normal response (any payload, any size), preflight passes for that connector.
3. If a probe call returns a transient error (timeout, 5xx, rate limit, network error), wait 30 seconds and retry once. If it fails twice, treat as missing.

**Abort condition**: if Slack OR Granola is missing after the rules above, immediately:

  - Write `$VAULT/.harvest/runs/$(date -u +%Y-%m-%dT%H%M%SZ).json` with `{"started_at": "<now>", "ok": false, "scheduler": "routine", "phase": "preflight", "error": "critical connector missing: <slack|granola>", "ended_at": "<now>"}`.
  - **Push that single file via the GitHub MCP `push_files` tool** — marker pushes stay on MCP push_files (NOT the new feature-branch primary) because a one-file marker doesn't justify the branch + PR ceremony. Direct `git push` to `main` would 403 here per #178 (proxy `acardote` principal has no main-branch-protection bypass). If `push_files` itself errors out (tool not attached / 4xx / 5xx-after-retry), emit `echo "FATAL: preflight abort run-status could not be pushed — both transports unavailable; failure visible only in routine logs" >&2` so the routine log carries the signal even though no cross-machine surface will.
  - Exit. **Do NOT proceed to harvest.** A successful-looking partial run with a critical source missing is worse than a clean failure.

The most likely cause of a missing critical connector is "the connector is enabled in claude.ai but not authenticated." Surface that explicitly in the error so the user knows where to look.

Also detect dual-active scheduler conflict. List `$VAULT/.harvest/runs/*.json` files modified within a window of `max(60min, 2 × your-routine-cadence-minutes)` — the larger window catches a launchd run from this morning when this routine fires this evening. If any matching file has `"scheduler": "launchd"`, append a `"warnings": ["launchd active alongside routine — pick one to avoid race conditions on writes to main and dedup state"]` entry to your run-status JSON. (Do not abort — the user may intentionally have both running for the Meet/transcript-drop workaround. Just surface the conflict.) The window may produce false negatives if the user's launchd cadence is sparser than 2x the routine cadence — accept this as best-effort detection.

Determine harvest cutoff. **This rule is load-bearing for gap recovery** — if the last successful push was N days ago (e.g. transport was broken or routine fires failed), the next successful fire must recover all N days, not just yesterday. Per [#152](https://github.com/acardote/personal-assistant-ultra/issues/152):

1. **List runs files matching the canonical name pattern.** Match strictly: `runs/YYYY-MM-DDTHHMMSSZ.json` (regex: `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z\.json$` on the basename). Skip ANY file that doesn't match — backup files, stale launchd-format files, hand-edits all get filtered. This shape is the one the shell-substituted `$(date -u +%Y-%m-%dT%H%M%SZ).json` upstream writes and `tools/scheduled-harvest.py:217` writes; nothing else is in-scope. The strict regex pins the lexicographic-equals-chronological invariant.

2. **Sort newest-first by filename** (lexicographic sort is correct under the strict regex above) and walk the list:
   - For each file: parse JSON. **If parsing fails, treat the file as `ok: false`** (corrupt → does not anchor). Do NOT abort the walk on parse errors; continue to the next file.
   - If `payload.get("ok") is True` (Python-truthy check on a literal boolean — missing or non-boolean `ok` counts as `ok: false`): this is the anchor candidate.
   - **No early termination on count.** Walk the entire list if necessary; only stop when the first `ok: true` is found OR every file has been examined.

3. **Anchor source** — and this is the LOAD-BEARING DECISION: anchor at the **filename timestamp** (parsed from the basename's `YYYY-MM-DDTHHMMSSZ` prefix), NOT the JSON's `started_at` field. Rationale: `started_at` is LLM-written inside the routine prompt and has been observed to drift forward by ~1 hour (Opus and Sonnet both, see #170 challenger evidence). The filename is shell-substituted (`date -u +%Y-%m-%dT%H%M%SZ`) upstream of the LLM and is the only timestamp on the artifact that's mechanically trustworthy.

   **How to parse the filename ts** (mechanical, no judgment):
   ```python
   import datetime, re
   m = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{6}Z)\.json$', basename)
   filename_dt = datetime.datetime.strptime(m.group(1), '%Y-%m-%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
   cutoff_iso = filename_dt.strftime('%Y-%m-%dT%H:%M:%SZ')   # ISO form with colons for the cutoff field
   ```
   The filename is compact (`061100Z`); the `cutoff` field on the run-status JSON is colon-separated ISO (`06:11:00Z`). Reformat explicitly — do not paste the basename into `cutoff` verbatim.

   If the LLM-written `started_at` in the file matches the parsed filename ts within ±5 minutes, that's confirmation; if it drifts more, prefer the filename ts and add a `"warnings": ["started_at_drift: file=<ts>, json=<ts>"]` entry so the divergence is visible.

4. **Same-second tiebreak** — two files with the same `YYYY-MM-DDTHHMMSSZ` (rare but possible — preflight-abort + commit-push partial in the same second): prefer the one whose `ok: true`. If both `ok: true` (unlikely), prefer the one with the most-recent `ended_at` field. If neither has `ended_at`, accept either deterministically (the lexicographic sort already does).

5. **If NO `ok: true` run is found** (empty directory, all `ok: false`, all parse-failures): this IS a cold start. Use "since 30 days ago".

Record the resolved cutoff in the run-status JSON's top-level `cutoff` field as an ISO timestamp (e.g. `"cutoff": "2026-05-08T06:11:00Z"`) or the sentinel `"30d cold-start"`. The canonical run-status schema (see "After all sources complete" block below) is extended to include this `cutoff` field. Do NOT use a calendar-yesterday string ("since yesterday", "last 24 hours") — that's the prior buggy rule that orphaned the 2026-05-08 → 2026-05-10T15:25Z window during the proxy-403 outage on [#153](https://github.com/acardote/personal-assistant-ultra/issues/153) (now the subject of backfill child [#172](https://github.com/acardote/personal-assistant-ultra/issues/172)).

**Runtime safety: if the anchor filename ts is more than 14 days old**, do NOT silently cap the harvest. That is the exact failure mode this whole rule is designed to eliminate. Instead: write `runs/<ts>.json` with `ok: false, phase: "cutoff", error: "anchor_older_than_14d_cap — file a deliberate backfill child (e.g. modeled on #172) to harvest this window via an explicit since parameter"`, push the marker via `push_files` (same transport as the commit-push procedure below). **If that `push_files` itself errors** (token-expired, MCP-unavailable, terminal 4xx), emit `echo "FATAL: anchor_older_than_14d_cap AND marker push failed — vault has no observable signal; check claude.ai/code/routines logs and file a backfill child manually." >&2` so the failure surfaces in the routine UI even though no cross-machine signal lands. Exit non-zero. The watchdog will surface STALE within 26h once the marker DOES reach the vault.

Open `$METHOD/.claude/skills/personal-assistant/SKILL.md` (it's the canonical orchestration spec) and follow its "Harvest orchestration" section for each enabled source. **Do not stop at the first few items per source — enumerate and fetch comprehensively** (per #34 — the original cold-start fetched only 9 items because the prompt was vague about completeness).

Per-source instructions (in this order):

**Slack** (via Slack MCP — UUID-namespaced tools):
- Use `slack_search_channels` with name patterns `external-*`, `customer-*`, `partner-*` to enumerate **ALL** matching channels — not just a few. Iterate `cursor` pagination until exhausted.
- Additionally: use `slack_search_public_and_private` for activity-driven channels not matching the prefix patterns. **MUST pass these parameters** (per #67 — the defaults silently miss most channel posts):
  - `query`: `from:<@<YOUR_SLACK_USER_ID>> after:<cutoff>` (substitute the user's Slack ID + cutoff date)
  - `sort`: `timestamp` (NOT default `score` — `score` ranks DMs above channel posts and they fall off page 1)
  - `channel_types`: `public_channel,private_channel` (exclude DMs/group-DMs; DM scope tracked in #68)
  - Paginate via `cursor` until exhausted. Do NOT stop at page 1.
- Additionally: search for threads carrying the user's `:pencil:` reaction (flag) regardless of channel — apply the **same `sort=timestamp`, `channel_types=public_channel,private_channel`, full pagination** parameters as the activity-driven query above (the pencil branch uses the same `slack_search_public_and_private` tool and inherits the same default-sort bug if you skip these).
- For each unique channel discovered, list threads since cutoff and call `slack_read_thread` per thread. Render each to `$VAULT/raw/slack_thread/<channel>-<thread_ts>.md`.
- Compress each via `tools/compress.py --kind thread --source-kind slack_thread`.
- Expected order of magnitude: tens of channels × multiple threads each = dozens of memory objects on a 30-day cold-start.
- **Hard floor (gate, not log)**: on cold-start, if you produce <5 Slack memory objects, this is a sign the enumeration was incomplete. Set `ok: false` on the run-status JSON with `error: "incomplete_slack_enumeration"` so the freshness check / watchdog surface it. Do NOT silently complete with low counts.

**Slack DMs and group-DMs** (via Slack MCP, per #68):
- Separate source kind from channel threads. Call `slack_search_public_and_private` with:
  - `query`: `after:<cutoff>` (NO `from:@me` — that would only capture DMs the user authored in; a DM where the counterpart did the talking would be invisible. Auth-scope visibility means the search only returns DMs the user is a participant in.)
  - `sort`: `timestamp`
  - `channel_types`: `im,mpim` (DMs and group-DMs only)
  - Paginate via `cursor` until exhausted.
- For each unique DM channel discovered, call `slack_read_thread` on the message's `(channel_id, message_ts)`. Render each to `$VAULT/raw/slack_dm/<channel-id>-<thread-ts>.md` with `## <iso> — user:<id>` headers per message; include a leading participants line so compress preserves the conversational structure.
- Compress each via `tools/compress.py --kind thread --source-kind slack_dm`. Memory lands at `$VAULT/memory/slack_dm/...`.
- Update `$VAULT/.harvest/slack_dm.json` with new dedup_keys (shape: `slack_dm-<channel-id>-<thread-ts>`).

**Gmail** (via Gmail MCP — UUID-namespaced tools):
- Use `search_threads` with `label:important newer_than:<since>` (or per-user override). Iterate paginated results until exhausted.
- For each: fetch via `get_thread`, render to `$VAULT/raw/gmail_thread/<thread-id>.md`, compress with `--kind email --source-kind gmail_thread`.
- Refuse broad inbox harvesting (F2 from #6) — `important` labeling is the user's curation signal.

**Granola** (via Granola MCP — `query_granola_meetings`):
- **Step 1 — enumerate**: call `query_granola_meetings` with `{"query": "List all my meetings since <cutoff-date>"}` (substitute `<cutoff-date>` with the actual ISO date, e.g. "2026-04-05"). Parse the response to extract titles, dates, and any UUIDs.
- **Step 2 — fetch each body**: for every meeting in the enumeration, call `query_granola_meetings` again with either `{"document_ids": ["<uuid>"]}` (preferred when UUIDs are returned) or `{"query": "Show me the full notes from <title> on <date>"}` (fallback when only titles are visible). Each call returns the body for ONE meeting.
- **Step 3 — write each**: render each meeting body to `$VAULT/raw/granola_note/<meeting-uuid-or-slug>.md`, compress with `--kind note --source-kind granola_note`.
- Probe 5 (2026-05-05) showed 40 meetings in 14 days — a 30-day cold-start should yield 30+ meetings unless the user has gaps.
- **Hard floor (gate, not log)**: on cold-start, if you produce <10 Granola memory objects, set `ok: false` on the run-status JSON with `error: "incomplete_granola_enumeration"`. The freshness check will surface it on the next user invocation; the watchdog routine will DM the user.
- Caveat: `query_granola_meetings` has a ~60s timeout on long natural-language queries.
- **Retry policy** (apply mechanically — no judgment): the single-meeting body queries are fast. The enumeration query is the risky one. For the enumeration call: if the first attempt times out, wait 30s and retry once with the same query. If THAT also times out (i.e., 2 timeouts on the same query), fall back to weekly-paginated enumeration:
  - Issue 4 separate `query_granola_meetings` calls, each with `{"query": "List all my meetings between <week-start> and <week-end>"}` substituting an explicit YYYY-MM-DD range for each of the 4 weeks of the 30-day cutoff window.
  - Each weekly call gets its own retry-once-on-timeout budget (i.e., up to 8 calls total in the worst case).
  - Concatenate results from all 4 weekly calls before proceeding to step 2 (per-meeting body fetch). The body-fetch loop is unchanged.
  - If a weekly call fails twice on the same week, log that week to errors and skip it (do not abort the whole harvest — partial coverage beats nothing).

**Google Meet transcripts**: skip in routine context — folder-watch path, needs local Meet-export sync.
**Generic transcript drop**: skip in routine context — same reason.

For each source's discovered items: write raw artifact to $VAULT/raw/<source-kind>/<id>.md, then invoke the compression pipeline:

  cd $METHOD && tools/compress.py $VAULT/raw/<source-kind>/<id>.md --kind <kind> --source-kind <source_kind>

Compress writes to $VAULT/memory/<source-kind>/, applies #10's clustering (event_id, is_canonical_for_event), and respects the per-kind expiry rules from #8.

Update $VAULT/.harvest/<source>.json dedup state. Append today's section to $VAULT/.harvest/daily/$(date -u +%Y-%m-%d).md following SKILL.md's daily digest format. Create $VAULT/.harvest/runs/$(date -u +%Y-%m-%dT%H%M%SZ).json with structured run status:

  {"started_at": "...", "ok": true|false, "scheduler": "routine", "cutoff": "<ISO-ts or '30d cold-start'>", "sources": {"slack": {"new": N, "errors": []}, ...}, "ended_at": "..."}

The `"scheduler": "routine"` field is mandatory — it is the marker the dual-active detection above grep's for. Match it exactly.

Bound the run by cadence:

- **Cold-start (no prior `runs/*.json` exists)**: budget ~30 minutes. A 30-day backfill across multiple sources with per-item compress.py LLM calls genuinely takes that long. Per #34, the original 10-minute budget caused the routine to stop at 9 items (vs. expected dozens-to-hundreds).
- **Steady-state (prior runs exist)**: budget ~10 minutes. Daily incremental harvest of 24h of new activity is small.

If a source is unreachable (MCP auth expired, tool not available), log to the digest's "errors:" line and the run-status JSON's "errors" key, then continue with other sources — do NOT retry.

**Live write-back catch-up (per #74)**: after the per-source harvest completes, run `tools/live-writeback.py` to absorb any artifacts the per-query path missed (machine off when a live finding was captured, skill exited ungracefully, push-collisions that didn't recover). Expected: typically 0-3 deferred artifacts per day in steady state; counts in the dozens during the rollout window.

**KB candidate scan (per #116 / #119)**: after the live write-back, run `cd $METHOD && tools/kb-scan.py` (no flags — incremental-since-watermark is the default) to walk new memory objects since the last scan watermark and emit candidate KB updates as `kind=memo` artefacts under `$VAULT/artefacts/memo/.unprocessed/`. Per ADR-0003 F2 (autonomous-producer carve-out), this step MAY emit memos but MUST NOT write to `$VAULT/kb/*` directly — those candidates land in the user's next interactive `/personal-assistant kb-process` session for the diff-and-approve flow.

`PA_SESSION_ID` is set earlier in this prompt; kb-scan reads it for the memo's `produced_by.session_id`. When the user later approves a candidate via `kb-process apply`, the inline kb provenance comment carries the user's interactive session_id, NOT this routine session (per #121's F3 closer).

If kb-scan crashes mid-run, do NOT retry — log to the digest's "errors:" line and continue with the git-commit step. The watermark is only advanced on clean exit, so the next run will retry from the same point.

Expected steady-state: 0-3 candidates per daily run. Bootstrap (full memory pool) is invoked manually via `/personal-assistant kb-backfill`, NOT from this routine.

In the daily digest entry, after the per-source counts, add: `- kb candidates: N pending review` where N = count of files matching `$VAULT/artefacts/memo/.unprocessed/art-*.md` after the kb-scan step. The line is always present (omit no zero-state — the consistency makes the digest scannable).

**KB drift scan (per #135 / #138)**: after kb-scan, run `cd $METHOD && tools/kb-drift-scan.py` (no flags — incremental-since-watermark is the default). Walks new memory objects since the last drift-scan watermark and intersects them against your vault's `kb/decisions.md` (`<content_root>/kb/decisions.md`) entries on their `**Scope:**` field, then runs `claude -p` per surviving (memory, decision) pair to judge drift; emits drift candidates as `kind=memo` artefacts under `$VAULT/artefacts/memo/.unprocessed/` with `drift_candidate: true`.

Drift detection is **always against landed kb decisions, NEVER against un-applied candidates** (closes F2 of slice 5 of #135). `kb-drift-scan.py` reads your vault's `kb/decisions.md` (`<content_root>/kb/decisions.md`), not the unprocessed memo directory — un-applied kb-scan candidates from earlier in the same run are invisible to drift-scan by construction.

`PA_SESSION_ID` is shared with kb-scan; emitted drift memos carry that session as their `produced_by.session_id`. When the user later approves a drift candidate via `/personal-assistant kb-process drift-apply`, the inline kb amendment carries the user's interactive session_id (per slice 3 / F4 closer).

**Distinguish three exit conditions** — this matrix is the dispatcher; everything below references it rather than restating the rule:

- **rc ≠ 0 (CRASH)**: tool aborted mid-run. Append a single error line to the digest: `- Errors: kb-drift-scan crashed (<rc>): <one-line stderr summary>`. For the summary, use the kb-drift-scan summary line (it starts with `[kb-drift-scan] `) if present in stderr; otherwise the first stderr line. Do NOT attempt to extract Python tracebacks — it's not the routine's job. Do NOT append the drift-count or quota lines (a count of `.unprocessed/` post-crash is misleading — the scan didn't finish). Watermark advances only on clean exit, so the next run resumes.
- **rc = 0 with `skipped_for_quota=N > 0` in stderr** (NOT a crash — clean exit, cap fired): append the drift-count line AND the quota-exhaustion line, per the templates below. F3 closer of #141: a green digest must NOT hide that detection didn't fully cover the pair pool.
- **rc = 0 with `skipped_for_quota=0`**: clean run. Append the drift-count line only.

Drift-count line template (used by both rc=0 branches above): after the `kb candidates` line, add `- kb drift candidates: M pending review` where **M = `cd $METHOD && tools/kb-process.py list --count-drift`**. Per F4 of #141, the count MUST use the `kb-process list --count-drift` helper rather than a bash glob; the helper checks `frontmatter.get("drift_candidate") is True` so memos with `drift_candidate: false` (or no drift field) are NOT counted.

Quota-exhaustion line template (used only by the second branch): immediately after the drift-count line, append `- kb drift: scan quota exhausted, N pairs unscanned (next run resumes)`.

Expected steady-state: 0-5 drift candidates per daily run (most pairs are cached or judged not-drifted). Cold-start fills the cache incrementally over multiple fires (subject to the default `--max-llm-calls=100` cap).

After all sources complete (including the live write-back catch-up + kb-scan + kb-drift-scan), commit the harvest to the vault via **`git push` to a per-fire feature branch + `gh pr create` for merge to main**. Rationale: [#178](https://github.com/acardote/personal-assistant-ultra/issues/178) — direct `git push` to `main` from the sandbox returns 403 (proxy `acardote` principal has no branch-protection bypass after the 2026-05-08→2026-05-11 identity swap), but feature-branch push and `gh pr create` both work under that same identity. The PR also gives every harvest fire an audit trail + adversarial-review hook (the merge step is slice 2 of #178; this slice 1 stops at PR-open).

**Slice 1 stop boundary**: this procedure ships the harvest to a feature branch and opens a PR. It does NOT auto-merge to `main`. Until slice 2 of #178 lands the auto-merge wiring, the operator is responsible for merging the PR after the routine finishes (the final user-facing message will surface the PR URL). The MCP `push_files` procedure is preserved further down as the documented fallback when `git push` or `gh pr create` fail authentication.

Procedure:

1. **Determine vault repo coordinates** from the local checkout (the linked-repo config is the source of truth — works for any vault, not just the author's):

       VAULT_URL=$(git -C "$VAULT" config --get remote.origin.url)
       VAULT_OWNER=$(echo "$VAULT_URL" | sed -E 's|.*github\.com[/:]([^/]+)/.*|\1|')
       # Strip a trailing .git and an optional trailing / from the last URL segment.
       # Uses .+? rather than [^/.]+ so repo names containing a dot (e.g. foo.bar.io) parse correctly.
       VAULT_REPO=$(echo "$VAULT_URL"  | sed -E 's|.*github\.com[/:][^/]+/(.+)|\1|; s|\.git/?$||; s|/$||')
       # Positive-match guards. If sed didn't match the input, $VAULT_URL passes through unchanged;
       # these checks catch that (and other malformed inputs) before they reach the gh CLI.
       if [ -z "$VAULT_OWNER" ] || [ -z "$VAULT_REPO" ] \
          || [ "$VAULT_OWNER" = "$VAULT_URL" ] || [ "$VAULT_REPO" = "$VAULT_URL" ] \
          || [[ "$VAULT_OWNER" == *"/"* ]] || [[ "$VAULT_REPO" == *"/"* ]]; then
         echo "FATAL: could not parse vault owner/repo from $VAULT_URL (got OWNER=$VAULT_OWNER REPO=$VAULT_REPO)" >&2
         exit 1
       fi

2. **Discover changed files** via the local checkout's git index. Use `-z` (NUL-delimited records) and `--untracked-files=all` (the default `normal` mode collapses brand-new top-level directories like `artefacts/memo/.unprocessed/` into a single trailing-slash entry — exactly the case `git add -A` would expand to per-file):

       cd "$VAULT"
       git status -z --porcelain --untracked-files=all

   Parse the output as NUL-separated fields with a *status-dependent record arity* (this matters — naive split-on-NUL with a fixed-arity assumption breaks on rename/copy entries):

   - **Most statuses** (`A`, `M`, `D`, `??`, etc.) consume ONE NUL-separated field per record. The field's leading 3 bytes are the two status chars + the separator space; bytes 4 onward are the raw path, verbatim — no octal-escape quoting, no surrounding double-quotes (that's what `-z` buys you).
   - **Rename `R` and copy `C`** consume TWO NUL-separated fields per logical record. The first field carries the status prefix + the NEW path; the second field is the OLD path with no status prefix. Treat the second field as part of the SAME record, not as a new record starting at byte 0.

   The first-field status prefix is exactly 3 bytes for *any* status (`A `, `??`, `RM`, `R `, etc.) — the two status chars may include a space (e.g. `R ` or `M `), followed by one literal space separator. Strip those 3 bytes to get the path.

   Verified shape on a test repo: `?? memory/slack_thread/André's-team.md\0` — non-ASCII bytes are passed through unchanged. <!-- legacy -->

   `git status` respects `.gitignore`, so `raw/` (per [ADR-0001](../../docs/adr/0001-storage-backend.md) + the vault's `.gitignore`) is excluded by construction — no special handling needed. The relevant statuses for harvest output are `A` (new file), `M` (modified), `??` (untracked); harvest does not produce deletions in normal operation, so `D` is unexpected — if you see one, log a warning to stderr and skip that path rather than try to delete via `push_files` (the tool's delete semantics differ across MCP server versions; out of scope here).

   **Renames / copies**: harvest doesn't produce these in normal operation. If you see one (per the two-field rule above), push the NEW path's content via `push_files` and treat the OLD path as a deletion (log to stderr, skip — the runs file will note the inconsistency).

   **Binary files**: today, all harvest outputs are Markdown or JSON (text). If the Read tool refuses a file as binary, OR the first 4 KB of file bytes contains a NUL byte, skip that path with a stderr warning and add `{"kind": "binary_skipped", "path": "..."}` to the run-status `errors` list. `push_files`'s `content` field expects a UTF-8 string; passing a binary-refusal string would silently corrupt the file in the vault.

   **If `git status` returns an empty result**: harvest produced no new files. Write `$VAULT/.harvest/runs/<ts>.json` locally with `ok: true, scheduler: "routine"`, populate the `sources` object with zero-shaped per-source entries (`"slack": {"new": 0, "errors": []}` etc.), and `notes: "harvest produced no new files"` — i.e. the SAME schema specified earlier in this prompt for the success-path run-status JSON, not a different shape. Then re-run `git status -z --porcelain --untracked-files=all`; the runs file will now appear as `??` and step 3 has a single-file payload to push for freshness-check visibility.

3. **Derive a unique branch name** from the run-status timestamp (already established convention for `runs/<ts>.json`):

       RUN_TS=$(date -u +%Y-%m-%dT%H%M%SZ)
       BRANCH="harvest-$RUN_TS"

   The second-resolution ts is the same one used for `.harvest/runs/<ts>.json`. If a fire retries (same-session re-run, rare), reusing the branch is safe with `--force-with-lease` (see error matrix). If two unrelated fires somehow generated the SAME `RUN_TS` (extremely improbable since the routine waits for completion before allowing another fire), the second fire's branch-create or push fails — the error matrix surfaces it as a hard error, not a silent overwrite.

4. **Create the branch + stage harvest paths**. Stage the same paths as `tools/live-commit-push.sh` (`memory/ .harvest/ kb/ artefacts/`); `raw/` is `.gitignore`'d per [ADR-0001](../../docs/adr/0001-storage-backend.md). Stage paths individually because `git add a b c` aborts on the first nonexistent path:

       cd "$VAULT"
       git checkout -b "$BRANCH"
       for path in memory/ .harvest/ kb/ artefacts/; do
           [ -e "$path" ] && git add "$path" 2>/dev/null || true
       done
       # Sanity: refuse to proceed if nothing got staged (shouldn't happen — step 2 verified ≥1 changed file).
       if git diff --cached --quiet; then
           echo "FATAL: no files staged after $(date) despite git status reporting changes; aborting before commit." >&2
           git checkout main
           git branch -D "$BRANCH"
           exit 1
       fi

5. **Commit the harvest**. The `(routine)` suffix in the subject disambiguates routine commits from hand-authored ones in `git log` / `git blame`:

       git commit -m "harvest $(date -u +%Y-%m-%d) (routine, $BRANCH)"

   Capture the resulting commit SHA: `SHA=$(git rev-parse HEAD)`. Echo to stderr so the run log carries it: `echo "harvest commit: $SHA on $BRANCH" >&2`.

6. **Push the branch to origin**:

       git push -u origin "$BRANCH"

   Apply the error matrix in step 9 below if push fails.

7. **Open the PR via `gh pr create`**. The PR body is short — it surfaces the commit + the branch + the runs.json path so the operator can spot-check before merging. Slice 1 of #178 stops at PR-open; slice 2 will wire auto-merge after adversarial review:

       gh pr create \
           --base main \
           --head "$BRANCH" \
           --title "harvest $(date -u +%Y-%m-%d) (routine)" \
           --body "$(printf 'Routine harvest fire %s.\n\n- Commit: %s\n- Branch: %s\n- Runs JSON: .harvest/runs/%s.json\n\nMerge after reviewing the diff. Adversarial-review automation lands in slice 2 of #178.\n' "$RUN_TS" "$SHA" "$BRANCH" "$RUN_TS")"

   Capture the resulting PR URL: `PR_URL=$(gh pr view "$BRANCH" --json url --jq .url)`. Echo to stderr: `echo "harvest PR: $PR_URL" >&2`.

   Apply the error matrix in step 9 below if `gh pr create` fails.

8. **Write the run-status JSON** at `$VAULT/.harvest/runs/<RUN_TS>.json` with the canonical schema (`started_at, ok: true, scheduler: "routine", sources, ended_at`) PLUS the new `push` block carrying the transport, branch, PR URL, and commit SHA:

       {
         "started_at": "...",
         "ok": true,
         "scheduler": "routine",
         "sources": { ... },
         "ended_at": "...",
         "push": {
           "transport": "git-feature-branch",
           "branch": "<BRANCH>",
           "pr_url": "<PR_URL>",
           "commit_shas": ["<SHA>"]
         }
       }

   The runs JSON itself is part of the harvest — meaning it lands AFTER the branch is pushed but BEFORE the routine ends. Two options:

   a) **Write-then-amend** (preferred for slice 1): write the runs JSON locally after step 7, then `git add .harvest/runs/<RUN_TS>.json && git commit --amend --no-edit && git push --force-with-lease origin "$BRANCH"` to fold it into the same commit. This keeps the branch a single-commit PR (cleaner merge).

   b) **Two-commit branch** (alternative): commit the harvest first, push, open PR, then commit the runs JSON as a second commit on the same branch and push again. The PR shows 2 commits.

   Default to (a). If `--amend` or `--force-with-lease` fails (no clean operator override on the routine sandbox), fall through to (b).

9. **Error matrix** (apply mechanically — no judgment). All run-status JSONs landed in this section use the SAME canonical schema as the success path and may carry forward-compatible diagnostic extension fields (`phase`, `error`, `push.fallback_reason`, etc.).

   - **`git push` network failure** (timeout, DNS, transient connection reset): retry the same push once after 30 seconds. If the retry succeeds, continue. If the retry also fails, fall back to MCP `push_files` (procedure below). Annotate `runs/<ts>.json` `push.transport: "mcp-push-files"`, `push.fallback_reason: "git_push_network_failure"`, `push.original_branch: "<BRANCH>"` (so the operator can tell the routine TRIED feature-branch first).
   - **`git push` returns 403 on feature branch** (A1 of #178 falsified — the proxy auth has changed again or the `acardote` principal has lost feature-branch write): fall back to MCP `push_files`. Annotate `push.transport: "mcp-push-files"`, `push.fallback_reason: "git_push_403_on_feature_branch_a1_falsified"`. Emit `echo "WARN: git push 403 on feature branch — #178 A1 (proxy auth stability) falsified. Falling back to MCP push_files. File evidence on #178." >&2` so the falsification is visible in the routine UI.
   - **`git push` returns "remote branch already exists" or non-fast-forward**: distinguish two sub-cases:
     - Sub-case A — *same `RUN_TS` retry* (same-session re-run after a partial failure): the branch you're pushing to is your own branch from the prior attempt. Retry with `git push --force-with-lease origin "$BRANCH"` (safe — `--force-with-lease` refuses if the remote has changed under you). If that succeeds, continue. If `--force-with-lease` itself refuses, treat as Sub-case B.
     - Sub-case B — *unrelated collision* (extremely improbable since `RUN_TS` is second-resolution and the routine waits for completion before allowing another fire): write `runs/<ts>.json` `ok: false, phase: "commit_push", error: "branch_collision_unrelated_runid"`, fall back to MCP `push_files`. Annotate `push.fallback_reason: "branch_collision"`.
   - **`gh pr create` failure** (auth, rate limit, API 4xx): the branch was already pushed in step 6, so the harvest data IS on GitHub — just not in a PR. Two options, in order:
     - First, retry `gh pr create` once after 30 seconds.
     - If retry fails, leave the branch as-is (don't tear it down), write `runs/<ts>.json` `push.transport: "git-feature-branch"`, `push.pr_url: null`, `push.pr_create_error: "<short error>"`. Emit `echo "WARN: branch pushed to $BRANCH but gh pr create failed: <err>. Operator must manually open the PR or merge the branch directly. Surface in final response." >&2`. Continue — the harvest data is durable on the feature branch; merge-via-manual-PR is the operator recovery path. **Do NOT fall back to MCP push_files** in this branch — the data is already on GitHub via `git push`; falling back would duplicate-commit the harvest content under a different transport.
   - **MCP `push_files` fallback path** (triggered by the `git push` failure branches above): see the "MCP `push_files` fallback" section below. The fallback procedure is unchanged from the prior MCP-as-primary implementation (it's verbatim what shipped in #161 / v0.4.2, including the 401-retry-on-demand-pause flow on #166); slice 1 just demotes it from primary to fallback.

10. **In your final user-facing response, surface the PR URL prominently** so the operator can review + merge. Template:

        Harvest complete (routine, $RUN_TS).
        - Sources: <one-line per-source summary>
        - Commit: $SHA on branch $BRANCH
        - PR: $PR_URL
        - Runs JSON: .harvest/runs/$RUN_TS.json
        - **Action**: merge $PR_URL after reviewing the diff. Auto-merge wires in slice 2 of #178.

   If the fallback path was hit, surface the fallback reason in place of the PR URL: e.g., `Push fell back to MCP push_files (reason: <fallback_reason>). Commit landed directly on main.`

### MCP `push_files` fallback

Triggered only by the error-matrix branches in step 9 that explicitly fall back. The procedure is unchanged from #161 / v0.4.2 (the original commit-push procedure shipped before the proxy auth swap was diagnosed):

1. Build the `files[]` payload from the staged paths (same construction as the prior MCP-primary implementation: read each path's content, append `{"path": "...", "content": "..."}` to a list in step-2 order, move the `.harvest/runs/<ts>.json` entry to the END of the list).
2. Batch at the 30-file cap (per A2 on #153). Push each batch as a separate `push_files` call.
3. Apply the prior error matrix:
   - **Tool not found / discovery error**: write `runs/<ts>.json` `ok: false, phase: "commit_push", error: "github_mcp_push_files_unavailable"`. The git-push primary already failed; both transports are down. Emit `echo "FATAL: both git-push and github_mcp_push_files unavailable — run-status was written locally but cannot reach the vault." >&2` and exit non-zero.
   - **Transient error (5xx, timeout, rate limit)**: retry once after 30s. On 401-on-retry, jump to the 401 branch.
   - **401 / token expired (per #166)**: persist deferred batches to `${TMPDIR:-/tmp}/harvest-<ts>.batches.json`, write the partial-run marker, attempt one single-file marker push, prompt the user with the exact text `*Routine paused — GitHub MCP token expired.* Pushed N of M batches successfully...`, pause for 5-min reply on `retry|go|resume`, fall through to FATAL exit if no reply.
   - **Terminal non-401 4xx**: write the runs JSON `ok: false, phase: "commit_push", error: "<MCP error code + first 2000 chars>"`, attempt one single-file runs-only push, exit if that also fails.

The 401-on-demand-pause flow (#166) is the load-bearing reason MCP `push_files` was the primary transport between #161 and #178. With `git push` as primary now, the 401 flow is reached only when BOTH transports fail in the same fire — much narrower failure surface.

### Race semantics

The historical concern (recorded for the MCP-primary era): `push_files`'s `updateRef` step's fast-forward enforcement varies across MCP server builds, which made cross-scheduler racing (routine + launchd both targeting `main`) potentially silently-clobbering.

Under the new feature-branch primary: `git push` to a per-fire feature branch CANNOT race launchd's direct-push-to-main — they target different refs. The PR merge step (slice 2) re-introduces the race surface, but only at merge time (a single small commit, much smaller window than the harvest itself). **The "Choose ONE scheduler" discipline remains load-bearing** for the dedup-state-file race; the branch-collision-on-`main` race is now structurally eliminated for the harvest payload itself.

In your final response, summarize:
- Which sources fired and what each produced.
- Any errors encountered.
- Whether `git push` + `gh pr create` succeeded, including the **PR URL** for operator review/merge, the commit SHA, and the branch name. If the MCP fallback fired, the fallback reason + commit SHA(s) from `push_files`.
- The run-status JSON path.
```

## On-demand variants

The same prompt works for ad-hoc on-demand harvest:

- Trigger via the routine's "Run now" action in claude.ai/code/routines.
- Or use the local wrapper at `tools/scheduled-harvest.py` which runs `claude -p` on your machine — useful for quick "harvest since lunch" runs from a terminal without consuming a routine slot.

## Updating

If the routine prompt or schedule needs to change:

- Edit this file (the canonical artifact-of-record).
- Update the routine via `RemoteTrigger` `action: update` or via claude.ai/code/routines UI.
- The two should match. If they drift, this file is wrong (or the routine is wrong); reconcile.

## Daily limits

- Pro: 5 routine runs / day. Daily-cadence routine + ~4 on-demand runs.
- Max: 15 / day. Plenty of headroom.
- Team / Enterprise: 25 / day.

If you regularly hit the limit, prefer on-demand via `tools/scheduled-harvest.py` over "Run now" — local-wrapper runs don't count against routine quota.
