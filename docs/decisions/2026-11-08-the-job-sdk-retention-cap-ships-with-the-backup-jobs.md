# The Job SDK's retention cap ships with the backup jobs as its first consumer, and the liveness predicate waits for an app that needs it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision

`JobSDK.register` gains only an opt-in per-kind count cap, `retention`, and it lands together with aws_control's backup kinds as its caller; the `live` predicate and the `on_evict` callback are not added until an app whose work continues after a run finishes needs them.

## Why

- A public App Kit argument ships with its first caller, so its shape is tested by a real consumer before apps depend on it.
- aws_control already runs on the Job SDK, adds a backup record every night that only app disable removes, and does no work after a run finishes, so a cap judged by status alone is safe for it.
- Landing only the supporting pieces (the eviction audit, the store delete and the no-follow scan) would ship code with no caller.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/7588#issuecomment-6719732185 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/pull/22711#pullrequestreview-5777846606 -- the review it narrows, which asked for the arguments to land with a consumer.
- https://github.com/kirodotdev/KiroCrew/issues/33058 -- the liveness predicate and eviction callback, split out.
