# The Discover sidebar row can be turned off in each browser, and Library opens Discover

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

Library's page header has a "Discover apps" button that opens Discover, and Settings >
Display > View has "Show Discover in the sidebar", on by default and kept per browser with the
sidebar pins; off takes only the Discover row off the rails, the sidebar's app updates count
then shows on the Library row, and Discover stays in Search Everywhere and at `/apps`.

## Why

- The request is a less crowded sidebar for people with many apps, with Discover still easy
  to reach; the button and the switch are the smallest change that does both.
- It is kept per browser because it hides one row, as a sidebar pin does. Task Runner and
  Channels are saved for the whole crew because off also takes their page out of Search
  Everywhere; Discover stays there.
- The updates count moves to Library rather than going away, so the sidebar updates badge
  still has no off switch (2026-11-10-the-discover-badge-counts-only-enabled-apps.md).
  Library already shows the pending updates and links to Discover's Updates tab.
- No switch for Library: the request offers it as optional, and hiding both rows would leave
  the sidebar with no way to the apps pages.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41654#issuecomment-7119716603 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/41654 -- the request.
