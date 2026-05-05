# Watchdog routine — out-of-band harvest-failure alerting

This is the canonical configuration for the harvest-failure watchdog, per [#32](https://github.com/acardote/personal-assistant-ultra/issues/32). It closes the part of F1 that the in-skill freshness check ([#27](https://github.com/acardote/personal-assistant-ultra/issues/27), [PR #31](https://github.com/acardote/personal-assistant-ultra/pull/31)) does NOT close: detection that surfaces *without* the user having to invoke `/personal-assistant`.

## Why a second routine

The in-skill check fires only when the user starts a Claude Code session and invokes `/personal-assistant`. If the user goes a week without using the assistant, the in-skill check is silent for that week even if the main routine stopped firing on day 1. The watchdog is a separate routine that fires on its own cadence, runs the freshness check, and sends a Slack DM to the user when state ≠ PASS.

Architecture:
- **Main routine** (per `harvest-routine.md`): fires daily, runs the harvest, writes run-status JSON to the vault.
- **Watchdog routine** (this file): fires daily at a different time, runs `tools/check-harvest-freshness.py --json`, sends Slack DM only on non-PASS states.

The two routines are independent. If the main routine stops firing entirely, the watchdog still fires and surfaces STALE/MISSING. If the watchdog itself stops firing, the in-skill check (one of the failure-detection layers) still surfaces issues when the user invokes the skill — that's the bottom of the recursion.

## Configuration

When you create the watchdog routine via `/schedule`, configure it as below. Architectural rationale captured in [ADR-0002](../../docs/adr/0002-scheduled-harvest-trigger.md).

### Schedule

- **Recurring cron**: `7 18 * * *` (daily at 18:07 UTC). The 12-hour offset from the main routine (`7 6 * * *`) means a main-routine failure at the morning fire is detected by the watchdog within 12 hours, well below the 26-hour staleness threshold. Adjust to your timezone if you want the DM at a specific local time — e.g., for 7pm Europe/Lisbon DST, use `7 18 * * *` (since UTC is 1h behind Lisbon DST, 18:07 UTC = 19:07 Lisbon).
- Routines have a 1-hour minimum interval. Daily is the right cadence for this watchdog — more frequent would waste quota and produce noise.

### Linked repos

Two `git_repository` sources, identical to the main routine:

1. `https://github.com/acardote/personal-assistant-ultra` (method repo — contains `tools/check-harvest-freshness.py`).
2. `https://github.com/<your-org>/<your-vault>` (content vault — destination for run-status reads). **This is your private vault — replace with your own.**

The watchdog needs to read `<vault>/.harvest/runs/*.json`, so the vault must be linked.

### MCP connectors

Auto-attached at routine create time:

- Slack (account-level connector at https://mcp.slack.com/mcp). The watchdog calls `slack_send_message` to DM the user with non-PASS results. Required.

The Granola and Gmail connectors will also auto-attach (they're account-level), but the watchdog doesn't use them.

### Allowed tools

`Bash`, `Read` — covers the watchdog's needs (run the Python check, read its JSON output).

### Model

`claude-haiku-4-5` recommended (this is a simple workflow that doesn't need Sonnet's reasoning). Faster + cheaper.

### Watchdog prompt

Copy this verbatim into the routine's prompt field. Replace `U03LA1MHLG0` with your own Slack user ID if you're not the original maintainer (`mcp__claude_ai_Slack__slack_search_users` resolves it from your name or email; the slack_send_message tool's description also tells you "the current logged in user's user_id is …").

```
Run the personal-assistant harvest watchdog.

Two repos are linked to this routine:
- METHOD: acardote/personal-assistant-ultra (cloned to your workspace)
- VAULT:  acardote-pa-vault (cloned to your workspace)

Identify the workspace paths:

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

Write a per-checkout config so the freshness check resolves content paths against the vault:

  cat > "$METHOD/.assistant.local.json" <<EOF
  {"paths": {"content_root": "$VAULT"}}
  EOF

Run the freshness check and capture its JSON output:

  cd "$METHOD" && tools/check-harvest-freshness.py --json > /tmp/freshness.json
  EXIT=$?

Read the JSON. If `state` is `PASS`, exit silently — do NOT send a Slack DM, do NOT log anything beyond a confirmation line. Notification fatigue is a real failure mode for monitoring; the watchdog only speaks up when there is something to say.

If `state` is anything other than `PASS` (STALE, FAILED, STUCK, STUCK_AND_STALE, MISSING, CORRUPT), send a Slack DM via `slack_send_message` to channel_id `U03LA1MHLG0` (the user's own Slack user ID — DMs to oneself work the same as DMs to any user). Format the message as:

  *Harvest watchdog alert* — state: `<state>`
  
  <summary from JSON>
  
  ---
  - newest run-status: `<newest_path or 'none'>`
  - last age: `<age_hours>h` (source: `<age_source>`)
  - scheduler: `<scheduler or 'unknown'>`
  - error: `<error or 'none'>`
  - consecutive failures: `<consecutive_failures or 'n/a'>`
  
  Routine status: https://claude.ai/code/routines

Use Slack markdown for the message. Single DM per fire — do not loop.

After sending (or skipping send on PASS), exit. Do NOT commit anything to the vault — the watchdog is read-only against the vault.

In your final response, summarize:
- The freshness state observed.
- Whether a DM was sent (and to whom).
- The freshness check exit code.
```

## On-demand variants

The same prompt works for ad-hoc on-demand watchdog fires:

- Trigger via the routine's "Run now" action in claude.ai/code/routines — useful to verify the watchdog works end-to-end before relying on it.

## Updating

If the watchdog prompt or schedule needs to change:

- Edit this file (the canonical artifact-of-record).
- Update the routine via `RemoteTrigger` `action: update` or via claude.ai/code/routines UI.
- The two should match. If they drift, this file is wrong (or the routine is wrong); reconcile.

## Quota considerations

Pro tier: 5 routines/day. Main + watchdog = 2/day, leaves 3 for on-demand. Acceptable.
Max tier: 15/day. Plenty.
Team / Enterprise: 25/day. Plenty.

If a user genuinely runs out of quota on Pro tier, removing the watchdog (and falling back to in-skill detection only) is the documented degradation path.
