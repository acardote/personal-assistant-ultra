# launchd templates

Scheduled-harvest LaunchAgent for the personal-assistant skill (per [#11](https://github.com/acardote/personal-assistant-ultra/issues/11)).

## What the routine actually invokes

The plist invokes `__PATH_TO_METHOD_REPO__/tools/scheduled-harvest.py`, NOT `claude -p` directly. That wrapper:

- Acquires an fcntl exclusive lock at `<content_root>/.harvest/.lock` so concurrent fires (scheduled at 7:07am while the user is mid-on-demand-harvest at 7:06am) can't race on the dedup state files.
- Spawns `claude -p` with the harvest prompt.
- Captures stdout / stderr / returncode into a structured run-status JSON at `<content_root>/.harvest/runs/<utc-ts>.json`. Every fire writes a status file, regardless of success — that's the F1 (silent-failure) defense.
- On success, commits + pushes the vault repo (skipped if `<content_root>` isn't a git repo, or via `--no-commit`). That's the F2 (cross-machine durability) defense.
- Detects "first run" (no prior `runs/<...>.json` exists) and widens the harvest window to 30 days for cold-start backfill; subsequent runs default to "since yesterday."
- Sets non-zero exit if claude failed, or git push failed, or the lock couldn't be acquired. The plist's `StandardErrorPath` log will show the failure; `tools/scheduled-harvest.py --status-only` prints the latest run's full status JSON.

To inspect the latest run's outcome at any time:
```
tools/scheduled-harvest.py --status-only
```

## Why launchd, not the Claude Code `schedule` skill?

The `schedule` skill / `CronCreate` only fires while a Claude Code session is running and idle (per its tool docs: *"Jobs only fire while the REPL is idle"*). For a daily 7am unattended harvest — where the laptop may be closed and Claude Code not running — that's not viable. See the probe outcome on [issue #11](https://github.com/acardote/personal-assistant-ultra/issues/11) for the full Option-A vs Option-B analysis.

`launchd → claude -p` is the path:
- launchd fires on schedule even when Claude Code isn't open.
- `claude -p` spawns a fresh headless Claude Code session inheriting your user-level MCP config.
- The headless session invokes the `personal-assistant` skill which orchestrates Slack / Gmail / Granola / Meet harvest via the configured MCPs.
- `RunAtLoad` (when set true; see below) covers laptop-was-asleep-at-7am via fire-on-next-wake.

## Installation

1. **Copy the template** into your LaunchAgents directory and rename without `.example`:
   ```
   cp templates/launchd/com.acardote.personal-assistant-harvest.plist.example \
      ~/Library/LaunchAgents/com.acardote.personal-assistant-harvest.plist
   ```

2. **Replace the three placeholders** in the new file:
   - `__YOUR_USERNAME__` — your macOS short username (e.g. `acardote`).
   - `__PATH_TO_CLAUDE__` — absolute path to the `claude` binary. Find it via `which claude`.
   - `__PATH_TO_METHOD_REPO__` — absolute path of your method-repo checkout (e.g. `/Users/<you>/Projects/personal-assistant-ultra`).

3. **Decide on `RunAtLoad` policy.** The template ships with `<false/>`. If your laptop is often asleep at 7am, set to `<true/>` so the harvest fires on next login/wake when the scheduled time was missed. The trade-off: `<true/>` also fires when you reboot mid-day, which may not be what you want. Pick whichever surprises you less.

4. **Bootstrap the agent**:
   ```
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.acardote.personal-assistant-harvest.plist
   ```
   This loads the agent + persists across logouts. Verify via `launchctl print gui/$(id -u)/com.acardote.personal-assistant-harvest`.

5. **Test it once on demand** (don't wait for 7:07am):
   ```
   launchctl kickstart -k gui/$(id -u)/com.acardote.personal-assistant-harvest
   ```
   Watch the log:
   ```
   tail -f ~/Library/Logs/personal-assistant-harvest.log
   ```
   First-run cold-start may take longer than subsequent runs (30-day backfill). See SKILL.md for what the routine is supposed to do.

## Uninstalling

```
launchctl bootstrap gui/$(id -u)/com.acardote.personal-assistant-harvest
rm ~/Library/LaunchAgents/com.acardote.personal-assistant-harvest.plist
```

## Cross-machine

The plist is per-machine (paths are absolute). On a second machine, repeat the install with that machine's paths. The dedup state in your content vault prevents duplicate harvests across machines (per #5 reopen + #10 multi-fidelity matching).

## Inspecting runs

Every fire produces a structured status JSON. Newest run at `<content_root>/.harvest/runs/<utc-ts>.json`. View the latest:

```
tools/scheduled-harvest.py --status-only
```

Stale-detection rule of thumb: if the newest entry in `runs/` is older than 26h, the routine is silently broken (laptop was asleep + RunAtLoad: false + reboot didn't fire it; or claude auth expired and you missed a day; or a configuration regressed). Treat as F1-fired and investigate.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Routine fires but the log shows `claude: command not found` | `__PATH_TO_CLAUDE__` is wrong. Hard-code the absolute path; launchd's PATH is minimal. |
| Routine fires but `claude -p` waits for interactive auth | First-run claude needs you to authenticate interactively. Run `claude` once manually before scheduling. |
| Routine appears not to fire at all | Check `launchctl print gui/$(id -u)/com.acardote.personal-assistant-harvest` for state. macOS sometimes pauses agents. Common cause: laptop was asleep AND `RunAtLoad: false` AND Sleep > 1 day, so the calendar-interval window was missed. |
| Permissions error writing to vault | The agent runs as your user. If `<content_root>` is on a network drive or different mount, ensure your user can write to it from a non-interactive session. |
| Daily digest growing too large | Per-day digest is at `<content_root>/.harvest/daily/YYYY-MM-DD.md`. Large days are usually post-cold-start; a one-time 30-day backfill produces a big day-1 digest. Subsequent days are small. |

## Linux equivalent (systemd timer)

Not yet templated. The shape would be a `~/.config/systemd/user/personal-assistant-harvest.{service,timer}` pair with `OnCalendar=*-*-* 07:07:00`. Open an issue if you'd like this added.
