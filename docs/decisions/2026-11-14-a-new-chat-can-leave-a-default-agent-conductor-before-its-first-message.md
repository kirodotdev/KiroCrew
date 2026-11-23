# A new chat can leave a conductor that is the default agent before its first message

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision
While the default agent is itself a conductor (as when `agent.crew_mode_agent` names it), a new conductor mode chat keeps its agent picker until its first message, listing the agent templates it can move to, and an Incognito or Temporary chat on that agent keeps its usual picker; a conductor mode chat that already has a message stays locked. Chats that were on the agent before the setting named it read as plain chats again once it names another agent, while a chat started under the setting stays in conductor mode unless it is moved before its first message.

## Why
A conductor mode chat is locked from its first message (2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md) and the server already allows the move before it, but with the conductor as the default agent the dashboard offered no control to make it.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/36774#issuecomment-6877987235 -- the maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/36774 -- the report.
