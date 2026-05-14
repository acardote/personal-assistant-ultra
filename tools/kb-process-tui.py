#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""kb-process-tui — fast keystroke-driven CLI for walking kb-scan candidates.

Slice 1 of #183 / closer of #184. Replaces the chat-based candidate-review
flow (where each entry took ~30-60s of LLM proposal + user response + tool
work) with a single-keystroke loop targeting ~3-5s per candidate.

Design choices (per pr-challenger falsifiers on #184):

- **No curses**: raw-mode stdin keystrokes via `tty`/`termios` + plain
  stdout. Avoids alt-screen / screen-corruption failure modes (Falsifiers
  5 + 6 of #184). $EDITOR for amend is a clean fork+exec — no suspend
  dance.
- **No `--default-scope` flag**: Falsifier 7 explicitly named the silent-
  wrong-scope footgun. v1 prompts for Scope on every `a`, with the
  last-used scope as the default (Enter accepts). Same keystroke count
  as a `--default-scope`-flag world for repeated-scope walks, but an
  explicit confirmation that survives Atlas-vs-Vera mistakes.
- **Per-action ops, not batch-staged**: each `a` / `r` immediately shells
  out to `tools/kb-process.py apply` / `reject`, then `c` shells out to
  `tools/live-commit-push.sh` for the actual transport. Crash safety is
  the OS filesystem's job (each kb-process subcommand is already
  atomic per its own F5 closer); the TUI's only ephemeral state is
  "what was last-used scope" + "next candidate to show", which is
  trivially recovered by listing `.unprocessed/` on restart.

What this tool is NOT:
- Not a curses TUI (slice 1 of #183 was originally framed that way; the
  simpler shape ships faster and is more robust).
- Not a catalog-rule-aware classifier (parent #183 slices 1 + 4 cover
  pre-marking; v1 is pure manual review).
- Not a batch-stage-then-commit loop (the kb-process subcommands already
  handle individual atomicity; batching at the TUI layer would just
  introduce new failure modes for no gain).

Invocation:

    tools/kb-process-tui.py                  # walks .unprocessed/ in lexical order
    tools/kb-process-tui.py --commit-at-end  # skips `c` prompt; runs live-commit at quit
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
import tty
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402


# --------------------------------------------------------------------------
# Memo helpers — inlined here rather than imported from `kb-process.py`
# because Python can't import a hyphenated module name. Kept in sync with
# the kb-process.py originals (same semantics; if those change, sync here).
# --------------------------------------------------------------------------


def memo_dir(content_root: Path, bucket: str) -> Path:
    return content_root / "artefacts" / "memo" / f".{bucket}"


def list_memos(content_root: Path, bucket: str = "unprocessed") -> list[Path]:
    """Sorted list of `art-*.md` memos in the bucket dir. Matching kb-process.py:93
    pattern (per pr-reviewer S1.1 on #185 — `iterdir` would accept stray *.md files
    like README.md that don't exist on the kb-process.py apply path)."""
    d = memo_dir(content_root, bucket)
    if not d.is_dir():
        return []
    return sorted(d.glob("art-*.md"))


def parse_memo_frontmatter(memo_path: Path) -> tuple[dict, str]:
    """Split on `---` delimiters and parse YAML frontmatter. Returns (fm, body).

    Matches kb-process.py:96 semantics (per pr-reviewer S1.2 + S1.3 on #185):
    - Closing fence accepts `\\n---` followed by any line ending or EOF.
    - Body has leading newlines stripped (lstrip("\\n")).
    """
    text = memo_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{memo_path.name}: no frontmatter delimiter")
    rest = text[4:]
    end = rest.find("\n---", 0)
    if end < 0:
        raise ValueError(f"{memo_path.name}: unterminated frontmatter")
    fm_text = rest[:end]
    # Skip past `\n---` plus the line-ending (newline or EOF).
    after_fence = end + 4
    if after_fence < len(rest) and rest[after_fence] == "\n":
        after_fence += 1
    body = rest[after_fence:].lstrip("\n")
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError(f"{memo_path.name}: frontmatter is not a mapping")
    return fm, body


_KIND_TITLE_RE = re.compile(r"Candidate\s+(person|org|decision|glossary)\s*:", re.IGNORECASE)


def detect_memo_kind(fm: dict) -> str:
    """Return person / org / decision / glossary parsed from the title prefix
    'Candidate <kind>: <referent>'. Raises ValueError on no-match — same shape
    as kb-process.py's version (per pr-challenger S2 on #185, drift-prevention).
    Caller is responsible for try/except + rendering '?' in the UI."""
    title = str(fm.get("title", ""))
    m = _KIND_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"can't detect memo kind from title {title!r}")
    return m.group(1).lower()


def is_drift_candidate(fm: dict) -> bool:
    return fm.get("drift_candidate") is True


# ANSI codes — minimal set, no ncurses.
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
GREY = "\x1b[90m"
CLEAR = "\x1b[2J\x1b[H"


# --------------------------------------------------------------------------
# Single-keystroke input — tty.setraw, then cooked-mode read for full-line
# prompts. We never enter alt-screen; on exception we always restore the
# original termios state (Falsifier 5 / #184 mitigation).
# --------------------------------------------------------------------------


def getch() -> str:
    """Read a single raw keystroke from stdin. Restores termios on any exit.
    Returns '' on EOF (pr-challenger S5 on #185 — closed stdin would otherwise
    infinite-loop). Caller treats '' as quit."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def prompt(label: str, default: str = "") -> str:
    """Cooked-mode line input. The raw-mode getch loop yields to readline-
    shaped editing here so the user can backspace / arrow-edit normally."""
    if default:
        sys.stdout.write(f"{label} [{default}]: ")
    else:
        sys.stdout.write(f"{label}: ")
    sys.stdout.flush()
    try:
        line = input("")
    except EOFError:
        return default
    line = line.rstrip("\n").strip()
    return line if line else default


# --------------------------------------------------------------------------
# Memo rendering for the per-candidate screen.
# --------------------------------------------------------------------------


def render_candidate(
    memo_path: Path,
    fm: dict,
    body: str,
    idx: int,
    total: int,
    prediction: dict | None = None,
) -> None:
    """Clear screen and render one candidate. If `prediction` is non-None, render
    the **Recommendation** block above the action prompt (slice 2 of #183 / #187)."""
    sys.stdout.write(CLEAR)

    art_id = memo_path.stem
    title = fm.get("title", "(no title)")
    try:
        kind = detect_memo_kind(fm)
    except ValueError:
        kind = "?"
    drift = is_drift_candidate(fm)
    drift_tag = f"{RED}[DRIFT]{RESET} " if drift else ""

    pb = fm.get("produced_by") or {}
    sources = pb.get("sources_cited") or []
    src_str = "\n".join(f"    - {s}" for s in sources) if sources else "    (none)"

    sys.stdout.write(
        f"{BOLD}{CYAN}[{idx}/{total}]{RESET} {drift_tag}{BOLD}{title}{RESET}\n"
        f"{GREY}  id: {art_id}{RESET}\n"
        f"{GREY}  kind: {kind}{RESET}\n"
        f"{GREY}  sources:\n{src_str}{RESET}\n"
        f"\n"
    )

    # Strip the diff section's leading boilerplate ("**Source memory objects**" etc.)
    # so the user sees the meaningful body fast.
    sys.stdout.write(f"{body.strip()}\n")

    if prediction is not None:
        # Color-coded by confidence.
        conf = (prediction.get("confidence") or "?").lower()
        conf_color = {"high": GREEN, "medium": YELLOW, "low": RED}.get(conf, GREY)
        action_label = {
            "a": "approve",
            "r": "reject",
            "m": "amend",
            "?": "unknown",
        }.get(prediction.get("action", "?"), "unknown")
        scope_line = f"  scope: {prediction.get('scope', '')}" if prediction.get("scope") else ""
        reason = (prediction.get("reasoning") or "").strip()
        late_tag = (
            f"  {YELLOW}[late-arrival, no pre-flight prediction available]{RESET}"
            if prediction.get("late_arrival")
            else ""
        )
        err_tag = (
            f"  {RED}[predict error: {prediction.get('error')}]{RESET}"
            if prediction.get("error")
            else ""
        )
        sys.stdout.write(
            f"\n{BOLD}{conf_color}━━━ Recommendation ({conf}) ━━━{RESET}\n"
            f"  {BOLD}action:{RESET} {action_label} ({prediction.get('action', '?')})\n"
            f"{scope_line}{('\n' if scope_line else '')}"
            f"  {DIM}reasoning:{RESET} {reason}{late_tag}{err_tag}\n"
        )

    sys.stdout.write(
        f"\n{DIM}─── {GREEN}(a){RESET}{DIM}pprove   {GREEN}(r){RESET}{DIM}eject   "
        f"{GREEN}(m){RESET}{DIM}amend   {GREEN}(s){RESET}{DIM}kip   "
        f"{GREEN}(c){RESET}{DIM}ommit-page   {GREEN}(q){RESET}{DIM}uit ───{RESET}\n"
    )
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Prediction infrastructure (slice 2 of #183 / #187).
#
# Folded-in falsifier mitigations:
# - F1 prompt-bias: PROMPT below is body-only. NO catalog/editorial-rules
#   framing — the prediction signal is "fresh-eyes claude reading the
#   candidate" not "claude predicting what catalog already encoded".
# - F2 agreed semantics: action_agreed and scope_agreed tracked SEPARATELY
#   in the TSV. Headline summary shows both.
# - F3 TSV corruption: reasoning sanitized via `_tsv_safe` (replaces tabs
#   with single space; literal `\n` for newlines; no other escaping —
#   stays plain readable in spreadsheets).
# --------------------------------------------------------------------------


PREDICT_PROMPT_TEMPLATE = """You are predicting an action a human reviewer will take on a kb-process candidate memo.

Candidate body (everything between the markers):
<<<BEGIN_CANDIDATE>>>
{body}
<<<END_CANDIDATE>>>

Output EXACTLY four lines in this format and nothing else. No preamble, no markdown:

ACTION: <a|r|m>
SCOPE: <if action is a or m AND kind is decision, the scope value (e.g. Vera, Atlas, Nexar); otherwise empty>
CONFIDENCE: <high|medium|low>
REASONING: <one short sentence — why this action>

Action meanings:
- a (approve): the candidate is a well-formed durable decision worth landing in kb/decisions.md as-is.
- r (reject): the candidate is a duplicate of an existing entry, ephemeral / tactical, not formalized (idea/brainstorm), or wrong-layer (e.g. team-cadence content that doesn't belong in always-loaded KB).
- m (amend): the candidate is mostly right but needs hand-editing before landing (incorrect scope, sensitive content to redact, wording cleanup).

Be conservative on confidence: only "high" when both action and scope are clearly determined by the body alone.
"""


def _tsv_safe(s: str) -> str:
    """Replace TSV-breaking characters (per F3 of #187). Tabs → single space;
    newlines → literal `\\n`. Other chars pass through."""
    if not s:
        return ""
    return s.replace("\t", " ").replace("\r\n", "\\n").replace("\n", "\\n")


def predict_one(memo_path: Path, body: str, timeout_s: int = 30) -> dict:
    """Shell out to `claude -p` for one candidate. Returns a dict with:
        action: 'a'|'r'|'m'|'?'
        scope: str
        confidence: 'high'|'medium'|'low'|'?'
        reasoning: str
        error: str or None  (set when claude -p failed)
    """
    prompt_text = PREDICT_PROMPT_TEMPLATE.format(body=body.strip())
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt_text],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return {"action": "?", "scope": "", "confidence": "?", "reasoning": "", "error": "claude_cli_not_found"}
    except subprocess.TimeoutExpired:
        return {"action": "?", "scope": "", "confidence": "?", "reasoning": "", "error": f"timeout_{timeout_s}s"}
    if proc.returncode != 0:
        err = proc.stderr.strip()[:160] or f"rc={proc.returncode}"
        return {"action": "?", "scope": "", "confidence": "?", "reasoning": "", "error": err}

    out = proc.stdout
    m_action = re.search(r"^\s*ACTION:\s*([armARM])", out, re.MULTILINE)
    m_scope = re.search(r"^\s*SCOPE:\s*(.+?)$", out, re.MULTILINE)
    m_conf = re.search(r"^\s*CONFIDENCE:\s*(high|medium|low)", out, re.IGNORECASE | re.MULTILINE)
    m_reason = re.search(r"^\s*REASONING:\s*(.+?)$", out, re.MULTILINE)

    if not m_action:
        return {
            "action": "?",
            "scope": "",
            "confidence": "?",
            "reasoning": out.strip()[:200],
            "error": "parse_action_missing",
        }
    return {
        "action": m_action.group(1).lower(),
        "scope": (m_scope.group(1).strip() if m_scope else "").strip(),
        "confidence": (m_conf.group(1).lower() if m_conf else "?"),
        "reasoning": (m_reason.group(1).strip() if m_reason else "").strip(),
        "error": None,
    }


def claude_cli_probe() -> tuple[bool, str]:
    """Pre-flight probe for `claude -p` availability (per C3 of #187). Returns
    (ok, version_or_error)."""
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "claude_cli_not_found_on_PATH"
    except subprocess.TimeoutExpired:
        return False, "version_probe_timeout"
    if proc.returncode != 0:
        return False, f"version_probe_rc={proc.returncode}: {proc.stderr.strip()[:120]}"
    return True, proc.stdout.strip() or proc.stderr.strip() or "version_unknown"


def pre_predict_all(memos: list[Path], max_workers: int = 5) -> dict[str, dict]:
    """Pre-flight predictions for all candidates. Returns {art_id: prediction_dict}.

    Falls back from parallel to serial if the parallel run produces >25% parse
    errors (suggests rate-limit or auth contention per concern in falsifiers).
    """
    bodies: dict[str, tuple[Path, str]] = {}
    for memo_path in memos:
        try:
            _, body = parse_memo_frontmatter(memo_path)
        except Exception:
            continue
        bodies[memo_path.stem] = (memo_path, body)

    sys.stdout.write(f"{DIM}Pre-predicting {len(bodies)} candidates (max {max_workers} parallel)…{RESET}\n")
    sys.stdout.flush()

    predictions: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(predict_one, mp, body): art_id
            for art_id, (mp, body) in bodies.items()
        }
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            art_id = futures[fut]
            try:
                predictions[art_id] = fut.result()
            except Exception as exc:
                predictions[art_id] = {
                    "action": "?",
                    "scope": "",
                    "confidence": "?",
                    "reasoning": "",
                    "error": f"future_exc: {exc}",
                }
            done_count += 1
            if done_count % 5 == 0 or done_count == len(bodies):
                sys.stdout.write(f"{DIM}  predicted {done_count}/{len(bodies)}{RESET}\n")
                sys.stdout.flush()

    parse_errors = sum(
        1 for p in predictions.values() if p.get("error", "").startswith("parse_")
    )
    parse_rate = parse_errors / max(1, len(predictions))
    if parse_rate > 0.25 and max_workers > 1:
        sys.stdout.write(
            f"{YELLOW}Parse-error rate {parse_rate:.0%} (>25%) on parallel run — "
            f"retrying serially…{RESET}\n"
        )
        # Re-run only the parse-failed ones serially.
        for art_id, p in list(predictions.items()):
            if p.get("error", "").startswith("parse_"):
                mp, body = bodies[art_id]
                predictions[art_id] = predict_one(mp, body)

    return predictions


# --------------------------------------------------------------------------
# Accuracy log (TSV).
# --------------------------------------------------------------------------


ACCURACY_TSV_HEADER = (
    "art_id\tpredicted_action\tpredicted_scope\tpredicted_confidence\t"
    "predicted_reasoning\tuser_action\tuser_scope\taction_agreed\tscope_agreed\tnotes\n"
)


def accuracy_log_path(content_root: Path, run_ts: str) -> Path:
    return content_root / ".harvest" / f"kb-tui-accuracy-{run_ts}.tsv"


def init_accuracy_log(path: Path, claude_version: str, model_hint: str) -> None:
    """Write TSV with metadata header rows (commented) + the actual column header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# kb-tui accuracy log (slice 2 of #183 / #187)\n")
        f.write(f"# run_ts: {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        f.write(f"# claude_version: {claude_version}\n")
        f.write(f"# model_hint: {model_hint}\n")
        f.write(ACCURACY_TSV_HEADER)


def log_accuracy_row(
    path: Path,
    art_id: str,
    prediction: dict | None,
    user_action: str,
    user_scope: str,
    notes: str = "",
) -> None:
    pred = prediction or {}
    p_action = pred.get("action", "")
    p_scope = pred.get("scope", "")
    p_conf = pred.get("confidence", "")
    p_reason = _tsv_safe(pred.get("reasoning", ""))
    action_agreed = "true" if p_action and p_action == user_action else "false"
    scope_agreed = (
        "true"
        if (p_scope.strip().lower() == user_scope.strip().lower()) and (p_scope or user_scope)
        else "false"
    )
    notes_safe = _tsv_safe(notes)
    row = (
        f"{art_id}\t{p_action}\t{p_scope}\t{p_conf}\t{p_reason}\t"
        f"{user_action}\t{user_scope}\t{action_agreed}\t{scope_agreed}\t{notes_safe}\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(row)


def print_accuracy_summary(path: Path) -> None:
    """Read TSV; print per-action-class + per-confidence agreement rates.
    Suppress n<5 buckets (per C7 of #187 — statistically meaningless).
    Mark 5<=n<10 with explicit sample-size warning."""
    if not path.is_file():
        return
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or line.startswith("art_id\t"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 10:
                continue
            rows.append(parts)
    if not rows:
        return

    total = len(rows)
    action_agreed = sum(1 for r in rows if r[7] == "true")
    scope_agreed = sum(1 for r in rows if r[8] == "true")

    sys.stdout.write(
        f"\n{BOLD}━━━ Accuracy summary ({total} candidates) ━━━{RESET}\n"
        f"  Overall action-agreement: {action_agreed}/{total} "
        f"({100*action_agreed/total:.0f}%)\n"
        f"  Overall scope-agreement:  {scope_agreed}/{total} "
        f"({100*scope_agreed/total:.0f}%)\n"
    )

    def _bucket_stats(filter_fn, label: str) -> None:
        bucket = [r for r in rows if filter_fn(r)]
        n = len(bucket)
        if n < 5:
            sys.stdout.write(f"  {label}: n={n} (suppressed; insufficient sample)\n")
            return
        agreed = sum(1 for r in bucket if r[7] == "true")
        warn = f" {YELLOW}(n<10, take with salt){RESET}" if n < 10 else ""
        sys.stdout.write(f"  {label}: {agreed}/{n} ({100*agreed/n:.0f}%){warn}\n")

    sys.stdout.write(f"\n{DIM}By predicted action:{RESET}\n")
    for act, name in [("a", "approve"), ("r", "reject"), ("m", "amend")]:
        _bucket_stats(lambda r, a=act: r[1] == a, f"  predicted={act} ({name})")
    sys.stdout.write(f"\n{DIM}By confidence:{RESET}\n")
    for conf in ("high", "medium", "low"):
        _bucket_stats(lambda r, c=conf: r[3] == c, f"  confidence={conf}")
    sys.stdout.write(f"\n{DIM}TSV at: {path}{RESET}\n")


# --------------------------------------------------------------------------
# Action handlers — shell out to existing kb-process.py subcommands.
# Per-action atomicity is inherited from kb-process.py's own F5 closer.
# --------------------------------------------------------------------------


def inject_scope_into_memo(memo_path: Path, scope: str) -> bool:
    """Inject `- **Scope:** <scope>` into the memo's proposed diff block.

    kb-scan default bodies don't include Scope. Per the editorial rule
    (#133), every decision needs one. This injects it inline before the
    diff's blank line that separates frontmatter-style bullets from the
    body paragraph.

    Returns True on success, False if the diff block shape isn't matched
    OR if a Scope line is already present (idempotency short-circuit per
    pr-reviewer S2 on #185 — survives precondition relaxation in future
    edits that might otherwise double-inject).
    """
    if not scope:
        return False
    text = memo_path.read_text(encoding="utf-8")
    # Idempotency: if a Scope line is already present in the diff block,
    # treat as success without re-injecting. Survives the case where the
    # operator added Scope in $EDITOR (`m` path) AND then typed a scope
    # at the prompt (pr-reviewer S3 on #185 — double-injection avoidance).
    if re.search(r"^\+\s*-\s*\*\*Scope:\*\*", text, re.MULTILINE):
        return True
    # The diff block looks like:
    #   ```diff
    #   + ## <heading>
    #   + - **Date:** ...
    #   + - **Status:** decided
    #   + - **Last verified:** ...
    #   + - **Expires:** never
    #   + - **Source:** ...
    #   + <blank line>
    #   + <body paragraph>
    #   ```
    # We want to insert `+ - **Scope:** <scope>` after the Source line
    # and before the blank-line + body. The precondition that the next
    # line is `+ ` (a blank-marker line) is load-bearing for shape
    # detection — don't relax without re-checking the idempotency guard
    # above.
    lines = text.split("\n")
    out: list[str] = []
    injected = False
    for i, line in enumerate(lines):
        out.append(line)
        if (
            not injected
            and line.startswith("+ - **Source:**")
            and i + 1 < len(lines)
            and lines[i + 1].strip() == "+"
        ):
            out.append(f"+ - **Scope:** {scope}")
            injected = True
    if not injected:
        return False
    memo_path.write_text("\n".join(out), encoding="utf-8")
    return True


def amend_in_editor(memo_path: Path) -> bool:
    """Open $EDITOR on the memo. Returns True if user saved (content hash
    changed), False if cancelled. No curses suspend dance — we're not in
    a curses screen.

    Per pr-reviewer S5 + N5 on #185:
    - `$EDITOR` is `shlex.split`'d to handle `EDITOR="code --wait"` shapes.
    - Change detection uses SHA-256 of the file bytes, NOT mtime (mtime
      has 1-second resolution on most FSes — vim `:wq` within the same
      second after a single-char edit can leave mtime unchanged).
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        for candidate in ("nano", "vim", "vi"):
            if shutil.which(candidate):
                editor = candidate
                break
    if not editor:
        sys.stdout.write(f"{RED}No $EDITOR / $VISUAL set and no nano/vim/vi found.{RESET}\n")
        return False
    before = hashlib.sha256(memo_path.read_bytes()).digest()
    cmd = shlex.split(editor) + [str(memo_path)]
    rc = subprocess.run(cmd).returncode
    after = hashlib.sha256(memo_path.read_bytes()).digest()
    if rc != 0:
        sys.stdout.write(f"{YELLOW}Editor exited with rc={rc}; not applying.{RESET}\n")
        return False
    return after != before


def apply_memo(method_root: Path, art_id: str) -> tuple[int, str]:
    """Shell out to kb-process.py apply. Returns (rc, combined-stderr-stdout)."""
    cmd = [str(method_root / "tools" / "kb-process.py"), "apply", art_id]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def reject_memo(method_root: Path, art_id: str, reason: str) -> tuple[int, str]:
    cmd = [str(method_root / "tools" / "kb-process.py"), "reject", art_id, "--reason", reason]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def commit_page(method_root: Path, content_root: Path, message: str) -> tuple[int, str]:
    cmd = [str(method_root / "tools" / "live-commit-push.sh"), str(content_root), message]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# --------------------------------------------------------------------------
# Main loop.
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit-each",
        action="store_true",
        help="Commit + push after every approved/rejected candidate (slow but safest). "
        "Default: commit only on `c` or at quit.",
    )
    parser.add_argument(
        "--predict",
        action="store_true",
        help="Slice 2 of #183 / #187: pre-flight LLM predictions for each candidate via "
        "`claude -p`, render a Recommendation block before the action prompt, and log "
        "(predicted, actual) pairs to <content_root>/.harvest/kb-tui-accuracy-<RUN_TS>.tsv.",
    )
    args = parser.parse_args(argv[1:])

    cfg = load_config()
    method_root = cfg.method_root
    content_root = cfg.content_root

    unprocessed = memo_dir(content_root, "unprocessed")
    memos = list_memos(content_root, "unprocessed")
    total = len(memos)
    if total == 0:
        sys.stdout.write(f"{GREEN}No unprocessed candidates. Queue is clean.{RESET}\n")
        return 0

    # --------- Slice 2 (#187): predict-mode pre-flight ---------
    predictions: dict[str, dict] = {}
    accuracy_log: Path | None = None
    run_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    if args.predict:
        ok, version = claude_cli_probe()
        if not ok:
            sys.stdout.write(
                f"{RED}--predict requires `claude` on PATH and authenticated. "
                f"Probe failed: {version}.{RESET}\n"
                f"{DIM}Either install / re-auth claude, or run without --predict.{RESET}\n"
            )
            return 1
        sys.stdout.write(f"{DIM}claude version probe: {version}{RESET}\n")
        # Note: model is whatever `claude -p` defaults to. The TSV header records
        # the version above; the actual model id can be queried later via
        # `claude --version --json` if needed for reconciliation.
        accuracy_log = accuracy_log_path(content_root, run_ts)
        init_accuracy_log(accuracy_log, version, "claude -p (default model)")
        predictions = pre_predict_all(memos, max_workers=5)
        sys.stdout.write(
            f"{GREEN}Pre-flight predictions complete.{RESET} "
            f"{DIM}Accuracy log: {accuracy_log}{RESET}\n\n"
        )

    sys.stdout.write(
        f"{BOLD}kb-process-tui{RESET} — walking {total} unprocessed candidates "
        f"in {DIM}{unprocessed}{RESET}\n"
        f"{DIM}Vault: {content_root}{RESET}\n"
        f"{DIM}Method: {method_root}{RESET}\n"
        f"{DIM}Per-candidate keys: (a)pprove (r)eject (m)amend (s)kip (c)ommit-page (q)uit{RESET}\n"
        f"{DIM}Predict mode: {'ON' if args.predict else 'off'}{RESET}\n"
        f"\nPress any key to begin.\n"
    )
    getch()

    last_scope = ""  # remembers the most-recently-used Scope value for fast re-use
    actions_since_commit = 0  # how many a/r we've done since the last commit
    last_commit_rc = 0  # tracked for B3 — if non-zero, refuse further `c` until manual recovery
    idx = 0

    while idx < len(memos):
        memo_path = memos[idx]
        # Memo may have been moved to .rejected/ or .processed/ by a prior
        # action's shell-out. If so, skip it.
        if not memo_path.is_file():
            idx += 1
            continue
        try:
            fm, body = parse_memo_frontmatter(memo_path)
        except Exception as exc:
            sys.stdout.write(f"{RED}Couldn't parse {memo_path.name}: {exc}{RESET}\n")
            sys.stdout.write(f"{DIM}Press any key to skip.{RESET}\n")
            getch()
            idx += 1
            continue

        # Resolve prediction for this candidate (slice 2 / #187).
        this_prediction = None
        if args.predict:
            this_prediction = predictions.get(memo_path.stem)
            if this_prediction is None:
                # Late-arrival: candidate showed up after pre-flight (e.g., kb-scan
                # fired mid-walk). Per C6 of #187, mark explicitly and do an
                # on-demand prediction so the operator isn't flying blind.
                sys.stdout.write(f"{DIM}  on-demand predicting late-arrival {memo_path.stem}…{RESET}\n")
                sys.stdout.flush()
                this_prediction = predict_one(memo_path, body)
                this_prediction["late_arrival"] = True
                predictions[memo_path.stem] = this_prediction

        render_candidate(memo_path, fm, body, idx + 1, total, prediction=this_prediction)
        key = getch()
        if key == "":
            # EOF on stdin (pr-challenger S5 on #185) — closed pipe or terminal hangup.
            sys.stdout.write(f"\n{YELLOW}EOF on stdin. {actions_since_commit} actions uncommitted.{RESET}\n")
            return 130
        key = key.lower()

        art_id = memo_path.stem

        # Scope is editorial-required for decision-kind only (rule #133).
        # Person/org/glossary candidates don't need it; suppressing the prompt
        # there saves a keystroke per N1 on #185.
        try:
            this_kind = detect_memo_kind(fm)
        except ValueError:
            this_kind = ""
        wants_scope = this_kind == "decision"

        if key == "a":
            if wants_scope:
                scope = prompt("Scope", default=last_scope)
                if scope:
                    injected = inject_scope_into_memo(memo_path, scope)
                    if not injected:
                        # B1 on #185 — silent-Scope-drop on parse-miss. Surface explicitly.
                        sys.stdout.write(
                            f"{RED}✗ Couldn't inject Scope into memo (diff-block shape didn't match). "
                            f"Use `m` to edit manually, then apply.{RESET}\n"
                        )
                        sys.stdout.write(f"{DIM}Press any key.{RESET}\n")
                        getch()
                        continue  # don't advance — let user amend
                    last_scope = scope
                else:
                    sys.stdout.write(
                        f"{YELLOW}No Scope provided — applying decision without one "
                        f"(editorial rule #133 wants one; lint won't refuse but the entry will be substandard).{RESET}\n"
                    )
            rc, out = apply_memo(method_root, art_id)
            if rc == 0:
                sys.stdout.write(f"{GREEN}✓ applied{RESET}: {out}\n")
                actions_since_commit += 1
                if accuracy_log:
                    log_accuracy_row(accuracy_log, art_id, this_prediction, "a", scope or "")
                if args.commit_each:
                    crc, cout = commit_page(method_root, content_root, f"kb: decision (via TUI, {art_id})")
                    sys.stdout.write(f"{GREEN if crc == 0 else RED}commit rc={crc}: {cout[:200]}{RESET}\n")
                    last_commit_rc = crc
                    if crc == 0:
                        actions_since_commit = 0
            else:
                sys.stdout.write(f"{RED}✗ apply failed rc={rc}{RESET}:\n{out}\n")
                sys.stdout.write(f"{DIM}Press any key to continue (candidate stays in .unprocessed/).{RESET}\n")
                getch()
                continue  # don't advance idx — let user retry
            idx += 1

        elif key == "r":
            reason = prompt("Reject reason", default="user-rejected via TUI walk")
            rc, out = reject_memo(method_root, art_id, reason)
            if rc == 0:
                sys.stdout.write(f"{YELLOW}✓ rejected{RESET}: {out}\n")
                actions_since_commit += 1
                if accuracy_log:
                    log_accuracy_row(accuracy_log, art_id, this_prediction, "r", "", notes=reason)
                if args.commit_each:
                    crc, cout = commit_page(method_root, content_root, f"kb-process: reject {art_id} (via TUI)")
                    sys.stdout.write(f"{GREEN if crc == 0 else RED}commit rc={crc}: {cout[:200]}{RESET}\n")
                    last_commit_rc = crc
                    if crc == 0:
                        actions_since_commit = 0
            else:
                sys.stdout.write(f"{RED}✗ reject failed rc={rc}{RESET}:\n{out}\n")
                sys.stdout.write(f"{DIM}Press any key to continue.{RESET}\n")
                getch()
                continue
            idx += 1

        elif key == "m":
            changed = amend_in_editor(memo_path)
            if not changed:
                sys.stdout.write(f"{DIM}Memo unchanged — press any key to re-render.{RESET}\n")
                getch()
                continue  # re-render same candidate
            # After editing, the user may have added Scope themselves. Prompt anyway
            # in case they didn't (Scope is editorial-required for decisions).
            m_scope = ""
            if wants_scope:
                memo_text = memo_path.read_text(encoding="utf-8")
                if "**Scope:**" in memo_text:
                    # Operator already added Scope inline — don't double-prompt.
                    # Try to recover the value for the accuracy log.
                    m = re.search(r"\*\*Scope:\*\*\s*(.+?)$", memo_text, re.MULTILINE)
                    m_scope = m.group(1).strip() if m else ""
                else:
                    m_scope = prompt("Scope (or empty to apply without)", default=last_scope)
                    if m_scope:
                        injected = inject_scope_into_memo(memo_path, m_scope)
                        if injected:
                            last_scope = m_scope
                        else:
                            sys.stdout.write(
                                f"{YELLOW}Scope inject failed (diff-block shape mismatch); applying without.{RESET}\n"
                            )
            rc, out = apply_memo(method_root, art_id)
            if rc == 0:
                sys.stdout.write(f"{GREEN}✓ applied (after amend){RESET}: {out}\n")
                actions_since_commit += 1
                if accuracy_log:
                    log_accuracy_row(accuracy_log, art_id, this_prediction, "m", m_scope, notes="amended in $EDITOR")
            else:
                sys.stdout.write(f"{RED}✗ apply failed rc={rc}{RESET}:\n{out}\n")
                sys.stdout.write(f"{DIM}Press any key.{RESET}\n")
                getch()
                continue
            idx += 1

        elif key == "s":
            sys.stdout.write(f"{DIM}skipped (memo stays in .unprocessed/){RESET}\n")
            if accuracy_log:
                # Log skip so the prediction's accuracy isn't silently dropped from the
                # denominator. user_action="s" means "no decision yet"; action_agreed
                # will be false unless predicted action was also '?' (which it never is
                # from claude -p's output contract).
                log_accuracy_row(accuracy_log, art_id, this_prediction, "s", "", notes="skipped")
            idx += 1

        elif key == "c":
            if actions_since_commit == 0:
                sys.stdout.write(f"{DIM}Nothing to commit since last commit.{RESET}\n")
            elif last_commit_rc != 0:
                # B3 on #185 — refuse to re-attempt after a non-zero commit. Operator must resolve.
                sys.stdout.write(
                    f"{RED}Refusing to commit: last commit returned rc={last_commit_rc} "
                    f"(likely rebase conflict or lint refusal).{RESET}\n"
                    f"{DIM}Operator action required:\n"
                    f"  cd {content_root}\n"
                    f"  git status\n"
                    f"  git pull --rebase   # resolve any conflicts\n"
                    f"  git push            # land the resolved state\n"
                    f"Then run the TUI again to continue. last_commit_rc resets to 0 on next clean commit.{RESET}\n"
                )
            else:
                crc, cout = commit_page(
                    method_root,
                    content_root,
                    f"kb: TUI walk batch ({actions_since_commit} actions, ending at {memos[idx-1].stem if idx > 0 else art_id})",
                )
                sys.stdout.write(f"{GREEN if crc == 0 else RED}commit rc={crc}: {cout[:300]}{RESET}\n")
                last_commit_rc = crc
                if crc == 0:
                    actions_since_commit = 0
            sys.stdout.write(f"{DIM}Press any key to continue walking.{RESET}\n")
            getch()
            # Re-fetch memo list — apply / reject moved things around.
            # B2 on #185: reset idx to 0 against the shrunk list, otherwise the
            # operator silently skips the candidates that shifted down by N.
            memos = list_memos(content_root, "unprocessed")
            idx = 0

        elif key == "q":
            quit_rc = 0
            if actions_since_commit > 0:
                if last_commit_rc != 0:
                    sys.stdout.write(
                        f"{RED}{actions_since_commit} actions uncommitted; last commit rc={last_commit_rc} "
                        f"blocked further auto-commit.{RESET}\n"
                        f"{DIM}Resolve manually (cd {content_root}; git status; pull --rebase; push). "
                        f"Your applied/rejected actions are already in .processed/.rejected/ on disk.{RESET}\n"
                    )
                    quit_rc = 2  # pr-reviewer S4 on #185 — surface failure to wrapper / $?
                else:
                    yn = prompt(
                        f"You have {actions_since_commit} uncommitted actions. Commit before quit? (y/n)",
                        default="y",
                    )
                    if yn.lower().startswith("y"):
                        crc, cout = commit_page(
                            method_root,
                            content_root,
                            f"kb: TUI walk final batch ({actions_since_commit} actions)",
                        )
                        sys.stdout.write(f"{GREEN if crc == 0 else RED}commit rc={crc}: {cout[:300]}{RESET}\n")
                        if crc != 0:
                            quit_rc = 2
            sys.stdout.write(f"\n{BOLD}Goodbye.{RESET} Remaining unprocessed: {len(list_memos(content_root, 'unprocessed'))}\n")
            if accuracy_log:
                print_accuracy_summary(accuracy_log)
            return quit_rc

        elif key == "\x03":  # ctrl-c
            sys.stdout.write(f"\n{YELLOW}Interrupted. {actions_since_commit} actions uncommitted.{RESET}\n")
            return 130

        else:
            sys.stdout.write(f"{DIM}(unknown key: {key!r} — try a/r/m/s/c/q){RESET}\n")
            sys.stdout.write(f"{DIM}Press any key.{RESET}\n")
            getch()

    # End of memos — auto-commit if anything pending.
    final_rc = 0
    if actions_since_commit > 0:
        if last_commit_rc != 0:
            sys.stdout.write(
                f"\n{RED}End of queue, but last commit rc={last_commit_rc} blocked further auto-commit. "
                f"{actions_since_commit} actions are on-disk but not pushed.{RESET}\n"
                f"{DIM}Resolve manually: cd {content_root}; git status; git pull --rebase; git push.{RESET}\n"
            )
            final_rc = 2  # pr-reviewer S4 on #185 — non-zero exit on commit failure
        else:
            sys.stdout.write(
                f"\n{BOLD}End of queue.{RESET} Committing final batch ({actions_since_commit} actions)…\n"
            )
            crc, cout = commit_page(
                method_root,
                content_root,
                f"kb: TUI walk final batch ({actions_since_commit} actions)",
            )
            sys.stdout.write(f"{GREEN if crc == 0 else RED}commit rc={crc}: {cout[:300]}{RESET}\n")
            if crc != 0:
                final_rc = 2
    else:
        sys.stdout.write(f"\n{BOLD}{GREEN}Queue cleared.{RESET}\n")
    if accuracy_log:
        print_accuracy_summary(accuracy_log)
    return final_rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{YELLOW}Interrupted.{RESET}\n")
        sys.exit(130)
