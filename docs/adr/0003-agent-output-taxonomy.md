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

**`knowledge`** — the assistant updates the layer-3 KB. Lands in `<content_root>/kb/{people,org,decisions}.md` or `<method_root>/kb/glossary.md` depending on kind. Updates in place; does NOT create parallel files. Editorial discipline from #4 still applies.

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

YAML frontmatter at the top of the file, mirroring memory-object frontmatter shape so the existing tooling pattern carries:

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

**Knowledge** (in-place edits to existing KB files):

Inline HTML comment marker placed immediately above the contributed lines:

```html
<!-- produced_by: session=<8-hex>, query="<short>", at=<iso8601>, sources=[<heading|memory-uri|url>, ...] -->
```

Inline because: KB is the always-in-context layer; a sidecar would force every consumer to do an extra read to know provenance. The comment is invisible to markdown rendering. **Critically, the comment must be stripped at assembly time** so it doesn't bloat the layer-3 prompt — `tools/assemble-kb.py` will be updated in a follow-up slice to filter `<!-- produced_by: ... -->` lines before concatenation. Until that slice lands, the comments are present in the prompt; their bytes are negligible (~150 chars each) but the principle still requires the strip step.

### Diff-and-approve default

Default mode for ALL knowledge updates AND artefacts is:

1. Assistant proposes the change as a diff in chat (or as a draft commit on a feature branch for larger artefacts).
2. User reviews.
3. Assistant commits + pushes only on explicit approval.

Silent commits to `kb/*` or `artefacts/*` are forbidden by default. The reasons:
- KB is always-in-context — a single bad edit poisons every downstream answer (per F2 on #3 / #4).
- Artefacts have user-facing value — silent generation creates noise the user has to clean up.

A flag-gated `auto_commit` mode is **out of scope** for this ADR. If we ever add it, it should require an explicit affirmative configuration and apply per-kind (e.g., auto-commit `report` and `export` but never `decision`).

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
- Memory object schema: `docs/schemas/memory-object.schema.json` (provenance pattern this ADR mirrors).
- ADR-0001 — Storage backend (the layer split this ADR extends).
- Issue #4 — Always-in-context layer-3 KB (the editorial discipline this ADR honors for autonomous producers).
