# Glossary

Layer-3 knowledge: terms-of-art used in this project. Always in context. The assistant uses these definitions when interpreting the user's requests; if the user says "memory object" the assistant should mean the layer-2 thing defined here, not generic LLM memory.

Each entry follows the format:

```
## <term>
- **Last verified:** <YYYY-MM-DD>
- **Source:** <where this term is defined or used>

<one-paragraph definition.>
```

---

## raw archive (layer 1)

- **Last verified:** 2026-05-05
- **Source:** [parent #1](https://github.com/acardote/personal-assistant-ultra/issues/1), [ADR-0001](../docs/adr/0001-storage-backend.md)

The unmodified original artifacts the assistant has access to: full Slack threads, full emails, full Granola exports, full meeting transcripts, full uploaded documents. Lives under `raw/`, mostly git-ignored. Never fed directly to the model — only to the compression pipeline that produces memory objects. This is per Sei: "never modified, never fed directly to a model."

## memory object (layer 2)

- **Last verified:** 2026-05-05
- **Source:** [`docs/schemas/memory-object.md`](../docs/schemas/memory-object.md)

The editorially compressed form of a layer-1 raw artifact. A Markdown file with YAML frontmatter conforming to the project schema. Body target ≤800 tokens. Carries provenance (`source_uri`) back to the raw artifact. Lives under `memory/`, git-tracked. Produced by `tools/compress.py`.

## knowledge base / KB (layer 3)

- **Last verified:** 2026-05-05
- **Source:** [parent #1](https://github.com/acardote/personal-assistant-ultra/issues/1), this directory

The always-in-context files that the assistant loads on every invocation: people, org, decisions, glossary. Lives under `kb/`. Token budget ≤4K total across all files.

## harvester

- **Last verified:** 2026-05-05
- **Source:** [parent #1](https://github.com/acardote/personal-assistant-ultra/issues/1), [issue #5](https://github.com/acardote/personal-assistant-ultra/issues/5)

The pipeline that pulls new items from a `Source` (Slack, Gmail, Granola, Google Meet, generic transcript drop) into layer 1, then runs the compression pipeline to land the corresponding memory objects in layer 2. Idempotent on re-run.

## editorial judgment

- **Last verified:** 2026-05-05
- **Source:** [Sei article](https://compoundandcare.substack.com/p/your-ai-assistant-has-amnesia-heres), [`tools/prompts/compress.md`](../tools/prompts/compress.md)

Sei's framing for compression: not summarization, but answering "what does the user need to know to make better decisions." Specific decision-grade detail (numbers, names, dates, constraints) is preserved verbatim; scaffolding is dropped; nothing is invented.

## falsifier

- **Last verified:** 2026-05-05
- **Source:** Bruno Method SPEC §3, `CLAUDE.md`

A concrete observation against landed reality that would force retraction of a claim or a piece of work. Format: "If <observable>, retract because <reason>." Distinct from acceptance criteria (pre-deploy contracts) and challenges (generic concerns).
