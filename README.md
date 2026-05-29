# personal-assistant-ultra

> **Status**: `v0.11.0` shipped — closed-and-reconciled: vault main-worktree desync detect+refuse+recover bundle (parent #249) addressing the 2026-05-28 incident where `refs/heads/main` advanced behind a frozen working tree and the next `git merge`-class operation captured the gap as 233 staged "deletions" via `.git/AUTO_MERGE`. The fix is a detection-and-refuse layer (per A5 of #249's assumption ledger) rather than a specific-command fix — #250 forensic verdict was "unrecoverable from available local logs" so the specific command class is targeted via class-level guards instead. **#251** (Child N2 — `tools/vault-desync-probe.py` checks two state-based signals: `.git/AUTO_MERGE` without `MERGE_HEAD` (post-failed-merge fingerprint) AND `git diff --diff-filter=D HEAD` count > 50 (WT-vs-HEAD tree mismatch via mass deletions); pr-challenger surfaced B1/B3 false-negatives on the original history-based reflog design — round-2 replaced with the state-based signals which survive both `git checkout main` (B1) and `core.logAllRefUpdates=false` (B3); 5-site preflight integration in `scripts/pa-session`, `tools/live-commit-push.sh`, `tools/scheduled-harvest.py`, `.claude/skills/personal-assistant/SKILL.md`, and a doc note in `templates/routines/harvest-routine.md`). **#252** (Child N3 — `tools/vault-desync-recover.py` one-shot recovery: restores HEAD-tracked files absent from WT, clears `.git/AUTO_MERGE`, re-probes to verify; pr-challenger B1 caught a data-loss bug where `git checkout HEAD --` would clobber a D-set path holding operator content — round-2 added `_partition_d_set` that intersects the D-set with on-disk presence and only restores the genuinely-missing subset; preserves user-uncommitted edits across all three classes — staged M, unstaged M, untracked). **#253** (Child N4 weight:heavy — class-level guard via `templates/git-hooks/pre-commit` installed per-vault by `pa-session new` and `pa-session doctor`; refuses commits when the probe fires; pr-challenger surfaced B1 (linked-worktree commits skipping the guard via `--show-toplevel` anchoring) and B2 (probe exit 2 misclassified as desync) — round-2 anchors on `git rev-parse --git-common-dir` to find the canonical vault from any worktree, and branches on probe exit codes 0/1/2 so invocation errors fail OPEN with a warning; `PA_VAULT_HOOK_DISABLE=1` bypass emits an auditable banner). **#254** (Child N5 — RELEASE.md "Vault desync recovery runbook" section with detection signals, recovery commands, blocked-D-set resolution, bypass mechanics, prevention guidance, and a 2026-05-28 worked example; all five mechanical refusal-message sites unified to point at the runbook; `release-policy.yaml` templates surface notes mention `templates/git-hooks/` explicitly). Assumption ledger fully accounted: A1+A4+A5 validated by children, A2 rescoped per #250 ("unknown command" → class-level guard via #253), A3 accepted by comment (desync is local-only, never reaches origin). 25 acceptance tests across probe / recover / hook / pa-session suites; all 16 existing pa-session tests still pass. Carry-over from `v0.10.4`'s bundle: **#239** (`bug` label) — harvest-routine emits decision-extract memos without `## Proposed diff` section; the 79 stuck memos in `.unprocessed/` came from a `harvest 2026-05-20 batch 5/6 (routine)` commit and remain unrehabbed; #229's + #241's children fixed the consumer side. From `v0.10.2`: #226 (C3 — GitHub MCP 403 remediation), #227 (C4 — post-merge empirical validation). Pre-`v0.10` carry-overs: parent #183 (#186 throughput measurement pending), parent #178 (harvest routine slices pending), parent #173 (kb-process autonomy, slices 2-3 pending), #165 + #166 still under empirical-monitoring windows through 2026-05-26.
> `latest` tracks the most recent immutable release tag — currently `v0.11.0`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
