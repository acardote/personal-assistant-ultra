#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/kb-process-tui.py slice 4 of #183 / #191 —
Scope-prompt default-to-predicted + scope_source TSV column.

Tests:
  T1  — scope_default_for_prompt: prediction with non-empty scope wins
  T2  — scope_default_for_prompt: empty predicted scope falls back to last_scope
  T3  — scope_default_for_prompt: whitespace-only predicted scope falls back
  T4  — scope_default_for_prompt: errored prediction falls back
  T5  — scope_default_for_prompt: None prediction falls back
  T6  — scope_default_for_prompt: predicted scope with empty last_scope
  T7  — scope_default_for_prompt: both empty → ("", default-last)
  T8  — log_accuracy_row emits 13-col rows with scope_source column
  T9  — log_accuracy_row defaults scope_source to "n/a" when not supplied
  T10 — print_accuracy_summary parses old 12-col TSV without crash
  T11 — print_accuracy_summary parses new 13-col TSV with all source values
  T12 — print_accuracy_summary buckets sum to headline scope_agreed total
        (the key fix from pr-reviewer S1 + pr-challenger B1 on #192)
  T13 — print_accuracy_summary tolerates ANY short row width (not just 12-col)
        — N2 fix on #192
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def _load_tui_module():
    """The TUI script has a hyphen in its filename so `import` doesn't work
    directly. Load it via the importlib machinery instead."""
    spec = spec_from_file_location(
        "kb_process_tui",
        PROJ / "tools" / "kb-process-tui.py",
    )
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tui = _load_tui_module()


# ----- T1..T7: scope_default_for_prompt -----


def test_scope_default_predicted_wins():
    d, s = tui.scope_default_for_prompt({"scope": "Atlas", "action": "a"}, "Vera")
    assert d == "Atlas", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_PREDICTED, s


def test_scope_default_empty_predicted_falls_back():
    d, s = tui.scope_default_for_prompt({"scope": "", "action": "a"}, "Vera")
    assert d == "Vera", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_LAST, s


def test_scope_default_whitespace_predicted_falls_back():
    d, s = tui.scope_default_for_prompt({"scope": "   ", "action": "a"}, "Vera")
    assert d == "Vera", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_LAST, s


def test_scope_default_errored_prediction_falls_back():
    d, s = tui.scope_default_for_prompt(
        {"error": "parse_markers_missing", "scope": "Atlas"}, "Vera"
    )
    assert d == "Vera", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_LAST, s


def test_scope_default_none_prediction_falls_back():
    d, s = tui.scope_default_for_prompt(None, "Vera")
    assert d == "Vera", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_LAST, s


def test_scope_default_predicted_with_empty_last_scope():
    d, s = tui.scope_default_for_prompt({"scope": "Atlas"}, "")
    assert d == "Atlas", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_PREDICTED, s


def test_scope_default_both_empty():
    d, s = tui.scope_default_for_prompt(None, "")
    assert d == "", d
    assert s == tui.SCOPE_SOURCE_DEFAULT_LAST, s


# ----- T8..T9: log_accuracy_row -----


def test_log_accuracy_row_emits_13_cols_with_source():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write(tui.ACCURACY_TSV_HEADER)
    pred = {"action": "a", "scope": "Atlas", "confidence": "high", "reasoning": "looks good"}
    tui.log_accuracy_row(
        path,
        art_id="art-001",
        prediction=pred,
        user_action="a",
        user_scope="Atlas",
        candidate_kind="decision",
        notes="happy path",
        scope_source=tui.SCOPE_SOURCE_DEFAULT_PREDICTED,
    )
    last = path.read_text(encoding="utf-8").rstrip("\n").split("\n")[-1]
    parts = last.split("\t")
    assert len(parts) == 13, f"expected 13 cols, got {len(parts)}: {parts}"
    assert parts[0] == "art-001"
    assert parts[1] == "decision"
    assert parts[9] == "true", f"action_agreed wrong: {parts[9]}"
    assert parts[10] == "true", f"scope_agreed wrong: {parts[10]}"
    assert parts[12] == tui.SCOPE_SOURCE_DEFAULT_PREDICTED, f"scope_source wrong: {parts[12]}"
    os.unlink(path)


def test_log_accuracy_row_default_source_is_na():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write(tui.ACCURACY_TSV_HEADER)
    tui.log_accuracy_row(
        path,
        art_id="art-002",
        prediction=None,
        user_action="s",
        user_scope="",
        candidate_kind="person",
        notes="skipped",
    )
    last = path.read_text(encoding="utf-8").rstrip("\n").split("\n")[-1]
    parts = last.split("\t")
    assert len(parts) == 13
    assert parts[12] == tui.SCOPE_SOURCE_NA, f"default scope_source not n/a: {parts[12]}"
    os.unlink(path)


# ----- T10..T13: print_accuracy_summary backward compat + bucket math -----


_OLD_TSV_HEADER = (
    "art_id\tcandidate_kind\tpred_mode\tpredicted_action\tpredicted_scope\t"
    "predicted_confidence\tpredicted_reasoning\tuser_action\tuser_scope\t"
    "action_agreed\tscope_agreed\tnotes\n"
)


def _summary_capture(path: Path) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        tui.print_accuracy_summary(path)
    return buf.getvalue()


def _strip_ansi(s: str) -> str:
    # remove ANSI escapes so assertions can check plain substrings
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_summary_parses_old_12_col_tsv():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write("# old TSV from slice 2\n")
        f.write(_OLD_TSV_HEADER)
        # Decision agreed, no scope_source col → padded to 'unknown'
        f.write("art-001\tdecision\tpre-flight\ta\tAtlas\thigh\told\ta\tAtlas\ttrue\ttrue\tlegacy\n")
        f.write("art-002\tperson\tnone\t\t\t\t\ta\t\tfalse\tn/a\told person\n")
    out = _strip_ansi(_summary_capture(path))
    assert "Accuracy summary" in out
    assert "Scope-agreement" in out
    # The legacy row should be reported in the new legacy-bucket line
    assert "legacy pre-slice-4" in out
    os.unlink(path)


def test_summary_parses_new_13_col_tsv():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write("# new TSV from slice 4\n")
        f.write(tui.ACCURACY_TSV_HEADER)
        # All four scope_source values represented, plus an n/a person row
        f.write(
            f"art-101\tdecision\tpre-flight\ta\tAtlas\thigh\tr\ta\tAtlas\ttrue\ttrue\tnote\t{tui.SCOPE_SOURCE_DEFAULT_PREDICTED}\n"
        )
        f.write(
            f"art-102\tdecision\tpre-flight\ta\tVera\thigh\tr\ta\tVera\ttrue\ttrue\tnote\t{tui.SCOPE_SOURCE_TYPED}\n"
        )
        f.write(
            f"art-103\tdecision\tpre-flight\ta\tWrong\thigh\tr\ta\tRight\ttrue\tfalse\tnote\t{tui.SCOPE_SOURCE_TYPED}\n"
        )
        f.write(
            f"art-104\tdecision\tnone\t\t\t\t\ta\tInherited\ttrue\tfalse\tno-predict\t{tui.SCOPE_SOURCE_DEFAULT_LAST}\n"
        )
        f.write(
            f"art-105\tperson\tnone\t\t\t\t\ta\t\tfalse\tn/a\tperson row\t{tui.SCOPE_SOURCE_NA}\n"
        )
    out = _strip_ansi(_summary_capture(path))
    assert "Scope-agreement (decisions only): 2/4" in out
    assert "accept-by-default-predicted: 1" in out
    assert "of which deliberate (typed / inherited-from-memo): 1" in out
    # 'accept-by-default-last' line should always print (even when 0)
    assert "accept-by-default-last" in out
    # No legacy bucket line — no unknown rows
    assert "legacy pre-slice-4" not in out
    os.unlink(path)


def test_summary_buckets_sum_to_headline():
    """pr-reviewer S1 / pr-challenger B1 on #192: every row that contributes
    to the headline `scope_agreed` numerator MUST appear in exactly one
    breakdown bucket. Mix three sources of agreement + a legacy row."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write(tui.ACCURACY_TSV_HEADER)
        # 1 typed + 1 default-predicted + 1 default-last → headline 3/3
        f.write(
            f"art-201\tdecision\tpre-flight\ta\tAtlas\thigh\tr\ta\tAtlas\ttrue\ttrue\tnote\t{tui.SCOPE_SOURCE_TYPED}\n"
        )
        f.write(
            f"art-202\tdecision\tpre-flight\ta\tVera\thigh\tr\ta\tVera\ttrue\ttrue\tnote\t{tui.SCOPE_SOURCE_DEFAULT_PREDICTED}\n"
        )
        f.write(
            f"art-203\tdecision\tnone\t\t\t\t\ta\tInherited\ttrue\ttrue\tno-predict\t{tui.SCOPE_SOURCE_DEFAULT_LAST}\n"
        )
    out = _strip_ansi(_summary_capture(path))
    headline_match = re.search(r"Scope-agreement \(decisions only\): (\d+)/(\d+)", out)
    assert headline_match, out
    headline_num = int(headline_match.group(1))
    deliberate_match = re.search(r"of which deliberate.*: (\d+)", out)
    pred_match = re.search(r"of which accept-by-default-predicted: (\d+)", out)
    last_match = re.search(r"of which accept-by-default-last.*: (\d+)", out)
    legacy_match = re.search(r"of which legacy pre-slice-4.*: (\d+)", out)
    deliberate = int(deliberate_match.group(1))
    pred = int(pred_match.group(1))
    last = int(last_match.group(1))
    legacy = int(legacy_match.group(1)) if legacy_match else 0
    bucket_sum = deliberate + pred + last + legacy
    assert bucket_sum == headline_num, (
        f"buckets must sum to headline: got {deliberate}+{pred}+{last}+{legacy}={bucket_sum} vs {headline_num}\n{out}"
    )
    os.unlink(path)


def test_summary_tolerates_any_short_row_width():
    """pr-challenger N2 on #192: padding logic should tolerate any short
    width, not just exactly len==12. Use a row with fewer columns to
    ensure the loop pads up rather than IndexError-ing on r[12]."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tsv") as f:
        path = Path(f.name)
        f.write(tui.ACCURACY_TSV_HEADER)
        # 12-col row (no scope_source) → pad to 13 with 'unknown'
        f.write("art-301\tdecision\tpre-flight\ta\tAtlas\thigh\tr\ta\tAtlas\ttrue\ttrue\tnote\n")
    # Should not raise even if a future column is added later.
    _summary_capture(path)
    os.unlink(path)


if __name__ == "__main__":
    test_scope_default_predicted_wins()
    test_scope_default_empty_predicted_falls_back()
    test_scope_default_whitespace_predicted_falls_back()
    test_scope_default_errored_prediction_falls_back()
    test_scope_default_none_prediction_falls_back()
    test_scope_default_predicted_with_empty_last_scope()
    test_scope_default_both_empty()
    test_log_accuracy_row_emits_13_cols_with_source()
    test_log_accuracy_row_default_source_is_na()
    test_summary_parses_old_12_col_tsv()
    test_summary_parses_new_13_col_tsv()
    test_summary_buckets_sum_to_headline()
    test_summary_tolerates_any_short_row_width()
    print("All kb-process-tui slice-4 (#192 / #191) tests passed.")
