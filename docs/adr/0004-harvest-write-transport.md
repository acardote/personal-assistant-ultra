# ADR-0004 — Harvest write transport: direct `git push` to `main`

- Status: Accepted (assumptions A1/A3 pending validation — [#272](https://github.com/acardote/personal-assistant-ultra/issues/272))
- Date: 2026-06-01
- Decider: acardote
- Related: parent [#178](https://github.com/acardote/personal-assistant-ultra/issues/178) (re-scope); transport predecessors [#153](https://github.com/acardote/personal-assistant-ultra/issues/153) / [#161](https://github.com/acardote/personal-assistant-ultra/issues/161) (MCP `push_files`), [#179](https://github.com/acardote/personal-assistant-ultra/issues/179) / [#181](https://github.com/acardote/personal-assistant-ultra/issues/181) (feature-branch + PR + auto-merge, superseded); revert child [#268](https://github.com/acardote/personal-assistant-ultra/issues/268); deploy child [#270](https://github.com/acardote/personal-assistant-ultra/issues/270); validation child [#272](https://github.com/acardote/personal-assistant-ultra/issues/272). Builds on [ADR-0002](0002-scheduled-harvest-trigger.md) (routine vs launchd).

## Context

The scheduled harvest routine (ADR-0002) must commit each fire's harvested memory objects, KB candidates, and run-status JSON back to the content vault (`acardote/acardote-pa-vault`). The **write transport** — how those commits reach the vault's `main` — has churned through three designs as the routine sandbox's git environment shifted:

1. **Direct `git push` to `main`** (original). Worked until 2026-05-08.
2. **MCP `push_files`** ([#153](https://github.com/acardote/personal-assistant-ultra/issues/153) / [#161](https://github.com/acardote/personal-assistant-ultra/issues/161), v0.4.2). Adopted when a sandbox-side proxy GitHub-identity swap (2026-05-08→05-11) caused direct `git push` to `main` to return 403: the proxy authenticated as a principal without branch-protection bypass. `push_files` goes through the github.com API under the routine's account OAuth — a separate auth path that still landed on `main`. It carried known costs: a finite-lifetime token that expired mid-multi-batch-push ([#166](https://github.com/acardote/personal-assistant-ultra/issues/166)), and a per-blob payload-construction burden.
3. **Feature-branch + PR + auto-merge** ([#179](https://github.com/acardote/personal-assistant-ultra/issues/179) / [#181](https://github.com/acardote/personal-assistant-ultra/issues/181), slices 1–2 of #178). Designed to restore `git` tooling under the swapped identity (feature-branch push still worked even though main push 403'd) and add a PR audit trail + adversarial-review hook. **This transport landed in the template but never fired in the serving routine** — the deployed routine was never updated off MCP `push_files`. Inspected 2026-06-01: the vault had 0 routine `harvest-<RUN_TS>` PRs ever; every routine harvest that reached `main` did so as a direct single-parent (MCP) commit, and post-2026-05-29 fires stranded on `claude/cool-lamport-*` session branches until manually reconciled ([#263](https://github.com/acardote/personal-assistant-ultra/issues/263) / [#267](https://github.com/acardote/personal-assistant-ultra/issues/267)).

In 2026-06-01 the constraint that motivated transports (2) and (3) was lifted: direct writes to `main` are available again from within the routine (the routine's vault source carries `allow_unrestricted_git_push: true`). The feature-branch workaround's entire reason for existing was gone.

## Decision

**The harvest routine writes via direct `git push` to `main`, delegating commit + push to `tools/live-commit-push.sh`.** The feature-branch + PR + auto-merge workaround (slices 1–2) is retired. Concretely:

- The routine ensures HEAD is `main`, synced to `origin/main`, **non-destructively** (stash → checkout → pop; never `reset --hard`, so local commits and modified-tracked harvest files are preserved). It hard-refuses to proceed off `main` — this is the fix for the `cool-lamport-*` stranding (the push can no longer land on an ephemeral session branch).
- It then delegates to `tools/live-commit-push.sh`, the same hardened path interactive sessions use: `vault-desync-probe.py` → `lint-provenance.py --require-vault` (the **sole** quality gate) → stage standard paths → commit once → push to `main` with a non-fast-forward rebase-retry.
- **No PR, no auto-merge, no PR-based adversarial review** (those bound on the now-removed feature branch; the org "no merge without adversarial review" rule binds on code-PR merges, not data pushes).
- **No automatic fallback.** A failed push (including a 403 recurrence) fails loudly and a human re-plans. MCP `push_files` is retained only as a documented manual operator escape hatch.

The deployed routine prompt was simultaneously converted from an inlined copy to a thin shim that reads + executes the canonical `### Routine prompt` block from `templates/routines/harvest-routine.md` in the freshly-cloned method repo ([#270](https://github.com/acardote/personal-assistant-ultra/issues/270)) — so the template is the single source of truth and this class of transport drift cannot recur silently.

## Consequences

**Positive**

- Simplest transport with no new failure modes: `git push` is single-request and re-auths per invocation (no long-lived-token expiration class of [#166](https://github.com/acardote/personal-assistant-ultra/issues/166)).
- Fixes the stranding class structurally (push targets `main` or the fire aborts).
- Reuses a battle-tested helper (desync probe + lint gate + rebase-retry) rather than re-implementing write logic in a prompt.
- Audit trail is preserved via `git log` + the per-fire `.harvest/runs/<ts>.json` (`push.transport: "git-direct-main"`).

**Negative**

- **Cross-scheduler write race reintroduced.** The feature-branch transport had structurally eliminated the routine-vs-launchd race on `main`; direct push brings it back. Mitigation: the helper's non-fast-forward rebase-retry handles the bounded case, and the **"Choose ONE scheduler" discipline (ADR-0002 / the routine prompt) is load-bearing** — it is the real guard, the rebase-retry only a backstop.
- **No PR audit trail or adversarial-review hook** on harvest writes. Accepted: harvest output is data, not code; the inline lint is the mechanical gate; volume/anomaly detection lives in the lint + watchdog, not a per-fire review.
- **No automatic resilience to another identity swap.** If direct main push 403s again (the failure that created #178), fires fail loudly until a human re-plans. Accepted (assumption A5).

## Assumption ledger

- **A1** — Direct `git push` to `main` works from within the routine under the current principal (`allow_unrestricted_git_push: true` on the vault source). *Validation: PENDING — slice [#272](https://github.com/acardote/personal-assistant-ultra/issues/272) (live fire); operator-asserted but not yet reconciled against a landed fire as of 2026-06-01. Falsify: a fire's push to `main` returns 403.*
- **A2** — The inline `lint-provenance --require-vault` gate is sufficient QA for autonomous direct-to-`main` data writes absent a PR/review. *Falsify: a harvest lands content on `main` the lint passed but a human would have blocked (PII/secret/gross anomaly) with material consequence.*
- **A3** — Pushing to `main` (vs the session branch) eliminates the `cool-lamport-*` stranding class. *Validation: PENDING — slice [#272](https://github.com/acardote/personal-assistant-ultra/issues/272) (live fire); not yet reconciled as of 2026-06-01. Falsify: a fire still strands content on a non-`main` branch.*
- **A4** — "Choose ONE scheduler" + the non-ff rebase-retry are sufficient against the reintroduced cross-scheduler race. *Falsify: two schedulers racing on `main` produce a lost/clobbered harvest commit.*
- **A5** — Fail-loud-without-fallback is operationally acceptable (a failed fire surfaces loudly; a human re-plans before the next fire's data is at risk). *Falsify: a fire fails its push, surfaces nothing actionable, and the gap goes unnoticed past the next fire.*
