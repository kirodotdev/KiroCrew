# An expired publication is no longer treated as published

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

A first publish to a destination that offers expiry periods records when it expires (the
time the publish returned plus the chosen period, not a time the destination reports);
the artifact page shows that date, a warning that reads "Expires soon, on <date>" in its
last 7 days, and, once it has passed, that the publication expired on that date beside a
Publish button that opens the Publish panel, and the library badges say
"Expired"; an expired publication gets no auto-sync push, and a new publish of it must
choose an expiry, first withdraws the old copy (stopping if the destination does not confirm
the removal) and then publishes a new link on the same destination, while a publish that
names no expiry is refused with the way out.

## Why

- When a destination deletes a page at the end of its period, Kiro Crew kept showing the
  dead link as live, gave no warning before it happened, and the next auto-sync failed with
  a generic error.
- Comparing a stored date costs no call to the destination, so no page open gets slower,
  and the gateway's clock both stores and checks the date.
- The publication record is the only handle to a copy that may still be served, so it is
  dropped only when the destination confirms the copy is gone, never because a date passed.
- A request that names no expiry comes from a panel or caller that still saw the old link
  as live; publishing a new link with a lifetime nobody chose would be worse than asking.
- Extending a live publication's expiry stays a separate change.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40442#issuecomment-7152047453 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/40442#issuecomment-7171683069 -- the maintainer's
  amendment after the review of #42566: the Publish button on the expired line and the words of
  the last-week warning.
- https://github.com/kirodotdev/KiroCrew/issues/40442 -- the issue and the options it was
  decided from.
- https://github.com/kirodotdev/KiroCrew/pull/32264 -- the change that let a destination
  declare expiry periods and left recording them for this follow-up.
