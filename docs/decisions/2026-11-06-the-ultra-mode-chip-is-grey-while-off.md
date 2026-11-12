# The Ultra Mode chip is grey while it is off

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-06
Supersedes: 2026-10-30-the-ultra-mode-name-wears-the-purple-to-blue-gradient.md (in part: "the bolt and every other Ultra Mode surface keep the solid accent", for the composer chip while off only); 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (in part: "one solid theme accent", for the composer chip while off only)

## Decision

While Ultra Mode is off, the composer's Ultra Mode chip is grey like the
composer's other chips: its bolt and its label use the muted text colour. The
accent (the tinted fill, the accent edge, the filled accent bolt and the
gradient label) is only for the chip's on state. Every other Ultra Mode surface
keeps the solid accent.

## Why

- An off switch in the accent colour reads as already on next to the grey chips
  beside it, and pulls the eye more than an idle control should.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/31635#issuecomment-6693912208 -- the
  maintainer's ruling, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/31609 -- the issue.
