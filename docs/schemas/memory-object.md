# Memory Object schema (layer 2)

A **memory object** is the layer-2 representation in the three-layer memory architecture defined in parent issue [#1](https://github.com/acardote/personal-assistant-ultra/issues/1). It is the editorially compressed, structured form of a layer-1 raw artifact, retaining provenance back to the original.

This document is the human-readable companion to [`memory-object.schema.json`](./memory-object.schema.json). The JSON Schema is the machine-readable contract; this doc explains intent.

## On-disk form

A memory object is a single Markdown file with YAML frontmatter:

```markdown
---
id: <uuid-or-hash>
source_uri: <resolvable-uri>
source_kind: <classifier>
created_at: <iso-datetime>
expires_at: <iso-datetime | null>
kind: <classifier>
tags: [<tag>, <tag>]
title: <optional human title>
summary: <optional one-sentence summary>
---

<body — editorially compressed content, target ≤800 tokens>
```

Files live under `memory/`. Subdirectories are organizational and do not carry semantics — the frontmatter is the only authoritative metadata.

**YAML gotcha**: Quote timestamp values (`created_at: "2026-05-05T00:00:00Z"`). PyYAML auto-parses unquoted ISO 8601 strings into `datetime` objects, which then fail JSON Schema validation against `type: string`. The validator script `tools/validate-memory-object.py` enforces this implicitly.

## Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Stable identifier. UUID v4 or content-derived hash. Must be unique within the memory store. |
| `source_uri` | string (URI) | Resolvable pointer to the layer-1 raw artifact. **Must dereference at write time.** Examples: `file:./raw/slack/T0123/abc.json`, `https://acardote.slack.com/archives/C123/p456`, `gmail://thread/abc`. |
| `source_kind` | string | Origin classifier. Recommended values listed below; **not strictly enumerated** so new sources can introduce new kinds without schema migration (mitigates F2 — schema fiction). |
| `created_at` | string (ISO 8601 datetime) | When the memory object was written. **Distinct from when the source was created** — source-creation time goes into source-specific metadata. |
| `expires_at` | string (ISO 8601 datetime) or `null` | When this memory object should be pruned/demoted. `null` means no expiry (e.g., legal). Default windows derived from `kind` per the expiry-rules child ([#8](https://github.com/acardote/personal-assistant-ultra/issues/8)). |
| `kind` | string | Content classifier driving expiry windows and retrieval ranking. Recommended values listed below. **Not strictly enumerated.** |
| `tags` | array of strings | Free-form labels. Empty array allowed. |

## Optional fields

| Field | Type | Notes |
|---|---|---|
| `title` | string | Human-readable title for retrieval display. |
| `summary` | string | One-sentence summary distinct from the body. |

Source-specific metadata (e.g., `slack_channel_id`, `gmail_thread_id`, `meet_event_id`) is allowed via `additionalProperties: true` in the JSON Schema. Adapters in children [#5](https://github.com/acardote/personal-assistant-ultra/issues/5) and [#6](https://github.com/acardote/personal-assistant-ultra/issues/6) define their own conventions.

## Recommended values

### `source_kind`

| Value | Origin |
|---|---|
| `slack_thread` | A Slack thread or message |
| `gmail_thread` | A Gmail thread |
| `gmeet_transcript` | A Google Meet transcript |
| `granola_note` | A Granola meeting note |
| `transcript_file` | A `.vtt`/`.srt`/`.txt` dropped into a watched folder |
| `deck` | A presentation (slides) |
| `doc` | A long-form document |
| `manual_note` | Authored directly by the user |

### `kind`

| Value | Typical default `expires_at` window |
|---|---|
| `strategy` | ~6 months |
| `weekly` | ~3 months |
| `retrospective` | ~6 months |
| `decision` | no expiry (`null`) |
| `legal` | no expiry (`null`) |
| `thread` | ~3 months |
| `note` | ~3 months |
| `glossary_term` | no expiry (`null`) |

These defaults are operationalized in child [#8](https://github.com/acardote/personal-assistant-ultra/issues/8). They are recommendations — the schema permits any `kind` value.

## Provenance invariant

`source_uri` must dereference at write time. The compression pipeline (child [#3](https://github.com/acardote/personal-assistant-ultra/issues/3)) is responsible for verifying this. Periodic prune (child [#8](https://github.com/acardote/personal-assistant-ultra/issues/8)) should detect and surface dangling pointers — though by ADR-0001's storage decision, the layer-1 archive is local-only, so dangling pointers across machines are expected and tolerated; only same-machine dangling pointers are a bug.

## Why `source_kind` and `kind` are not strict enums

Falsifier F2 on issue #2 warns that a frontmatter real sources cannot honestly populate becomes "a fiction that erodes the very provenance and queryability layer 2 exists to provide." Strict enums force adapters to either lie (`kind: other`) or to cause schema migrations every time a new source is added. By keeping these as open strings with documented recommendations, adapters can introduce new values truthfully; retrieval and expiry can fall back to defaults for unrecognized values.
