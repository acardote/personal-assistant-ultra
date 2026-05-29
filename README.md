# personal-assistant-ultra

> **Status**: `v0.11.1` shipped — closed-and-reconciled: `tools/live-commit-push.sh` worktree + project-tier routing patch (parent #245) fixing the three gaps that surfaced during the 2026-05-28 Atlas-onboarding session where the helper exited 1 with `is not a git repo` on the `.pa-worktrees/vivian-onboarding/` worktree. **#246** (Child 1 — repo-presence check switched from `[[ ! -d "$CONTENT_ROOT/.git" ]]` to `[[ ! -e ... ]]` so worktree gitfile pointers — `.git` as a regular file — pass the check while non-git paths still exit 1; adversarial-review acceptance/falsifier + shell-correctness + regression-on-non-worktree-flat-flow lenses all cleared on the first round). **#247** (Child 2 — `projects/` added to the per-path stage loop so project-tier artefacts `projects/<slug>/artefacts/<kind>/art-*.md` and `projects/<slug>/project.md` get committed in the Phase-3 write-back; per-path `[[ -e ]] && git add ... || true` tolerance preserved so absent `projects/` does not crash the helper; regression lens verified flat-artefact sessions still commit only the expected paths). **#248** (Child 3 — separate dispatch branch for the "no upstream branch" push failure that one-shot retries with `git push --set-upstream origin <current-branch>` from `git rev-parse --abbrev-ref HEAD`; distinct from the existing `non-fast-forward\|fetch first` rebase-retry path which stays unchanged; new exit class added without reshuffling the 0/1/2/3/4/5/6 taxonomy). Move-5 reconciliation posted on all three children with PR-URL provenance; assumption ledger A1+A2+A3 validated by the children, A4 validated by the regression review lens, A5 explicitly accepted (no structural mirror of the gaps found in `tools/lint-provenance.py` or `tools/project.py touch` during today's worktree+project run — if a future Phase-3 helper surfaces the same shape, file a parent of its own rather than reopen #245). Note: close-gate footgun caught during this release — the gate regex `^#{2,4}\s*falsifiers\b` does NOT match the issue-template body's `## Falsifier(s)` heading, so closing a child requires an explicit `## Falsifiers (move:4)` comment even when the body lists the falsifiers (saved as feedback memory for future Bruno-driven work). Carry-over from `v0.11.0`'s bundle: vault main-worktree desync detect+refuse+recover stack (parent #249, children #251-#254) addressing the 2026-05-28 mass-deletion incident — `tools/vault-desync-probe.py` (state-based AUTO_MERGE-without-MERGE_HEAD + diff-D-count>50 signals), `tools/vault-desync-recover.py` (D-set partition that preserves operator content), `templates/git-hooks/pre-commit` (class-level guard installed by `pa-session new`/`doctor`, anchored on `git rev-parse --git-common-dir` for linked-worktree safety), and the RELEASE.md "Vault desync recovery runbook" section. From `v0.10.4`: **#239** (`bug` label) — harvest-routine decision-extract memos without `## Proposed diff` section; the 79 stuck memos in `.unprocessed/` remain unrehabbed. From `v0.10.2`: #226 (C3 — GitHub MCP 403 remediation), #227 (C4 — post-merge empirical validation). Pre-`v0.10` carry-overs: parent #183 (#186 throughput measurement pending), parent #178 (harvest routine slices pending), parent #173 (kb-process autonomy, slices 2-3 pending), #165 + #166 still under empirical-monitoring windows through 2026-05-26.
> `latest` tracks the most recent immutable release tag — currently `v0.11.1`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
