# A saved task-plan workflow is refused while Task Runner is off

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

While Task Runner is switched off in Library, running a saved `task-plan`
workflow (the Run button in Customize > Workflows, `/workflow <name>`, and the
`workflow_run` tool) is refused with the same `409 app_disabled` answer and
message as the other Task Runner starts; saved Python workflows are not
affected, and switching Task Runner back on restores the run.

## Why

- A saved task plan is planned and executed by Task Runner itself, so it is the
  same start through another door.
- With Task Runner off, a failed step cannot be retried and a paused run cannot
  be resumed.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33879#issuecomment-6757515930 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/33879 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/33951 -- the sibling fix that left
  this question to the issue.
