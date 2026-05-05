---
name: personal-assistant
description: Personal assistant grounded in the layer-3 knowledge base — people, org, decisions, glossary — and the layer-2 memory objects compressed from the user's raw artifacts (Slack/Gmail/Granola/Meet/transcripts/docs). Use when the user wants help with their working life that should be informed by their own accumulated context, not a fresh-mint generic answer.
---

# personal-assistant

A Claude Code skill implementing the three-layer memory architecture defined in [issue #1](https://github.com/acardote/personal-assistant-ultra/issues/1) (Sei's "Your AI Assistant Has Amnesia" model adapted to a personal context).

## Method vs. content (per #12)

This skill operates against **two repos**:

- **Method repo** (`acardote/personal-assistant-ultra`): code, schemas, prompts, glossary, ADRs. The skill itself lives here. Path: wherever you cloned it.
- **Content vault** (`getnexar/acardote-pa-vault`): your real `memory/`, `kb/{people,org,decisions}.md`, `.harvest/` state, `raw/`. Per-checkout location resolved from `<method-root>/.assistant.local.json`'s `paths.content_root`.

Tools resolve paths via `tools/_config.py`. When `.assistant.local.json` is missing or malformed, tools emit a LOUD stderr warning and fall back to the method root (OK for fixtures/tests; NOT OK for real harvest). Setup: copy `.assistant.local.json.example` → `.assistant.local.json` and edit `paths.content_root` to point at your vault checkout.

## What "always in context" means here

The layer-3 knowledge base is the assistant's ground truth on the user, the user's org, the user's durable decisions, and project-specific terms. It must be loaded on every invocation. Three layers:

1. **Layer 1 — `<content_root>/raw/`** (local-only by policy; PII): unmodified raw artifacts. NOT loaded directly into context.
2. **Layer 2 — `<content_root>/memory/`** (vault-tracked): editorially compressed memory objects. Loaded selectively per query (see retrieval in `tools/route.py`; ranked-retrieval landing in [#10](https://github.com/acardote/personal-assistant-ultra/issues/10)).
3. **Layer 3 — split**: `<method_root>/kb/glossary.md` (canonical project terms) + `<content_root>/kb/{people,org,decisions}.md` (your content). Combined assembly via `tools/assemble-kb.py`. Token budget ≤4K total.

## Activation contract — load layer 3 first

When this skill is invoked, your **first action** is to load layer-3 by running:

```
tools/assemble-kb.py
```

(executed from the project root). The output is the user's ground truth. Treat its content as authoritative for facts about the user, the user's people, the user's org, the user's durable decisions, and project-specific terms. Quote KB entries by their `## <heading>` when citing.

Do not proceed with the user's request until layer 3 is loaded into your working context. If `tools/assemble-kb.py` fails or produces empty output, surface the failure to the user and stop — operating without layer 3 violates the "always-in-context" invariant on issue #4.

## Editorial discipline

- **Never invent KB entries.** If the user asks about something that isn't in the KB and isn't derivable from layer-2 memory objects (when retrieval lands), say so explicitly. Hallucinated grounding is the failure mode that poisons every downstream consumer (see falsifier F2 on issue #3 / #4).
- **Cite the KB.** When a claim about the user, their org, or a durable decision rests on a KB entry, mention which file/heading it came from. This makes drift detectable.
- **Honor the Bruno Method discipline** documented in `kb/decisions.md`. Don't propose closing claims without reconciliation against landed state; don't accept work without explicit falsifiers.

## How to extend the KB

- Add or revise entries by editing `kb/people.md`, `kb/org.md`, `kb/decisions.md`, `kb/glossary.md`.
- Each entry should carry `Last verified` and `Expires` metadata. The expiry-rules child ([#8](https://github.com/acardote/personal-assistant-ultra/issues/8)) will operationalize automatic decay.
- After editing, run `tools/assemble-kb.py --check` to verify the 4K token budget is still respected.

## How to add memory objects

The compression pipeline lives at `tools/compress.py` (see [issue #3](https://github.com/acardote/personal-assistant-ultra/issues/3)). Run:

```
tools/compress.py raw/<source-kind>/<artifact>.md --kind <strategy|weekly|...>
```

to ingest a raw artifact and land its memory object under `memory/`. Idempotent harvesters for Slack/Gmail/Granola/Meet/file-drop are tracked in [#5](https://github.com/acardote/personal-assistant-ultra/issues/5) / [#6](https://github.com/acardote/personal-assistant-ultra/issues/6).

## Open extensions

- Multi-agent router with adversarial critic: [#7](https://github.com/acardote/personal-assistant-ultra/issues/7).
- Per-document-type expiry rules: [#8](https://github.com/acardote/personal-assistant-ultra/issues/8).
- Evaluation harness: [#9](https://github.com/acardote/personal-assistant-ultra/issues/9).
