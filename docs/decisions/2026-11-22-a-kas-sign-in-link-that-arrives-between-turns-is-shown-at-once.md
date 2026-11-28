# A KAS sign-in link that arrives between turns is shown at once, and a server is named without its separators

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

On KAS, a remote MCP server's consent link that reaches a dashboard chat's session while no turn is reading it is drawn at once in that open chat as the same Authorize banner a mid-turn link gets, through the same pipeline (the session's checks, endpoint approval, child stamp, ended-attempt hook); the sign-in still starts at turn start, from the person's own message, and every other surface queues the link as before; a server name also matches when the person leaves out its separators (`acmedocs` and `AcmeDocs` name `acme-docs`), accepting that a configured name splitting a compound word (`work-flow`) is also named by the glued word; the 630 s sign-in slot timeout is unchanged and the turn is not kept open until the link arrives; a Sign in button on the chat notice moved to issue #40501, and a Connections > MCP Servers Sign in control, a generic "sign in" offer, reverse, partial or URL-host name matching, an agent-callable sign-in and starting a second named server without a new message to issue #41147.

## Why

- The engine's discovery and client registration take seconds, so a short turn ends before the link arrives; nothing read the session queue between turns, and by the next message the link was dead.
- One pipeline for both paths keeps a single set of URL checks, dedupe and withdrawal rules instead of a second banner writer.
- People write a hyphenated server name glued or in camel case; the glued-word false positive is bounded because only a server already waiting for a sign-in, named in the person's own typed message, is offered.
- The engine answers the reset 60 s in while a 300 s server's attempt continues, so a banner drawn between turns is not withdrawn by that answer, and the slot timeout needs no change.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34492#issuecomment-7071217798 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34492 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/41147 -- the other ways to start a sign-in, split out of #40501.
