# The browser tool sets its tab's dark or light mode and its viewport size

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The `browser` tool has two ops that are not Browser Harness helpers: `set_appearance` (`prefers-color-scheme` light, dark or reset) and `resize` (viewport width and height in CSS pixels, or reset). Each changes only a tab the agent opened and ends when that tab closes; a site's "read" grant covers them.

## Why

- The owner asked on 2026-11-23 (PST) to add dark and light mode and window-size changes to the browser tool. He stated on #41329 that it overrides, for these two ops only, his earlier ruling that the browser work builds only what Browser Harness's own helpers do.

## Evidence

- The owner's ruling, on record: [#41329](https://github.com/kirodotdev/KiroCrew/issues/41329) (2026-11-23, @bolichen97), item 2.
