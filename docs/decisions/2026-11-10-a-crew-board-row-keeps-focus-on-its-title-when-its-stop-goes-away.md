# A Crew board row keeps focus on its title when its Stop goes away

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

When an orphaned row's Stop or Take over controls go away on the Crew board
while keyboard focus is on them, focus moves to that row's title, and no extra
announcement is added.

## Why

- The title is the one element every row always has, and it names the item the
  reader acted on.
- Moving to the band heading leaves the item, and moving to the next row's Stop
  would put a second Enter on a different worker's Stop.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32273#issuecomment-6779985275 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32273 -- the issue.
