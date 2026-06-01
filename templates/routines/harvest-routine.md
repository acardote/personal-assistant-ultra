# Harvest routine — canonical configuration

This is the artifact-of-record for the production scheduled harvest, per [#25](https://github.com/acardote/personal-assistant-ultra/issues/25). The actual routine is created by the user via `/schedule` in Claude Code or at https://claude.ai/code/routines; this file documents what the user should configure so a fresh-clone setup is reproducible. Architectural rationale (why routines, why not launchd, what trade-offs were accepted) is captured in [ADR-0002](../../docs/adr/0002-scheduled-harvest-trigger.md).

## Why routines (not launchd)

Claude Code routines run on Anthropic's web infrastructure (verified end-to-end by probes 1–6 on 2026-05-05; canonical reference: probe `trig_012bbTLE2G6RYFsncQH89Ysy` for MCP discovery + `trig_01E63nUVn7TsfKVdCTnZbjHJ` for Granola body extraction). They:

- Fire on schedule even when your laptop is closed or off.
- Push commits to the vault via **a single `git commit` + direct `git push` to `main`** — the write transport restored in [#178](https://github.com/acardote/personal-assistant-ultra/issues/178) after direct main writes became available again from within the routine (the 2026-05-08→2026-05-11 sandbox proxy GitHub-identity swap that briefly blocked them, and motivated the feature-branch + PR + auto-merge workaround of slices 1–2, has been resolved). That workaround is retired. The earlier MCP `push_files` transport (per [#153](https://github.com/acardote/personal-assistant-ultra/issues/153)) remains only as a documented **manual** operator-recovery escape hatch, not an automatic fallback.
- Auto-attach your account-level MCP connectors (Slack, Gmail, Granola, GitHub all confirmed reachable from the routine sandbox).
- Draw down the same Claude subscription as interactive sessions (no separate billing).
- Are subject to per-tier daily limits (Pro: 5/day, Max: 15/day, Team/Enterprise: 25/day).

**Choose ONE scheduler** — do not run routines and launchd against the same vault simultaneously. They will race on writes to the vault's `main` branch (both now push directly to `main`) and the dedup state files (`.harvest/<source>.json`) are last-writer-wins JSON. The launchd-based path (`templates/launchd/`) remains as an **alternative** for users without routine eligibility (lower tiers, certain enterprise restrictions) or who prefer strictly local execution. If you switch, disable the previous scheduler before enabling the new one.

**Sources NOT covered by the routine path**: Google Meet folder watch and generic transcript drop. These are file-system-based sources that need either local Meet-export sync or a folder you drop transcripts into — neither exists in the routine sandbox. If you depend on those sources, either keep launchd active alongside routines for those sources only (with the same vault — but bear in mind the lock-and-race caveat above), or run them ad-hoc via `tools/harvest.py --source gmeet|transcripts --folder <path>` from a Mac session.

### Architectural caveat: §11 trade-off accepted

The routine path is, by construction, an LLM session executing the prompt below. There is no shell harness wrapping the LLM that could enforce gates *before* it runs. As a result, the in-prompt PREFLIGHT block is an instruction the LLM is expected to follow, not a deterministic gate that prevents harvest if it fails. This is a structural §11 dependency on LLM compliance — flagged explicitly by adversarial review on PR #26 (round 3), and accepted as a property of the routine architecture rather than a fixable issue. Mitigations:

- The launchd alternative path (`templates/launchd/`) does have a deterministic Python wrapper (`tools/scheduled-harvest.py`) that can grow real preflight gates if needed; users who require enforced gates should use that path. As of [#251](https://github.com/acardote/personal-assistant-ultra/issues/251) of [#249](https://github.com/acardote/personal-assistant-ultra/issues/249), that wrapper invokes `tools/vault-desync-probe.py` before commit/push and refuses if the vault is in the May-28 desync class (HEAD ref ahead of working tree). The routine sandbox path doesn't need the probe because each fire starts from a fresh `git reset --hard origin/main` (step 3 of "Routine prompt body" below) — the desync class can only arise in a long-lived local checkout.
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
2. `https://github.com/<your-org>/<your-vault>` (content vault — destination for memory objects, KB, harvest state). **This is your private vault — replace with your own.** The author's example for reference: `https://github.com/acardote/acardote-pa-vault`.

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
- VAULT:  acardote/acardote-pa-vault (cloned to your workspace)

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
  - **Push that single marker file directly to `main`** via the same transport as a normal harvest, minus the lint gate (a failure marker is exempt). The marker is a new untracked file, so a fresh-main relocate preserves it: `cd "$VAULT" && git fetch origin main && git checkout main 2>/dev/null || git checkout -B main origin/main; git reset --hard origin/main` (untracked marker survives the reset), then `git add .harvest/runs/<ts>.json && git commit -m "preflight abort (routine)" && git push origin main`. If the push fails (e.g. a 403 recurrence), emit `echo "FATAL: preflight abort run-status could not be pushed to main — failure visible only in routine logs" >&2` so the signal is at least in the routine log. (MCP `push_files` remains a manual operator escape hatch, not an automatic fallback.)
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

**Runtime safety: if the anchor filename ts is more than 14 days old**, do NOT silently cap the harvest. That is the exact failure mode this whole rule is designed to eliminate. Instead: write `runs/<ts>.json` with `ok: false, phase: "cutoff", error: "anchor_older_than_14d_cap — file a deliberate backfill child (e.g. modeled on #172) to harvest this window via an explicit since parameter"`, push the marker via direct `git push` to `main` (same transport as the commit-push procedure below — ensure HEAD is `main` synced to `origin/main` first, as in step 3 of that procedure). **If that push itself fails** (403, network-after-retry), emit `echo "FATAL: anchor_older_than_14d_cap AND marker push failed — vault has no observable signal; check claude.ai/code/routines logs and file a backfill child manually." >&2` so the failure surfaces in the routine UI even though no cross-machine signal lands. Exit non-zero. The watchdog will surface STALE within 26h once the marker DOES reach the vault.

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

After all sources complete (including the live write-back catch-up + kb-scan + kb-drift-scan), commit the harvest to the vault via **a single `git commit` + direct `git push` to `main`**. Rationale: [#178](https://github.com/acardote/personal-assistant-ultra/issues/178) — direct writes to `main` from within the routine are available again (the 2026-05-08→2026-05-11 sandbox proxy GitHub-identity swap that briefly blocked them, and motivated the feature-branch + PR + auto-merge workaround of slices 1–2, has been resolved). That workaround is retired: there is no per-fire branch, no PR, no auto-merge, and no PR-based adversarial review. The inline lint HARD gate (step 4) is the sole quality gate, and there is **no automatic fallback** — a failed push fails loudly and a human re-plans (#178 A5).

**Critical invariant — the push MUST target `main`, never the session branch.** The routine sandbox can start on an ephemeral session branch (e.g. `claude/cool-lamport-*`). A harvest committed + pushed on that branch strands and never reaches `main` — the exact failure class the 2026-06-01 re-scope fixes. Step 3 relocates the harvest output onto a fresh `main` synced to `origin/main` before committing, and refuses to push if it cannot reach `main`.

Procedure:

1. **Move to the vault + detect whether there is anything to commit**:

       cd "$VAULT"
       if [ -z "$(git status --porcelain --untracked-files=all)" ]; then
           # No harvest output this fire. Still write a zero-shaped success runs JSON so the
           # freshness check has a marker to anchor on; it becomes the only staged change below.
           RUN_TS=$(date -u +%Y-%m-%dT%H%M%SZ)
           # Write runs/<RUN_TS>.json with the canonical success schema: ok:true, scheduler:"routine",
           # zero-shaped per-source counts ("slack":{"new":0,"errors":[]} ...),
           # notes:"harvest produced no new files", push.transport:"git-direct-main".
       fi

   `git status` respects `.gitignore`, so `raw/` (per [ADR-0001](../../docs/adr/0001-storage-backend.md)) and the regenerable `kb-scan-cache` / `kb-drift-scan-cache` are excluded by construction.

2. **Establish `RUN_TS` + write the run-status JSON locally FIRST**, then stage everything together (no "pushed without a runs marker" window):

       RUN_TS=${RUN_TS:-$(date -u +%Y-%m-%dT%H%M%SZ)}
       cat > "$VAULT/.harvest/runs/$RUN_TS.json" <<EOF
       {
         "started_at": "<step-1 start ts>",
         "ok": true,
         "scheduler": "routine",
         "sources": { <per-source counts> },
         "ended_at": "<now>",
         "push": { "transport": "git-direct-main" }
       }
       EOF

   No `pr_url` / `branch` / `auto_merge` fields — those were feature-branch-transport artifacts. `commit_shas` is omitted here (the commit hasn't happened yet); a downstream tool can add it by walking `git log` if a consumer needs it.

3. **Relocate the harvest output onto a fresh `main` synced to `origin/main`** — the stranding fix. Harvest output lives in the working tree (uncommitted: new untracked files + modified tracked files). Stash it (including untracked), hard-reset `main` to `origin/main`, then re-apply:

       git fetch origin main 2>&1 >&2
       STASHED=0
       if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
           git stash push --include-untracked -m "harvest-$RUN_TS" 2>&1 >&2 && STASHED=1
       fi
       git checkout main 2>&1 >&2 || git checkout -B main origin/main 2>&1 >&2
       git reset --hard origin/main 2>&1 >&2
       if [ "$STASHED" = "1" ] && ! git stash pop 2>&1 >&2; then
           echo "FATAL: stash pop conflicted re-applying harvest output onto fresh main — origin/main changed a file this fire also touched. Working tree carries the conflict markers for operator inspection." >&2
           # Overwrite runs/<RUN_TS>.json: ok:false, phase:"relocate_to_main", error:"stash_pop_conflict". exit 1.
           exit 1
       fi
       # Hard guards: we are on main, and the harvest output survived the relocate.
       [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "FATAL: not on main after relocate (HEAD=$(git rev-parse --abbrev-ref HEAD)) — refusing to push harvest to a non-main branch (the stranding failure class)." >&2; exit 1; }
       if [ -z "$(git status --porcelain --untracked-files=all)" ]; then
           echo "FATAL: harvest output disappeared after relocate to main — aborting rather than committing an empty fire." >&2
           exit 1
       fi

4. **Inline lint HARD gate** — the sole quality gate now that there is no PR/review hook. If lint fails, abort BEFORE commit (leave the working tree dirty for operator inspection) and write a failure marker:

       METHOD="$(dirname "$VAULT")/personal-assistant-ultra"
       [ -d "$METHOD/tools" ] || { echo "FATAL: method repo not found at $METHOD" >&2; exit 1; }
       if ! "$METHOD/tools/lint-provenance.py" --require-vault 2>&1 >&2; then
           cat > "$VAULT/.harvest/runs/$RUN_TS.json" <<EOF
       {
         "started_at": "<step-1 start>",
         "ok": false,
         "scheduler": "routine",
         "sources": { <per-source counts> },
         "ended_at": "<now>",
         "phase": "inline_lint",
         "error": "lint_provenance_failed",
         "push": { "transport": "git-direct-main", "aborted_before_commit": true }
       }
       EOF
           echo "FATAL: tools/lint-provenance.py --require-vault refused — harvest aborted before commit. Working tree in $VAULT carries the failed state for inspection." >&2
           exit 1
       fi
       # JSON-validity probe on the runs JSON (catches step-2 quoting bugs).
       python3 -c "import json; json.load(open('$VAULT/.harvest/runs/$RUN_TS.json'))" 2>&1 >&2 \
           || { echo "FATAL: runs/$RUN_TS.json is not valid JSON — step 2 produced malformed output. Aborting." >&2; exit 1; }

5. **Stage harvest paths + commit once**. Stage the same paths as `tools/live-commit-push.sh` (`memory/ .harvest/ kb/ artefacts/ projects/`); `raw/` is `.gitignore`'d. Stage individually because `git add a b c` aborts on the first nonexistent path:

       for path in memory/ .harvest/ kb/ artefacts/ projects/; do
           [ -e "$path" ] && git add "$path" 2>/dev/null || true
       done
       if git diff --cached --quiet; then
           echo "FATAL: nothing staged after step 5 despite step 1 detecting changes — aborting." >&2
           exit 1
       fi
       git commit -m "harvest $(date -u +%Y-%m-%d) (routine)"
       SHA=$(git rev-parse HEAD)
       echo "harvest commit: $SHA on main" >&2

6. **Push to `main`** — single push, with a non-fast-forward rebase-retry (another machine / the other scheduler pushed between our fetch and our push). Mirrors `tools/live-commit-push.sh` semantics:

       ERR_FILE=$(mktemp -t harvest-push.XXXXXX)
       if git push origin main 2>"$ERR_FILE"; then
           echo "harvest pushed to main: $SHA" >&2
       elif grep -qiE 'non-fast-forward|fetch first|behind' "$ERR_FILE"; then
           echo "WARN: non-ff rejection on main — pulling + rebasing once, then retrying" >&2
           if ! git pull --rebase origin main 2>&1 >&2; then
               git rebase --abort 2>/dev/null || true
               echo "FATAL: rebase onto origin/main failed (conflict) — working tree left clean; next fire retries. Harvest NOT pushed." >&2
               # Overwrite runs JSON: ok:false, phase:"commit_push", error:"rebase_failed". exit 1.
               exit 1
           fi
           SHA=$(git rev-parse HEAD)
           git push origin main 2>&1 >&2 || { echo "FATAL: second push to main failed after rebase. Harvest NOT pushed." >&2; exit 1; }
           echo "harvest pushed to main after rebase: $SHA" >&2
       else
           PUSH_ERR=$(cat "$ERR_FILE")
           if echo "$PUSH_ERR" | grep -qiE '403|permission|denied|protected'; then
               echo "FATAL: git push to main returned 403/denied — #178 A1 (direct main writes available) is FALSIFIED; the proxy principal may have lost main-write access again (cf. the 2026-05-08 identity swap). Harvest committed locally ($SHA) but NOT pushed. File evidence on #178 and re-plan the transport. Manual recovery: push the local commit from a machine with main-write access, or use the MCP push_files escape hatch by hand." >&2
               # Overwrite runs JSON: ok:false, phase:"commit_push", error:"push_403_main_a1_falsified". exit 1.
           else
               echo "FATAL: git push to main failed (not non-ff): $PUSH_ERR" >&2
               # Overwrite runs JSON: ok:false, phase:"commit_push", error:"push_failed_other". exit 1.
           fi
           exit 1
       fi

7. **Final user-facing response** — report the commit on `main`:

       Harvest complete (routine, $RUN_TS).
       - Sources: <one-line per-source summary>
       - Commit: $SHA on main
       - Runs JSON: .harvest/runs/$RUN_TS.json

   On any FATAL branch above, the run-status JSON carries `ok:false` + the `phase`/`error`, the working tree is left for operator inspection where the message says so, and the watchdog surfaces STALE within 26h.

### Manual recovery (operator-only) — MCP `push_files`

Direct push to `main` is the sole automatic transport; there is **no automatic fallback** (#178 A5 — fail-loud + human-re-plan). If a fire fails its push (e.g. a 403 recurrence of the 2026-05-08 identity swap), an operator can recover by hand: the historical MCP `push_files` transport (per [#161](https://github.com/acardote/personal-assistant-ultra/issues/161) / v0.4.2, including the [#166](https://github.com/acardote/personal-assistant-ultra/issues/166) 401-retry-on-demand-pause flow) lands commits on `main` via a separate auth path and remains available as a manual escape hatch. It is intentionally NOT wired as an automatic routine fallback.

### Race semantics

Direct push to `main` reintroduces the cross-scheduler write race that the feature-branch transport had structurally eliminated: a routine fire and a launchd run can both target `main`. The non-fast-forward rebase-retry in step 6 handles the bounded case (one pushes between the other's fetch and push). **The "Choose ONE scheduler" discipline remains load-bearing** — it is the real guard; the rebase-retry is a backstop, not a license to run both. The dedup-state-file race (`.harvest/<source>.json`, last-writer-wins JSON) is likewise only safe under one scheduler.

Because the harvest lands on `main` synchronously (no PR-merge latency), the slice-1→slice-2 "freshness window" regression is gone: fire N's `runs/<ts>.json` and `.harvest/<source>.json` are on `origin/main` the moment the push returns, so fire N+1 anchors its cutoff and seeds its dedup state correctly.

In your final response, summarize:
- Which sources fired and what each produced.
- Any errors encountered.
- Whether the `git push` to `main` succeeded, including the commit SHA. If it failed, the failure phase/error and the manual-recovery hint.
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
