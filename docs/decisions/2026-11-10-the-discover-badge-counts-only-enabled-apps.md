# The Discover badge counts only enabled apps

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

The sidebar Discover badge counts only enabled apps with an update the store
can install; the Discover Updates tab, its count and Update All keep listing
disabled apps, and the badge has no off switch.

## Why

- A disabled app does not run, so its update needs nothing from the person until
  they turn the app back on, and the Updates tab still lists it.
- A badge that counts only the apps the person uses goes out once they update
  them, so the badge gets no off switch.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33848#issuecomment-6757299922 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/33848 -- the issue.
