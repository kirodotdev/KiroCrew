# Crew Mode is a composer switch to the conductor template

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-27

## Decision

**Crew Mode** is a per-chat switch in the composer. Turning it on runs that chat on
the conductor template named by `agent.crew_mode_agent` (default
`kirocrew-conductor`); it is the ordinary agent switch, with a persisted stamp so the
chat is known as Crew Mode. There is no `"crew"` slot mode and no new chat-creation
field. The switch can be turned off until the chat's first message and is locked
after it.

A conductor's **crew members** are ordinary sessions. They are kept out of the
person's own session lists, Recents and folders, and are shown under their root in
the chat's **Crew** panel instead. A crew member's tool approval is decided by its
conductor. Every shipped conductor is Crew Mode; the design lands as a series of
small pull requests, each inert until the UI that uses it arrives.

User-facing text says **Crew** and **crew member**.

## Why

- Running a goal too big for one chat meant opening sessions by hand and losing
  track of which was blocked, waiting or done. One switch turns "here is the goal"
  into a crew that reports back.
- It is a UI over an agent switch, so memory, history and workspace stay what they
  were; nothing is migrated or deleted.
- Crew members out of the person's lists keeps the sidebar the person's own.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/24860 -- the design (seven numbered
  decisions), triaged `tier:T4`, and the maintainer's acceptance comment
  https://github.com/kirodotdev/KiroCrew/issues/24860#issuecomment-6459373703.
- https://github.com/kirodotdev/KiroCrew/pull/24895#issuecomment-6459374089 -- the
  maintainer restating the decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/24895 -- part 1, the pull request
  that adds this entry.
