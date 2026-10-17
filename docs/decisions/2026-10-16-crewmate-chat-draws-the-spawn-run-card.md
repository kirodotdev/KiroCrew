# The crewmate chat draws the spawn_run launch card, and no other tool row

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-16
Supersedes: 2026-10-09-crewmate-chat-draws-messages-only.md

## Decision

A crewmate's chat draws the person's rows, the crewmate's own speech, and ONE tool row: the `spawn_run` launch, as the same live sub-agent card the chat page draws, whose counts and per-worker state update in place while the workers run and return. Every other tool row stays hidden, as does every runtime `notice` row and every injected envelope, the `[Subagent completion event]` included; the rest of the superseded entry stands.

## Why

- Delegated work outlives the crewmate's own turn: it says "on it", ends its turn, and the workers run for minutes. The typing bubble and its caption mount only while the crewmate's own turn is live, so a chat that drew messages only was perfectly still for all of that time and read as frozen; people stopped the crewmate or re-sent.
- The launch card is the one row that is alive across that gap. Alternatives that kept the chat to messages only (a one-line "waiting on 2 workers" caption, iMessage-style receipt lines, an expandable caption, an auto-opened side panel, a per-turn work card) were each prototyped as real-component screenshots and rejected by the maintainer; the existing card, unfiltered, was chosen.
- The superseded entry's Decision section is about runtime `notice` rows and stands; its first sentence said "nothing else", which this entry narrows by exactly one row kind.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/21142#issuecomment-6211655779 -- the decision, recorded on the pull request that applies it, on the maintainer's instruction.
- https://github.com/kirodotdev/KiroCrew/issues/21138 -- the report, the prototypes considered and the maintainer's choice.
- https://github.com/kirodotdev/KiroCrew/pull/21142 -- the change that applies it.
