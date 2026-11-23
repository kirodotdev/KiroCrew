# The notice that an MCP server waits for a sign-in names each server once per gateway run, not once in every chat

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The chat row that says an MCP server needs a sign-in names each waiting server once per gateway
run, in the first chat whose session reports it waiting, and not again in another chat or in a
chat whose transcript already shows it for that server; its text, the sign-in card exclusions and
the rule that it shows only where naming a server starts its sign-in stay as they are, with no new
setting and no "don't remind me" control.

## Why

- The request was to stop the row repeating in every chat for a server the person never uses, and
  not to post it again after a restart in a chat that already shows it, while a person who wants a
  server's tools still learns why they are missing and how to sign in.
- Once per server per gateway run stops the repeat in every chat while keeping what the row was
  added for.
- Only a chat a person opened uses up the row (a dashboard chat with origin user, no app, no mode
  and no creating session). A cron's, an app's, a channel's or an app page's worker chat, or a
  session an agent opened or forked, still gets its own row but uses up nothing, since nobody may
  read it; so a server can be named more than once in a run, never hidden.
- Accepted limits: after a gateway restart a server still waiting is named once more, in one new
  chat; a sign-in lost a second time in the same run is named after the next restart; in a shared
  crew only the first chat a person opened that reports the server uses up the row; a chat
  reopened after a restart sees only its newest 500 rows, so in a longer chat an older row is not
  seen and the server is named again.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38457#issuecomment-7077785071 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/38457#issuecomment-7089592431 -- the refinement that only a chat a person opened uses up the row.
- https://github.com/kirodotdev/KiroCrew/issues/38457 -- the request.
- https://github.com/kirodotdev/KiroCrew/pull/36135 -- the change that added the row.
