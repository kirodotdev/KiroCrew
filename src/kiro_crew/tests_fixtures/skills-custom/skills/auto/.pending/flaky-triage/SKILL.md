---
name: auto/flaky-triage
description: Classify a red test as a real regression, a flake, or an environment problem before patching.
triggers: flaky test, red shard, rerun failed, triage failure
source: auto
session_key: fixture
created_at: 2026-01-15T09:00:00+00:00
---

# flaky-triage (auto-generated)

## When to use
A shard went red and it is not yet known whether the diff caused it.

## Steps
1. Check whether the same test fails on the base commit.
2. Check whether it fails on one platform only.
3. Only patch once the cause is named.

## Gotchas
- A fail-closed gate red is often a cascade of one real failure.
