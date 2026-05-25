# personal-assistant-ultra

> **Status**: `v0.10.4` shipped — closed-and-reconciled: kb-process TUI follow-up hotfix bundle (parent #241) closing the diff-less-memo robustness gap left by v0.10.3's #235 in the `a` (approve) and `M` (direct-`\$EDITOR` amend) handlers. User reported post-v0.10.3 walk: pressing `a` on a diff-less memo + typing a scope produced "✗ Couldn't inject Scope into memo (diff-block shape didn't match). Use `m` to edit manually, then apply." — but `m` ALSO can't apply diff-less memos post-#235, so the help text misdirected. #242 (Child 1 — entry gates on `a` and `M` mirror #235's `m` gate; post-amend re-checks on both `m` and `M` handle the case where claude or `\$EDITOR` deletes the ```diff fence during the amend session — pr-challenger F4 surfaced this as a load-bearing bug during review; accuracy-log row dropped on the bounce to avoid `action="a"` collision with "applied" semantics in downstream aggregations per pr-challenger F7; F3 test marker hardened from `#241 / #242` to `if not memo_has_diff_block(memo_path` for issue-number-agnostic coverage per F6). The `M` handler's post-edit re-check additionally rolls back to the pre-edit snapshot (`m_original_text` from #190 Concern 4) so the operator's pre-edit state is preserved if they accidentally delete the diff fence. New structural-invariant tests pin: 5 `memo_has_diff_block` call sites (3 entry gates + 2 post-amend re-checks); every `apply_memo` downstream of an amend session has a re-check between; no "Use `m`... then apply" misdirection inside any gate block. All 107 tests across kb-process-tui / kb-process / kb-scan acceptance suites pass. Carry-over from `v0.10.3`'s bundle: **#239** (`bug` label) — harvest-routine emits decision-extract memos without `## Proposed diff` section; the 79 stuck memos in `.unprocessed/` came from a `harvest 2026-05-20 batch 5/6 (routine)` commit and remain unrehabbed; #229's + #241's children fixed the consumer side so the operator can walk past them cleanly. From `v0.10.2`: #226 (C3 — GitHub MCP 403 remediation), #227 (C4 — post-merge empirical validation). Pre-`v0.10` carry-overs: parent #183 (#186 throughput measurement pending), parent #178 (harvest routine slices pending), parent #173 (kb-process autonomy, slices 2-3 pending), #165 + #166 still under empirical-monitoring windows through 2026-05-26.
> `latest` tracks the most recent immutable release tag — currently `v0.10.4`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
