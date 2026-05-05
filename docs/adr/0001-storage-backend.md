# ADR-0001 — Storage backend for the three-layer memory architecture

- Status: Accepted
- Date: 2026-05-05
- Decider: acardote
- Related: parent issue [#1](https://github.com/acardote/personal-assistant-ultra/issues/1), child [#2](https://github.com/acardote/personal-assistant-ultra/issues/2)

## Context

The parent project builds a personal assistant whose long-term memory follows Sei's three-layer architecture: a raw archive (layer 1), editorially compressed memory objects (layer 2), and an always-in-context knowledge base (layer 3). Each layer needs a storage backend. Three obvious candidates:

1. **Flat filesystem** — layers are directories of files (Markdown + YAML frontmatter for layer 2/3, arbitrary blobs for layer 1).
2. **SQLite** — a single file-backed database with tables for memory objects, sources, KB entries.
3. **Vector store** (Chroma / SQLite-VSS / similar) — embedding-indexed memory objects with text columns alongside.

The decision needs to support:
- Provenance from layer 2 → layer 1 (the load-bearing invariant per the issue body's falsifier).
- Editing memory objects by hand when the LLM gets compression wrong.
- Diffability under git for layer 2 and layer 3 (the user wants to see how the assistant's memory evolves).
- Reasonable retrieval performance up to a few thousand memory objects (single-user scale).
- Privacy: layer 1 contains PII (Slack threads, email, transcripts) and must not leak via git.

## Decision

**Flat filesystem with split git treatment**:

- Layer 1 (`raw/`): git-ignored except for committed worked examples under `raw/examples/`. Real harvested raw data is local-only.
- Layer 2 (`memory/`): git-tracked. Memory objects are individual Markdown files with YAML frontmatter conforming to [`docs/schemas/memory-object.schema.json`](../schemas/memory-object.schema.json).
- Layer 3 (`kb/`): git-tracked. Format defined in child [#4](https://github.com/acardote/personal-assistant-ultra/issues/4).

Provenance is preserved by `source_uri` in the memory-object frontmatter pointing at a layer-1 path. `file:./raw/...` URIs are intentionally relative-to-project so the layer-2 repo remains useful when checked out on another machine even though the layer-1 files are absent there (dangling pointers across machines are expected and tolerated; only same-machine dangling pointers are a bug — see falsifier F1).

## Consequences

**Positive**

- Hand-editable: when the compression pipeline gets a memory object wrong, the user opens the file and fixes it.
- Diffable: git history shows exactly how the assistant's memory evolved over time.
- Tooling-agnostic: any text editor works; no DB driver required.
- Provenance is a first-class field — not a foreign key obscured behind a query.
- Privacy by default: real raw artifacts never enter git unless the user explicitly opts in.

**Negative**

- No native indexing. Locating relevant memory objects for a query requires reading frontmatter across files. **For single-user single-machine scale (hundreds to low thousands), this is acceptable.** If volume grows past that and operations become visibly slow, ADR-0001 must be re-opened — falsifier F3 calls this out explicitly.
- Cross-file invariants (e.g., unique `id`) are not enforced by the backend. Validation must happen at write time in the compression pipeline ([#3](https://github.com/acardote/personal-assistant-ultra/issues/3)).
- No semantic search out of the box. Retrieval in this architecture is initially expected to use frontmatter filtering (`kind`, `tags`, date ranges); semantic search via embeddings can be added later as a layer on top of the filesystem (e.g., a derived index file refreshed on write) without changing the storage of truth.

## Rejected alternatives

### SQLite

Rejected. A SQLite file is opaque to a casual reader — `git diff` shows binary churn, hand-editing requires a CLI, and the user explicitly wants to see how the assistant's memory evolves under git history. SQLite would also make the raw/memory/kb separation harder to enforce visually and harder to apply different git-tracking rules per layer.

If F3 fires (filesystem ops becoming slow at scale), SQLite is the most likely re-opening target — but only as a derived index layer, with the canonical store still being flat files.

### Vector store (Chroma / SQLite-VSS / etc.)

Rejected for the same reasons as SQLite, plus: vector indices conflate storage and retrieval. The architecture wants storage and retrieval to be separable so that retrieval strategies can change without re-architecting the store. A vector index is a natural addition on top of the flat filesystem (build it from the layer-2 contents, refresh on change), not the canonical store itself.

## Re-opening criteria (F3 trip wires)

ADR-0001 should be re-opened if:

1. Routine retrieval over the layer-2 corpus consistently exceeds a small budget (target: <1s for filtering+ranking the working set).
2. The router is forced to load substantially more context than it uses because filesystem-walk-based selection is too coarse.
3. Manual prune/expire over the corpus takes long enough to be a friction point.

The expiry rules child ([#8](https://github.com/acardote/personal-assistant-ultra/issues/8)) and the evaluation harness ([#9](https://github.com/acardote/personal-assistant-ultra/issues/9)) are likely places where these symptoms first appear.
