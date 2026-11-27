# A running workflow step shows the tool call it last started

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

While a workflow step runs, its row in the run tree shows the tool call it most recently started, from the run's existing `agent_progress` event: the first tool call at once, a newer one at most every 30 seconds, and nothing after the step finishes; a finished row looks as it did before, and step sessions stay out of the sidebar's session list.

## Why

- A person watching a long step needs to tell a step that is still moving from one that is stuck, on the run page they already watch.
- The event, its fields and the run tree's place for the tool already exist; only the emission was missing, and a bound keeps the run's event stream from growing with every tool call.
- A read-only view of a step's transcript is a later, separate question on the same issue.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/42127#issuecomment-7168932655
- Issue: https://github.com/kirodotdev/KiroCrew/issues/42127
