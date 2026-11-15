# A pinned message the person wrote can be put back in the composer from the Pins tab, and is never sent by it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

In the chat's Pins tab, a pin of a message the person wrote has a **Put in
composer** button that brings that message into view, puts its full text in the
composer after anything already typed, and never sends it; pins of replies keep
Jump, Copy text, Copy link and Unpin as they are.

## Why

- The request was a one-click rerun of a pinned prompt: fill the composer and
  let the person edit before sending.
- Copy text on a pin copies only the pin's short preview, so a long prompt could
  not be re-sent from the pin.
- The other options were to wait for the open pins designs, which the maintainer
  decision says are parked, or to close the request as covered by Copy text.
- The triage comment posted from Ray Xu's account (maintainer) laid out the three
  options and recommended this one: a small addition to the existing Pins tab
  that adds no agent-facing action and never sends on its own.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30544 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/30544#issuecomment-6673446979 --
  the triage comment: the three options and the recommendation.
- https://github.com/kirodotdev/KiroCrew/issues/30544#issuecomment-6731775131 --
  the maintainer decision on the issue.
