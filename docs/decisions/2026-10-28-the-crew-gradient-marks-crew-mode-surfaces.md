# The crew gradient marks Crew Mode's surfaces

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-28

## Decision

The **crew gradient** -- `--aim` to `--info`, the `.crew-grad` fill in
the dashboard stylesheet -- is the one mark of a chat whose turns are taken by a crew
rather than by the person. The CREW MODE session row and its eyebrow, the session
filter's Crew Mode chip, the composer's Crew Mode chip and the Crew panel's crew
cards all wear it. It is the one decorative gradient the brand system's flat-fill
rule (`website/docs/theming-contract.md`, "Fills are flat") admits, and it is
admitted for that reason alone: it is not a style for anything else.

## Why

- A Crew Mode chat is never mistaken for the person's own at a glance, on any
  surface that shows it: one mark, everywhere, is what makes it one thing.
- The flat-fill rule exists so that no surface looks different from its siblings
  for taste alone. This surface differs for a reason the rule does not cover: it
  names a different kind of chat, and the gradient is that name.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/24860 -- the design, decision 7
  ("a gradient CREW MODE session row"), triaged `tier:T4`, maintainer-signed.
- https://github.com/kirodotdev/KiroCrew/pull/24931#issuecomment-6470979122 --
  the maintainer restating the decision, carried to every Crew Mode surface, on
  the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/24931 -- part 5, the pull request
  that adds this entry and the chip that wears the gradient.
