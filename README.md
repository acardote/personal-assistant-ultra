# personal-assistant-ultra

> **Status**: `v0.12.0` shipped — closed-and-reconciled: **parent #178** — the harvest routine's write transport, **re-scoped to direct `git push` to `main`** now that direct main writes are available again from within the routine (the 2026-05-08→05-11 proxy identity-swap that 403'd them, and motivated the feature-branch+PR+auto-merge workaround, is resolved). The workaround (slices 1–2, #179/#181) is retired — inspection showed it never fired in production (0 routine PRs ever; harvests landed via direct single-parent MCP commits and otherwise stranded on `claude/cool-lamport-*` session branches). Four re-scoped slices: **#268 (R1)** — the routine template's commit-push section now ensures HEAD is `main` *non-destructively* (stash→checkout→pop, never `reset --hard`) then delegates to `tools/live-commit-push.sh` (desync-probe + `lint-provenance --require-vault` HARD gate + commit + non-ff rebase-retry push); inline lint is the sole gate, no PR/auto-merge, no automatic fallback (fail-loud on 403). The redesign was driven by adversarial review: the first cut hand-rolled a `reset --hard` relocate with a silent-data-loss edge + dropped desync gate (pr-challenger B1/B2/B3), so the merged version reuses the hardened helper. **#270 (R2)** — deployed routine `trig_01NJDhwNffAF2GLyHJaJ8fC2` updated; it had drifted *two transports behind* (still inlined MCP `push_files`), so it was converted to a **shim that reads the canonical template each fire** — drift class eliminated. **#271 (R3)** — ADR-0004 documenting the transport arc. **#272 (R4)** — validation fire landed harvest commit `092b8f376` **directly on `main`** (single-parent, `transport=git-direct-main`, zero new branches → no stranding), empirically validating A1 (direct push works) + A3 (no stranding); A2/A4/A5 accepted with reasons. The vault routine source carries `allow_unrestricted_git_push: true` — what re-enabled direct main writes. Carry-over from `v0.11.x`: `tools/live-commit-push.sh` worktree + project-tier routing (parent #245, #246/#247/#248) and the vault main-worktree desync detect+refuse+recover stack (parent #249, #251-#254 — `tools/vault-desync-probe.py`, `tools/vault-desync-recover.py`, `templates/git-hooks/pre-commit`, RELEASE.md recovery runbook). From `v0.10.4`: **#239** (`bug`) — harvest decision-extract memos without `## Proposed diff`; 79 stuck `.unprocessed/` memos remain unrehabbed. Open carry-overs: parent #183 (#186 throughput pending), parent #173 (kb-process autonomy slices 2-3), the #263 stranding-backlog children (#264-267).
> `latest` tracks the most recent immutable release tag — currently `v0.12.0`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
