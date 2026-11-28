# Plumb-4B's local server stops an ask once its caller has gone

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-28

## Decision

The Plumb-4B local server checks, before each model layer, whether the caller of the decision in
progress is still connected, and stops the decision once the caller has closed its connection: no
answer is sent and the server's one slot is free for the next decision. The request, the gateway's
client and every other local model server stay as they are.

## Why

- The report expected that an ask whose caller has gone stops, or never starts when it cannot finish
  in time. A decision the gateway gave up on was computed to the end, about 15 s of 16 busy cores for
  one 0.5 s browser check, and every decision sent meanwhile was refused.
- Stopping on the closed connection covers every abandoned ask on a CPU, Plumb-4B's slowest ones
  included, and needs no new request field, setting or state.
- Rejected: sending the caller's remaining time and refusing asks the server's measured speed says
  cannot finish, which adds a request field every local server must read and a speed estimate that
  one slow measurement can turn into refusing every later ask; keeping waiting, because asks are
  still abandoned once the browser's pick and check are no longer sent.
- Accepted limits: the stop lands before the next layer, so a very long prompt can still hold the
  slot past the 1 s queue wait; on an Apple GPU it lands only before the next pass; Strands
  Decider 2B's server is unchanged (#43885).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41197#issuecomment-7236150486 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/41197 -- the report.
