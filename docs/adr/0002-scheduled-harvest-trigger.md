# ADR-0002 — Scheduled-harvest trigger: Claude Code routine, not launchd

- Status: Accepted
- Date: 2026-05-05
- Decider: acardote
- Related: parent issue [#1](https://github.com/acardote/personal-assistant-ultra/issues/1), original child [#11](https://github.com/acardote/personal-assistant-ultra/issues/11) (closed; landed launchd path), corrective child [#25](https://github.com/acardote/personal-assistant-ultra/issues/25) (closed; migrated to routines), follow-ups [#27](https://github.com/acardote/personal-assistant-ultra/issues/27) (F1 stale-run detection regression) and [#28](https://github.com/acardote/personal-assistant-ultra/issues/28) (this ADR)

## Context

The personal-assistant skill needs a daily unattended scheduled trigger that fires at ~7am the user's local time and runs the harvest orchestration: Slack + Gmail + Granola + Meet folder + generic transcript drop → raw archive → editorially compressed memory objects → vault commit + push. The trigger must work when the user's laptop is closed or asleep — i.e., it cannot depend on a local Claude Code session being open and idle.

Three viable scheduling mechanisms were considered:

1. **`CronCreate` (in-session scheduling skill)** — a tool that fires while a Claude Code REPL is running and idle. Disqualified up-front: the user's actual primary failure mode is "laptop closed, harvest must still fire," and `CronCreate` cannot service that.
2. **launchd LaunchAgent + `claude -p` headless invocation** — a local Mac scheduler invoking a Python wrapper (`tools/scheduled-harvest.py`) that spawns a headless Claude Code session against the personal-assistant skill. Originally landed in [#11](https://github.com/acardote/personal-assistant-ultra/issues/11) as the production path.
3. **Claude Code routines** — Anthropic-hosted scheduled agents that fire on cron, clone linked git repos into an ephemeral workspace, run a self-contained prompt, and have authenticated git push back to the linked repos. Auto-attach the user's account-level MCP connectors.

## Decision

**Routines are the production scheduled trigger. launchd is the documented alternative for users without routine access (lower tiers, certain enterprise restrictions) or who prefer strictly local execution. Both paths use the same orchestration code (`personal-assistant` skill); they differ only in the triggering mechanism, working-directory model, and git-auth path.**

The migration was performed in [#25](https://github.com/acardote/personal-assistant-ultra/issues/25) / PR [#26](https://github.com/acardote/personal-assistant-ultra/pull/26), verified end-to-end by probes 1–6 on 2026-05-05.

Two probe IDs of record:
- `trig_012bbTLE2G6RYFsncQH89Ysy` — MCP discovery: confirmed Slack, Gmail, Granola, GitHub all auto-attach to routine sessions when the corresponding account-level connectors are authenticated.
- `trig_01E63nUVn7TsfKVdCTnZbjHJ` — Granola body extraction: confirmed `query_granola_meetings` returns full structured meeting bodies (sections, discussion bullets, action items, attendees), not just metadata.

## Consequences

**Positive**

- Fires when the user's laptop is closed/off — the original load-bearing requirement.
- Authenticated git push to linked repos — no per-machine `gh auth` setup needed; cross-machine durability (F2) is solved by routine infra.
- Auto-attached MCP connectors — Slack, Gmail, Granola, GitHub all reachable from the sandbox without manual config in the routine create body.
- Same Claude subscription as interactive sessions — no separate billing or auth surface.
- Workspace-ephemeral execution model — no long-lived state in the routine itself; the vault is the canonical store.

**Negative**

- **Per-tier daily limits**: Pro 5 runs/day, Max 15/day, Team/Enterprise 25/day. A daily-cadence routine + 4 on-demand "Run now" fires already exhausts Pro's budget. Mitigation: the on-demand local wrapper (`tools/scheduled-harvest.py`) doesn't count against routine quota and remains available for ad-hoc terminal runs.
- **§11 dependency on LLM compliance**: routines are LLM sessions hosted by Anthropic infra; there is no shell harness wrapping the LLM where deterministic preflight gates could live. The in-prompt PREFLIGHT block in `templates/routines/harvest-routine.md` is followed-instruction, not enforced gate. Documented architectural caveat. Falsifier: if a routine produces `ok: true` with a critical connector missing, the §11 dependency has bitten and the principled response is to revert production trigger to launchd. Tracked at parent [#1](https://github.com/acardote/personal-assistant-ultra/issues/1#issuecomment-4380559607).
- **F1 silent-failure regression vs. #11**: launchd's `tools/scheduled-harvest.py --status-only` and stale-run detection (`runs/<utc>.json + 26h freshness`) gave the user a way to detect routines that stopped firing. The routine path writes the same files but provides no surfaced detector. Filed as **#27** with a 1-week SLA from #25 merge.
- **Coverage gap for file-system sources**: Google Meet folder watch and generic transcript drop are not reachable from the routine sandbox. Users who depend on those sources either run them ad-hoc via `tools/harvest.py --source gmeet|transcripts --folder <path>` from a Mac session, or keep launchd active for those sources only. "Pick one scheduler per source" — racing routines and launchd against the same vault is documented-not-allowed (last-writer-wins on dedup state; non-fast-forward push collisions).
- **Connector enablement vs. authentication**: probes 3 (Granola enabled-but-unauthenticated, absent) vs. 5 (after auth, attached) showed that mere "connector enabled in claude.ai" is not sufficient. The connector must be authenticated. Documented in `templates/routines/harvest-routine.md` §"MCP connectors".

## Rejected alternatives

### `CronCreate` (in-session scheduling)

Rejected up-front. The schedule skill's contract is "jobs only fire while the REPL is idle." For a daily 7am unattended harvest where the laptop may be closed and Claude Code not running, the trigger is non-existent. Verified during the original [#11](https://github.com/acardote/personal-assistant-ultra/issues/11) probe series.

### launchd-only (the [#11](https://github.com/acardote/personal-assistant-ultra/issues/11) original)

Demoted to alternative, not rejected. The path is functional and has a deterministic Python wrapper that *can* host real preflight gates if they're needed (the routine path cannot). Retained because:

- Some users won't have routine access (tier or enterprise restrictions).
- Some users explicitly prefer strictly local execution.
- File-system-based sources (Meet folder, transcript drop) need it.

The launchd path is documented in `templates/launchd/README.md` with an unmissable "(alternative scheduler)" framing, pointing readers at the routines doc first.

### Hybrid (routine for cloud sources, launchd for file sources)

Considered but rejected as default config. The two-trigger model adds operational complexity (two failure surfaces, two status surfaces, one "pick one" rule per source) that's hard to communicate to a fresh user. The migration ships routines as the single primary, with the hybrid available to power users who explicitly need the Meet/transcript-drop coverage.

## Re-opening criteria (trip wires)

ADR-0002 should be re-opened if:

1. **§11 falsifier fires** — a routine produces `ok: true` with a critical connector missing. Recovery: revert production trigger to launchd, ship #27's stale-run detection on the launchd path, treat routines as best-effort secondary.
2. **Anthropic changes routine semantics** in a way that breaks one of the load-bearing properties — e.g., MCP auto-attach removed, git-push auth scoped down, daily limits dropped below daily-cadence threshold.
3. **F1 silent-failure window in the wild** — if a real-world routine outage of >7 days goes undetected because #27 hasn't landed and there's no other surfaced check, that's evidence the regression is operationally worse than the docs admit and the migration should be paused/reverted.
4. **Quota exhaustion becomes a steady-state friction** — if daily limits force users to choose between scheduled-harvest and other routine uses, ADR may need to recommend Max/Team tier as a soft prerequisite.
