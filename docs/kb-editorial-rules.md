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

## Kind selector (F2 mitigation — deterministic routing)

The four kinds are mutually exclusive on the target file. Pick the FIRST rule below that matches; do not consider later rules:

1. If the insight defines or refines a **project term** (a noun used inside this project's vocabulary — e.g., "memory object", "harvester", "live-call adapter") → **`glossary-term`**, target `<method_root>/kb/glossary.md`.
2. If the insight is **a decision the user made or committed to** (architectural, scope, policy, partnership) → **`decision`**, target `<content_root>/kb/decisions.md`.
3. If the insight is about **a person's role, responsibilities, or relationship to the user** (the user's colleagues, partners, customers, contacts) → **`person-update`**, target `<content_root>/kb/people.md`.
4. If the insight is about **a team, org, business unit, or external organization** (Nexar internal teams, customer companies, vendor companies) → **`org-update`**, target `<content_root>/kb/org.md`.
5. If none of 1–4 match → it is NOT a KB contribution. Either it's an artefact (route to `<content_root>/artefacts/<kind>/...` per ADR-0003) or it's chat output (don't capture).

The LLM must apply rules 1–4 IN ORDER. A statement like "We decided that Leonor leads the Atlas team" is `decision` (rule 2 fires first), even though it also looks org-shaped (rule 4) and person-shaped (rule 3). Order eliminates two-readers ambiguity.

## Triggers — when the LLM proposes an update

The LLM proposes a KB update when one of these observable conditions holds. Triggers are necessary but not sufficient — the LLM still has to draft a diff worth approving.

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
   + ### 2026-05-06 — Skill orchestrates live MCP calls
   + Per parent issue #39, the skill (not route.py) is the live-call orchestrator. Sequential calls; fire all sources on zero_hit; split implementation per source.
   ```
5. **User** reviews, approves.
6. **Skill** writes the diff to `<content_root>/kb/decisions.md`, commits with message `kb: live-call orchestration architecture (decision per #51)`, pushes.

## Cross-reference

ADR-0003 is the parent design; this document operationalizes its `knowledge` half. The vault-side artefact layout (the `artefact` half) is documented at `<content_root>/artefacts/README.md` per child #79.
