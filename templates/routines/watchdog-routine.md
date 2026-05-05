# Watchdog routine — out-of-band harvest-failure alerting

This is the canonical configuration for the harvest-failure watchdog, per [#32](https://github.com/acardote/personal-assistant-ultra/issues/32). It closes the part of F1 that the in-skill freshness check ([#27](https://github.com/acardote/personal-assistant-ultra/issues/27), [PR #31](https://github.com/acardote/personal-assistant-ultra/pull/31)) does NOT close: detection that surfaces *without* the user having to invoke `/personal-assistant`.

## Why a second routine

The in-skill check fires only when the user starts a Claude Code session and invokes `/personal-assistant`. If the user goes a week without using the assistant, the in-skill check is silent for that week even if the main routine stopped firing on day 1. The watchdog is a separate routine that fires on its own cadence, runs the freshness check, and sends a Slack DM to the user when state ≠ PASS.

Architecture:
- **Main routine** (per `harvest-routine.md`): fires daily, runs the harvest, writes run-status JSON to the vault.
- **Watchdog routine** (this file): fires daily at a different time, runs `tools/check-harvest-freshness.py --json`, sends Slack DM only on non-PASS states (plus a weekly Sunday heartbeat — see "Liveness signal" below).

The two routines are independent. If the main routine stops firing entirely, the watchdog still fires and surfaces STALE/MISSING.

### Honest scope: this reduces F1, does not eliminate it

This watchdog **reduces** F1 (silent failure of the harvest pipeline) — it does NOT eliminate it. There are still failure modes that escape detection:

- **Watchdog itself stops firing** (auth lapse on Slack MCP, quota exhaustion, Anthropic infra blip). With the watchdog dead, the system reverts to pre-#31 behavior — the in-skill check still works on next user invocation but it inherits the same user-cadence dependency the watchdog was meant to escape. The weekly heartbeat (below) makes this detectable: if the user notices "haven't received my Sunday pulse for three weeks," that's the signal.
- **Slack MCP misconfigured** (user deauthed Slack at the account level). Watchdog runs successfully but the DM silently fails to send. No other surface alerts. Same recursion problem one level deeper.
- **Both routines fail simultaneously** (account-level auth lapse, account suspension). Neither fires; user discovers via /personal-assistant invocation when the in-skill check surfaces. That's the bottom of the recursion — not because there's nothing below it, but because everything below it is on the user's invocation cadence anyway.

The principled framing: the watchdog reduces the worst-case silent-failure window from "bounded by user habit" (could be 7+ days) to "bounded by 24h of watchdog cadence" (assuming the watchdog works). The remaining failure modes are documented, not hidden.

### Liveness signal — weekly heartbeat

Pure silent-on-PASS has no positive signal that the alerting works. To address this, the watchdog **also sends a weekly heartbeat DM on Sundays** even when state is PASS — a single short "watchdog is alive" message. Six days of the week PASS is silent (no notification fatigue); on Sunday a healthy run produces a brief pulse. If the user notices the Sunday pulse hasn't arrived for two weeks running, that's a signal that the watchdog itself broke.

## Configuration

When you create the watchdog routine via `/schedule`, configure it as below. Architectural rationale captured in [ADR-0002](../../docs/adr/0002-scheduled-harvest-trigger.md).

### Schedule

- **Recurring cron**: `7 18 * * *` (daily at 18:07 UTC). The 12-hour offset from the main routine (`7 6 * * *`) is a defensible default but not derived from any hard constraint — anything in the 6h–20h offset range gives reasonable detection latency. 12h was chosen because it puts the watchdog roughly in the middle of the user's awake window and gives the main routine ~12h of retry headroom before the watchdog might preemptively alert. Adjust to your timezone if you want the DM at a specific local time — e.g., for 7pm Europe/Lisbon DST, `7 18 * * *` resolves to 19:07 Lisbon (UTC+1 in DST).
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

**Before pasting**: replace `<YOUR_SLACK_USER_ID>` in the prompt below with your own Slack user ID. To find it, ask your Slack workspace settings, or in Claude Code run `slack_search_users` with your name/email — the `slack_send_message` tool's own description also tells you "the current logged in user's user_id is …" if you're already authenticated. The literal string `<YOUR_SLACK_USER_ID>` is the placeholder; do NOT paste the prompt without substituting it (see `tools/lint-docs.py` — it will refuse to lint clean if a template carries the placeholder *or* a hardcoded `U[A-Z0-9]+` in a watchdog template that isn't this one's example).

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

Determine whether to send a DM:

- If `state` is anything OTHER than `PASS` (STALE, FAILED, STUCK, STUCK_AND_STALE, MISSING, CORRUPT) → send a DM with the alert template below.
- If `state` is `PASS` AND today is Sunday (`date -u +%u` returns `7`) → send a brief weekly heartbeat DM: `*Harvest watchdog heartbeat* — state: PASS. Watchdog alive; harvest healthy.` This is the liveness signal that disambiguates "no DMs = healthy" from "no DMs = watchdog dead."
- If `state` is `PASS` AND today is NOT Sunday → exit silently, no DM.

For DMs (alert or heartbeat), use `slack_send_message` with `channel_id: <YOUR_SLACK_USER_ID>` (the user's own Slack user ID — DMs to oneself work the same as DMs to any user). Substitute the placeholder with your actual ID before pasting this prompt into `/schedule`.

For the alert variant, format the message as:

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
