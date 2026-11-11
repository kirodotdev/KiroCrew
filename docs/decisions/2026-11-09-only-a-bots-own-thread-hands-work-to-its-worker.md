# Only a bot's own thread hands work to its worker

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

Only a bot's own thread (its standing thread on the **Bots** page) hands work
to its worker; a chat that runs a bot on its conductor anywhere else (the agent
picker, a chat folder's default agent, an agent channel) is still offered, not
refused, and does the work in that chat itself, is told so in its instructions,
and cannot use abilities added to the bot, which reach only its worker.

## Why

- Binding those chats as the bot's own chat would change how every session
  path recognises a bot's own chat, which is a design change, not a bug fix.
- Refusing them would remove a choice the agent picker and folder defaults
  offer today.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/28546#issuecomment-6746398874 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/33996 -- the feature request
  that keeps the bind open.
- https://github.com/kirodotdev/KiroCrew/issues/28546 -- the issue.
