# A cloud crew cloned at a release tag updates to the hub's own release tag, never to main

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

When a release-build hub presses Update on any crew whose git checkout is
detached exactly at a release tag, which is what `kirocrew cloud launch` from a
packaged install makes, the crew runs
`kirocrew update --release <the hub's version>` and moves to that release's tag,
fetching only that tag and never moving to an older release; every other
install, and the bare `kirocrew update`, update as they did before.

## Why

- The launch clone is `--depth 1` at one release tag, and its history shares
  nothing with `main`. The bare update fetched `main` into it, which is the
  whole history (4.5 GB, about 8 minutes in the report), and timed out every
  time. A completed fetch was then refused as diverged, because release tags are
  not on `main`, and `--force` landed on `main`, whose major.minor the hub would
  not pair with.
- The hub already pins the crew to its own release tag at launch
  (`resolve_public_ref`), so updating to the hub's release tag keeps the pair the
  launch chose.
- The launch clone stays depth-1: nothing is unshallowed, and a crew launched
  before this change is not rescued by it.
- A crew whose CLI predates `--release` answers argparse's exit 2, and the hub
  then runs the bare `update` once, as before.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39315 -- the report: a cloud crew
  launched from a packaged install cannot be updated.
- https://github.com/kirodotdev/KiroCrew/issues/39315#issuecomment-7080227170 --
  Bolin Chen's comment on that issue stating this decision.
