# A live policy refresh that refuses the gateway's bind address restarts the gateway

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision

When a policy refresh no longer permits the address the gateway is listening on, the gateway restarts so the narrowed address applies, and if that restart cannot run it shuts down instead of staying on the old address.

## Why

- A restart is the smallest way to meet the issue's "failing closed" ask with what the gateway already has.
- The `tailnets` list part follows #24951, which adds that list.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/24363#issuecomment-6717815282 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/24363 -- the issue recording the request.
