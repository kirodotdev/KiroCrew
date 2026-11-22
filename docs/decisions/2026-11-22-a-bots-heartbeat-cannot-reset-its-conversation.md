# A bot's heartbeat cannot reset its conversation

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

`reset_conversation` on a turn the gateway starts in a bot's chat (its heartbeat, a goal check-in or a worker report) is refused and does not clear the chat's context; the tool's reply and description name these turns, and the Bots guide says a heartbeat wake cannot reset it.

## Why

- Letting such a turn reset a bot's conversation would widen a deliberate restriction, which needs a design sign-off from a maintainer.
- A bot that read "Conversation reset requested" called the tool at the end of three heartbeats while its chat grew. The model reads only the tool's own text, so that text names the refusal.
- A bot's chat can be kept small by setting its own "Summarize old messages at" threshold on its Profile, which leaves other chats alone.
- Whether a turn another session sent, or a cron job a chat owns, may reset its conversation is not decided here (#17471, #18337).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37072#issuecomment-7073092704 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/37072 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/37131 -- the per-bot threshold.
