"""Shared token-counting utilities for the personal-assistant tools.

This is a deliberately rough character-based estimator. We use it instead of
tiktoken for two reasons (per #34):

1. **tiktoken is OpenAI's tokenizer**, not Claude's. The original codebase
   used `cl100k_base` as a "reasonable proxy for Claude tokenization (within
   ~5% on natural text)" — i.e., the precision was never the point. All use
   sites are soft-warns or budget assertions where a few percent of drift is
   irrelevant to correctness.

2. **tiktoken needs network access** at first call to download its BPE files
   from `openaipublic.blob.core.windows.net`. Claude Code routine sandboxes
   block that URL (verified 2026-05-05: the production routine's first
   harvest run logged 403 errors and ran in degraded mode). Removing
   tiktoken removes this systemic failure mode.

The trade-off: char-based estimates drift ~10-15% from actual Claude tokens
on natural English (vs. tiktoken's ~5%). For all current callers — soft-warn
on memory-object body size, KB-budget assertion, eval-harness context
sizing — that drift is well within the budget headroom.

If a future caller needs precise Claude-specific token counts, use the
Anthropic API's `/v1/messages/count_tokens` endpoint instead — accurate but
costs an API call per check.

CHARS_PER_TOKEN was set to 4 because OpenAI's published guidance ("100
tokens ~= 75 words ~= 3.75 chars/token") and Anthropic's anthropic-tokenizer
heuristic both cluster around that value for English. Adjust the constant
if the corpus skews heavily toward code, non-English, or otherwise unusual.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough character-based token estimator. ±10-15% of actual Claude tokens
    on natural English text. Use only for soft-warn / budget-assertion paths
    where precise counts aren't required."""
    return len(text) // CHARS_PER_TOKEN


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate `text` so the result has approximately `max_tokens` tokens.
    Same precision caveat as `estimate_tokens` — output can be ±10-15% off
    in actual token count. Sufficient for budgeted-context assembly where
    a slight under/over-shoot is acceptable; not sufficient for hard
    enforcement at API limits."""
    if max_tokens <= 0:
        return ""
    return text[: max_tokens * CHARS_PER_TOKEN]
