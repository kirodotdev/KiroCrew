# A repeated proposal shows the waiting change card again at the end of the chat

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision
When a Bot asks again for a change whose approval card is still waiting in the same chat, the one card is shown again at the end of the chat and its first place keeps a "shown again below" line; the repeat is still refused and no second card is made.

## Why
The person could not reach a waiting card from the latest message and the Bot could not help; the newest turn re-states the ask, as 2026-09-13-options-are-read-from-the-newest-reply-only.md settles for options, and the duplicate refusal stays so two copies of one change never race each other's apply.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/36894#issuecomment-6880532615 -- the maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/36894 -- the report.
