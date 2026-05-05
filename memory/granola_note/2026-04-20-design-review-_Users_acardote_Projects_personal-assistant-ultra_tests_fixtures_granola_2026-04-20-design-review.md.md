---
id: mem-51895f9b-f53c-40e3-846c-f1ee96cae69a
source_uri: file:./raw/granola_note/2026-04-20-design-review-_Users_acardote_Projects_personal-assistant-ultra_tests_fixtures_granola_2026-04-20-design-review.md.md
source_kind: granola_note
created_at: '2026-05-05T07:19:39Z'
expires_at: '2026-08-03T07:19:39Z'
kind: note
tags:
- spine-v2
- design-review
- webhooks
- field-naming
- platform
title: Spine v2 contract draft — snake_case, at-least-once webhooks, 6mo v1 deprecation
summary: Design review locked v2 field naming (snake_case + OAuth camelCase carve-out), at-least-once webhooks with idempotency_key,
  and a 6-month v1 deprecation window.
---

## What was decided / what is true

- **Field naming**: v2 standardizes on snake_case for all field names. Carve-out: OAuth scope claim names stay camelCase per upstream Google convention.
- **Webhook contract**: v2 adds `delivery_attempt` (integer) and `idempotency_key` (UUID) to every event payload. Delivery is **at-least-once**; customers are responsible for dedup via `idempotency_key`.
- **Retry cap**: 12 attempts, exponential backoff, max delay 1 hour.
- **Audit export**: same shape as v1 (structured logs to customer bucket), now includes `idempotency_key` so customers can correlate.
- **v1 deprecation**: 6-month window, matching the spine versioning rule from Q2 strategy.
- **Docs**: a "common mistakes" callout ships with v2 publication, not after first incident.

## Why

- v1 incident on 2026-04-18 showed customers shipping camelCase adapters against a snake_case canonical. v2 should not introduce additional drift.
- Google upstream spec dictates camelCase for OAuth claim names — carve-out preserves spec compliance.

## Load-bearing constraints

- **Bob**: draft retry semantics doc by **2026-04-30**.
- **Alice**: propose v2 schema PR by **2026-05-10**.
- **Dana**: confirm OAuth claim-name carve-out with security review board by **2026-05-05**.
- **Chris**: sync with sibling team on v2 impact to their integration timelines (no date set).
- v2 ship target: before **2026-09-30** (Q3 review) is preliminary, conditional on no v1 incidents between 2026-04-20 and **2026-08-15**; otherwise slip to Q4. Not yet a decision.
- Retry cap: 12 attempts, max delay 1 hour, exponential backoff.
- v1 deprecation window: 6 months post-v2 GA.

## What to remember when this comes up later

- Snake_case is the v2 default; the only camelCase fields are OAuth claim names. Any other camelCase appearing in v2 schema is a regression against this decision.
- Customers MUST dedup on `idempotency_key`. Server-side exactly-once was explicitly rejected in favor of at-least-once.
- Audit-line correlation requires `idempotency_key` — drop it from audit and you break customer correlation workflows.
- Q3 ship is contingent, not committed. Treat 2026-08-15 as the incident-freeze checkpoint that determines Q3 vs Q4.

## Open questions deferred

- Whether `delivery_attempt` is exposed on the customer-facing dashboard.
- Whether v2 ships in Q3 2026 or slips to Q4 — depends on v1 incident record through 2026-08-15.
- Sibling-team integration timeline impact (Chris's sync pending).
