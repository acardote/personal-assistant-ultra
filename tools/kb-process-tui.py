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


def render_candidate(memo_path: Path, fm: dict, body: str, idx: int, total: int) -> None:
    """Clear screen and render one candidate."""
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
    sys.stdout.write(
        f"\n{DIM}─── {GREEN}(a){RESET}{DIM}pprove   {GREEN}(r){RESET}{DIM}eject   "
        f"{GREEN}(m){RESET}{DIM}amend   {GREEN}(s){RESET}{DIM}kip   "
        f"{GREEN}(c){RESET}{DIM}ommit-page   {GREEN}(q){RESET}{DIM}uit ───{RESET}\n"
    )
    sys.stdout.flush()


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

    sys.stdout.write(
        f"{BOLD}kb-process-tui{RESET} — walking {total} unprocessed candidates "
        f"in {DIM}{unprocessed}{RESET}\n"
        f"{DIM}Vault: {content_root}{RESET}\n"
        f"{DIM}Method: {method_root}{RESET}\n"
        f"{DIM}Per-candidate keys: (a)pprove (r)eject (m)amend (s)kip (c)ommit-page (q)uit{RESET}\n"
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

        render_candidate(memo_path, fm, body, idx + 1, total)
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
            if wants_scope:
                memo_text = memo_path.read_text(encoding="utf-8")
                if "**Scope:**" in memo_text:
                    # Operator already added Scope inline — don't double-prompt.
                    pass
                else:
                    scope = prompt("Scope (or empty to apply without)", default=last_scope)
                    if scope:
                        injected = inject_scope_into_memo(memo_path, scope)
                        if injected:
                            last_scope = scope
                        else:
                            sys.stdout.write(
                                f"{YELLOW}Scope inject failed (diff-block shape mismatch); applying without.{RESET}\n"
                            )
            rc, out = apply_memo(method_root, art_id)
            if rc == 0:
                sys.stdout.write(f"{GREEN}✓ applied (after amend){RESET}: {out}\n")
                actions_since_commit += 1
            else:
                sys.stdout.write(f"{RED}✗ apply failed rc={rc}{RESET}:\n{out}\n")
                sys.stdout.write(f"{DIM}Press any key.{RESET}\n")
                getch()
                continue
            idx += 1

        elif key == "s":
            sys.stdout.write(f"{DIM}skipped (memo stays in .unprocessed/){RESET}\n")
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
    return final_rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{YELLOW}Interrupted.{RESET}\n")
        sys.exit(130)
