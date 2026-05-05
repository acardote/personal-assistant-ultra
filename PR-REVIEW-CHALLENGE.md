# PR-REVIEW-CHALLENGE.md

- **Time/Date**: 2026-05-05
- **PR**: #20 — `#10 — multi-fidelity event matching + ranked retrieval`
- **Branch**: `main` (single commit `cda6904`)
- **Lint check**: SUCCESS

## Description

Closes #10 (multi-fidelity event matching + ranked retrieval). Implements the C-model dedup: same event via Granola + Meet + Gmail produces ONE canonical + ranked alternates, not three duplicates. Adds:

- `tools/dedup.py` — bag-of-words cosine + date-window clustering, authority-based canonical selection
- `tools/dedup-config.json` — per-source authority and thresholds (cluster_threshold=0.4, cosine_min=0.2, date_window=7d)
- Schema additions: `event_id`, `is_canonical_for_event`, `superseded_by`, `expiry_locked`
- `tools/compress.py` clustering integration (write-time)
- `tools/route.py` 0.85x canonical bonus
- 3 same-event Q3 fixtures + 3 acceptance tests

## Comments

(none)

## Commit log

- `cda6904` — `#10 — multi-fidelity event matching + ranked retrieval`

## Review

See full adversarial review in the assistant message accompanying this file. Key findings:

### CRITICAL

1. **F1 falsifier triggered against the PR's own fixtures**: a realistic week-2 follow-up meeting with same recurring vocabulary (BADAS, Atlas, Acko, Nuro) at +4 days produces combined score 0.511 ≥ 0.4 threshold. Role=alternate. Contradictory decisions get buried under stale week-1 canonical. T3 in the acceptance test only proves "totally unrelated topic doesn't merge" — never the actual F1 hazard.

2. **F2 falsifier NOT defended**: `pick_canonical()` keys on `(authority, created_at_asc)`. There is no path by which an edited Granola note becomes canonical over an older Gmail thread. PR docstring claims F2 is "tracked"; in fact authority-only ranking is exactly the failure F2 named.

### HIGH

3. **Compress.py race**: two simultaneous `compress.py` invocations on related memos can both decide "I'm canonical for new event" → two canonicals with different event_ids. No locking. Personal-scale low-frequency, but real.

4. **No relational invariants in schema**: `is_canonical_for_event=True` AND `superseded_by="mem-x"` simultaneously passes validation. So does an alternate with no `superseded_by` pointer.

### MEDIUM

5. F3 has a degraded mode for "general-topic queries with unique alternate asides": canonical with even one keyword hit beats alternate with no hits, regardless of alternate's unique content.

6. T2 acceptance test says "PASS — cluster_id stable on re-process" but actually exercises a re-cluster that returns role=canonical (because Gmail outranks Granola/Meet pseudo-canonical). The comment hand-waves this with "integration de-dup is compress.py's job" but neither the test nor compress.py implements that de-dup.

### LOW

7. `route.py` regex `is_canonical_for_event:\s*(true|false|True|False)` ignores YAML's full boolean vocabulary (`yes`/`no`/`Yes`/`No`/`!!bool`). PyYAML's `safe_dump` only emits `true`/`false`, so this is fine for compress-written files; brittle for hand-edited frontmatter.

8. Per-line frontmatter regex parsing in `route.py` (instead of a single `yaml.safe_load`) duplicates work and is the second time route.py opens and parses frontmatter — small inefficiency.

9. `created_at` round-trip on demote-rewrite: relies on the original file having quoted ISO strings to keep them as strings through PyYAML. If a user hand-writes unquoted `created_at: 2026-04-23T18:30:00Z`, PyYAML auto-coerces to datetime, then `safe_dump` emits a different shape. Not currently breaking because compress.py always writes quoted strings, but a non-trivial coupling.
