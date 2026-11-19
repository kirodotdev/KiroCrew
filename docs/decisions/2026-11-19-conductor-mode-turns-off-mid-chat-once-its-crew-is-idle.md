# Conductor mode can be turned off after the first message once its crew is idle

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-19
Supersedes: 2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md (in part: "is locked after it"), 2026-11-02-an-ultra-mode-row-shows-only-what-needs-attention.md (in part: "It is one way")

## Decision

A conductor mode root chat that has a message can be turned off from its composer's
bolt while no item on the chat's work ledger is open and no crew member is running.
While work is open or running the bolt is held and its tip says why, and the gateway
refuses the switch (`409 crew_mode_locked`). Turning it off switches the chat back to
the default agent and closes its Board and Dashboard tabs the way the turn-off before
the first message does, and ends the chat's work-ledger patrol. The work ledger is
kept, so turning conductor mode on again shows the same tasks. Everything else in the
superseded entries applies unchanged.

## Why

- A chat whose goal is done had no way back to a plain chat but a new chat, which
  loses the conversation's place.
- While a task is open or a crew member runs, leaving conductor mode would strand
  that work without the conductor that reads its reports, so the lock stays then.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39557 -- the request, filed for the
  maintainer: "Allow turning off conductor mode after one session has already been
  set. Make the button clickable when no workers are doing stuff."
- The pull request that adds this entry carries the maintainer's restatement.
