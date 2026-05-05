# Design review — spine v2 contract draft

**Date:** 2026-04-20 14:00 UTC
**Attendees:** Alice (platform-spine), Bob (platform-spine), Chris (sibling team — v2 spine planning), Dana (security)

## Discussion notes

### Field naming convention (carry-forward from v1 incident on 2026-04-18)

- v1 incident showed customers shipping camelCase adapters against a snake_case canonical. v2 should not introduce additional drift.
- **Decision**: v2 standardizes on snake_case for all field names. Add an explicit "common mistakes" callout in v2 docs at publication time, not after the first incident.
- Dana raised: the OAuth scope claim names follow Google convention (camelCase). Carve-out: claim names stay camelCase per upstream spec; everything else is snake_case.

### Webhook contract

- v2 adds a `delivery_attempt` integer and a `idempotency_key` UUID to every event payload.
- Discussed retry semantics. Decision: **at-least-once with idempotency_key**, customers responsible for dedup. Bob will draft retry doc by 2026-04-30.
- Cap on retry attempts: 12 with exponential backoff, max delay 1 hour. Discussed but not decided whether to expose `delivery_attempt` on the customer-facing dashboard. Deferred.

### Audit export

- Same shape as v1 (structured logs to customer bucket).
- New: include the v2 `idempotency_key` in audit lines so customers can correlate.

### Open items

- Whether v2 ships before 2026-09-30 (Q3 review). Alice's preliminary read: yes if no incidents on v1 between now and 2026-08-15. Otherwise slip to Q4. Not a decision yet.
- v2 deprecation policy for v1: 6-month window matching the spine versioning rule from Q2 strategy.

## Action items

- [ ] **Bob** — draft retry semantics doc by **2026-04-30**.
- [ ] **Alice** — propose v2 schema PR by 2026-05-10.
- [ ] **Dana** — confirm OAuth claim-name carve-out with the security review board by 2026-05-05.
- [ ] **Chris** — sync with sibling team on whether v2 affects their integration timelines.

## Decisions (load-bearing)

1. v2 field names: snake_case everywhere except OAuth claim names.
2. Webhook contract: at-least-once delivery + customer-side dedup via idempotency_key.
3. v2 deprecates v1 over 6 months matching Q2 strategy's spine versioning rule.
