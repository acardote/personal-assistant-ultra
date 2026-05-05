---
id: mem-8bf9960b-a8eb-4856-8f89-cd950551be99
source_uri: file:./raw/transcript_file/2026-04-25-customer-spine-call-_Users_acardote_Projects_personal-assistant-ultra_tests_fixtures_transcripts_2026-04-25-customer-spine-call.txt.md
source_kind: transcript_file
created_at: '2026-05-05T07:20:26Z'
expires_at: '2026-07-30T23:59:59+00:00'
kind: thread
tags:
- acme
- spine-v1
- integration
- migration
- rate-limit
- audit-export
title: Acme spine v1 integration kickoff — migration deadlines and rate-limit terms
summary: Acme is migrating from spine v0 to v1; hard cutoff 2026-07-30, extension flag deadline 2026-05-21, plus rate-limit
  and audit-export specifics.
---

## What was decided / what is true

- Acme is implementing spine v1 against the snake_case fields (camelCase deprecated after a prior incident).
- Audit export schema supports both per-user OAuth grant and service account for bucket-write IAM. Service account is the recommended production path; user OAuth is intended only for the integration test phase. Most customers move to service accounts within the first month.
- Webhook rate limit: documented 100 events/second/customer is a **soft** limit that auto-scales under sustained traffic. Bursts to 600/s are fine. Throttling triggers on sustained 1000+/s for more than 30 seconds. Customers expecting that pattern should request pre-provisioning in advance.
- v0→v1 migration window is fixed-date, not per-customer-signature: started 2026-05-01, hard cutoff 2026-07-30. Per-customer extension up to 30 additional days available, but only if the SE rep flags it before day 21 of the window (i.e., by 2026-05-21 for Acme).
- Acme committed to baking `idempotency_key` into every v1 event from day one even though it is optional in v1, because v2 will require it and early adopters skip v2 retry-handling complexity.
- Erin (Acme integration lead) committed to pinging Acme's SE rep on 2026-04-26 about the extension, given Q2 release schedule pressure.
- Alice committed to a follow-up email with contact list and SE rep handoff.

## Why

- Service-account recommendation is grounded in observed customer migration pattern (most move within month one).
- Rate-limit thresholds reflect actual platform throttling rules, not the documented soft cap.
- Migration window is fixed-date so the spine team can deprecate v0 cleanly; extension mechanism exists but is gated on early SE-rep flagging.
- `idempotency_key` recommendation is forward-looking: v2 will enforce it.

## Load-bearing constraints

- **2026-05-01** — v0→v1 migration window opens (fixed date, applies to all customers).
- **2026-05-21** — Acme's deadline to flag SE rep for migration extension (day 21 of window).
- **2026-07-30** — hard cutoff for v0→v1 migration; +30 days max with approved extension.
- **100 events/s/customer** — documented soft webhook rate limit; auto-scales.
- **600/s short burst** — confirmed acceptable for Acme quarterly cycles.
- **1000+/s sustained for >30s** — actual throttling trigger; requires pre-provisioning request.
- **Owners:** Alice (platform-spine), Erin (Acme integration lead), Frank (Acme architect), unnamed Acme SE rep.

## What to remember when this comes up later

- If Acme misses the 2026-05-21 SE-rep flag deadline, no extension is available and they must complete migration by 2026-07-30.
- If another customer asks about v0→v1 migration timing, the answer is fixed-date (2026-05-01 → 2026-07-30), not signature-relative.
- Webhook docs understate true throttling threshold by 10×; cite the 1000/s sustained / 30s rule, not the 100/s soft cap, when advising on capacity planning.
- For new integrations, default IAM advice is service account, not per-user OAuth.
- Encourage `idempotency_key` adoption on v1 to avoid a v2 retry-handling lift later.

## Open questions deferred

- Whether Acme's SE rep will approve the 30-day extension (action pending Erin's 2026-04-26 outreach).
- Acme's exact Q2 release schedule and whether it conflicts with the 2026-07-30 cutoff even with extension.
- Whether the spine team has communicated the 1000/s sustained throttling threshold publicly or only via direct conversations.
