# `!voice` is not deprecated, and `!voice global` takes on or off

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

`!voice global on` and `!voice global off` set the saved `voice_reply.enabled`, a bare `!voice global` keeps toggling it and any other word after `global` gets a usage reply that changes nothing; `!voice` posts no deprecation notice, because its `on`/`off` forms act on the thread they are typed in, which a slash command cannot name, and `!voice global on|off` is the switch in the bot DM for an owner who turned the `/kirocrew` command off.

## Why

- An owner who keeps the workspace-wide `/kirocrew` command turned off had no way to say "voice off" from the bot DM: `!voice global` only flipped the saved value, and the `!voice` notice sent them back to the command they turned off.
- `voice_reply.enabled` already turns voice replies on in every thread (with it off a new thread starts without voice replies), so no separate default is needed.
- Options not chosen: an owner-only `!config <key> <value>` (a new way to change settings from chat that the request does not need); waiting for one shared slash command routed per user (no date).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40090 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/40090#issuecomment-7143226055 -- the maintainer's decision on that issue.
