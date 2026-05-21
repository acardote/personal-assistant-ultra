# personal-assistant-ultra

> **Status**: `v0.10.1` shipped — closed-and-reconciled: `pa-session reopen <short>` verb (#221 closer of #222) — inverse of `close --keep-branch`. Recreates the worktree from the existing `project/<short>` branch, flips `project.md` status `archived → active`, drops `archived_at`, re-writes `.pa-active-project.json`, commits the un-archive flip, exec's claude. Refuses on (a) existing worktree (collision; user should `resume`) or (b) missing branch (the `--merge` close case where the branch was deleted; refusal message surfaces the manual recovery: `git worktree add -b project/<short> .pa-worktrees/<short> main` + frontmatter flip). Also adds `tools/project.py reopen <slug>` as the filesystem-level state contract (frontmatter flip + state write; refuses against already-active projects). `surgical_update_frontmatter` extended with a `None` sentinel for key deletion (additive, existing callers unaffected). Closes the post-close lifecycle gap surfaced during the `v0.10.0` ship review: `close --keep-branch` was previously a one-way door because `resume` requires an existing worktree and close removed it. 6 new acceptance tests (3 `project.py` archive ↔ reopen round-trip + 3 `pa-session` reopen verb + refusals); all 51 tests across `test_project_acceptance` / `test_pa_session_acceptance` / `test_config_acceptance` pass. Carry-over from `v0.10.0`'s bundle: parent #183 (#186 throughput measurement pending; slice 4 falsifier-F2 Move-4 gap acknowledged), parent #178 (harvest routine slice 3 branch cleanup + slice 4 ADR + slice 5 live-fire validation pending), parent #173 (kb-process autonomy, slices 2-3 pending), #165 + #166 still under empirical-monitoring windows through 2026-05-26.
> `latest` tracks the most recent immutable release tag — currently `v0.10.1`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
