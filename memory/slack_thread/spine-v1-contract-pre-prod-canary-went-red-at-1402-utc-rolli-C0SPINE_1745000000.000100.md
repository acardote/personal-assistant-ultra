---
id: mem-bacb06d2-34eb-4a61-b298-1f2b0e511871
source_uri: file:./raw/slack_thread/spine-v1-contract-pre-prod-canary-went-red-at-1402-utc-rolli-C0SPINE_1745000000.000100.md
source_kind: slack_thread
created_at: '2026-05-05T07:12:54Z'
expires_at: '2026-08-03T07:12:54Z'
kind: thread
tags:
- spine-v1
- incident
- scim
- contract
- deprecation
title: 'Spine v1 pre-prod incident: externalId vs external_id, 30-day shim decided'
summary: Pre-prod canary failed on SCIM field-name drift; canonical stays external_id (snake_case) with a 30-day translation
  shim accepting externalId until 2026-06-15.
---

## What was decided / what is true

- Pre-prod canary went red 2026-04-18 14:02 UTC; rollback complete 14:08 UTC (6-minute decided→incident, fastest detection on this surface).
- Root cause: SCIM provisioning callback expected `external_id` (snake_case, published 2026-05-12) but customer adapter sent `externalId` (camelCase, written against an earlier draft).
- Canonical field name remains `external_id` (snake_case). No contract rev.
- Backport a translation shim accepting `externalId` for 30 days with a deprecation header.
- Hard cutoff: after **2026-06-15**, only snake_case is accepted.
- Amended for the 4 already-converted custom-contract accounts: SE team pings on day 7; if no response by day 21, that account's personal deadline extends by another 30 days. The canonical contract is not renegotiated.
- Doc update: published contract docs get an explicit "common mistake" callout for snake_case. Bob owns by EOD **2026-04-16**.
- Alice owns the customer ping. Chris stays on v2 spine planning, off both items.

## Why

- Drift is between published draft and customer-implemented adapter, not a contract defect — translation shim is cheaper than a contract rev.
- 2 of the 4 converted accounts ship monthly and could miss a 30-day window; the day-7 ping + day-21 personal extension absorbs that risk without compromising the canonical contract.
- Doc callout: one paragraph cost vs. real-deploy failure mode.

## Load-bearing constraints

- **2026-06-15** — shim removed; only `external_id` accepted globally.
- **2026-04-16 EOD** — Bob's doc-update deadline.
- **Day 7 / Day 21** — SE ping cadence for the 4 converted accounts; +30-day personal extension if no response by day 21.
- SE team owns the customer ping per Q2 SE allocation policy.
- Custom-contract accounts retained at current scope per Q2 strategy.

## What to remember when this comes up later

- Recovery cost of this drift: 6 minutes incident + 30-day shim. Logged as Q3-review evidence against the 2026-09-30 falsification check on Q2 strategy; **does not fire any of the four falsification criteria**.
- If a similar field-name drift recurs, the precedent is shim + deprecation window, not contract rev.
- Personal-extension mechanism (day-21 → +30 days) applies only to the 4 already-converted accounts, not as general policy.
- Detection speed (6 min) is a new baseline for this surface.

## Open questions deferred

- None explicitly left open. Chris's doc-callout question was resolved yes in-thread.
