# KB editorial rules — agent-produced contributions

Rules for how the personal-assistant agent updates the layer-3 knowledge base. Companion to [ADR-0003](adr/0003-agent-output-taxonomy.md). Parent issue [#76](https://github.com/acardote/personal-assistant-ultra/issues/76), child [#81](https://github.com/acardote/personal-assistant-ultra/issues/81).

The four knowledge kinds from ADR-0003 are: `person-update`, `org-update`, `decision`, `glossary-term`. Each has a single target file, a deterministic kind selector, and a mechanical diff-shape rule.

## Actor model (F4 mitigation)

Every step below uses one of three explicit roles:

| Role | Who | What |
|---|---|---|
| **LLM** | The personal-assistant skill executing in a Claude session | Detects the trigger, drafts the diff, presents it in chat with provenance |
| **User** | The human reviewing the diff in chat | Approves, requests changes, or rejects |
| **Skill** | The same LLM, acting on user approval | Writes the file, commits, pushes |

The user **never authors** under these rules — they review and approve. If the user wants to author KB content directly, they edit the file and commit; that path bypasses these rules entirely.

## Kind selector — primary + compound (F2 mitigation, with #82 challenger fix)

The kind selector picks the **primary** kind (which file is the canonical home for this insight). Compound insights — where one observation legitimately updates multiple kinds — emit **secondary** diff proposals so no facet of the change is lost.

### Primary kind (rules 1–4, first-match-wins)

1. If the insight defines or refines a **project term** (a noun used inside this project's vocabulary — e.g., "memory object", "harvester", "live-call adapter") → primary kind **`glossary-term`**, target `<method_root>/kb/glossary.md`.
2. If the insight is **a decision the user made or committed to** (architectural, scope, policy, partnership) → primary kind **`decision`**, target `<content_root>/kb/decisions.md`.
3. If the insight is about **a person's role, responsibilities, or relationship to the user** (the user's colleagues, partners, customers, contacts) → primary kind **`person-update`**, target `<content_root>/kb/people.md`.
4. If the insight is about **a team, org, business unit, or external organization** → primary kind **`org-update`**, target `<content_root>/kb/org.md`.
5. If none of 1–4 match → it is NOT a KB contribution. Either it's an artefact (route to `<content_root>/artefacts/<kind>/...` per ADR-0003) or it's chat output (don't capture).

### Secondary diffs — the compound-insight rule

After picking the primary kind, the LLM **must also check** whether the same insight references a person, org, or term that already has a heading in its respective KB file. For each match, propose a **secondary diff** under the corresponding kind. Examples:

- **"We decided that Leonor leads the Atlas team"** — primary: `decision` (decisions.md). Secondaries: `person-update` if `## Leonor Mendonça` exists in people.md (her role line changes); `org-update` if `## Atlas` exists in org.md (its lead line changes). All three diffs proposed in chat together; user can approve any subset.
- **"Decided to drop Polestar from the H2 customer list"** — primary: `decision`. Secondary: `org-update` if `## Polestar` exists in org.md (status changes from prospect/customer to dropped).
- **"Live-call adapter is now the term for #39's per-source live MCP wrappers"** — primary: `glossary-term`. No secondaries; the term is purely vocabulary.

The first-match-wins rule resolves which file gets the *named* version of the change (the entry headed `### 2026-05-07 — Atlas leadership change` lives in decisions.md). Secondaries are smaller, follow-on updates that keep people.md and org.md from going stale on referents they already track.

The user can approve all, some, or none of the proposed diffs. Each diff is a separate commit (so one rejection doesn't block the others).

### When a referent doesn't yet exist in a secondary file

If the primary insight is about Leonor but `## Leonor Mendonça` doesn't yet exist in `<content_root>/kb/people.md`, the LLM does NOT auto-create a person heading **on the in-session path** — that would be silent over-capture from a single retrieval. The LLM proposes only the primary kind. New headings come from rules 3/4 firing as their OWN primary in a future observation, once the trigger threshold is met.

**This rule is in-session-only.** The scan-driven path (see "Scan-driven contributions" below) explicitly DOES propose new headings — but only after aggregating ≥2 distinct sources at scan time, and always under user approval. The two paths protect against different failure modes: in-session guards against single-retrieval over-capture; scan-driven aggregates accumulated evidence so harvested signal isn't lost.

## Two paths to a KB update

A KB update can originate from one of two paths. **Both paths share the same approval gate** — user reviews the proposed diff and approves before the skill writes. They differ only in WHEN aggregation happens:

- **In-session path** (per-turn): LLM detects drift mid-skill-turn against retrieved memory + the active KB. Aggregation happens at query time. Triggers detailed in "In-session triggers" below.
- **Scan-driven path** (per-batch): autonomous scan walks accumulated memory between sessions, aggregates per-kind candidates, emits them as `kind=memo` artefacts. Next interactive session reads the candidates and runs the standard diff-and-approve flow per candidate. Detailed in "Scan-driven contributions" below.

When proposing an update, the LLM MUST be explicit which path it's following (e.g., "scan-driven candidate from `<memo path>`" vs "in-session detection on the current turn"). This disambiguates path provenance at proposal time for the reviewer.

**Scope of this requirement (slice-1 honesty)**: this is editorial — enforced at proposal time, not at file-write time. Once an approved diff lands in `kb/*` with its inline `<!-- produced_by ... -->` comment, the lint verifies provenance shape (session_id, query, sources_cited) but does NOT carry a `path=scan|in-session` field today. A third party reading a KB entry post-write cannot tell which path produced it from the file alone. Adding the field + lint enforcement is on #116's path forward (slice 2 onward); this slice establishes the contract.

The two paths use the **same numeric thresholds**. What changes is the temporal scope of aggregation, not the threshold values.

## In-session triggers

The LLM proposes a KB update mid-turn when one of these observable conditions holds. Triggers are necessary but not sufficient — the LLM still has to draft a diff worth approving.

### `person-update`
- **Trigger**: ≥2 distinct memory objects from different sources (e.g., one Slack thread + one Granola note) consistently report the same person in a role/responsibility different from the existing entry in `<content_root>/kb/people.md`. Single-source observations are NOT enough — they could be a single misattribution.
- **Worked example**: memory shows Leonor in three meetings as the Atlas engineering lead, but `<content_root>/kb/people.md`'s heading "## Leonor Mendonça" still lists her as Product Manager. LLM proposes a person-update.

### `org-update`
- **Trigger**: ≥2 distinct memory objects (any sources) report a structural change to a team / org / external organization (head changes, team renames, customer status changes from prospect → pilot → customer, vendor relationship terminating).
- **Worked example**: memory shows Atlas was renamed from "Atlas Platform" to "Atlas Workspace" in two weekly meetings. LLM proposes an org-update.

### `decision`
- **Trigger**: the user explicitly states a decision in a memory-captured context (Slack message, Granola meeting), OR confirms a recommendation in chat. **Single-occurrence is sufficient** — decisions don't repeat; the user's word is the source.
- **Worked example**: in today's chat, user says "let's go with Option 2 for #51 — skill orchestrates live calls." LLM proposes a decision entry.

### `glossary-term`
- **Trigger**: a project-specific term appears in ≥3 distinct memory objects without an existing definition in `<method_root>/kb/glossary.md`, OR the user explicitly asks for a definition to be added/refined.
- **Worked example**: "live-pinned topic" appears in five different harvest digests and chat threads but isn't defined in glossary.md. LLM proposes a glossary-term.

If a trigger fires but the LLM judges the insight is too speculative, low-stakes, or already implicit in another KB entry, it MAY skip — but must not skip silently if the user explicitly asked. The user-asked path always proposes.

## Scan-driven contributions (per [#116](https://github.com/acardote/personal-assistant-ultra/issues/116))

The in-session path above misses signal that lives in passively-harvested content the user never asks about explicitly: memory objects accumulate between sessions, but no query retrieves them, so no in-session trigger fires.

The scan-driven path closes that gap. `tools/kb-scan.py` (slice 2 of #116) will walk `<content_root>/memory/` between sessions, aggregate per-kind candidates against the **same numeric thresholds** as the in-session path, and emit each candidate as a `kind=memo` artefact under `<content_root>/artefacts/memo/.unprocessed/`. The next interactive session presents the candidates via `/personal-assistant kb-process` (slice 3); the user runs the standard diff-and-approve flow per candidate; approved candidates land in `kb/*` exactly as the in-session path lands them.

This document establishes the contract scan-driven slices must satisfy; tooling implementations follow.

### What the scan-driven path may do

- Aggregate ≥2 distinct memory objects from different sources into a `person-update` candidate (same threshold as in-session). Allowed even if the referent has no existing heading in `<content_root>/kb/people.md` — the candidate proposes the new heading, the user approves or rejects.
- Aggregate ≥2 distinct memory objects into an `org-update` candidate (same threshold).
- Surface a `decision` candidate from a single explicit decision-shape statement in memory (same threshold; decisions are single-occurrence even in-session).
- Surface a `glossary-term` candidate from ≥3 distinct mentions absent from `<method_root>/kb/glossary.md` (same threshold).

### What the scan-driven path MUST NOT do

- Auto-write to `kb/*`. Even after aggregation, every candidate goes through the user-approval gate. No silent merge, no auto-commit, no "high-confidence bypass."
- Lower thresholds below the in-session values. Scan-driven aggregation operates on the accumulated memory pool — that's a wider window than session retrieval, but the threshold values are unchanged.
- Invent a heading from a single mention in a single source. Aggregation is the load-bearing primitive that distinguishes signal-driven creation from hallucination. (See also the in-session "When a referent doesn't yet exist" rule above — same anti-hallucination spirit, different temporal scope.)

### Threshold semantics — temporal scope

In-session: aggregation across the **memory objects retrieved for the current query** (a small subset). Scan-driven: aggregation across the **full memory pool since the last scan watermark** (potentially hundreds). Same thresholds, different denominators. A person who appears in 1 retrieved memory object during a session won't trigger in-session (need ≥2), but might trigger scan-driven if they appear in ≥2 memory objects across the scan window.

This verdict asymmetry is the gap #116 closes — it's not threshold drift. The scan-driven path produces verdicts the in-session path structurally cannot, by design. A future benchmarker comparing the two paths' verdict counts on the same memory pool should expect scan-driven to be a strict superset, not equal.

### Path provenance on approved diffs

When the skill writes an approved scan-driven candidate, the inline `<!-- produced_by: ... -->` comment carries `sources=[mem://<id1>, mem://<id2>, ...]` — the source memory objects the candidate aggregated. The session_id is the interactive session where the user approved (NOT the routine session that emitted the candidate memo). This keeps the trail honest about WHO approved (a human in a session) and HOW the signal was assembled (which memory objects).

## Diff-shape rule (F3 mitigation — mechanical extend-vs-new)

Within the target file, the LLM picks between two diff shapes using a single deterministic test:

**Test**: does the existing file contain a `## <heading>` whose subject is the **same proper-noun referent** as the proposed insight?

- **Same referent → extend** that heading. Add a new bullet, a new paragraph, OR a `<!-- produced_by ... -->` line followed by the contributed lines, depending on the kind:
  - `person-update`: add a dated bullet under the existing heading. Format: `- 2026-05-07: <one-line update>`.
  - `org-update`: same dated-bullet format.
  - `decision`: add a sub-section `### <iso-date> — <decision-title>` under the existing topic heading.
  - `glossary-term`: REFINE the existing definition by editing in place; add a `<!-- produced_by ... -->` comment immediately above the changed line(s). Glossary entries are short — extend means refine, not append.

- **Different referent OR no existing heading → new heading**. Create `## <referent>` (people / org / glossary) or `## <decision-area>` (decisions). Place alphabetically for people and glossary; reverse-chronologically for decisions; by-team-or-customer-name grouping for org.

**Same referent test** is concrete: case-insensitive exact match of the proper noun OR a previously-recorded alias. If neither matches, it's a new heading. There is no fuzzy match — if "Leonor Mendonça" exists and the insight is about "Leonor M.", the LLM normalizes the alias as part of the diff (extends the existing heading).

## Decision Scope field (per [#133](https://github.com/acardote/personal-assistant-ultra/issues/133))

Every entry in `<content_root>/kb/decisions.md` MUST include a `**Scope:**` line naming the referent the decision is about — a person, org, team, or project. This is what makes context-dependent decisions retrievable from the always-in-context layer.

Without Scope, a heading like "NYC added as fourth priority geography" is orphaned: a future query about Nuro can't match it (the body says "NYC" + "delivery" — Nuro doesn't appear). With `- **Scope:** Nuro`, the always-loaded layer-3 line links the decision to its referent.

For decisions that apply broadly (e.g., a company-wide policy), use the operating org name (e.g., `Nexar`). For decisions that span multiple referents, name the most direct one and add the others in the body.

The drift detector (per parent [#135](https://github.com/acardote/personal-assistant-ultra/issues/135)) routes newly-harvested memory against decisions sharing the same Scope as the memory's tags or extracted referent. Without Scope, drift detection degrades to noisy topic-overlap inference.

`tools/kb-scan.py`'s decision-extraction prompt emits `referent` in its YAML output today, but the renderer drops it (the bug [#132](https://github.com/acardote/personal-assistant-ultra/issues/132) tracks). Once #132 lands, kb-scan-emitted candidates will carry Scope by construction. Until then, candidates land without Scope and need backfill (as happened to the 54 entries from #116's bootstrap). Existing seed entries (pre-#133) without Scope are grandfathered — `tools/lint-provenance.py` does NOT yet enforce Scope as required (a stricter gate to add later if needed).

## Provenance (per ADR-0003)

Every diff carries a `<!-- produced_by: session=<8-hex>, query="<short>", at=<iso8601>, sources=[...] -->` line:
- For vault-scoped knowledge (people, org, decisions): inline immediately above the contributed lines, persisted in the file. Stripped at assembly time by `tools/assemble-kb.py` (follow-up slice).
- For method-scoped knowledge (glossary): NOT persisted in the file. Provenance lives in the PR description; the source file stays clean for shipping to other users.

## Worked example: full flow for a `decision`

User chat: *"let's go with Option 2 for #51"*.

1. **LLM** identifies trigger: explicit decision statement.
2. **LLM** applies kind selector: rule 2 fires (decision); skip 3, 4.
3. **LLM** applies same-referent test against `<content_root>/kb/decisions.md`. Heading "## Live-call orchestration architecture" doesn't exist; new heading.
4. **LLM** drafts diff in chat:
   ```diff
   + ## Live-call orchestration architecture
   + <!-- produced_by: session=9864c3e9, query="proceed with #51 architecture decision", at=2026-05-06T17:42:00Z, sources=[https://github.com/acardote/personal-assistant-ultra/issues/51] -->
   + - **Scope:** personal-assistant skill (architecture)
   + ### 2026-05-06 — Skill orchestrates live MCP calls
   + Per parent issue #39, the skill (not route.py) is the live-call orchestrator. Sequential calls; fire all sources on zero_hit; split implementation per source.
   ```
5. **User** reviews, approves.
6. **Skill** writes the diff to `<content_root>/kb/decisions.md`, commits with message `kb: live-call orchestration architecture (decision per #51)`, pushes.

In this example there are no secondary diffs because no person or org referent is named in the decision. A counter-example with secondaries: "decided that Leonor leads Atlas" would emit one primary `decision` diff plus a secondary `person-update` diff (if Leonor exists in people.md) and a secondary `org-update` diff (if Atlas exists in org.md), each as a separate commit on user approval.

## Cross-reference

ADR-0003 is the parent design; this document operationalizes its `knowledge` half. The vault-side artefact layout (the `artefact` half) is documented at `<content_root>/artefacts/README.md` per child #79.

## Project tier (per ADR-0003 Amendment 1)

**Knowledge contributions are global, not project-scoped.** Even when an insight emerges from inside an active PA project (per #88), `person-update` / `org-update` / `decision` / `glossary-term` updates land in the same `<content_root>/kb/` files (or `<method_root>/kb/glossary.md`) — never under `<content_root>/projects/<slug>/`. The kind selector and triggers above are unchanged by the project tier.

If the same insight produces both a project artefact (e.g., a memo summarizing Q3 strategy decisions) and a KB update (e.g., a new entry in `decisions.md`), they're separate diffs: the artefact lands in `<content_root>/projects/<slug>/artefacts/memo/`, the KB update lands in `<content_root>/kb/decisions.md`. The compound-insight rule (primary + secondary diffs) operates on the KB side only — projects don't appear in it.
