# Decisions

Layer-3 knowledge: durable decisions the user has made. Always in context. These are the load-bearing commitments — the assistant should treat them as standing rules unless the user revises them.

Each entry follows the format:

```
## <Decision title>
- **Date:** <YYYY-MM-DD>
- **Status:** decided | proposed | revisited
- **Last verified:** <YYYY-MM-DD>
- **Expires:** <YYYY-MM-DD or never>
- **Source:** <ADR / issue / conversation / manual>

<short body — what was decided + the load-bearing reason.>
```

This file does not duplicate full ADRs. ADRs live in `docs/adr/*` and link from here when they exist.

---

## Storage backend = flat filesystem

- **Date:** 2026-05-05
- **Status:** decided
- **Last verified:** 2026-05-05
- **Expires:** never (revisit if F3 fires — see ADR for trip wires)
- **Source:** [`docs/adr/0001-storage-backend.md`](../docs/adr/0001-storage-backend.md), [issue #2](https://github.com/acardote/personal-assistant-ultra/issues/2)

The three-layer memory architecture stores data on the flat filesystem: `raw/` (layer 1, mostly git-ignored), `memory/` (layer 2, git-tracked), `kb/` (layer 3, git-tracked). SQLite and vector stores were rejected. The decision is re-opened only if filesystem ops become slow at scale or the router is forced to load substantially more context than it uses.

## Bruno Method discipline is in force

- **Date:** 2026-05-05
- **Status:** decided
- **Last verified:** 2026-05-05
- **Expires:** never (this is a working discipline, not a tactical decision)
- **Source:** [`.bruno/config.toml`](../.bruno/config.toml), `CLAUDE.md`

Work in this project is structured with the Bruno Method: parent + child issues, falsifiers per child, evidence with provenance, reconciliation against landed state before close. The canonical IR is GitHub Issues at `acardote/personal-assistant-ultra`. The assistant should follow this discipline when proposing or reviewing work — never close a claim without reconciliation, never accept a child without ≥1 falsifier, and treat evidence-without-provenance as a non-claim.
