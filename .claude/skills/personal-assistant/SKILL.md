---
name: personal-assistant
description: Personal assistant grounded in the layer-3 knowledge base — people, org, decisions, glossary — and the layer-2 memory objects compressed from the user's raw artifacts (Slack/Gmail/Granola/Meet/transcripts/docs). Use when the user wants help with their working life that should be informed by their own accumulated context, not a fresh-mint generic answer.
---

# personal-assistant

A Claude Code skill implementing the three-layer memory architecture defined in [issue #1](https://github.com/acardote/personal-assistant-ultra/issues/1) (Sei's "Your AI Assistant Has Amnesia" model adapted to a personal context).

## Method vs. content (per [#12](https://github.com/acardote/personal-assistant-ultra/issues/12))

This skill operates against **two repos**:

- **Method repo** (`acardote/personal-assistant-ultra`): code, schemas, prompts, ADRs, **`kb/glossary.md`** (canonical project terms). The skill itself lives here.
- **Content vault** (`getnexar/acardote-pa-vault`): your real `memory/`, `kb/{people,org,decisions}.md`, `.harvest/` state, `raw/`. Per-checkout location resolved from `<method-root>/.assistant.local.json`'s `paths.content_root`.

Tools resolve paths via `tools/_config.py`. When `.assistant.local.json` is missing or malformed, tools emit a LOUD stderr warning and fall back to the method root (OK for fixtures/tests; **NOT OK for real harvest** — it's the F1 pollution path #12 was scoped to close). Setup: copy `.assistant.local.json.example` → `.assistant.local.json` and edit `paths.content_root` to point at your vault checkout. End-to-end setup walkthrough lives in [#14](https://github.com/acardote/personal-assistant-ultra/issues/14)'s deliverables (`docs/setup.md` + `tools/bootstrap.py`).

## What "always in context" means here

The layer-3 knowledge base is the assistant's ground truth on the user, the user's org, the user's durable decisions, and project-specific terms. It must be loaded on every invocation.

| Layer | Lives at | Tracked? | What it holds |
|---|---|---|---|
| **Layer 1 — raw archive** | `<content_root>/raw/` | local-only by default (PII) | Unmodified raw artifacts (Slack threads, Gmail bodies, Meet transcripts). NOT loaded directly into context. |
| **Layer 2 — memory objects** | `<content_root>/memory/` | vault-tracked (real harvest); method's `memory/examples/` holds fixture-derived test outputs only | Editorially compressed memory objects. Loaded selectively per query (retrieval in `tools/route.py`; ranked retrieval in [#10](https://github.com/acardote/personal-assistant-ultra/issues/10)). |
| **Layer 3 — split** | `<method_root>/kb/glossary.md` (canonical project terms) + `<content_root>/kb/{people,org,decisions}.md` (your content) | both tracked | Always loaded. Combined assembly via `tools/assemble-kb.py`. Token budget ≤4K total. |

## Activation contract — load layer 3 first

When this skill is invoked, your **first action** is to load layer-3 by running:

```
tools/assemble-kb.py
```

(executed from the method-repo root). The output is the user's ground truth — combined from method-side `kb/glossary.md` and content-side `<content_root>/kb/{people,org,decisions}.md`. Treat its content as authoritative for facts about the user, the user's people, the user's org, the user's durable decisions, and project-specific terms. Quote KB entries by their `## <heading>` when citing.

Do not proceed with the user's request until layer 3 is loaded into your working context. If `tools/assemble-kb.py` fails or produces empty output, surface the failure to the user and stop — operating without layer 3 violates the "always-in-context" invariant on issue #4. If `.assistant.local.json` is missing, the assembler will emit a loud warning + fall back to method-root: that's fixture/test mode, not production; if you see the warning during a real session, **stop and tell the user to set up the config** before proceeding.

## Editorial discipline

- **Never invent KB entries.** If the user asks about something that isn't in the KB and isn't derivable from layer-2 memory objects (when retrieval lands), say so explicitly. Hallucinated grounding is the failure mode that poisons every downstream consumer (see falsifier F2 on issue #3 / #4).
- **Cite the KB.** When a claim about the user, their org, or a durable decision rests on a KB entry, mention which file/heading it came from. This makes drift detectable.
- **Honor the Bruno Method discipline.** Method-architectural decisions live in `<method_root>/docs/adr/*` (immutable, ADR-style); user-domain durable decisions live in `<content_root>/kb/decisions.md`. Don't propose closing claims without reconciliation against landed state; don't accept work without explicit falsifiers.

## How to extend the KB

The KB is split between method (canonical project terms) and content vault (user-specific). Edit the right side based on what you're adding:

- **Add a person, org/team entry, or durable user decision** — edit the file in your content vault, NOT the method repo:
  - `<content_root>/kb/people.md`
  - `<content_root>/kb/org.md`
  - `<content_root>/kb/decisions.md`
  - If you don't have these files yet, copy `<method_root>/kb-templates/{people,org,decisions}.md.example` into `<content_root>/kb/` as a starting scaffolding.
- **Update a project-specific term** (e.g., a glossary definition like "memory object", "harvester") — edit `<method_root>/kb/glossary.md`. These are method-canonical; everyone using the skill should share the same definitions.

After editing either side, run `tools/assemble-kb.py --check` from the method-repo root to verify the 4K token budget is still respected.

## How to add memory objects

The compression pipeline lives at `<method_root>/tools/compress.py` (see [issue #3](https://github.com/acardote/personal-assistant-ultra/issues/3)). Run from the method-repo root:

```
tools/compress.py <content_root>/raw/<source-kind>/<artifact>.md --kind <strategy|weekly|...>
```

The script reads `content_root` from `.assistant.local.json` and lands the output at `<content_root>/memory/<source-kind>/...`. With config missing it falls back to method's `memory/` with a loud warning — same fixture/test caveat as above.

Idempotent harvesters for Slack/Gmail/Granola/Meet/file-drop are tracked in [#5](https://github.com/acardote/personal-assistant-ultra/issues/5) / [#6](https://github.com/acardote/personal-assistant-ultra/issues/6) (orchestrated through this skill via MCPs once those reopen close); scheduled harvest in [#11](https://github.com/acardote/personal-assistant-ultra/issues/11).

## Harvest orchestration (per [#11](https://github.com/acardote/personal-assistant-ultra/issues/11))

When the user asks the skill to harvest (or the launchd routine fires `/personal-assistant harvest --since <when>`), follow this procedure source-by-source. Each source uses its MCP — Python-side Web-API and OAuth classes have been retired (#5/#6 reopen).

### Slack (via Slack MCP)

1. Read `<content_root>/.harvest/slack-allow.txt` if it exists for the user's explicit channel allow-list (one channel ID or `#name` per line). If absent, fall back to discovery via search.
2. **Discovery (when no allow-list)**: call `mcp__claude_ai_Slack__slack_search_channels` with name patterns `external-*`, `customer-*`, `partner-*` to find structurally-relevant channels. Combine with `mcp__claude_ai_Slack__slack_search_public_and_private` for `from:@me` posts since the cutoff to find activity-driven channels.
3. **Flagged threads**: search `has::pencil:` reactions placed by the user. Threads carrying the user's pencil reaction get harvested regardless of channel. (If the search operator isn't reliable, fall back to listing recent threads by the user and inspecting reactions per-thread.)
4. **Per thread to harvest**: call `mcp__claude_ai_Slack__slack_read_thread`. Render to a Markdown file with `## <iso> — user:<id>` headers per message (preserves speaker attribution per F3). Write to `<content_root>/raw/slack_thread/<channel>-<thread_ts>.md`.
5. Run `tools/compress.py <raw-path> --kind thread --source-kind slack_thread` to produce the memory object. Compress writes to `<content_root>/memory/slack_thread/...` and applies clustering per #10.
6. Update `<content_root>/.harvest/slack.json` with the new dedup_keys.
7. Append a per-thread line to today's daily digest (see digest format below).

### Gmail (via Gmail MCP)

1. Use a query scoped to a labeled / starred set the user maintains (default: `label:important newer_than:<since>` if no per-user override). Refuse to default to broad inbox harvesting (F2 mitigation).
2. For each thread returned: fetch the body via Gmail MCP, render to Markdown preserving headers (From, Subject, Date) and message boundaries, write to `<content_root>/raw/gmail_thread/<thread-id>.md`.
3. Compress + dedup as above.
4. Update `<content_root>/.harvest/gmail.json`.

### Granola (via Granola MCP)

1. Call `mcp__granola__list_meetings` (or `query_granola_meetings` if available) for items since the cutoff.
2. For each meeting: call `mcp__granola__get_meeting_transcript` (or equivalent) to fetch full content.
3. Render + write to `<content_root>/raw/granola_note/<meeting-id>.md`.
4. Compress + dedup. The dedup matching from #10 will cluster across sources — Granola + Meet + Gmail of the same meeting produce one canonical + alternates.
5. Update `<content_root>/.harvest/granola.json`.

### Google Meet transcripts

There is no public Meet MCP for transcripts. Use one of:
- A Drive folder synced locally where Meet auto-saves transcripts. Run `tools/harvest.py --source gmeet --folder <path>`.
- Manual file drop into the configured transcripts folder. Run `tools/harvest.py --source transcripts --folder <path>`.

The skill can invoke either via `Bash` if needed, but typically the user runs these directly because they involve filesystem watching, which is more naturally a CLI / cron pattern.

### Daily digest format

Each harvest run appends a section to `<content_root>/.harvest/daily/YYYY-MM-DD.md`:

```markdown
## 2026-05-05

### 07:07 — scheduled harvest
- slack: 8 new memory objects (channels: external-acko, partner-waymo)
- granola: 3 new
- gmail: 2 new (query: label:important)
- gmeet: 1 new
- transcripts: 0
- Errors: none

### 14:32 — on-demand harvest (you asked: "harvest since lunch")
- slack: 4 new (1 thread you flagged with :pencil:)
- granola: 1 new
- Errors: none
```

The file is append-only. Multiple runs in one day each get a timestamped section with a run-type marker (scheduled / on-demand) and a per-source count. Errors get their own line so silent-failure (F2 from #6) doesn't slip past — if an MCP is unreachable, surface it here AND in the routine's exit code.

### Cold-start (first run)

The first scheduled harvest after install is a 30-day backfill. The wrapper at `tools/scheduled-harvest.py` detects cold-start (no prior runs in `<content_root>/.harvest/runs/`) and widens the prompt's `--since` window. After that, each daily run uses `--since yesterday`. The user can later run a 90-day backfill on-demand by asking the skill to "harvest the last 90 days"; the dedup state from the 30-day window prevents duplicates (per #5 / #10).

### Known limitations of harvest orchestration (per PR #24 review)

- **Slack `has::pencil:` operator may not be a real Slack search operator.** The skill should attempt the search; if no results come back when the user has actually placed pencil reactions, fall back to listing the user's recent threads via `from:@<user>` and inspecting reactions per-thread. The first real harvest will tell us which approach Slack search supports.
- **`/personal-assistant harvest <args>` is interpreted as a prompt, not parsed as a slash command** (no `.claude/commands/personal-assistant.md` registers a parser). The skill receives the prompt as freeform text; argument resolution (especially time windows like "yesterday") is at the model's discretion. The wrapper at `tools/scheduled-harvest.py` constructs explicit `--since <N>d` strings to keep the time-window discrete.
- **MCP capability is not pre-checked at run start.** If a tool the skill expects (e.g., `mcp__claude_ai_Slack__slack_search_public_and_private`) has been renamed or removed, the run fails when the call is attempted. The run-status file captures this; the lint-docs CI gate doesn't catch MCP-tool drift. A future child can add a smoke probe at run start.
- **Bash tool permission inheritance in headless `claude -p` is not guaranteed.** The skill calls `tools/compress.py` via Bash; if the headless session doesn't have Bash permission, compression fails. The wrapper's run-status will show this failure mode if it fires.
- **Semantic F1 gap (claude exit 0 ≠ harvest success).** The wrapper at `tools/scheduled-harvest.py` writes `ok: true` whenever `claude -p` exits 0. That signals "the process didn't crash," NOT "the harvest produced meaningful output." Today, an empty-vault cold-start that hit MCP-auth failures and bailed would still show `ok: true` because the model decided to log-and-move-on rather than abort. The full F1 closure requires the skill to write a structured per-source result back to the wrapper (e.g., a `<harvest_dir>/runs/<ts>.harvest-result.json`) which the wrapper inspects to determine `ok`. Tracked as future work; for now, supplement the wrapper's binary `ok` with a manual look at the daily digest counts.
- **`git push` non-fast-forward on cross-machine contention.** When two machines (e.g. laptop + workstation) both try to push within seconds of each other, the second push fails with non-fast-forward. The wrapper currently exits 1 in that case (failure is loud, which is correct), but a future iteration could `git pull --rebase` and retry. For now: if you run on multiple machines, stagger the launchd `StartCalendarInterval` minutes so they don't collide.

## Open extensions

- Multi-fidelity event matching + ranked retrieval: [#10](https://github.com/acardote/personal-assistant-ultra/issues/10).
- Slack/Gmail/Granola/Meet via MCPs (skill orchestration): [#5](https://github.com/acardote/personal-assistant-ultra/issues/5) + [#6](https://github.com/acardote/personal-assistant-ultra/issues/6) reopens.
- Scheduled harvest routine + daily digest: [#11](https://github.com/acardote/personal-assistant-ultra/issues/11).
- Per-document-type expiry rules: [#8](https://github.com/acardote/personal-assistant-ultra/issues/8) (closed; integrated).
- Backup/migrate tooling: [#13](https://github.com/acardote/personal-assistant-ultra/issues/13).
- Setup docs + bootstrap: [#14](https://github.com/acardote/personal-assistant-ultra/issues/14).
- Evaluation harness: [#9](https://github.com/acardote/personal-assistant-ultra/issues/9).
