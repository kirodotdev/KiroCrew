# A Remote Crew pane's Pop out opens a desktop window that frames the crew's pop-out page

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

In the desktop app, Pop out from a Remote Crew's pane opens a desktop window of the hub dashboard that shows the crew's own pop-out page inside it, the way the pane shows the crew, for chat, artifact, terminal and file pop-outs alike.

## Why

- It keeps the desktop shell's rule that a window opens only pages from its own gateway.
- It needs no change to the desktop shell itself.
- Crews opened through the relay are not part of this decision; they get their own follow-up issue.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35985#issuecomment-7074137167 -- the maintainer's decision comment.
- https://github.com/kirodotdev/KiroCrew/issues/35985 -- the request this decision answers.
