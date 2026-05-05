# Harvest routine — canonical configuration

This is the artifact-of-record for the production scheduled harvest, per [#25](https://github.com/acardote/personal-assistant-ultra/issues/25). The actual routine is created by the user via `/schedule` in Claude Code or at https://claude.ai/code/routines; this file documents what the user should configure so a fresh-clone setup is reproducible.

## Why routines (not launchd)

Claude Code routines run on Anthropic's web infrastructure (verified by probe `trig_01TgF2k8aNeWsYtrjQ4JZ6RE` on 2026-05-05). They:

- Fire on schedule even when your laptop is closed or off.
- Have authenticated git push to linked repos (no per-machine `gh auth` setup needed).
- Auto-attach your account-level MCP connectors (Slack and Gmail confirmed).
- Draw down the same Claude subscription as interactive sessions (no separate billing).
- Are subject to per-tier daily limits (Pro: 5/day, Max: 15/day, Team/Enterprise: 25/day).

The launchd-based path (`templates/launchd/`) remains as an **alternative scheduler** for users without routine eligibility (lower tiers, certain enterprise restrictions) or who prefer strictly local execution. Routines are the recommended primary.

## Configuration

When you create the routine via `/schedule`, configure it as below.

### Schedule

- **Recurring cron**: `7 7 * * *` (daily at 7:07 UTC). The off-the-hour minute is the schedule skill's anti-stampede convention. Adjust to your timezone — the cron expression is in UTC, so for 7am Europe/Lisbon use `7 6 * * *` in summer (DST) or `7 7 * * *` in winter. The routine confirmation flow will echo the converted local time before saving.
- Routines have a 1-hour minimum interval; daily-or-coarser is the sweet spot for harvest cadence.

### Linked repos

Two `git_repository` sources, in this order:

1. `https://github.com/acardote/personal-assistant-ultra` (method repo — contains `.claude/skills/personal-assistant/SKILL.md`, `tools/compress.py`, schemas, prompts).
2. `https://github.com/getnexar/acardote-pa-vault` (content vault — destination for memory objects, KB, harvest state).

The routine workspace clones both. The vault is the git-write target.

### MCP connectors

Auto-attached at routine create time (no manual config in the create body needed) — verified by probes 2/3/5 against `trig_012bbTLE2G6RYFsncQH89Ysy` on 2026-05-05:

- Slack (account-level connector at https://mcp.slack.com/mcp).
- Gmail (account-level connector at https://gmailmcp.googleapis.com/mcp/v1).
- Granola (account-level connector at https://mcp.granola.ai/mcp). Exposes `query_granola_meetings`. Connector must be **authenticated** in claude.ai (not just enabled) — probes 3 and 4 showed Granola absent from the auto-attach list when only enabled but unauthenticated, and present once authenticated.

If you have additional connectors, the API attaches them automatically based on your account's connected (and authenticated) list. Verify the create response's `mcp_connections` array matches expectations before declaring the routine ready.

### Allowed tools

`Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep` — covers the orchestration's needs (filesystem, shell, structured edits).

### Model

`claude-sonnet-4-6` (default). Override only if you have a specific reason.

### Routine prompt

Copy this verbatim into the routine's prompt field. It's self-contained — the routine starts with zero conversational context, so the prompt has to carry everything.

```
Run the personal-assistant scheduled harvest.

Two repos are linked to this routine:
- METHOD: acardote/personal-assistant-ultra (cloned to your workspace)
- VAULT:  getnexar/acardote-pa-vault (cloned to your workspace)

Identify their paths via `find / -name personal-assistant-ultra -type d 2>/dev/null` and `find / -name acardote-pa-vault -type d 2>/dev/null`, picking the one inside the workspace (typically under /root/ or /workspace/). Treat those as $METHOD and $VAULT for the rest of this prompt.

Write a per-checkout config so the method-repo tools resolve content paths against the vault:

  cat > $METHOD/.assistant.local.json <<EOF
  {"paths": {"content_root": "$VAULT"}}
  EOF

Determine harvest cutoff:
- If $VAULT/.harvest/runs/ contains any file, this is NOT a cold start: use "since yesterday" (last 24 hours).
- Otherwise: this IS a cold start: use "since 30 days ago".

Open $METHOD/.claude/skills/personal-assistant/SKILL.md (it's the canonical orchestration spec) and follow its "Harvest orchestration" section for each enabled source, in this order:
- Slack (via Slack MCP — check `mcp__claude_ai_Slack__slack_search_*` tool family)
- Gmail (via Gmail MCP — check the auto-attached connector for the tool name)
- Granola (via Granola MCP — verified auto-attached when the account-level connector is authenticated; the namespace is `mcp__<connector-uuid>__` with `query_granola_meetings` as the entry point. If the namespace is absent, the connector is enabled but unauthenticated — log to errors and skip)
- Google Meet transcripts (via Drive folder — skip in routine context, this is a folder-watch path that needs local Meet-export sync)
- Generic transcript drop (skip in routine context — same reason)

For each source's discovered items: write raw artifact to $VAULT/raw/<source-kind>/<id>.md, then invoke the compression pipeline:

  cd $METHOD && tools/compress.py $VAULT/raw/<source-kind>/<id>.md --kind <kind> --source-kind <source_kind>

Compress writes to $VAULT/memory/<source-kind>/, applies #10's clustering (event_id, is_canonical_for_event), and respects the per-kind expiry rules from #8.

Update $VAULT/.harvest/<source>.json dedup state. Append today's section to $VAULT/.harvest/daily/$(date -u +%Y-%m-%d).md following SKILL.md's daily digest format. Create $VAULT/.harvest/runs/$(date -u +%Y-%m-%dT%H%M%SZ).json with structured run status:

  {"started_at": "...", "ok": true|false, "sources": {"slack": {"new": N, "errors": []}, ...}, "ended_at": "..."}

Bound the run to ~10 minutes total. If a source is unreachable (MCP auth expired, tool not available), log to the digest's "errors:" line and the run-status JSON's "errors" key, then continue with other sources — do NOT retry.

After all sources complete, from $VAULT:

  git add -A
  git commit -m "harvest $(date -u +%Y-%m-%d) (routine)"
  git push origin main

If `git push` fails (e.g., non-fast-forward because another machine pushed first), do NOT loop — write the failure to the run-status JSON and exit. The next day's run will pull and proceed.

In your final response, summarize:
- Which sources fired and what each produced.
- Any errors encountered.
- Whether git push succeeded.
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
