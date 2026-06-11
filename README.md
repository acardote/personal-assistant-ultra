# personal-assistant-ultra

> **Status**: `v0.12.1` shipped (PATCH) — closed-and-reconciled: **parent #274** — the harvest **watchdog was firing daily false-`STALE` alerts while the harvest was actually healthy**. Root cause (A3): `tools/check-harvest-freshness.py` selected the "newest" run-status file by **filesystem mtime**; the watchdog fresh-clones the vault every fire (`persist_session:false`), and `git clone` stamps every `runs/*.json` with one checkout mtime — so selection arbitrarily pinned an old file (`2026-06-05`) and reported STALE while `origin/main` carried fresh `ok:true` runs daily. **#276** fixes selection to the **filename timestamp** (lexicographic == chronological for the canonical `YYYY-MM-DDTHHMMSSZ.json` name), aligning with the harvest routine's own cutoff anchor (#170); T7 (which had codified the unsound mtime design) replaced + a clone equal-mtime regression added; adversarially reviewed (PR #278). Diagnosis slices: **#275** (reproduced + confirmed harvest healthy on `origin/main`; the challenger overturned a premature "fresh-clone PASS" read, promoting A3 from latent to active), **#277** (characterized the separate **>500min `kb-drift-scan`** task — crash-loop since ~05-16 from stale-schema cache entries + a missing `claude -p` timeout; A4 verdict: **independent** of the watchdog firing). A1 (stale-checkout) was **refuted** (`persist_session:false`). Follow-up **parent #279** tracks the kb-drift-scan reliability fixes. Known pre-existing test drift: **#280** (`bug`). Prior release `v0.12.0` — **parent #178**, harvest write transport re-scoped to direct `git push` to `main` (#268/#270/#271/#272; the feature-branch+PR workaround retired). Carry-over from `v0.11.x`: `tools/live-commit-push.sh` worktree + project-tier routing (parent #245) and the vault desync detect+refuse+recover stack (parent #249, `tools/vault-desync-probe.py`/`vault-desync-recover.py`). From `v0.10.4`: **#239** (`bug`) — 79 stuck `.unprocessed/` memos. Open carry-overs: parent #183 (#186 pending), parent #173 (kb-process autonomy), the #263 stranding-backlog children (#264-267).
> `latest` tracks the most recent immutable release tag — currently `v0.12.1`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
