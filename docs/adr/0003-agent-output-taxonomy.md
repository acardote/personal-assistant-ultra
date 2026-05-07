# ADR-0003 — Agent output taxonomy and provenance shape

- Status: Accepted
- Date: 2026-05-07
- Decider: acardote
- Related: parent issue [#76](https://github.com/acardote/personal-assistant-ultra/issues/76), child [#77](https://github.com/acardote/personal-assistant-ultra/issues/77)

## Context

The personal-assistant skill today does **read + remember**: it pulls from layer-3 KB and layer-2 memory, optionally augments via live MCP calls (per #39), and answers. Memory comes from harvested external sources (Slack threads, Granola notes, Gmail threads) compressed by `tools/compress.py` with full provenance back to `raw/`.

The skill is now extending into **work execution**: drafting analyses, generating plans, producing reports, recommending decisions, refining glossary terms. This work creates outputs the skill itself authored — not external artifacts to be remembered.

Without a taxonomy and a provenance shape, every work execution invents its own conventions. Today's session is the canonical motivating case: across a single working day we filed eight issues (#67, #68, #70, #72, #73, #74, #76, #77), produced an eval report (`vault/.eval/baselines/2026-05-06.md`), wrote a recovery harvest summary, and made dozens of design decisions in chat — none of which flowed back into the KB, and the artefacts only reached the vault because we eyeballed the commits.

The decision below names two output types, where each lives, and how each carries a back-reference to the work that produced it.

## Decision

### Two output types

**`knowledge`** — the assistant updates the layer-3 KB. Lands in:
- `<content_root>/kb/{people,org,decisions}.md` for `person-update`, `org-update`, `decision` kinds — these are **vault-scoped** (this user's content).
- `<method_root>/kb/glossary.md` for `glossary-term` kind — this is **method-repo-scoped** (shipped to every user of the skill).

Updates in place; does NOT create parallel files. Editorial discipline from #4 still applies. The two repo scopes have **different provenance flows** — see the Provenance shape section below.

**`artefact`** — a deliverable the user wants to find later (a draft, a plan, a report, an export). Lands in a vault-tracked folder distinct from `memory/`, `raw/`, and `kb/`. Folder layout, naming, and gitignore rules are deferred to the follow-up vault-layout slice (#76 sequence-map item 2).

### Boundary rule (F3 mitigation — decision-memo borderline)

The boundary follows **two questions answered in order**:

1. **Does any part of this output update durable state about the user's people, org, decisions, or project glossary?** If yes, that part is `knowledge`, regardless of whether the surrounding narrative is also worth keeping.
2. **If the surrounding narrative has standalone value beyond the KB update, the narrative is an `artefact`** (typically `kind=memo` or `kind=analysis`) that **cites the knowledge entry it produced**. The artefact links to the KB; the KB does not link back (KB is the always-in-context layer; cross-references would bloat it).

Worked example: a "decision memo" that explains why the team picked storage backend X. The decision itself updates `<content_root>/kb/decisions.md` (knowledge). The memo explaining the reasoning lands in `<content_root>/artefacts/memo/<id>.md` (artefact) and references the decision heading. The KB stays terse; the memo stays long.

If both questions answer no, the output isn't either type — it's chat output, ephemeral. Don't capture.

### Valid kinds (extensible by ADR amendment, NOT free-form)

- **knowledge kinds**: `person-update`, `org-update`, `decision`, `glossary-term`. New kinds require an ADR amendment — keeps the type from sprawling.
- **artefact kinds**: `analysis`, `plan`, `draft`, `report`, `export`, `memo`. Same amendment-only rule. `export` carries non-text content (e.g., CSV, JSON, XLSX); the others are Markdown.

### Provenance shape per type

**Artefacts** (full files we own):

YAML frontmatter at the top of the file, mirroring memory-object frontmatter shape so the existing tooling pattern carries. Note the two ID schemes and why: `id: art-<uuid>` is **content-addressable identity** (the artefact survives across sessions and is referenced by future queries), while `produced_by.session_id: <8-hex>` is **session-scoped lookup** (matches the dashboard's session table, valid only within a finite window). They serve different lookup paths — a UUID for "find this artefact" and a short session-id for "what else came out of this query/turn."

```yaml
---
id: art-<uuid>
kind: analysis | plan | draft | report | export | memo
created_at: <iso8601>
produced_by:
  session_id: <8-hex>
  query: <short user query that motivated this work>
  model: <claude-opus-4-7 | claude-sonnet-4-6 | ...>
  sources_cited:
    - <kb-heading-or-memory-uri-or-url>
    - ...
title: <short>
summary: <one-line>
---
```

For non-text artefacts (CSV, JSON, etc.), provenance lives in a sidecar `<id>.provenance.json` with the same fields. The body file (`<id>.csv`) is the actual artefact.

**Knowledge — two flows depending on repo scope** (per pr-challenger B1 on PR #78):

***Vault-scoped knowledge*** (`person-update`, `org-update`, `decision` → `<content_root>/kb/`):

Inline HTML comment marker placed immediately above the contributed lines:

```html
<!-- produced_by: session=<8-hex>, query="<short>", at=<iso8601>, sources=[<heading|memory-uri|url>, ...] -->
```

Inline because: KB is the always-in-context layer; a sidecar would force every consumer to do an extra read to know provenance. The comment is invisible to markdown rendering. **Critically, the comment must be stripped at assembly time** so it doesn't bloat the layer-3 prompt — `tools/assemble-kb.py` will be updated in a follow-up slice to filter `<!-- produced_by: ... -->` lines before concatenation, scoped to **vault paths only**. Until that slice lands, the comments are present in the prompt; their bytes are negligible (~150 chars each) but the principle still requires the strip step.

***Method-scoped knowledge*** (`glossary-term` → `<method_root>/kb/glossary.md`):

The glossary file is shipped to every user of the skill, so embedding `session_id` + `query` in the source file would leak one user's context into a shared artifact and create per-user merge churn. Method-scoped knowledge therefore uses a **PR-only provenance flow**:

1. The assistant proposes the glossary diff in chat.
2. On approval, the assistant opens a PR against the method repo with the diff. The PR description carries the `produced_by` fields — that's the canonical record.
3. No inline `<!-- produced_by -->` comment lands in `glossary.md`. The git history (PR + commit message) IS the provenance.

This means `tools/assemble-kb.py`'s strip step only needs to handle vault paths. Method-repo glossary stays clean by construction.

***Sources_cited canonical forms*** (per pr-challenger non-blocking on #78):

Three accepted reference syntaxes, pinned for the verification-lint slice:
- `kb#heading` — references a `## <heading>` in any layer-3 KB file (resolved by the assembled-KB index).
- `mem://<memory-id>` — references a memory object by its `id:` frontmatter field (e.g., `mem://mem-abc123-...`).
- `https://...` — bare URL for external sources (Slack permalinks, Granola URLs, GitHub issues).

Anything else is invalid; the verification lint will refuse it.

### Diff-and-approve default

Default mode for ALL knowledge updates AND artefacts is:

1. Assistant proposes the change as a diff in chat (or as a draft commit on a feature branch for larger artefacts).
2. User reviews.
3. Assistant commits + pushes only on explicit approval.

Silent commits to `kb/*` or `artefacts/*` are forbidden by default. The reasons:
- KB is always-in-context — a single bad edit poisons every downstream answer (per F2 on #3 / #4).
- Artefacts have user-facing value — silent generation creates noise the user has to clean up.

A flag-gated `auto_commit` mode is **out of scope** for this ADR. If we ever add it, it should require an explicit affirmative configuration and apply per-kind (e.g., auto-commit `report` and `export` but never `decision`).

### Retroactive scope (no grandfathering of past outputs)

Today's session produced eight issues, an eval report, a recovery summary, and dozens of design decisions in chat that motivated this ADR. Those outputs are **grandfathered**: the taxonomy applies forward, not retroactively. We do NOT go back and add provenance frontmatter to existing artefacts in the vault. The motivating examples are referenced in this ADR's Context section as the rationale; that's their permanent provenance trail.

If a specific past artefact turns out to be load-bearing later (e.g., a future query needs to cite the 2026-05-06 eval baseline) and we want it discoverable by the verification lint, the cleanup is one-off: add the frontmatter manually and commit. No bulk migration.

### F2 mitigation — autonomous producers

The diff-and-approve default assumes a human is in the loop. The harvest routine and watchdog routine are autonomous (no human review per fire). For autonomous Claude paths producing output:

- Routines MAY produce **artefacts** (e.g., a daily harvest digest is already a routine-produced artefact today, just not labelled as one). Provenance frontmatter still applies; `produced_by.session_id` is the routine's session.
- Routines MUST NOT update **knowledge** (KB) without human review. If a routine surfaces a candidate KB update (e.g., "this person changed roles, glossary term X is now ambiguous"), it writes the candidate as an artefact (`kind=memo` with a clear "candidate KB update" framing) and surfaces it in the daily digest for the user to review and apply. The next interactive skill session can read the memo and propose the diff under the standard approval flow.

This keeps the always-in-context layer human-curated even as autonomous work produces other outputs.

## Consequences

**Positive**

- Every produced output has a typed home and an auditable trail. Asking "what changed in the KB this week?" or "which artefacts did the assistant produce on this query?" is a deterministic file-system question.
- The boundary rule reduces to two ordered questions; classifying borderline cases (decision memos) is mechanical rather than judgment-driven.
- Autonomous producers are accommodated without breaking KB curation discipline.
- The frontmatter shape mirrors memory objects, so existing tooling patterns (lint, dashboard, dedup) extend with minimal new code.

**Negative**

- Inline HTML comments in `kb/*` require `tools/assemble-kb.py` to filter at assembly time. Until that follow-up lands, the comments leak into the layer-3 prompt (small but present).
- Adding new kinds requires ADR amendment, which is friction. Defensible: keeps the type space small enough to reason about end-to-end.
- The default diff-and-approve flow adds latency to every produced output. The user may want to flag-gate auto-commit for low-stakes kinds eventually; that's a separate ADR.

**Neutral**

- Artefact folder layout is deferred to #76 sequence-map item 2. This ADR doesn't pick the path; it only declares that artefacts have a folder home distinct from `memory/`, `raw/`, and `kb/`.
- The provenance shape for non-text artefacts uses a sidecar file. Two-files-per-artefact is mild discipline overhead but keeps the body file portable (a `.csv` is still a `.csv`).

## Falsifiers

These come from the parent #76 + this child's adversarial pass. They post-deploy:

- **F1 (taxonomy leakage)**: If a follow-up child (#76-B vault layout or #76-C editorial rules) has to redefine the type boundary to do its work, retract — the abstraction is leaking.
- **F2 (autonomous producers — addressed above)**: If a routine ends up updating KB silently despite the rule against it, retract — the design didn't bind the autonomous path.
- **F3 (decision-memo borderline — addressed above)**: If a real borderline case can't be classified by the two ordered questions without reviewer judgment, retract — the boundary is underspecified.
- **F4 (KB invariant pollution)**: If the inline `<!-- produced_by: ... -->` comments either trip the lint or land in the layer-3 prompt at a volume that bloats it post-strip-step, retract — the chosen knowledge provenance shape is incompatible with the KB invariant.

## References

- Parent #76 — Capture knowledge + artefacts produced by agent work execution.
- Per-kind editorial rules: [`docs/kb-editorial-rules.md`](../kb-editorial-rules.md) (child #81) — operationalizes the knowledge half of this ADR with deterministic kind selection, mechanical diff shape, and explicit actor model.
- Memory object schema: `docs/schemas/memory-object.schema.json` (provenance pattern this ADR mirrors).
- ADR-0001 — Storage backend (the layer split this ADR extends).
- Issue #4 — Always-in-context layer-3 KB (the editorial discipline this ADR honors for autonomous producers).

## Amendment 1 — Project tier (2026-05-07)

Source: parent [#88](https://github.com/acardote/personal-assistant-ultra/issues/88) (PA projects), child [#89](https://github.com/acardote/personal-assistant-ultra/issues/89). The original ADR treated every work-execution turn as standalone. Real agent-executed work clusters into multi-session efforts (drafting a strategy doc over weeks, building a presentation across machines, iterating on a memo with stakeholder feedback). Without a container, continuity depends entirely on keyword retrieval.

This amendment introduces a **third top-level type** alongside `knowledge` and `artefact`.

### `project` — multi-session container for agent-executed work

Lives at `<content_root>/projects/<slug>/`. A project owns:

- **`project.md`** — required. YAML frontmatter + body.
- **`artefacts/<kind>/art-*.<ext>`** — project-scoped artefacts. Same body shape as flat artefacts, with one frontmatter addition: `project_id: <slug>`.
- **`notes.md`** — optional. The running context the assistant maintains across sessions for this project. Free-form Markdown.

Project-less artefacts continue to land at `<content_root>/artefacts/<kind>/` (flat tier). Default behavior on any turn without `PA_PROJECT_ID` set. **Project tier is opt-in** — most agent-executed work is one-shot and stays flat.

### `project.md` frontmatter shape

```yaml
---
id: <slug>                          # equals the folder name
title: <short title>
intent: <one-paragraph statement of what this project is producing and why>
status: active | archived
started_at: <iso8601 date>
last_active: <iso8601 date>          # touched on every project-scoped write
archived_at: <iso8601 date>          # present iff status=archived
bruno_parent: <issue-url>            # optional; ties project to a vault-repo Bruno parent for discipline overlay
---

<body — running notes, decisions, sources cited, what's next. The assistant maintains this across sessions; humans may edit too.>
```

Required: `id`, `title`, `intent`, `status`, `started_at`, `last_active`. Optional: `archived_at`, `bruno_parent`.

### Slug convention (F3 mitigation — slug collision across machines)

Slugs follow `<YYYYMMDD>-<short-name>-<4hex>`. Example: `20260507-q3-strategy-doc-a91f`.

- `<YYYYMMDD>` (UTC) gives chronological ordering and disambiguates same-name reuse across days.
- `<short-name>` is the user-supplied kebab-case label (≤30 chars, `[a-z0-9-]+`).
- `<4hex>` is a random 4-hex-digit suffix (`openssl rand -hex 2`) generated at create time.

The 4-hex suffix makes same-day cross-machine collision probability ~1 in 65k per shared `<short-name>`. The `project new` command also performs a local existence check and refuses if the slug already exists in `<content_root>/projects/`. Cross-machine "both machines created same slug independently" is reduced to "both rolled the same 4 hex digits AND chose the same short-name on the same UTC day" — vanishingly rare. If it ever does, the second-to-push gets a non-fast-forward push rejection and the user reconciles manually.

### Project-scoped vs flat artefact reference forms (F2 mitigation — promotion-path graph)

Artefact-to-artefact references MUST use the canonical form `art://<uuid>`, not filesystem paths. This is added as a fourth canonical source form alongside the existing three from ADR-0003:

- `kb#heading`
- `mem://<memory-id>`
- `https://...`
- **`art://<art-uuid>`** (new) — references an artefact by its `id` field, not its path.

Lookup is by `id`. Promotion (flat → project) and cross-project copy (described below) change physical location but **not** the artefact's `id`, so existing `art://...` references stay valid by construction. There is no graph traversal at promotion time because there is no path-based reference graph to rewrite. If an artefact body contains a path-based reference, that's a violation the verification lint surfaces (per #85, extended to recognize `art://` as canonical).

`tools/lint-provenance.py` is extended to include `art://` in `CANONICAL_SOURCE_RE` and to refuse path-based artefact references in `produced_by.sources_cited`. Implementation lands in slice 5 of #88.

### Promotion: flat → project

Command: `/personal-assistant project promote <art-uuid> <slug>`. Effect:

1. Locate the flat artefact at `<content_root>/artefacts/<kind>/art-<uuid>.<ext>`.
2. Verify the target project exists and is `status=active`.
3. Move the file (and its sidecar, if export) into `<content_root>/projects/<slug>/artefacts/<kind>/`.
4. Add `project_id: <slug>` to the frontmatter.
5. Touch `last_active` on the destination project's `project.md`.
6. Commit + push via `tools/live-commit-push.sh`.

Because references use `art://<uuid>`, no graph traversal is needed. The artefact keeps its `id`. This closes F2.

### Cross-project artefact reuse: copy with `derived_from`

When an artefact in project A should also appear in project B, the user runs `/personal-assistant project copy-artefact <art-uuid> <dest-slug>`. The destination gets:

- A fresh `id` (new UUID).
- `project_id: <dest-slug>`.
- A new field `derived_from: <orig-art-uuid>` in the frontmatter, persisting the lineage.
- The body content copied verbatim.

The original is unchanged. Each project owns its copy. `art://<orig-uuid>` references resolve to the original; `art://<new-uuid>` to the copy. No symlinks (Windows portability + git ergonomics).

### Resume-context budget (F1 — clarification, NOT cap)

Loading a project on resume reads `project.md` + the artefact manifest + `notes.md` into the turn's context. **No size cap is imposed** — partial loads yield a lossy continuation that defeats the feature.

Critically: **projects are NOT layer-3.** Layer 3 is the always-in-context KB (`<method_root>/kb/glossary.md` + `<content_root>/kb/{people,org,decisions}.md`), bounded at 4K tokens by the invariant in #4. Projects are a **fourth tier** — *selective per-turn load*, opt-in via explicit `project resume <slug>`. Project loads do not count against the layer-3 budget; they consume the per-turn prompt context (200K+).

If a project's `project.md` + manifest + notes ever grow large enough to bump the per-turn context window itself (rare; would need months of dense activity), the user can split it: `project archive <slug>` and `project new <new-slug>` continuing the work. The amendment doesn't pre-empt this with mandatory rotation; the practical bound is high enough that automating it would be premature.

This closes F1: layer-3 invariant intact, project tier explicitly outside it.

### Archival lifecycle

Project transitions to `status: archived` after one month with no `last_active` update. Mechanism:

- **Passive (always available)**: `/personal-assistant project archive <slug>` flips status, sets `archived_at`, commits.
- **Active (optional, deferred)**: a routine sweeps projects, finds `last_active < now - 30d`, and proposes archives in the daily digest for the user to confirm.

Archived projects stay in `projects/<slug>/` (not moved to `projects/.archive/` or similar — slug-based discovery should remain stable). `project list` filters by `status=active` by default; `project list --include-archived` shows the full set.

### Bruno overlay (optional, not default)

Most PA projects are fluid creative work where Bruno's falsifier + reconciliation overhead would be wrong. If a particular project warrants discipline (deliverable, deadline, stakeholder ask):

1. The user runs `/bruno init` in the vault repo (sets up `<content_root>/.bruno/config.toml` with `github_repo = <vault-repo>`).
2. From inside the vault, `/bruno parent` opens a parent issue against the vault repo.
3. The PA project's `project.md` carries `bruno_parent: <issue-url>` to tie them.

Method-development work (this repo, `acardote/personal-assistant-ultra`) keeps its existing Bruno discipline against this repo's issue tracker. The two layers are separated by which repo the `.bruno/config.toml` lives in. There is no "promote a PA project to a Bruno parent automatically" — the user makes the explicit call.

If `bruno_parent` references an issue that closes while the PA project remains `status=active`, that's intentional — the discipline overlay was a Bruno milestone (e.g., "MVP-v1 reconciled") and the project's work continues. The two are coupled by reference, not by lifecycle.

### Updated kinds + boundary

The valid `artefact` kinds list (`analysis | plan | draft | report | export | memo`) is unchanged. `project` is **not an `artefact` kind** — it's a top-level type. The two-questions boundary rule from the original ADR remains the test for routing a piece of agent output: knowledge vs artefact. Whether an artefact lands in a project folder or flat is a separate question, decided by the active `PA_PROJECT_ID` env var (set by `project resume`).

### Cross-reference to `docs/kb-editorial-rules.md`

KB contributions (people, org, decisions, glossary) **do not become project-scoped** even when produced from inside a project. Knowledge is global to the user's content; projects scope artefacts only. The kind selector in editorial rules is unchanged; if the same insight produces both a project artefact (e.g., a memo) and a KB update (e.g., a `decision`), the artefact lands in the project folder while the KB update lands in `<content_root>/kb/decisions.md` per existing rules.

### Falsifiers (added to original list)

- **F5 (project sprawl)**: If `project list --include-archived` ever grows past 100 entries with no archival sweep having run, retract — the lifecycle wasn't actually closed by archival.
- **F6 (ambiguous current-project)**: If a turn lands an artefact in the wrong project because `PA_PROJECT_ID` was inherited from a stale session, retract — the explicit-resume-only design didn't actually scope the env var per-session.
- **F7 (slug collision in practice)**: If a real cross-machine slug collision occurs in production within the first 90 days, retract — the 4-hex suffix wasn't sufficient and we need a stronger scheme (machine-id, content-hash, or fail-on-conflict).
- **F8 (path-based artefact references slip past lint)**: If an agent-produced artefact lands with a filesystem path in `sources_cited` instead of `art://<uuid>` and the lint exits clean, retract — the canonical-source enforcement isn't binding.
