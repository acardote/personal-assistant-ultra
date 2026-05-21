# Setup — from clone to first harvest

This walkthrough is the contract for [issue #14](https://github.com/acardote/personal-assistant-ultra/issues/14)'s A7 invariant: anyone with this method repo + their own MCP config + a content vault should reach first successful harvest in ≤30 minutes following only what's documented here.

If the time-budget claim breaks for you, that's an A7 falsifier — please open an issue with the step where you got stuck and how long you'd been at it.

## Prerequisites

- macOS or Linux (the harvest tooling is shell + Python; not tested on Windows yet).
- Python 3.10+ on PATH.
- [`uv`](https://docs.astral.sh/uv/) — the tools use uv's inline-PEP-723 dependency declarations so you don't manage venvs explicitly. Install with `brew install uv` or follow upstream.
- [`gh`](https://cli.github.com) — the GitHub CLI, for cloning and (optionally) interacting with PRs.
- `claude` authenticated and on PATH — bootstrap checks this exists; if it's installed but not authenticated yet, run `claude` once interactively (it'll walk you through auth) before bootstrap so that the harvest pipeline's `claude -p` calls don't hit auth-expiry errors at runtime.
- [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) installed (`claude` on PATH). The skill runs inside Claude Code; the harvester also calls `claude -p` for compression.
- A content-vault GitHub repo you control (or somewhere local — see step 2). Empty repo is fine.
- The MCPs you want to harvest from — see [`docs/mcp-setup.md`](mcp-setup.md). At minimum, the Granola, Slack, or Gmail MCPs configured in your Claude Code account if you want to run the live-data harvest. For first setup you can skip MCPs and use the synthetic fixture path.

## Step 1 — clone this method repo

```
gh repo clone acardote/personal-assistant-ultra ~/Projects/personal-assistant-ultra
cd ~/Projects/personal-assistant-ultra
```

Path-of-clone is your call; the rest of this doc assumes the example path above.

## Step 2 — provision your content vault

Two options:

- **GitHub repo (recommended)**: create an empty private repo (e.g., `<your>-pa-vault`), then `gh repo clone <owner>/<your>-pa-vault ~/Projects/<your>-pa-vault`. This gives you cross-machine transfer + git history of your KB and memory objects.
- **Local-only directory**: just `mkdir ~/Projects/<your>-pa-vault`. Your content is local-only; you can add a remote later.

Either way, the path you'll point `.assistant.local.json` at is the vault checkout (or the local directory).

## Step 3 — run the bootstrap walker

From the method-repo root:

```
tools/bootstrap.py
```

The walker:
1. Asks you for the absolute path of your content vault.
2. Validates the path exists and is a directory.
3. Refuses if the vault is at-or-inside the method repo (would be the F1 pollution path #12 closes).
4. Writes `.assistant.local.json` (gitignored) at the method root.
5. Probes your environment: `claude` on PATH, `uv` on PATH, `git` on PATH, KB assembly clean against the configured vault.
6. Reports a concise pass/fail summary.

If it reports a problem, fix it as instructed and re-run. Bootstrap is idempotent — re-running with an existing config asks before overwriting.

## Step 4 — copy KB templates into your vault

Replace `<content_root>` below with the vault path you gave bootstrap (or use `python3 -c "import json; print(json.load(open('.assistant.local.json'))['paths']['content_root'])"` to read it back without `jq`):

```
VAULT=$(python3 -c "import json; print(json.load(open('.assistant.local.json'))['paths']['content_root'])")
mkdir -p "$VAULT/kb"
cp kb-templates/people.md.example   "$VAULT/kb/people.md"
cp kb-templates/org.md.example      "$VAULT/kb/org.md"
cp kb-templates/decisions.md.example "$VAULT/kb/decisions.md"
```

Open the three files in your vault and replace placeholder content with real entries about you, your org, and your durable decisions. The templates carry the format documentation inline; just fill in.

Then verify the assembled KB is well-formed:

```
tools/assemble-kb.py --check
```

This should print `clean` with the file count and token total. Fix any reported errors.

## Step 5 — first harvest (synthetic fixture)

For first-time setup, run the synthetic Slack fixture path to confirm the pipeline works end-to-end without depending on your live MCPs:

```
tools/harvest.py --source slack-fixture --since 2025-01-01
```

This will:
- Read the synthetic fixture at `tests/fixtures/slack/2026-04-15-spine-rollout-rollback.json` (a fake but realistic Slack thread).
- Render a raw-archive copy at `<content_root>/raw/slack_thread/<...>.md`.
- Compress it via `tools/compress.py` (calls `claude -p` for the editorial-judgment pass).
- Land a memory object at `<content_root>/memory/slack_thread/<...>.md` with frontmatter populated, including event_id (per #10's clustering).
- Update `.harvest/slack-fixture.json` dedup state.

If this completes without errors, your full pipeline works. The next step (live MCP harvest) is the same flow against your real data.

## Step 6 — first real harvest

The production scheduled trigger is a **Claude Code routine** (verified end-to-end against the auto-attached Slack/Gmail/Granola MCPs on 2026-05-05). Configure it via `/schedule` in Claude Code or at https://claude.ai/code/routines, following [`templates/routines/harvest-routine.md`](../templates/routines/harvest-routine.md) — it documents the cron, repos, MCP expectations, and the self-contained routine prompt.

For full silent-failure coverage, also configure the watchdog routine documented in [`templates/routines/watchdog-routine.md`](../templates/routines/watchdog-routine.md). It fires daily at a different time, runs the freshness check, and DMs you on Slack when state ≠ PASS — the in-skill freshness check (per #27) only fires when you invoke `/personal-assistant`, so the watchdog is what closes the rest of F1.

Once the routine is configured, you can:

- Wait for the next scheduled fire, or click **Run now** in the routines UI for an immediate harvest.
- Run on-demand harvests from your terminal with `tools/scheduled-harvest.py` — useful for "harvest since lunch" without consuming routine quota.

If you're on a tier without routine access (or want strictly local execution), the launchd alternative is documented at [`templates/launchd/`](../templates/launchd/). Same orchestration, different scheduler.

**Sources NOT covered by the routine path**: Google Meet folder watch and generic transcript drop. These are file-system-based — they need either local Meet-export sync or a folder you drop transcripts into — and neither exists in the routine sandbox. If you depend on those sources, run them ad-hoc via `tools/harvest.py --source gmeet --folder <path>` or `tools/harvest.py --source transcripts --folder <path>` from a Mac session, OR keep launchd active for those sources only. Pick one scheduler per vault — running both simultaneously will race on git push and dedup state (per the warning in `templates/routines/harvest-routine.md`).

For ad-hoc manual harvests, you can also open a Claude Code session in your method-repo checkout, invoke `/personal-assistant`, and ask it to harvest interactively — same skill, same code path.

## Step 7 — query

In a Claude Code session at the method-repo checkout:

```
tools/route.py "what's the latest on Acko launch certification?"
```

This runs the multi-agent router (advisor + adversarial critic + optional specialist) over your KB + retrieved memory objects.

## Working on multiple projects in parallel

By default, every Claude Code session you launch from the method-repo checkout writes to the single canonical content vault declared in `.assistant.local.json`. Two concurrent sessions on the same vault will serialize on the git working tree (one's commit blocks the other) or step on each other's untracked / staged state.

The `scripts/pa-session` helper (per [#214](https://github.com/acardote/personal-assistant-ultra/issues/214)) gives each project its own vault worktree at `<vault>/.pa-worktrees/<short>/` on branch `project/<short>`, routes the session to that worktree via the `PA_CONTENT_ROOT` env var, and isolates per-project commits / `.harvest/` / `.pa-active-project.json` state. Two concurrent sessions on different `<short>` slugs do not collide.

### Lifecycle

```
# Start a new project. First run also adds /.pa-worktrees/ to <vault>/.gitignore
# (committed on main) so the worktrees don't show up as untracked in the canonical
# vault's `git status`. Pass --auto to skip the confirmation prompt.
scripts/pa-session new q3-strategy "draft of the Q3 strategy doc"

# Pause: just exit claude. The worktree + branch + .pa-active-project.json persist.

# Resume: re-launches claude pointed at the existing worktree.
scripts/pa-session resume q3-strategy

# Enumerate sessions you have on disk. Default filter --status active --format table
# sorted by last_active desc. --status archived / all, --format json also supported.
scripts/pa-session list

# Close: archive the project (project.md status flip), commit the archive flip on the
# project branch, optionally merge back to main, and remove the worktree.
scripts/pa-session close q3-strategy --merge        # merge project/q3-strategy → main, then remove worktree
scripts/pa-session close q3-strategy --keep-branch  # leave branch standalone (e.g. sensitive projects); remove worktree only

# Reopen a project that was closed with --keep-branch (the branch is still present
# locally). Recreates the worktree from project/<short>, flips status back to active,
# drops archived_at, re-sets .pa-active-project.json, commits the un-archive flip,
# and launches claude. Refuses if the worktree already exists OR if the branch was
# deleted (the --merge close case — refusal message surfaces the manual recovery).
scripts/pa-session reopen q3-strategy

# Helpers:
scripts/pa-session path q3-strategy    # print absolute worktree path (for shell composition)
scripts/pa-session doctor              # self-check: gitignore entry, registered worktrees, orphan dirs, missing scaffolds
```

### What the helper sets up under the hood

For `pa-session new q3-strategy "...":`
1. First-run only: appends `/.pa-worktrees/` to `<vault>/.gitignore` and commits on main.
2. `git -C <vault> worktree add .pa-worktrees/q3-strategy -b project/q3-strategy`.
3. Calls `PA_CONTENT_ROOT=<vault>/.pa-worktrees/q3-strategy tools/project.py new q3-strategy "..."` to scaffold the project + write `.pa-active-project.json` inside the worktree.
4. `os.execvpe("claude", env={... PA_CONTENT_ROOT=<wt> ...})` — the session inherits the env-routed content_root.

For `pa-session resume`/`new` the spawned `claude` session reads `PA_CONTENT_ROOT` via `tools/_config.py:load_config()` and routes every path-resolving tool (`tools/project.py`, `tools/lint-provenance.py`, `tools/route.py`, etc.) to the worktree.

### Env vars exposed

- **`PA_CONTENT_ROOT`** — when set, `tools/_config.py:load_config()` honors this as the session's content_root, ignoring `.assistant.local.json`. The helper sets this on your behalf; you can also set it manually for ad-hoc routing.
- **`PA_QUIET=1`** — suppresses the `[pa] content_root via PA_CONTENT_ROOT = <path>` stderr breadcrumb. Useful for non-interactive runs (tests, scripts).
- **`PA_PROJECT_ID`** — set by `tools/project.py new` / `resume` on stdout (as a shell-source line). The skill activation contract reads this for per-turn project context.

### When NOT to use the helper

- Single-project workflows: if you only ever have one project active at a time, the canonical-vault flow remains the simplest path. Just `claude` from the method-repo root and `tools/project.py new` as before.
- Sensitive / one-off projects whose artefacts must never merge to the canonical vault: use `pa-session close --keep-branch`. The branch retains the history on disk but main never sees it.

## When things go wrong

- **`.assistant.local.json` not found warning**: tools fall back to the method root with a loud stderr banner. That's OK for fixtures; NOT OK for real harvest. Re-run bootstrap to create the config.
- **`assemble-kb.py` errors**: usually means your vault's `kb/` is missing files. Copy from `kb-templates/`.
- **`claude -p` hangs**: the compression pipeline calls `claude -p` headlessly. If your Claude Code authentication has expired, refresh it.
- **Cross-machine setup**: `git pull` on the vault on machine B; copy / re-create `.assistant.local.json` (it's gitignored — per-machine paths). MCP-specific creds (e.g., Gmail OAuth) live under `<content_root>/.harvest/*-credentials.json` and are gitignored — set them up on each machine.

## Time budget

Aim for ≤30 minutes from `gh repo clone` to first synthetic harvest output. Steps 1-5 take longer than 30 if you're setting up MCPs from scratch — that's MCP setup time, not method-repo setup time. If the *method-side* setup (steps 1, 3, 4, 5) takes more than 30 minutes, the A7 budget is broken and we want to know.

## Per-step success criteria

| Step | Success | Failure | Likely cause |
|---|---|---|---|
| 1 | Repo cloned, you can `cd` into it | clone fails | network, gh auth |
| 2 | Vault directory exists | nothing to clone / `mkdir` fails | path permissions |
| 3 | `tools/bootstrap.py` exits 0 with all checks green | reports specific failure | follow its instructions |
| 4 | `tools/assemble-kb.py --check` says clean, ~2-4K tokens | partial-truncation error | KB files missing in vault, copy templates |
| 5 | `tools/harvest.py --source slack-fixture --since 2025-01-01` writes raw + memory; subsequent runs are idempotent (skipped=1, new_memory=[]) | crash | `claude` not on PATH, `uv` not on PATH, or content-root misconfigured |
| 6 | Routine fires on schedule and produces real memory objects in vault | routine doesn't fire / silent failure | connector not authenticated, routine misconfigured (see `templates/routines/harvest-routine.md`), or quota exhausted |
| 7 | `tools/route.py "..."` returns advisor + critic perspectives | crash | KB assembly broken (step 4 regression) |
