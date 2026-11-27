# A prompt the dashboard re-sends after a failed attempt tells its hooks it is a replay

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-17

## Decision

When a dashboard chat sends a prompt again, unchanged, after the prompt hooks
already ran for an earlier attempt of it that did not finish the turn (it
failed, was refused, or stopped early), its `UserPromptSubmit` hook stdin is
marked `"replay": true` and every hook still fires, rather than firing the hook
once per message. A continuation Kiro Crew writes to resume a turn is never
marked, even when re-sent, and an edited queued re-send is a new prompt and is
not marked.

## Why

A marker leaves each hook free to choose: skip recording a second person turn,
or still process the text. Firing the hook only once per message would change
what every hook sees, and would hand them context from a failed attempt that
can be up to the whole retry ladder old.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37978#issuecomment-6944672086
- https://github.com/kirodotdev/KiroCrew/issues/37978
