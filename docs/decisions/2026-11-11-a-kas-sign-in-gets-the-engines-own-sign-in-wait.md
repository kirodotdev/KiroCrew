# A KAS MCP sign-in gets the engine's own sign-in wait, and a Settings sign-in ends only when its attempt does

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

On KAS, a remote MCP server whose entry sets no `timeout` is sent 300 s, the engine's own sign-in wait, and a Settings or Connections sign-in on KAS keeps its engine running until the attempt ends rather than stopping at the engine's first answer; the sign-in is not moved to the Authorize click and the chat card is not sent to Settings.

## Why

- KAS closes a sign-in's callback listener when the server's `timeout` runs out (60 s by default), and the listener starts when the sign-in is offered, so a consent that took longer, or a callback pasted back from another machine, found nothing listening.
- Starting the sign-in at the Authorize click, or sending the chat card to the Settings sign-in, leaves both paths under the same per-server timer, and is a larger change to how the sign-in flow is designed.
- A longer per-server `timeout` was already the documented workaround; the cost is that a remote server whose connect hangs takes as long to report `failed`.
- The engine-side fix (a consent-sized wait for the explicit sign-in) stays the long-term one; this default can drop once it ships.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26525#issuecomment-6806847609 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/26525#issuecomment-6540628880 -- the root cause and the two Crew-side options.
- https://github.com/kirodotdev/KiroCrew/issues/26525 -- the issue.
