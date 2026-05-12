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

### Drift suppression — knobs

When a drift candidate against decision `art-<via>` has been dismissed `--threshold` times (default 3), `kb-process drift-dismiss` flips `suppressed_at` for that decision in `<vault>/.harvest/kb-drift-suppress.json`, and `kb-drift-scan` skips it on the next run. Sticky design: only `kb-process drift-reenable art-<via>` (or `drift-apply` of an amendment, which contradicts past dismissals and clears suppression automatically) re-opens the decision for re-evaluation.

Configurable via `<vault>/.harvest/kb-drift-config.json`:

```json
{ "drift_dismissal_threshold": 3 }
```

Missing or malformed config falls back to the default 3 with a stderr warning. Inspect `<vault>/.harvest/kb-drift-suppress.json` directly to see currently-suppressed decisions and the most-recent N dismissal reasons per decision.

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

If the same insight produces both a project artefact (e.g., a memo summarizing Q3 strategy decisions) and a KB update (e.g., a new entry in `<content_root>/kb/decisions.md`), they're separate diffs: the artefact lands in `<content_root>/projects/<slug>/artefacts/memo/`, the KB update lands in `<content_root>/kb/decisions.md`. The compound-insight rule (primary + secondary diffs) operates on the KB side only — projects don't appear in it.

## Pattern catalog (per [#173](https://github.com/acardote/personal-assistant-ultra/issues/173), slice 1 [#174](https://github.com/acardote/personal-assistant-ultra/issues/174))

Empirical rules harvested from `/personal-assistant kb-process` walk sessions. Each rule captures a generalizable correction the user made during candidate review, distilled into a trigger + action shape that can be applied (manually now; mechanically in #173's deferred slice 3) on future walks.

**Purpose**: shift kb-process candidate review from a per-candidate human gate to mostly-autonomous classification by accumulating user corrections as a structured rubric. Parent issue: [#173](https://github.com/acardote/personal-assistant-ultra/issues/173). This section: [#174](https://github.com/acardote/personal-assistant-ultra/issues/174).

**Rule shape — exactly 5 fields, in this order, parseable mechanically by slice 2's Phase-1 reader**:

- **Name** — encoded in the `### Rule N: <imperative title>` heading; short imperative (3–10 words).
- `- **Trigger**:` observable condition on a kb-scan-emitted candidate (one bullet, single paragraph).
- `- **Action**:` one of `approve`, `reject as <category>`, `amend <field>: <value>` (one bullet, single paragraph).
- `- **Reasoning**:` one line explaining why the rule is load-bearing (one bullet, single paragraph).
- `- **Example**:` a verifiable reference — commit SHA in `getnexar/acardote-pa-vault` (preferred) OR art-uuid under `<content_root>/artefacts/memo/.rejected/` (one bullet, single paragraph).

**Parser regex contract (for slice 2)**: rules begin with `^### Rule \d+:` and each field bullet begins with `^- \*\*(Trigger|Action|Reasoning|Example)\*\*:`. The field regex is **scoped to lines between successive `### Rule N:` headings only** — pre-existing `- **Trigger**:` lines in this document's `## In-session triggers` section (rules 67–82, outside any `### Rule N:` heading) are NOT part of the catalog and MUST be ignored by slice 2's parser. Multi-paragraph fields are NOT allowed — keep each field to one paragraph so the parser can tokenize on the next `- **` boundary. Example fields MAY contain multiple comma-separated refs in one paragraph (e.g., "commit `c82f768`; commit `705a847`") — slice 3's classifier MUST split on `;` or `,` boundaries.

**Precedence**: rules apply **first-match-wins by rule number** — lower-numbered rules win on overlap. When a new rule contradicts an existing rule's action on a shape both can match, EITHER (a) refactor the older rule by inserting a guard condition, OR (b) renumber the new rule below the existing one so first-match-wins resolves to the older behavior. This is the load-bearing tie-breaker for falsifier 1 on #174.

**Categories for `reject as <X>`** (used in the Action field):

- `not-formalized` — an idea, brainstorm, or unconfirmed option; not a committed decision.
- `ephemeral` — tactical/operational decision that expires within days (booth setup, demo prep, one-off message).
- `wrong-layer` — should be person/org/glossary, not decision (or vice versa).
- `inaccurate` — extract materially misrepresents the source memo; unsalvageable.
- `dup-of-<art-uuid>` — duplicate of another candidate; use the canonical art-uuid. **Reserved**: no rule in slice 1 of the catalog uses this category. Listed here so slice 3's classifier knows it's a legitimate `reject as` value.

---

### Rule 1: Default Vera scope on Granola product-team-huddle decisions

- **Trigger**: source memo is a Granola note tagged with the daily-huddle pattern (`product-team-daily-huddle-*`, or any Granola note whose title contains `product team huddle`) AND body covers a product/pricing/indexing decision AND body does NOT explicitly mention Atlas surfaces (MCP, data sources, ordering flow). Reference memo from 2026-05-12: `mem-60438256-0519-4a6b-b442-2cf96a873132`.
- **Action**: `amend Scope: Vera`.
- **Reasoning**: kb-scan defaults this source to "Atlas" or "Nexar-wide"; user-corrected to Vera against this memo (commit `fd08f9f`). Rule 9 takes precedence if the trigger also matches an `org-update`.
- **Example**: commit `fd08f9f` in `getnexar/acardote-pa-vault` (composite risk indices, Atlas → Vera amendment, scope explicitly named in the produced_by comment).

### Rule 2: Disambiguate pricing direction

- **Trigger**: candidate title or body mentions "<vendor> pricing unchanged", "<vendor> integration pricing", or "pricing to/from <vendor>" without naming WHICH party pays whom.
- **Action**: `amend body: name pricing direction explicitly (one of "<vendor>→customers" or "Nexar→<vendor>"); also update the heading to reflect the direction`.
- **Reasoning**: the same English phrase covers two opposite commercial decisions; the user's intent is always one direction, never both. Rule 5 takes precedence if the candidate is exploratory rather than a committed price.
- **Example**: commit `802856b` in `getnexar/acardote-pa-vault` (Axon's customer-facing pricing, amended from generic "Axon integration pricing"; same commit also archives Rule 6's `art-6c8a7f9d` rejection, so cross-reference Rule 6 when auditing).

### Rule 3: Reject ephemeral event/booth/demo prep

- **Trigger**: candidate title contains `booth`, `screen showcase`, `scale-up for <X demo>`, `<event> setup`, `showcase plan`, or names a specific upcoming date as the entire frame ("Monday Nuro demo slot").
- **Action**: `reject as ephemeral`.
- **Reasoning**: tactical event prep doesn't survive the event; not durable-decision shape. Rule 4 takes precedence on single-recipient comms; Rule 8 takes precedence on launch-dated decisions (those are durable, the date is the bound).
- **Example**: art-uuid `art-4567594d-9dbf-4905-b8ce-34fc8451cc36` (IBM booth showcase plan, rejected; archived under `<content_root>/artefacts/memo/.rejected/` via commit `c82f768`); art-uuid `art-0497f974-cf01-4067-92a2-44570c361dc0` (Atlas scale-up before Monday Nuro demo, rejected; archived via commit `705a847`).

### Rule 4: Reject single-recipient tactical comms unless reframed around a durable stance

- **Trigger**: candidate body is about a single message/note/email to a single person, with no underlying durable policy named.
- **Action**: `reject as ephemeral`.
- **Reasoning**: one-off communications are not decisions; the policy behind them is. The user MAY override this rule mid-review by reframing the candidate around a durable stance (e.g., "decline Waylens re-engagement" → "do not re-engage Waylens, durable stance"); when that happens, the override is the rule that fires, not this one. Rule 4 ships its default action only.
- **Example**: commit `4d041cb` in `getnexar/acardote-pa-vault` (Waylens "soft note" — landed only after the user-reframed "do not re-engage Waylens" durable stance replaced the per-note framing; the produced_by comment on the landed entry documents the reframe).

### Rule 5: Reject ideas and brainstorms

- **Trigger**: candidate body uses speculative language ("adopted a structure where...", "positioned as...", multi-tier pricing options without commitment, hypothetical scoping), OR the user-facing prose reads like exploration rather than a committed decision.
- **Action**: `reject as not-formalized`.
- **Reasoning**: brainstorm captures and decisions look the same to kb-scan; only the latter belong in KB. Rule 2 takes precedence on candidates that are exploratory but pricing-directional (those want amendment, not rejection); Rule 7 takes precedence on candidates that are exploratory but Q-goal-shaped.
- **Example**: art-uuid `art-9261401a-7b46-4eeb-b839-a8474c7ae11b` (Sharing-model pricing with 10× multiplier, rejected with reason "was just an idea, not a formalized decision"; dup pair member with `art-9c66f6a7-3d73-47e2-89d1-c2a4f9e29a74`; archived via commit `96d7fc2`).

### Rule 6: Reject inaccurate kb-scan extracts (do not amend)

- **Trigger**: candidate body materially misrepresents the source memo (reversed constraints, fabricated detail, contradicts the source on a load-bearing claim).
- **Action**: `reject as inaccurate`. Do NOT amend — the extract is unsalvageable; salvaging costs more than waiting for the underlying memo to regenerate a candidate.
- **Reasoning**: kb-scan occasionally hallucinates structure that contradicts the source; partial fixes leave subtle drift in the KB.
- **Example**: art-uuid `art-6c8a7f9d-3d6a-425c-a462-1035e9f9a89f` (Vera MVP candidate, rejected with reason "highly inaccurate"; dup pair member with `art-b36abf1f-c96f-4ff6-a82a-d0675137a002`; both archived under `<content_root>/artefacts/memo/.rejected/`).

### Rule 7: Q-goals get bounded Expires

- **Trigger**: candidate title or body names a quarterly goal (`Q<N> goal`, `quarterly goal`, `this quarter's commitment`).
- **Action**: `amend Expires: end of Q<N> (or until quarterly goals refresh)`.
- **Reasoning**: Q-goals expire by definition; `Expires: never` is structurally wrong-shaped for them.
- **Example**: commit `00fe30f` in `getnexar/acardote-pa-vault` (MoD business case validation as Q-goal, amended `never` → `end of Q2`).

### Rule 8: Launch-dated decisions get bounded Expires

- **Trigger**: candidate title contains a target launch window (`launches end of June`, `GA early August`, `<feature> launch <date>`).
- **Action**: `amend Expires: at launch (~<date>)`.
- **Reasoning**: launch decisions self-expire at launch; `Expires: never` overstates durability.
- **Example**: commit `6d3a6f3` in `getnexar/acardote-pa-vault` (CartTag launches end of June / beginning of July 2026, amended to `at launch (~2026-07)`).

### Rule 9: Reject Nexar products mistakenly extracted as external orgs

- **Trigger**: candidate kind is `org-update` AND the referent is a Nexar product (Vera, Atlas, CartTag, BADAS, MoD, Guardian Mode, Atlas 2.0, etc.).
- **Action**: `reject as wrong-layer`.
- **Reasoning**: kb-scan surfaces product names as nouns and sometimes extracts them as external partner orgs; this poisons `<content_root>/kb/org.md` retrieval.
- **Example**: commit `6e27d3c` in `getnexar/acardote-pa-vault` (Vera-as-org candidate `art-f451c201-dfdd-4b62-b342-469e0c503dfa`, rejected with user reason "Vera is one thing — Nexar product").

### Rule 10: Correct Nauto/merged-entity CEO to Zach

- **Trigger**: candidate body names anyone other than Zach (commonly: Eran) as the post-merger Nauto/Nexar CEO.
- **Action**: `amend body: replace named CEO with "Zach"` (matching the landed convention in `<content_root>/kb/decisions.md`; full surname is in `<content_root>/kb/people.md` but the decision entries are first-name-only).
- **Reasoning**: kb-scan misattributes the Nauto founder Eran to the CEO role; Zach is the named CEO of the merged entity. Verifiable in `<content_root>/kb/people.md` heading.
- **Example**: commit `3630094` in `getnexar/acardote-pa-vault` (Nauto merger plan, user-corrected Eran → Zach during the 2026-05-12 walk).

### Rule 11: Internal product announcements happen at NLM

- **Trigger**: candidate body names "upcoming conference", "next big event", or another external venue as the announcement channel for an internal Nexar product / spin-out / org change.
- **Action**: `amend body: replace external-venue mention with "NLM (Nexar all-hands meeting)"`.
- **Reasoning**: internal-facing announcements happen at NLM by Nexar convention; kb-scan defaults to external venues. Slice-1 evidence is N=1 (single Mithran example); rule retracts if the next walk produces a counter-example.
- **Example**: commit `93b1151` in `getnexar/acardote-pa-vault` (Mithran spin-out, user-corrected announcement venue to NLM).

### Rule 12: Disambiguate heat-map type (opt-in vs coverage)

- **Trigger**: candidate body about "Nexar opt-in heat maps to Axon" (or similar partner data delivery) without naming the heat-map TYPE.
- **Action**: `amend body: replace "opt-in heat maps" with "opt-in or coverage heat maps"`.
- **Reasoning**: two heat-map types exist; kb-scan defaults to "opt-in" only and loses the coverage variant. Slice-1 evidence is N=1 (single Axon heat-map example); rule retracts if the next walk produces a counter-example or the Axon partnership scope evolves.
- **Example**: commit `35986fa` in `getnexar/acardote-pa-vault` (heat maps to Axon, user-amended to opt-in OR coverage).

---

### Applying the catalog

The catalog is used in two contexts:

1. **Manual review (today)**: during a `/personal-assistant kb-process` walk, the LLM consults the catalog after running `tools/kb-process.py show <art-uuid>` and BEFORE drafting the proposed diff. If any rule's trigger matches, the LLM proposes the rule's action with the rule's name cited. The user still has the final say. Slice 2 of #173 will wire the catalog into Phase 1 of the personal-assistant skill so this consultation is automatic.

2. **Mechanical pre-bin (deferred, slice 3 of #173)**: `tools/kb-classify.py` walks `.unprocessed/` once per session, applies the catalog's triggers, and routes each candidate into `auto-approve`, `auto-reject`, or `needs-review` piles. The user only walks `needs-review`. Triggered when catalog has ≥50 rules AND prediction agreement is reliably ≥85%.

### How to add a rule

When a user correction during a walk reveals a generalizable pattern (the same correction would plausibly apply to ≥3 future candidates):

1. Draft the rule using the 5-field template above. All 5 fields are required; missing fields fail the slice-2 parser.
2. Choose an existing-commit example from the current session (preferred), or reference an art-uuid in `<content_root>/artefacts/memo/.rejected/`.
3. Append the rule under the next available `### Rule N:` heading. Numbering is monotonic — never reuse a number after a rule is superseded.
4. Run `tools/lint-docs.py` to confirm the catalog parses.

Do NOT add rules for one-off corrections; the catalog's value is generalization, not narration.

### Catalog hygiene

- **Contradictions**: if a new rule contradicts an existing rule on the same trigger shape, refactor by adding a guard condition to the older rule and a new rule for the new branch. Two rules SHOULD NEVER produce contradictory actions on the same candidate (per falsifier 1 on #174). When refactoring is heavy, the **Precedence** clause (first-match-wins by rule number, declared in the rule-shape contract above) is the safety net.
- **Deletion**: rules are not deleted when superseded — instead, append `**Status:** superseded by Rule N` as a sixth field, keeping the entry for audit-trail purposes. **Slice 2's parser MUST treat superseded rules as inert** (the `Status` field is for human audit only; superseded rules are not matched against incoming candidates).
- **Drift**: if a rule's `Example` commit becomes unreachable (force-pushed away, history rewritten), re-anchor to a different verifiable example or retire the rule. Unreachable examples fail falsifier 3 on #174.

### Known weaknesses (slice 1)

These are limitations of the catalog AS SHIPPED that future walks must address; they are NOT merge-blockers but they ARE the falsification surfaces slice-2 empirical work should hit hardest:

- **N=1 generalization risk** on Rules 10, 11, 12. Each derives from a single user correction in the 2026-05-12 walk. If the next walk produces a counter-example for any of these three rules, the rule retracts (per the rule's own Reasoning field). Until ≥3 reinforcing observations land, treat these as provisional.
- **Trigger-vs-escape-clause asymmetry on Rule 1**. The trigger filters by Granola source-pattern; the real classification work is done by the `body does NOT explicitly mention Atlas surfaces` guard. Slice 2 should evaluate whether the trigger is doing useful filtering or whether the guard alone suffices (and the rule could be simplified).
- **Coverage measurement deferred**. Falsifier 4 on #174 ("≥50% of next batch matches at least one rule") cannot fire until a fresh kb-scan batch is harvested. File a follow-up child of #173 to anchor that measurement so the falsifier doesn't go dormant.
- **Inaccurate-extract rule (Rule 6) requires LLM-with-context**. The trigger ("body materially misrepresents the source memo") can't be evaluated by a pure regex classifier — it requires loading both the candidate AND the source memo. Slice 3's mechanical pre-binner CANNOT auto-apply Rule 6; it must route Rule-6-candidates to `needs-review` regardless of confidence. This is a deliberate limit on slice 3's autonomy.
