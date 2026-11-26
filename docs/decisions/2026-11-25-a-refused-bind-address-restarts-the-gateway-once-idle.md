# A live policy refresh that refuses the gateway's bind address restarts the gateway once it is idle

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25
Supersedes: 2026-11-10-updates-never-restart-a-running-gateway.md (in part, for its rule that no unattended path restarts a running gateway, and only for the restart below once the gateway is idle after a policy refresh refuses its bind address; an update a person armed applies at that start, as at any start; every other rule of that entry stands, and no update path restarts a running gateway)

## Decision

When a policy refresh no longer permits the address the gateway listens on, the gateway keeps serving until it is idle and then restarts so the narrowed address applies; it never forces that restart while work runs and checks again on a capped backoff, and if the restart cannot run once the gateway is idle it stops instead of staying on the old address.

## Why

- The maintainer's ruling of 2026-11-25 on the pull request: keep the refused-bind detection; restart only once the gateway is idle, with a bounded backoff, never forced.
- The maintainer holds this to the aim of the 2026-11-10 entry: a running gateway never restarts itself while work runs. It is governance enforcement, not an update, and it waits for the same idle check the unattended update paths read. That entry's text also rules out a restart at an idle gate, so this entry supersedes that one rule, for a refused bind address only. Until the gateway is idle, it keeps serving the refused address.
- A restart is the smallest way to meet the issue's "failing closed" ask with what the gateway already has (the maintainer's decision of 2026-11-08 on the issue).
- The `tailnets` list part follows #24951, which adds that list.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/33040#issuecomment-7157495367 -- the maintainer's 2026-11-25 ruling.
- https://github.com/kirodotdev/KiroCrew/issues/24363#issuecomment-6717815282 -- the maintainer's 2026-11-08 decision.
- https://github.com/kirodotdev/KiroCrew/issues/24363 -- the issue recording the request.
