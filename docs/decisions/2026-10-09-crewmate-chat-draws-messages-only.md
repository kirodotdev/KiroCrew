# The crewmate chat draws messages only; runtime notices go to the crewmate, not the person

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-09

## Decision

A crewmate's chat draws the person's rows and the crewmate's own speech, and nothing else: no runner or gateway `notice` row of any kind, whatever its tag. Anything the runtime has to say about its own machinery is delivered to the crewmate as an injected message on its next turn, or logged, never drawn in the crewmate chat. The Sessions page keeps drawing notices.

## Why

- The crewmate chat is a conversation between a person and a named crewmate. A yellow bar saying "Automation update refused: a stopped loop cannot be restarted by updating it" is the runtime talking to the agent; the person reading the chat can do nothing with it, and it reads as an error in a chat that is working as designed.
- The current filter drops notices by one tag (`empty_turn`) and deliberately keeps every other notice "because the person may have to act on it". The maintainer rejected that premise for this surface: the crewmate acts on it, not the person.
- The arm refusal today reaches nobody who can act: the MCP tool answers "requested" before the gateway applies the directive, so the refusal lands after the agent's turn ended. Feeding it into the next turn is what lets the crewmate call `monitor_stop` and `monitor_start` itself.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/18669 -- the issue recording the decision and the change that applies it.
- https://github.com/kirodotdev/KiroCrew/issues/18232 -- the earlier report asking to draw the empty-turn give-up notice again in the crewmate chat; this decision settles that surface the other way.
- https://github.com/kirodotdev/KiroCrew/pull/16429 -- the change that introduced the tag-only filter in `crewmateBubbles.ts`.
- https://github.com/kirodotdev/KiroCrew/pull/18672#issuecomment-6091359535 -- the maintainer's on-record restatement on the pull request that adds this entry.
