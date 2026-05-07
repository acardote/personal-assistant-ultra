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

## Activation contract — bootstrap session, then load layer 3

When this skill is invoked, your **first two actions** are:

1. **Bootstrap a metrics session** so the per-query instrumentation events from
   subsequent tools all share one `session_id`. This is what lets the dashboard
   group `query_start` → `kb_load_end` → `memory_retrieve_end` → `query_end`
   into a single user-facing turn (per [#41](https://github.com/acardote/personal-assistant-ultra/issues/41)). Run:

   ```bash
   export PA_SESSION_ID=$(openssl rand -hex 4)
   echo "PA session: $PA_SESSION_ID" >&2
   tools/log-event.py skill_start --inherit-session --data trigger=user
   ```

   Do this exactly once at turn start. The `echo` to stderr surfaces the
   session id so a missing/broken `openssl` produces a visible failure
   (empty session id → first tool mints its own; the inheritance contract
   relies on the env being set).

   Subsequent tool invocations within the same turn (route.py, assemble-kb.py,
   compress.py, check-harvest-freshness.py) will inherit the session via
   `PA_SESSION_ID` and emit events that aggregate correctly. If you skip
   this step entirely, the first tool to run will mint its own session and
   the rest will inherit that — within-turn aggregation still works, but
   the session start point is the first tool call rather than skill entry.
   Cross-turn isolation is unaffected.

2. **Load layer-3** by running:

```
tools/assemble-kb.py
```

(executed from the method-repo root). The output is the user's ground truth — combined from method-side `kb/glossary.md` and content-side `<content_root>/kb/{people,org,decisions}.md`. Treat its content as authoritative for facts about the user, the user's people, the user's org, the user's durable decisions, and project-specific terms. Quote KB entries by their `## <heading>` when citing.

Do not proceed with the user's request until layer 3 is loaded into your working context. If `tools/assemble-kb.py` fails or produces empty output, surface the failure to the user and stop — operating without layer 3 violates the "always-in-context" invariant on issue #4. If `.assistant.local.json` is missing, the assembler will emit a loud warning + fall back to method-root: that's fixture/test mode, not production; if you see the warning during a real session, **stop and tell the user to set up the config** before proceeding.

### Pre-flight: harvest freshness check (per [#27](https://github.com/acardote/personal-assistant-ultra/issues/27))

Immediately after layer-3 loads, run:

```
tools/check-harvest-freshness.py --quiet
```

(also from the method-repo root). The check reads `<content_root>/.harvest/runs/*.json`, finds the newest by mtime, and exits 0 only if the most recent run is `ok: true` AND younger than 26 hours (using `started_at` from the run-status payload, falling back to filesystem mtime). On non-zero exit it emits a stderr banner with one of these states:

- **STALE** — no successful run lately (older than threshold).
- **FAILED** — most recent run reported `ok: false`.
- **STUCK** — N (default 3) consecutive `ok: false` runs with the same `error`. The issue is chronic, not transient.
- **STUCK_AND_STALE** — STUCK conditions hold AND the newest run also exceeds the staleness threshold. Two distinct problems present.
- **MISSING** — no runs/ directory or no .json files in it (normal for a freshly-configured setup before the routine fires for the first time).
- **CORRUPT** — newest .json file is unparseable (truncated write or manual corruption — likely the last run crashed mid-write).

When the check exits non-zero, the banner already includes a state-appropriate summary (the `error` field is inlined, no extraction needed). Surface the banner verbatim to the user **before answering their request** and add remediation:

- **STALE**: the routine may have stopped firing — direct the user to https://claude.ai/code/routines (or their launchd plist if they're on the alternative path). If the banner also surfaces a last-run error (e.g., "git push failed"), pass that along too.
- **FAILED**: relay the banner's error inline; the most common cause is "critical connector enabled but not authenticated" (per the #25 / #26 §11 caveat).
- **STUCK**: more urgent than FAILED — same error repeated N times. Tell the user to fix the underlying issue *before* the next fire (re-auth a connector, fix a config typo, etc.). Don't suggest "wait and see."
- **STUCK_AND_STALE**: most urgent. The chronic error is masking a stalled scheduler — both problems need attention. Address the chronic error first (so the next fire might succeed), then verify the routine is actually firing.
- **MISSING**: the harvest has never run — in real use this either means the routine hasn't been configured yet (point the user at `templates/routines/harvest-routine.md`), or this is a fresh clone on a new machine and the user hasn't yet pulled the vault's first runs.
- **CORRUPT**: ask the user to inspect the file directly. Don't auto-fix or auto-delete.

If the check exits 0 silently, proceed with the user's request — the harvest is healthy.

**Honest scope note**: this check fires *only* when the user invokes the skill. It does not provide a true 26h SLA on detection — the detection window is bounded by user invocation cadence, not by the threshold. Out-of-band alerting (Slack self-DM from the harvest itself, daily-digest entry consumed by another surface) is tracked separately as follow-up work to fully close F1.

## Editorial discipline

- **Never invent KB entries.** If the user asks about something that isn't in the KB and isn't derivable from layer-2 memory objects (when retrieval lands), say so explicitly. Hallucinated grounding is the failure mode that poisons every downstream consumer (see falsifier F2 on issue #3 / #4).
- **Cite the KB.** When a claim about the user, their org, or a durable decision rests on a KB entry, mention which file/heading it came from. This makes drift detectable.
- **Honor the Bruno Method discipline.** Method-architectural decisions live in `<method_root>/docs/adr/*` (immutable, ADR-style); user-domain durable decisions live in `<content_root>/kb/decisions.md`. Don't propose closing claims without reconciliation against landed state; don't accept work without explicit falsifiers.

## Work execution procedure (per [ADR-0003](../../../docs/adr/0003-agent-output-taxonomy.md), [editorial rules](../../../docs/kb-editorial-rules.md))

When you **execute work** (draft, plan, analyze, recommend a decision) — beyond answering from memory — outputs are typed:

- **Knowledge** → in-place edits to `<content_root>/kb/{people,org,decisions}.md` (vault-scoped) or `<method_root>/kb/glossary.md` (method-scoped, PR-only). Per-kind triggers + diff shape in [docs/kb-editorial-rules.md](../../../docs/kb-editorial-rules.md).
- **Artefacts** → `<content_root>/artefacts/<kind>/art-<uuid>.<ext>` with YAML provenance frontmatter. Layout + sidecar rules in `<content_root>/artefacts/README.md`.

Apply the three-phase procedure below for any work-execution turn.

### Phase 1 — Pre-execute gather

Ground the work on KB + memory + (optionally) live calls — same retrieval as a question-answering query.

**Skip the gather** when this turn is a continuation of an in-flight conversation that already loaded the relevant context (e.g., the user just asked a related question, you answered, now they ask for a draft based on the answer). Re-grounding burns 30+ seconds of latency to re-read the same memory you already have in the prompt. The skip rule (concrete test): if the same `PA_SESSION_ID` already emitted `kb_load_end` + `memory_retrieve_end` AND the primary noun phrase from the current turn (proper nouns, project terms from glossary, issue refs) appears in either a previous turn's KB headings OR a memory id retrieved this session — proceed directly to Phase 2 with the context in hand. If neither overlaps, gather fresh.

### Phase 2 — Mid-execute capture (in-memory, not on disk)

While drafting, track:

- **Sources cited**: every KB heading, memory object id, or external URL that informed the output. Keep these as a mental list — they go into `produced_by.sources_cited[]` per ADR-0003. Use the canonical forms: `kb#heading`, `mem://<memory-id>`, `https://...`.
- **Output kinds proposed**: identify the primary kind per the kind selector in [editorial rules](../../../docs/kb-editorial-rules.md), and any secondary kinds the insight touches (e.g. a `decision` that names a person/org may produce secondary `person-update` / `org-update` diffs — one diff per kind).
- **Artefact body** if the output is artefact-shaped: draft the Markdown (or non-text payload) including the YAML frontmatter shape from `<content_root>/artefacts/README.md`.

Don't write anything to disk yet. The user hasn't approved.

### Phase 3 — Post-execute write-back (after delivering the answer/draft to the user)

Once the user has the answer/draft visible in chat:

1. **Propose** the diff(s) explicitly — show the file path, the proposed contents, and the `produced_by` provenance. For compound insights, propose ALL diffs together (one per kind) so the user sees the full set; explain that they can approve any subset.
2. **Wait for explicit user approval.** "yes", "approve", "go", "land that" all count. Idle conversation, "ok", "thanks" do NOT count — when ambiguous, ask. Silent writes are forbidden by ADR-0003.
3. **Write per-type, then lint, then commit + push**:
   - **Artefact**: write to `<content_root>/artefacts/<kind>/art-<uuid>.<ext>` (generate the UUID inline with `uuidgen` or Python's `uuid.uuid4()`). For exports, write the body file plus `<id>.provenance.json` sidecar in the same directory. Run `tools/lint-provenance.py --require-vault` before commit — it refuses malformed `produced_by` shape, missing sidecars, or non-canonical `sources_cited` entries. If the lint fails, fix the file and re-run; do not bypass. Then run `tools/live-commit-push.sh <content_root> "art: <kind> <short-title>"` — re-uses #74's commit-push helper with rebase-retry.
   - **Vault-scoped knowledge** (people / org / decision): apply the diff to the target file with the inline `<!-- produced_by: ... -->` comment per editorial rules. Run `tools/lint-provenance.py --require-vault` before commit (catches missing-comment-on-post-ADR-heading + non-canonical sources). Then `tools/live-commit-push.sh <content_root> "kb: <kind> <heading-or-summary>"`.
   - **Method-scoped knowledge** (glossary): open a PR against `acardote/personal-assistant-ultra` with the diff. Provenance lives in the PR description (NOT in `glossary.md`). The PR is the canonical record. CI runs `tools/lint-provenance.py --method-only` on glossary PRs to refuse accidental `<!-- produced_by -->` leakage.
4. **Confirm to the user** which commits landed. Run `git -C <content_root> rev-parse HEAD` (the helper itself doesn't print the SHA) and paste the result, or the PR URL for the glossary path.

### Non-interactive producers

The phases above assume an interactive Claude session with a human reviewing in chat. **Routines (harvest, watchdog) and any non-chat execution path** follow a reduced flow:

- Phase 1 still applies (gather context).
- Phase 2 still applies, but the mid-execute "kinds proposed" gets resolved differently:
  - Routines MAY produce **artefacts** of `kind=memo` (per ADR-0003 autonomous-producer carve-out) — the memo describes a candidate KB update WITHOUT proposing the diff. The next interactive session reads the memo and runs the full Phase 3 from there.
  - Routines MUST NOT update knowledge directly. No exceptions.
- Phase 3 collapses for routines: they write the memo artefact to disk, run `tools/live-commit-push.sh` (no human gate), and surface the memo path in the daily digest. There is no "user approves" step because there is no synchronous user.
- **Session id**: the activation contract's `PA_SESSION_ID` bootstrap is interactive-only. Routines mint their own at routine start (`export PA_SESSION_ID=$(openssl rand -hex 4)` in the routine prompt) so the artefact's `produced_by.session_id` is non-empty.

### Worked example

User has been chatting about live-call architecture. They say: *"draft a one-page memo capturing why we picked Option 2 for #51, suitable for sharing with the eng team."*

- **Phase 1**: skip — the same session has already retrieved memory about #51 in earlier turns. Context is loaded.
- **Phase 2**: kind = `artefact / memo` (primary). No secondary diffs — the memo describes a decision but the decision was already captured to `<content_root>/kb/decisions.md` earlier in the session, so this isn't a NEW decision update; the memo is a sharing artefact about an existing decision. Sources cited: the existing decisions.md heading, issue #51, the issue body. Draft the memo body in chat with proposed frontmatter (placeholders shown — generate real values at write time, never reuse these strings verbatim):
  ```yaml
  id: art-EXAMPLE-uuid-here       # generate fresh: uuidgen / uuid.uuid4()
  kind: memo
  ...
  produced_by:
    session_id: aaaaaaaa           # placeholder — replace with current 8-hex from $PA_SESSION_ID
    query: "draft a one-page memo capturing why we picked Option 2 for #51..."
    sources_cited:
      - kb#Live-call-orchestration-architecture
      - https://github.com/acardote/personal-assistant-ultra/issues/51
  title: Why we picked Option 2 for live-call orchestration
  ```
- **Phase 3**: propose the file path + body in chat. User reviews, says "approve." Skill writes to `<content_root>/artefacts/memo/art-<uuid>.md`, runs `tools/live-commit-push.sh <content_root> "art: memo on live-call architecture choice"`, then runs `git -C <content_root> rev-parse HEAD` and surfaces that SHA to the user.

In this example there are no secondary diffs because the decision already existed in `<content_root>/kb/decisions.md`. A counter-example with secondaries is in [editorial rules](../../../docs/kb-editorial-rules.md).

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

Idempotent harvesters for Slack/Gmail/Granola/Meet/file-drop are orchestrated through this skill via MCPs (per [#5](https://github.com/acardote/personal-assistant-ultra/issues/5)/[#6](https://github.com/acardote/personal-assistant-ultra/issues/6) reopens). The scheduled trigger is a Claude Code routine (per [#25](https://github.com/acardote/personal-assistant-ultra/issues/25), superseding the earlier launchd-only design from #11).

## Harvest orchestration (per [#11](https://github.com/acardote/personal-assistant-ultra/issues/11) + [#25](https://github.com/acardote/personal-assistant-ultra/issues/25))

The production scheduled trigger is a **Claude Code routine** — not launchd. The routine fires on Anthropic web infrastructure on its cron schedule, clones both the method and content-vault repos into its workspace, runs the orchestration below, and commits+pushes the vault. See `templates/routines/harvest-routine.md` for the canonical configuration the user creates via `/schedule`.

The launchd path (`templates/launchd/`) and `tools/scheduled-harvest.py` are alternatives for users on tiers without routine access or who want local-only execution.

Whether triggered by a routine, by launchd, or by an interactive Claude Code session ("/personal-assistant harvest since lunch"), the orchestration is the same — what differs is the trigger mechanism, the working-directory model, and the commit+push step (the routine's runtime handles git auth automatically; launchd inherits the user's gh; interactive sessions commit on the user's local machine).

When the user asks the skill to harvest (or the routine prompt invokes the skill's orchestration), follow this procedure source-by-source. Each source uses its MCP — Python-side Web-API and OAuth classes have been retired (#5/#6 reopen).

### Slack (via Slack MCP)

1. Read `<content_root>/.harvest/slack-allow.txt` if it exists for the user's explicit channel allow-list (one channel ID or `#name` per line). If absent, fall back to discovery via search.
2. **Discovery (when no allow-list)**: enumerate **comprehensively** — not just a sample. Call `slack_search_channels` with name patterns `external-*`, `customer-*`, `partner-*`, paginating via `cursor` until exhausted. Then `slack_search_public_and_private` for activity-driven channels not matching the prefix patterns, **using these explicit parameters** (per #67 fix — the defaults silently miss most channel posts):
   - `query: "from:<@U03LA1MHLG0> after:<cutoff>"` (substitute the user's Slack ID + cutoff date)
   - `sort: "timestamp"` — NOT the default `score`. Default `sort=score` ranks DMs above channel posts, so on a typical day the first 20 results are ALL DMs and channel posts fall off page 1 entirely. The routine has been silently missing channel-thread activity for this reason.
   - `channel_types: "public_channel,private_channel"` — exclude DMs/group-DMs from the channel-discovery search. (DM/group-DM harvesting is tracked separately in [#68](https://github.com/acardote/personal-assistant-ultra/issues/68).)
   - **Paginate via `cursor` until the API returns no more pages.** Don't stop at page 1 — a power-day will span multiple pages.
   Then channels whose threads carry the user's `:pencil:` reaction (flagged threads) regardless of channel name. The pencil-reaction search uses the same `slack_search_public_and_private` tool, so apply the **same sort=timestamp + channel_types=public_channel,private_channel + full pagination** rules to it (per #67). If the `has::pencil:` operator returns nothing despite known-flagged threads, fall back to listing the user's recent threads via `from:<@<USER_ID>>` (with the same parameters) and inspecting reactions per-thread.
3. **Per thread to harvest**: call `mcp__claude_ai_Slack__slack_read_thread`. Render to a Markdown file with `## <iso> — user:<id>` headers per message (preserves speaker attribution per F3). Write to `<content_root>/raw/slack_thread/<channel>-<thread_ts>.md`.
4. Run `tools/compress.py <raw-path> --kind thread --source-kind slack_thread` to produce the memory object. Compress writes to `<content_root>/memory/slack_thread/...` and applies clustering per #10.
5. Update `<content_root>/.harvest/slack.json` with the new dedup_keys.
6. Append a per-thread line to today's daily digest (see digest format below).
7. **Hard floor (per [#34](https://github.com/acardote/personal-assistant-ultra/issues/34))**: if a 30-day cold-start produces <5 Slack memory objects despite the user having known active channels, set `ok: false` on the run-status JSON with `error: "incomplete_slack_enumeration"`. This is a gate, not a log line — the freshness check + watchdog will surface the failure to the user. Right-shape number is dozens (channels × threads), not single digits.

### Slack DMs and group-DMs (via Slack MCP, per [#68](https://github.com/acardote/personal-assistant-ultra/issues/68))

DMs and group-DMs are a separate source kind — `slack_dm` — because they have a different participant model and dedup shape than channel threads (no channel name; D-prefix or G-prefix IDs only). Both 1:1 and multi-party DMs share the kind; participant count and IDs land in the rendered Markdown so compress can pick up the structure.

1. **Discovery**: call `slack_search_public_and_private` with:
   - `query: "after:<cutoff>"` (NO `from:@me` filter — that would only capture DMs the user authored in. A DM where the counterpart did the talking would be invisible. The user is a participant in every DM the search returns by virtue of having auth-scope visibility.)
   - `sort: "timestamp"`
   - `channel_types: "im,mpim"` — DMs only; channels are covered by the `slack_thread` procedure above.
   - Paginate via `cursor` until exhausted.
2. **Per DM**: extract `channel_id` (D-prefix for 1:1, G-prefix for group) and `message_ts`. For threaded DMs, use the parent message's ts; for top-level DM messages, use the message's own ts. Call `mcp__claude_ai_Slack__slack_read_thread`. The same tool that reads channel threads handles DMs identically (verified A1 probe 2026-05-07).
3. **Render** to `<content_root>/raw/slack_dm/<channel-id>-<thread-ts>.md` with `## <iso> — user:<id>` headers per message (same shape as `slack_thread`). Include a leading line listing participants by user ID so compress can preserve the conversational structure.
4. **Compress** via `tools/compress.py <raw-path> --kind thread --source-kind slack_dm`. The output lands at `<content_root>/memory/slack_dm/...`. The existing `thread` prompt is reused (per A4 — if quality turns out poor, F2/F3 on #70 cover that and a DM-specific prompt is the follow-up).
5. **Update** `<content_root>/.harvest/slack_dm.json` with the new dedup_keys (shape: `slack_dm-<channel-id>-<thread-ts>`).
6. **Append** to today's daily digest as a separate per-source line (`slack_dm: N new`).

**Privacy posture**: DMs typically contain more candid / unfiltered content than channel threads. Same vault-only storage as other `raw/` artifacts; no automatic sharing. If a DM is on a topic the user explicitly considers off-limits for memory, they can `.harvest/slack-dm-deny.txt` (one channel-id per line) to block it from harvest — that file is read-first like `slack-allow.txt`. (This deny-list mechanism is a follow-up; default behavior today is harvest-all-DMs-from-active-window.)

### Gmail (via Gmail MCP)

1. Use a query scoped to a labeled / starred set the user maintains (default: `label:important newer_than:<since>` if no per-user override). Refuse to default to broad inbox harvesting (F2 mitigation).
2. For each thread returned: fetch the body via Gmail MCP, render to Markdown preserving headers (From, Subject, Date) and message boundaries, write to `<content_root>/raw/gmail_thread/<thread-id>.md`.
3. Compress + dedup as above.
4. Update `<content_root>/.harvest/gmail.json`.

### Granola (via Granola MCP)

The Granola MCP exposes a single tool `query_granola_meetings` with shape `{query: string, document_ids?: uuid[]}`. Per #34, the cold-start harvest produced only 1 Granola memory object because the prior orchestration phrasing led the model to fetch a single most-recent meeting. Use the explicit two-step pattern:

1. **Enumerate**: call `query_granola_meetings` with `{"query": "List all my meetings since <cutoff-date>"}` (substitute cutoff). The response is a list of meeting metadata (titles, dates, possibly UUIDs). Parse and accumulate.
2. **Fetch each body**: for every meeting in the enumeration, call `query_granola_meetings` again with either `{"document_ids": ["<uuid>"]}` (preferred when UUIDs are visible) or `{"query": "Show me the full notes from <title> on <date>"}` (fallback). Each call returns ONE meeting's body.
3. **Write each**: render to `<content_root>/raw/granola_note/<meeting-uuid-or-slug>.md`, compress with `--kind note --source-kind granola_note`.
4. Dedup matching from #10 will cluster across sources — Granola + Meet + Gmail of the same meeting produce one canonical + alternates.
5. Update `<content_root>/.harvest/granola.json`.

**Hard floor**: probe 5 (2026-05-05) showed 40 meetings in 14 days. A 30-day cold-start should yield 30+ meetings unless the user has gaps. If you produce <10 Granola memory objects on cold-start, set `ok: false` with `error: "incomplete_granola_enumeration"`. Gate, not log.

**Retry policy on the enumeration query** (apply mechanically): single-meeting body queries are fast. The enumeration query is the risky one. If the enumeration query times out, wait 30s and retry once with the same query. If THAT also times out (2 timeouts on the same query), fall back to weekly pagination: 4 separate `query_granola_meetings` calls each with `{"query": "List all my meetings between <YYYY-MM-DD> and <YYYY-MM-DD>"}` substituting explicit week boundaries. Each weekly call gets its own retry-once-on-timeout budget. Concatenate results before per-meeting fetch. If a weekly call fails twice, log that week to errors and skip — partial coverage beats nothing.

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
- **`tools/scheduled-harvest.py` constructs explicit `--since <N>d` strings** to keep routine-driven time windows discrete (the routine doesn't go through the slash command — see "Routine ops surface" below for the interactive entry point).
- **MCP capability is not pre-checked at run start.** If a tool the skill expects (e.g., `mcp__claude_ai_Slack__slack_search_public_and_private`) has been renamed or removed, the run fails when the call is attempted. The run-status file captures this; the lint-docs CI gate doesn't catch MCP-tool drift. A future child can add a smoke probe at run start.
- **Bash tool permission inheritance in headless `claude -p` is not guaranteed.** The skill calls `tools/compress.py` via Bash; if the headless session doesn't have Bash permission, compression fails. The wrapper's run-status will show this failure mode if it fires.
- **Semantic F1 gap (claude exit 0 ≠ harvest success).** The wrapper at `tools/scheduled-harvest.py` writes `ok: true` whenever `claude -p` exits 0. That signals "the process didn't crash," NOT "the harvest produced meaningful output." Today, an empty-vault cold-start that hit MCP-auth failures and bailed would still show `ok: true` because the model decided to log-and-move-on rather than abort. The full F1 closure requires the skill to write a structured per-source result back to the wrapper (e.g., a `<harvest_dir>/runs/<ts>.harvest-result.json`) which the wrapper inspects to determine `ok`. Tracked as future work; for now, supplement the wrapper's binary `ok` with a manual look at the daily digest counts.
- **`git push` non-fast-forward on cross-machine contention.** When two machines (e.g. laptop + workstation) both try to push within seconds of each other, the second push fails with non-fast-forward. The wrapper currently exits 1 in that case (failure is loud, which is correct), but a future iteration could `git pull --rebase` and retry. For now: if you run on multiple machines, stagger the launchd `StartCalendarInterval` minutes so they don't collide.

## Live-call gap detection (per [#39](https://github.com/acardote/personal-assistant-ultra/issues/39))

`tools/route.py` checks whether memory looks insufficient for the current query and emits a `gap_detected` metric event when so. Two triggers:

- **zero_hit** — `load_memory_objects` returned 0 matches for the query keywords.
- **topic_pinned** — the query mentions a topic listed in `<content_root>/.harvest/live-pinned.txt`. One topic per line; blank lines and lines starting with `#` are ignored. Matching is **case-insensitive, word-boundary** against the raw query (so pinned `sync` matches `"the sync"` but not `"asynchronous"`). Use this for fast-evolving topics (e.g. weekly syncs) where harvest cadence lags reality. Pin entries surface in metrics events as a bounded `matched_topic` (≤64 chars), so keep them short and non-sensitive.

#39-A emits the signal; #39-B implements the live-call adapters. The skill is the live-call orchestrator (per the architecture decision on #51): when `route.py --json` returns `gap_detected: true`, the skill makes the live MCP call, captures the result via `tools/live-result-write.py`, and folds the findings into the answer. Live calls are sequential (not parallel) and per-source.

If the user's question requires *producing* something (a memo, draft, plan, analysis, KB update) — not just answering from gathered context — also follow the [Work execution procedure](#work-execution-procedure-per-adr-0003-editorial-rules) above. Live findings count as `sources_cited` in the artefact's frontmatter.

### Live-call procedure (Granola — #39-B.1)

When you'd otherwise answer a question and the gap signal points at meetings / weekly-sync content:

1. **Probe the gap.** Run `tools/route.py "<query>" --json --no-critic --no-specialist` and parse the JSON. If `gap_detected` is `true` AND the query reads as meeting-relevant (mentions a meeting cadence, attendee names, or a topic from `live-pinned.txt`), proceed with a live Granola call. If `gap_detected` is `false`, answer normally from memory + KB — **don't fire live calls speculatively.**
2. **Capture the start ts and emit `live_call_start`.** Record `start_iso = <ISO-with-ms>` and run `tools/log-event.py live_call_start --inherit-session --data source=granola_note --json-data start_iso=\"$start_iso\"`. The start ts gets passed back to the helper in step 4 so latency measurement (F4 on #52) is robust to MCP failures.
3. **Targeted Granola query.** Call `mcp__claude_ai_Granola__query_granola_meetings` with `{"query": "<the user's question, lightly rephrased to Granola's natural-language style>"}`. Unlike harvest's two-step enumerate-then-fetch (#34), live mode is single-shot — Granola's natural-language path is fine for a focused query, and the latency budget (<30s p95 from #39) doesn't allow the two-step pattern.
4. **Capture the result.** Pipe the raw response body to `tools/live-result-write.py --source granola_note --query "<original user query>" --start-iso "<start_iso>"`. The helper writes `<content_root>/raw/live/granola_note/<ts-with-ms>-<hash>.md` with a leading provenance HTML comment, and emits a `live_call_end` event with `status=success` and `duration_ms` so the dashboard's `live_calls_per_query` and live latency p95 stay accurate.
5. **Fold findings into the answer.** Treat the live response as if it were just-loaded memory: cite specifics, prefer it over staler memory hits, and quote where the user's intent is "what's the latest." Do not duplicate the live findings in your response when they merely confirm memory — fold them inline.
6. **Inline write-back AFTER answering** (per [#74](https://github.com/acardote/personal-assistant-ultra/issues/74)): once the user has the answer on screen, run `tools/live-writeback.py --source granola_note` to compress the just-written raw artifact into memory, then `tools/live-commit-push.sh <content_root> "live: <query-hash> granola"` to commit + push with rebase-retry on non-fast-forward. Foreground (~30–60s); the user already has the answer, so this delays only the next prompt. Skip if the live call returned `status=empty` — there's nothing to compress.

The path-separation (`raw/live/<source>/` ≠ `raw/<source>/`) keeps live artifacts away from harvest's compress + dedup paths so harvest doesn't accidentally pollute memory with no-provenance live notes; `live-writeback.py` does the compress with `--provenance live` so the resulting memory object is correctly tagged.

**Failure paths**: if the Granola MCP call fails (timeout, auth, no results), emit a `live_call_end` event with `status=error` (or `status=timeout`) via `tools/log-event.py live_call_end --inherit-session --data source=granola_note --json-data status='"error"'`, **NOT** a separate `live_call_error` event — the unified-event approach (per pr-challenger C3 on #53) keeps the dashboard's start/end pairing intact so latency p95 doesn't get biased low by orphan starts. Then proceed with memory-only answer, surfacing the gap to the user (e.g., *"I don't have current notes on this — Granola was unavailable just now"*).

**Privacy note**: the helper writes the user's query verbatim into the artifact's leading HTML comment (provenance trail). `<content_root>/raw/live/` files inherit the same privacy posture as harvest's `raw/` artifacts (they contain user content). The metrics events file remains PII-filtered per `_metrics.py`'s denylist.

### Live-call procedure (Slack — #39-B.2)

Same shape as the Granola path; differences below.

1. **Probe the gap.** As above. Slack-relevant queries: people-by-channel-mention, status-of-X-thread, recent-discussion-of-Y. If `gap_detected` is `false` AND the question doesn't read as a Slack-conversation question, answer from memory.
2. **Capture the start ts and emit `live_call_start`** with `source=slack_thread`. Same shape as Granola.
3. **Two-step Slack call** (single-shot doesn't fit Slack's MCP shape — search returns snippets, threads need a separate read):
   - Call `mcp__claude_ai_Slack__slack_search_public_and_private` with `{"query": "<topic terms from the user's question>"}`. Inspect the top result(s) for relevance — Slack search ranks by recency by default, so a recent post about the topic beats stale ones.
   - Call `mcp__claude_ai_Slack__slack_read_thread` on the top match's thread. If the top match is mid-thread, the read returns the full thread context.
   - Repeat the read for at most 2 additional matches if the first thread didn't answer the question. Hard cap: **3 thread reads per live call**, to stay within the <30s budget.
4. **Capture the result.** Render the thread(s) to Markdown with `## <iso> — user:<id>` per message (same format as harvest writes for `slack_thread`), concatenate, and pipe to `tools/live-result-write.py --source slack_thread --query "<original user query>" --start-iso "<start_iso>"`. Helper writes to `<content_root>/raw/live/slack_thread/<ts-ms>-<hash>.md`.
5. **Fold findings into the answer.** Quote speaker attribution where it matters (per #5/#6 F3: "the speaker matters as much as the content"). If multiple threads contribute, group by channel.
6. **Inline write-back AFTER answering** — same as Granola: `tools/live-writeback.py --source slack_thread` then `tools/live-commit-push.sh <content_root> "live: <query-hash> slack"`. Skip when `status=empty`.

**Failure paths**: same contract as Granola — emit `live_call_end` with `status=error` / `status=timeout`, NOT a separate `live_call_error` event. Surface gaps to the user (e.g., *"I couldn't find a current thread on this — Slack search returned nothing relevant"*).

**Slack-specific over-firing risk**: Slack search is more permissive than Granola's natural-language path; it will return *something* for almost any query. Don't trust the top result blindly — if the snippet doesn't clearly relate to the question, skip the read and emit `status=empty` rather than fetching irrelevant threads (#39-B.2's F1).

### Live-call procedure (Gmail — #39-B.3)

Same shape as Slack (two-step search → read); differences below.

1. **Probe the gap.** As above. Gmail-relevant queries: status of an email thread, a vendor / partner conversation, a contract / renewal lookup. If `gap_detected` is `false` AND the question doesn't read as an email-thread question, answer from memory.
2. **Capture the start ts and emit `live_call_start`** with `source=gmail_thread`.
3. **Two-step Gmail call** (search returns thread metadata, body fetch is separate):
   - **Label scope** — match the harvest procedure's: default to `label:important`, but if the user maintains a different curated label (the same per-user override the harvest section refers to at "if no per-user override"), use that. Live MUST use the same label as harvest — diverging would silently search a label the user doesn't curate, exactly the failure F2 on #5/#6 was meant to prevent.
   - **Time filter** — default `newer_than:30d` to bound noise on un-anchored questions. **Drop the filter ONLY when the user's question contains an explicit date or period reference older than 30 days** (e.g. "what did the contract say in February", "last quarter's renewal") — substitute `after:YYYY-MM-DD before:YYYY-MM-DD`. Date-free historical asks ("did marketing renew", "what did legal say about the MSA") stay in the 30-day window — the live signal will likely show `status=empty`, which is the right cue to fall back to memory rather than expanding the search blindly (per F5 on #56).
   - **Tool selection** — the Gmail MCP exposes auth tools (`mcp__claude_ai_Gmail__authenticate`, `mcp__claude_ai_Gmail__complete_authentication`) plus search/read tools that surface only after auth. Use the same MCP tools harvest uses; check the running session's tool list rather than hardcoding names that may evolve.
   - **Hard cap: 2 thread reads per live call** (provisional — Gmail threads typically denser than Slack; revisit at Move 5 once `live_call_end.duration_ms` data exists). Body cap (`MAX_BODY_CHARS=65536`) inherited from the helper.
4. **Capture the result.** Render to Markdown preserving headers (From, Subject, Date) and message boundaries — same format harvest writes for `gmail_thread`. Pipe to `tools/live-result-write.py --source gmail_thread --query "<original user query>" --start-iso "<start_iso>"`. Helper writes to `<content_root>/raw/live/gmail_thread/<ts-ms>-<hash>.md`.
5. **Fold findings into the answer.** Quote sender + date when citing — for email, "who said what when" is load-bearing context. If multiple threads contribute, group by subject.
6. **Inline write-back AFTER answering** — same as Granola/Slack: `tools/live-writeback.py --source gmail_thread` then `tools/live-commit-push.sh <content_root> "live: <query-hash> gmail"`. Skip when `status=empty`.

**Failure paths**: same contract — emit `live_call_end` with `status=error` / `status=timeout` via `tools/log-event.py`, NOT a separate `live_call_error` event. Surface the gap to the user (e.g., *"I couldn't find a labeled-important thread on this — Gmail returned nothing relevant"*).

**Gmail-specific risks**:
- **Label-scope leak**: if the live query slips to broad inbox search (no `label:important`), F2 from #5/#6 fires — noise pollutes the answer and any future write-back. The procedure's "default scope to label:important" rule is honor-system; F2 below catches drift in production.
- **Long threads**: legal / vendor email threads can be 40+ messages. The body cap from #39-B.2 (MAX_BODY_CHARS) protects against context blow-up; check `body_truncated=true` rate on the dashboard.

### Write-back: live findings → memory (#39-D)

Live raw artifacts written by `live-result-write.py` accumulate in `<content_root>/raw/live/<source>/`. They explicitly do NOT compress at fetch time — that latency would blow the <30s p95 query budget. Compression happens out-of-band via:

```
tools/live-writeback.py             # process all sources
tools/live-writeback.py --source granola_note   # one source only
tools/live-writeback.py --dry-run   # list, don't move
```

For each unprocessed file, the tool runs `tools/compress.py <file> --source-kind <source> --provenance live`. The `--provenance live` flag does two things:
1. Adds `provenance: live` to the memory object's frontmatter — the dashboard / future analytics can distinguish live-born memory from harvest-born.
2. Strips the `live/` segment when deriving the memory path, so the resulting object lands at `memory/<source>/<file>.md` alongside harvest-fetched memory. The existing #10 event-id dedup catches dupes across pipelines without separate state.

Successfully-compressed raw files are moved to `<content_root>/raw/live/<source>/.processed/`. Failed compresses leave the file in place for the next run to retry.

**When to invoke** (per [#74](https://github.com/acardote/personal-assistant-ultra/issues/74)):
- **Per-query (primary)**: every live-call procedure above ends with `live-writeback.py` + `live-commit-push.sh` after the user has their answer. Memory is up-to-date on origin within ~30–60s of the live call landing.
- **Daily harvest routine (catch-up)**: the routine ends with one final `live-writeback.py` + commit pass to absorb anything the per-query path missed (machine off, skill exited ungracefully, push collisions that didn't recover).
- **Manual escape hatch**: `/personal-assistant live-writeback` (slash command) for ad-hoc clearing of any backlog the operator wants to inspect first.

### Cross-source synthesis (#39-C)

When `gap_detected` fires with `reason=zero_hit`, the procedure says "fire all sources." Multiple `live_call_end` events can land in one query. The skill's job at that point is to merge memory + KB + multiple live findings into one coherent answer, not stitch them as labeled sections. Rules in priority order:

1. **Freshness wins for "what's the latest" / "current state" intent.** When the user's question is anchored to *now*, prefer live findings over memory hits even when memory's sample is broader. Example from the 2026-05-06 eval Q08 ("last 1-1 with Leonor"): full-skill scored 5 because the granola live call surfaced the current 1-1 ahead of an older memory hit.
2. **Memory wins for established / load-bearing context.** When the question asks about durable state ("what did we decide about pricing?"), prefer memory. Live calls are recency-biased and can surface ephemera that contradicts a settled decision. Cite KB / memory `## <heading>` and treat live as supporting detail.
   - **Rule 1/2 tiebreaker for hybrid intent** (e.g. *"what's the current pricing?"* — both durable AND now-anchored): treat live and memory as both authoritative; if they agree, cite both as confirmation; if they disagree, fall through to Rule 3.
3. **Surface conflicts when you notice them.** Conflict detection is honor-system — there is no automated polarity check. The minimum operational test: if memory and live name the same entity AND attach a different value (date, status, decision, number), and the timestamps differ by more than the harvest-cadence window (~24h), treat as a conflict candidate. Render: *"Memory (compressed 2026-05-04) says X; live Slack today says Y. Treating live as authoritative because [reason]."* Don't fabricate conflicts where the difference is tense or phrasing only.
4. **Deduplicate by event, not by source.** When the same meeting / thread appears in multiple sources (e.g. a Granola note AND a Slack channel-recap), cite once with the strongest source — don't repeat the same fact wearing different labels. (#10 dedup logic does this for memory; live findings need to do it inline.)
5. **Don't expose source-by-source structure.** Bad: *"## From Granola: ... ## From Slack: ..."* — group-by-source headers in the answer. Good: a unified answer that cites sources inline by `## <heading>` per the per-source procedures (Slack `## <iso> — user:<id>`, Gmail From/Subject/Date, Granola meeting title). Inline citations are fine and load-bearing — *grouping the entire answer by source is the anti-pattern*. The 2026-05-05 eval rated source-stacked answers low ("contains an adversarial critic that I don't care to be exposed to") — same shape, same fix.
6. **Surface empty live calls when memory's claim is load-bearing.** Don't pretend the live call confirmed memory. If granola fired and returned empty AND memory's claim is what the user will act on, surface it: *"Memory has X; granola live returned no current notes on this topic, so X may be stale."* If memory's claim is incidental color, mentioning the empty live adds noise — drop it.

## Routine ops surface (`/personal-assistant`, per [#59](https://github.com/acardote/personal-assistant-ultra/issues/59))

For operator tasks that don't need the full skill activation contract (no KB load, no freshness pre-flight), use the slash command at `.claude/commands/personal-assistant.md`:

- `/personal-assistant metrics [--days N | --since … --until …]` — refresh the dashboard.
- `/personal-assistant freshness-check [--quiet|--json|--stuck-threshold N]` — surface harvest health.
- `/personal-assistant harvest [<scope>]` — on-demand harvest, e.g. `since yesterday`, `last 90 days`, `slack only`. Defaults to `since yesterday`.
- `/personal-assistant live-writeback [--source <kind>|--dry-run]` — fold accumulated live findings into memory after a live-call-heavy session.

The slash command is operator-task-shaped: it doesn't run the freshness pre-flight check before dispatch (re-running freshness-check on every dashboard refresh is noise). For free-form questions and exploratory work, address the skill directly via prose — that path runs the full activation contract.

A future operator-task entry that *produces content* (e.g., `/personal-assistant draft …`) MUST go through the [Work execution procedure](#work-execution-procedure-per-adr-0003-editorial-rules) for write-back — operator tasks don't bypass the diff-and-approve floor.

Empty / unknown subcommands list the valid set rather than silently falling through to skill activation, so typos surface as typos.

## Open extensions

- Multi-fidelity event matching + ranked retrieval: [#10](https://github.com/acardote/personal-assistant-ultra/issues/10).
- Slack/Gmail/Granola/Meet via MCPs (skill orchestration): [#5](https://github.com/acardote/personal-assistant-ultra/issues/5) + [#6](https://github.com/acardote/personal-assistant-ultra/issues/6) reopens.
- Scheduled harvest via Claude Code routine: [#25](https://github.com/acardote/personal-assistant-ultra/issues/25) (closed; supersedes the launchd path from #11). Out-of-band watchdog alerting: [#32](https://github.com/acardote/personal-assistant-ultra/issues/32) — see `templates/routines/watchdog-routine.md`.
- Per-document-type expiry rules: [#8](https://github.com/acardote/personal-assistant-ultra/issues/8) (closed; integrated).
- Backup/migrate tooling: [#13](https://github.com/acardote/personal-assistant-ultra/issues/13).
- Setup docs + bootstrap: [#14](https://github.com/acardote/personal-assistant-ultra/issues/14).
- Evaluation harness: [#9](https://github.com/acardote/personal-assistant-ultra/issues/9).
