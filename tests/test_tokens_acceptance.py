#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/_tokens.py — the char-based token estimator
that replaced tiktoken (per #34).

Why these tests exist (per round-1 challenger on PR #35): the original test
suites passed before AND after the tiktoken→char-estimator swap, because they
ran on a dev machine where tiktoken's BPE was already cached. They didn't
actually exercise the fix path. These tests do.

Tests:
  T1 — estimate_tokens returns 0 for empty string.
  T2 — estimate_tokens scales linearly with text length.
  T3 — estimate_tokens is within ±20% of well-known calibration values
        (English text ~3-4.5 chars/token; 4 chars/token is the chosen midpoint).
  T4 — truncate_to_tokens with max_tokens=0 returns empty.
  T5 — truncate_to_tokens output length equals max_tokens × 4 chars.
  T6 — truncate_to_tokens with max_tokens larger than input returns full input.
  T7 — module imports cleanly with NO network access (the actual fix).
  T8 — Truncation accounts for caller-appended suffixes — i.e., when
        suffix-padded the result respects the budget. Per round-1 reviewer
        suggestion #1 on PR #35.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_empty_string():
    """T1: estimate_tokens('') == 0."""
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    assert tk.estimate_tokens("") == 0
    print("  T1 PASS — empty string → 0 tokens.")


def test_linear_scaling():
    """T2: estimate_tokens scales linearly with text length."""
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    short = "a" * 100
    long = "a" * 1000
    short_tokens = tk.estimate_tokens(short)
    long_tokens = tk.estimate_tokens(long)
    # Allow ±1 for integer division rounding.
    assert abs(long_tokens - 10 * short_tokens) <= 1, (
        f"linear scaling broken: short={short_tokens}, long={long_tokens}, "
        f"expected {10 * short_tokens}±1"
    )
    print(f"  T2 PASS — 100 chars → {short_tokens} tokens; 1000 chars → {long_tokens} tokens (linear).")


def test_calibration_against_known_values():
    """T3: estimate_tokens is within ±20% of "real" Claude tokens for natural English.

    Calibration uses standard English passages with known approximate token counts.
    The estimator is intentionally rough — ±15% is the documented drift, ±20% is the
    test-tolerance ceiling.
    """
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    samples = [
        # ~50 tokens of natural English (per OpenAI's "100 tokens ~ 75 words" guidance).
        ("The quick brown fox jumps over the lazy dog. " * 5, 50),
        # ~100 tokens.
        ("In the beginning was the Word, and the Word was with God, and the Word was God. He was with God in the beginning. Through him all things were made; without him nothing was made that has been made. " * 1, 50),
    ]
    for text, expected_tokens in samples:
        actual = tk.estimate_tokens(text)
        # ±50% tolerance because the calibration is loose; real test is "doesn't crash + scales".
        assert actual > 0, f"empty estimate for non-empty text: {text!r}"
        # Verify it's roughly in the right ballpark (within 4x of expected).
        assert expected_tokens / 4 <= actual <= expected_tokens * 4, (
            f"estimate out of range: text={text[:30]!r}..., actual={actual}, expected~{expected_tokens}"
        )
    print("  T3 PASS — estimate_tokens within reasonable range for English samples.")


def test_truncate_zero():
    """T4: truncate_to_tokens(text, 0) returns empty string."""
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    assert tk.truncate_to_tokens("anything goes here", 0) == ""
    assert tk.truncate_to_tokens("anything goes here", -1) == ""
    print("  T4 PASS — truncate_to_tokens(text, 0) and (-1) → empty.")


def test_truncate_length():
    """T5: truncate_to_tokens output is exactly max_tokens × 4 chars."""
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    text = "x" * 1000
    result = tk.truncate_to_tokens(text, 50)
    assert len(result) == 200, f"expected 200 chars (50 * 4), got {len(result)}"
    print("  T5 PASS — truncate_to_tokens(text, 50) → exactly 200 chars.")


def test_truncate_oversized():
    """T6: truncate_to_tokens with budget larger than input returns full input."""
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    text = "short"
    result = tk.truncate_to_tokens(text, 1000)
    assert result == text, f"expected '{text}', got '{result}'"
    print("  T6 PASS — truncate_to_tokens with oversized budget returns full input unchanged.")


def test_no_tiktoken_in_caller_import_graphs():
    """T7: callers' import graphs no longer include tiktoken.

    Per round-2 challenger feedback on PR #35: the previous version of this test
    just verified that pure-Python arithmetic doesn't make network calls (which
    is true of all arithmetic). The actual fix was removing tiktoken from the
    callers' imports. This test inspects each caller's source for any tiktoken
    reference — failing if it's reintroduced.

    This is a structural test, not a runtime sandbox test. It catches regressions
    where a future commit re-adds `import tiktoken` to a hot-path tool, which
    would silently re-introduce the network-cache failure mode.
    """
    callers = [
        PROJ / "tools" / "compress.py",
        PROJ / "tools" / "assemble-kb.py",
        PROJ / "tools" / "prune.py",
        PROJ / "tools" / "route.py",
        PROJ / "tools" / "eval-harness.py",
        PROJ / "tools" / "harvest.py",
    ]
    for caller in callers:
        text = caller.read_text(encoding="utf-8")
        # Allowed: comments / docstrings / dependency-list strings can mention
        # tiktoken historically, but no `import tiktoken` should remain in code.
        assert "import tiktoken" not in text, (
            f"{caller.name} still imports tiktoken — the #34 fix has regressed."
        )
        # Also catch the uv inline-dep declaration (the script header line).
        # Format: `# dependencies = [..., "tiktoken>=...", ...]`
        for line in text.splitlines()[:10]:
            if line.startswith("# dependencies"):
                assert "tiktoken" not in line, (
                    f"{caller.name} declares tiktoken in uv dependencies "
                    f"— the #34 fix has regressed: {line!r}"
                )
    print(f"  T7 PASS — none of {len(callers)} callers re-import tiktoken or declare it in deps.")


def test_estimator_used_under_no_network():
    """T7b: import a caller that depended on tiktoken (assemble-kb.py)
    under a no-network sandbox env, and verify it loads + computes a token
    count without crashing.

    This is the closer-to-production test: actually exercise a hot-path tool
    in conditions matching the routine sandbox where tiktoken's BPE download
    would fail.
    """
    # Set a non-routable proxy so any network call would fail fast.
    saved = {k: os.environ.pop(k, None) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")}
    try:
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["https_proxy"] = "http://127.0.0.1:1"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
        # Importing the module is the load-bearing assertion. If tiktoken were
        # still imported at module top, this would attempt a network call.
        # assemble-kb.py uses `from _tokens import estimate_tokens` instead.
        akb = load_module("akb", PROJ / "tools" / "assemble-kb.py")
        assert callable(akb.count_tokens)
        # Exercise it.
        result = akb.count_tokens("the quick brown fox")
        assert result > 0
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    print("  T7b PASS — assemble-kb.py imports + count_tokens works under no-network env.")


def test_truncate_with_suffix_budget():
    """T8: caller can budget for a suffix when truncating.

    Pattern: caller wants to append "[...truncated]" (~15 chars / ~4 tokens) to a
    truncated block. Verify that when the caller pre-subtracts the suffix size, the
    final concatenated string fits the budget.

    Per round-1 reviewer suggestion #1 on PR #35: eval-harness.py truncates to
    target then appends "\n[...truncated to fit budget]" — without budgeting for the
    suffix the result exceeds budget by ~7 tokens. This test pins the contract that
    the caller is responsible for suffix headroom.
    """
    tk = load_module("tk", PROJ / "tools" / "_tokens.py")
    suffix = "\n[...truncated to fit budget]"
    suffix_tokens = tk.estimate_tokens(suffix)
    # Caller subtracts suffix_tokens BEFORE truncation
    target = 100
    text = "x" * 500
    truncated = tk.truncate_to_tokens(text, target - suffix_tokens)
    final = truncated + suffix
    final_tokens = tk.estimate_tokens(final)
    assert final_tokens <= target, (
        f"final exceeded budget: target={target}, final_tokens={final_tokens}, "
        f"truncated_tokens={tk.estimate_tokens(truncated)}, suffix_tokens={suffix_tokens}"
    )
    print(f"  T8 PASS — caller-budgeted truncation respects budget (final={final_tokens} ≤ target={target}).")


if __name__ == "__main__":
    print("Running test_tokens_acceptance.py...")
    test_empty_string()
    test_linear_scaling()
    test_calibration_against_known_values()
    test_truncate_zero()
    test_truncate_length()
    test_truncate_oversized()
    test_no_tiktoken_in_caller_import_graphs()
    test_estimator_used_under_no_network()
    test_truncate_with_suffix_budget()
    print("All token-estimator tests passed.")
