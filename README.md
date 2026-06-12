# personal-assistant-ultra

> **Status**: `v0.12.2` shipped (PATCH) — two harvest-reliability fixes, both closed-and-reconciled and production-confirmed. **(1) parent #279 — `kb-drift-scan` crash-loop + unbounded hang.** Since ~2026-05-16 every harvest fire crashed `kb-drift-scan` on `DriftVerdict(**verdict_data)` over **82 pre-schema cache entries** (no cache-schema version), and `call_claude` had no `claude -p` timeout (the >500min task). **#281/#282** add tolerant verdict construction (poison entries evicted + re-judged) + a `--llm-timeout`, and generalize the watermark gate so a quota-skipped/timed-out/evicted pair never advances the watermark. Confirmed in production: the 06-12 fire logged `kb_drift_scan.status: clean` — first clean exit since 05-26, 12 poison entries evicted. Backlog self-drains over fires (A3 accepted; optional one-shot acceleration #287). **(2) parent #283 — harvest ensure-main stash-pop recurrence.** The ensure-main step ran `git checkout main` onto a **stale local `main`** (never FF'd; 1→23 commits behind) then `git stash pop`, conflicting on `.harvest/*.json` state files (and on 06-11 cascading into a false `vault-desync-probe` `rc=6` via the `live-commit-push.sh` wrapper remap). **#286** inserts `git merge --ff-only origin/main` before the pop — `--ff-only` refuses on divergence (no `reset --hard`), so it's data-safe; + a regression test. Both fixes diagnosed via fleet workflows with independent `pr-challenger` review (data-safety proven). Known pre-existing test drift: **#280** (`bug`). Prior release `v0.12.1` — **parent #274**, watchdog false-`STALE` fixed by selecting newest run by filename ts not mtime (#276/PR #278). Carry-over: `v0.12.0` **#178** write transport; `v0.11.x` desync detect+refuse+recover stack (parent #249). From `v0.10.4`: **#239** (`bug`) — 79 stuck `.unprocessed/` memos. Open carry-overs: parent #183 (#186 pending), parent #173 (kb-process autonomy), the #263 stranding-backlog children (#264-267).
> `latest` tracks the most recent immutable release tag — currently `v0.12.2`. See [`RELEASE.md`](RELEASE.md) for the policy.

A Claude Code skill that gives Claude a long-term, three-layer memory architecture (raw archive → editorially compressed memory objects → always-in-context knowledge base) over your accumulated working context (Slack, Gmail, Granola, Google Meet, transcripts, docs).

Architecture follows Leo Sei's ["Your AI Assistant Has Amnesia"](https://compoundandcare.substack.com/p/your-ai-assistant-has-amnesia-heres). Project discipline follows the [Bruno Method](https://www.olympum.com/bruno-method/SPEC.md).

## What's in here

- **Method (this repo)** — code, schemas, prompts, skill definition, ADRs, project glossary, KB templates. Public-shareable.
- **Content vault (separate repo)** — your real `memory/`, `kb/{people,org,decisions}.md`, `.harvest/` state, `raw/`. Per-checkout location resolved from `.assistant.local.json`. Per the user-facing path discipline, real content lives in your private content vault — see [setup](docs/setup.md).

The split exists so the method evolves independently from your content. See parent issue [#1](https://github.com/acardote/personal-assistant-ultra/issues/1) for the full design.

## Quick start

If you want the deep version, jump to [`docs/setup.md`](docs/setup.md). The short path:

1. **Clone this repo** — `git clone git@github.com:acardote/personal-assistant-ultra.git`
2. **Create a content vault repo** — a private repo somewhere you control. Empty is fine.
3. **Clone the vault locally** — anywhere; commonly `~/Projects/<your>-pa-vault`.
4. **Run the bootstrap walker** — `tools/bootstrap.py` interactively creates `.assistant.local.json` pointing at the vault, verifies the local environment (claude/uv/git on PATH, vault writable, KB assembly clean), and runs a harvest dry-run smoke to catch pipeline-import regressions. It does NOT verify MCP availability or run live harvests; that's step 6 of [`docs/setup.md`](docs/setup.md).
5. **Invoke the skill** — open a Claude Code session in this method-repo checkout and run `/personal-assistant`.

If `tools/bootstrap.py` says everything's healthy, you're ready. If not, it tells you what's missing with prescriptive next steps.

## Layout

```
personal-assistant-ultra/      # this repo (method)
├── .claude/skills/personal-assistant/SKILL.md   # the skill
├── tools/                                        # compress, harvest, route, prune, dedup, ...
│   └── prompts/                                  # editorial-judgment + advisor + critic prompts
├── docs/
│   ├── adr/                                      # architecture decisions (storage, ...)
│   ├── schemas/                                  # memory-object schema (canonical)
│   ├── setup.md                                  # full setup walkthrough
│   └── mcp-setup.md                              # MCP configuration pointers
├── kb/glossary.md                                # canonical project terms
├── kb-templates/                                 # template KB files (copy into your vault)
├── tests/                                        # acceptance + lint tests
├── .bruno/                                       # Bruno Method discipline config
└── .assistant.local.json.example                 # config template (copy + edit)
```

Real content (`memory/{...}`, `kb/{people,org,decisions}.md`, `raw/`, `.harvest/<source>.json`) lives in your content vault (resolved from `.assistant.local.json`'s `paths.content_root`), not here. The method repo has no real user content; only synthetic fixtures under `memory/examples/` and `tests/fixtures/`.

## What the skill does

- **Loads layer-3 KB on every invocation** (people / org / decisions in your vault + glossary in this method repo). The skill stops if KB load fails — operating without ground truth is the failure mode #4 was scoped to prevent.
- **Compresses raw artifacts into memory objects** via editorial-judgment LLM prompt; provenance preserved (`source_uri`); body target ≤800 tokens; per-document-type expiry.
- **Multi-fidelity event matching**: when the same event lands via Granola + Meet + Gmail, produces ONE canonical + ranked alternates (per #10). Source authority: Gmail > Slack-notes > Granola > Meet > generic transcripts.
- **Multi-agent router**: advisor + adversarial critic ("you are not allowed to agree with the primary response") + optional domain specialist (per #7).
- **Per-document-type expiry**: TTL metadata + periodic prune + retrieval-time recency decay (per #8).
- **Backup/migrate**: single-file tar.gz of the vault for cross-machine transfer (per #13).

## Status

The system is built and Bruno-tracked. Closed children: #2, #3, #4, #5, #6, #7, #8, #10, #11, #12, #13, #14, #18. In flight: #25 (migrate scheduled harvest to Claude Code routines — verified Granola+Slack+Gmail reach the routine sandbox 2026-05-05), #9 (real-data eval harness — gates on harvest producing real content).

See [parent #1](https://github.com/acardote/personal-assistant-ultra/issues/1) for the live sequence map and assumption ledger.

## License

Private project. No license declared at this time.
