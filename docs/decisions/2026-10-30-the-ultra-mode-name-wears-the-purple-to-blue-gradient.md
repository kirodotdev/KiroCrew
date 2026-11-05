# The Ultra Mode name wears the purple-to-blue gradient, and turning it on plays a short effect

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-30
Supersedes: 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (in part: "one solid theme accent instead of the crew gradient", for the Ultra Mode words only)

## Decision

With the preview on, the words "Ultra Mode" (the Sessions row's ULTRA MODE tag, the
composer chip's label while on, and the welcome title) wear a purple-to-blue text
gradient from the theme's `--aim` to its `--info`, at WCAG AA in light and dark;
the bolt and every other Ultra Mode surface keep the solid accent. Turning Ultra
Mode on in a new chat plays one short full-chat effect (a bolt flash from the switch,
a purple-to-blue sweep, one glow on the Board tab) that blocks no input, moves
nothing, plays only on turning on, and is a quick fade under reduced motion.

## Why

- The owner wants the mode to stand out once it is on, the way the original
  purple-to-blue "CREW MODE" label did, and turning it on to feel like a moment.
- The gradient stays on the name alone, so the flat-fill rule still holds for every
  fill, card, row and chip.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/26441#issuecomment-6534271129 -- the
  maintainer's ruling, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/26439 -- the issue.
