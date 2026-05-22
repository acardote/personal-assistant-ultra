# personal-assistant-ultra

> **Status**: `v0.10.3` shipped — closed-and-reconciled: kb-process TUI + kb-scan bug-fix bundle around the "Couldn't extract diff block from memo" amend-flow regression (parent #229). #231 (Child 3a — kb-process-tui's `m` handler detects diff-less memos via a new `memo_has_diff_block` guard and routes them to `\$EDITOR` for free-edit instead of the red error-shaped fallback; Mode-A AssertionError trip-wire pins the upstream-guard contract; module-level sentinel constants discriminate `\$EDITOR`-route reasons in the accuracy log without substring-matching free text). #233 (Child 3b — kb-scan's `render_person_org_diff` / `render_decision_diff` route wrapped bodies through a new `_prefix_diff_body` helper that emits `+ ` on every non-empty line and bare `+` on blank lines; idempotency footgun against CommonMark `+`-bullets dropped during review after pr-challenger surfaced silent-bullet-destruction; round-trip tests cover both `extract_diff_block_content` (amend) and `extract_proposed_diff` (apply) paths). #235 (follow-up — diff-less memos short-circuit before scope-prompt + apply-attempt; cascade-failure on the user's 79 unprocessed diff-less memos removed; help-line ordering nudges hand-edit / `s` / `r`-last-resort; accuracy-log dedupes non-action events). #234 (Child 4 — structured Mode-B failure surface: `_mode_b_violation_lines` reports ALL shape-violating file line numbers via `prefix_newlines + body_idx` arithmetic; message names mode + violations + memo path with NO line content echo per privacy floor; ANSI-injection-safe via `repr()` on the path; CRLF caveat documented + consistency trip-wire pins extractor/helper agreement). All 101+ tests across kb-process-tui / kb-process / kb-scan acceptance suites pass. **Follow-up filed as #239** (`bug` label) — harvest-routine emits decision-extract memos without `## Proposed diff` section; the 79 stuck memos in `.unprocessed/` came from a `harvest 2026-05-20 batch 5/6 (routine)` commit and remain unrehabbed; #229's children fixed the consumer side so the operator can walk past them cleanly. Carry-over from `v0.10.2`'s bundle: #226 (C3 — GitHub MCP 403 remediation, OAuth-scope axis), #227 (C4 — post-merge empirical validation of interactive preflight + scheduled non-regression). Pre-`v0.10` carry-overs: parent #183 (#186 throughput measurement pending; slice 4 falsifier-F2 Move-4 gap acknowledged), parent #178 (harvest routine slice 3 branch cleanup + slice 4 ADR + slice 5 live-fire validation pending), parent #173 (kb-process autonomy, slices 2-3 pending), #165 + #166 still under empirical-monitoring windows through 2026-05-26.
> `latest` tracks the most recent immutable release tag — currently `v0.10.3`. See [`RELEASE.md`](RELEASE.md) for the policy.

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
