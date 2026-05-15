# personal-assistant-ultra

> **Status**: `v0.9.3` shipped, partial — closed-and-reconciled: kb-process TUI v1 keystroke-driven candidate review (#185 closer of #184 slice 1 of #183) + `--predict` LLM-prediction surface with per-walk accuracy TSV (#188 closer of #187 slice 2 of #183) + NL amend flow on `m` keystroke (#190 closer of #189 slice 3 of #183) + `--predict` Scope prompt defaults to predicted scope, TSV gains `scope_source` provenance column (#192 closer of #191 slice 4 of #183) + lint-provenance art:// prefix-mismatch tolerance fix (#197 closer of #196 / C3 of #193; repro at #195 closer of #194 / C1) + lint-provenance kb# regex widened to accept literal heading text (#202 closer of #200 / C1 of #199) + lint-provenance `source-pin` artefact kind added with ADR-0003 Amendment 2 (#203 closer of #201 / C1 of #198) + author-vault URL relocation from `getnexar/acardote-pa-vault` to `acardote/acardote-pa-vault` across docs, routine templates, and SKILL.md (#206; mirror push preserved commit SHAs). The tool replaces the chat-based ~30s/candidate review flow with single-keystroke a/r/m/s actions; `--predict` pre-flights `claude -p` per candidate and logs (predicted, actual) pairs for accuracy measurement; `m` prompts for a natural-language instruction → claude amends → preview → a/r/e/c confirm (with `M` preserving legacy direct-`$EDITOR` editing); under `--predict`, pressing Enter on the Scope prompt now accepts the prediction; `lint-provenance` tolerates both `art://<uuid>` and `art://art-<uuid>` URI shapes, accepts literal `kb#<heading>` references (spaces / punctuation / em-dashes / unicode), and recognizes `source-pin` as a first-class kind for pre-harvest snapshots of upstream content. Also bundled but with parents still OPEN: parent #183 (#186 throughput measurement pending; slice 4 carries an acknowledged falsifier-F2 Move-4 gap); parent #178 (harvest routine slice 3 branch cleanup + slice 4 ADR + slice 5 live-fire validation pending); parent #173 (kb-process autonomy, slices 2-3 pending); #165 (Opus 4.7 default model) + #166 (mid-batched-push 401 handler) still under empirical-monitoring windows through 2026-05-26. Follow-up children #204 (strict kind-vs-dir) + #205 (notice path for unknown `upstream.kind`) tracking tightening enhancements from #198 review.
> `latest` tracks the most recent immutable release tag — currently `v0.9.3`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
