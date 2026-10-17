# Crew members message each other through `session_send`; there is no separate member inbox subsystem

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-09-13

## Decision

A crew member reaches another member through the existing `session_send` path: a narrow allow in front of the creator fence, the steer path for a member that is busy, and a distinct "from member" row in the receiving thread. Kiro Crew does not build a separate inbox subsystem for members (typed envelopes, an inbox store, a scheduler, a wake runner, a projection UI), and member-to-member messaging carries no runtime hop counter or per-pair rate limit.

## Why

- Issue #10527 asked for exactly the narrow allow. The design discussion then grew it into an inbox model (RFC revision 2 in #10528, implementation #10535, #10543, #10559; 6384 additions and 31 review rounds on M0 alone).
- After design review the maintainer closed all four: "Closing by owner decision (2026-09-13): after design review we are not pursuing a separate inbox subsystem. The minimal path -- allow member→member `session_send` behind the existing creator fence, render session_send rows distinctly in the Members thread, and cap relay hops -- supersedes this." On the issue he then set the hop cap aside as well: "**Considered, not adopted (owner: trust the agents):** a hop counter in the envelope with a cap and a per-pair rate limit. No runtime anti-loop for member<->member DM; the agents are trusted to recognise and stop a ping-pong themselves, and the prompt-level rule is enough."

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/10527#issuecomment-5657419591 -- the maintainer's statement of the minimal scope.
- https://github.com/kirodotdev/KiroCrew/pull/10535#issuecomment-5657413052 -- the closing comment on the inbox store PR; #10543 and #10559 were closed the same way.
- https://github.com/kirodotdev/KiroCrew/pull/10528 -- the RFC whose second revision proposed the inbox model, closed unmerged.
- https://github.com/kirodotdev/KiroCrew/pull/10690 -- the follow-up implementing the minimal path.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
