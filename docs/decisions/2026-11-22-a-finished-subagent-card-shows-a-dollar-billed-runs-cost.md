# A finished subagent card shows a dollar-billed run's cost instead of "Credit usage not reported"

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

When a run Kiro Crew starts itself (a managed `spawn_run` subagent, not a harness's native sub-agent) finishes on a harness that bills in dollars, its expanded Subagents card shows the run's dollar cost, formatted the way the chat's own turn line formats it: positive credits keep the credits line, else a positive dollar cost shows the cost line, else the card keeps "Credit usage not reported"; the choice is made from the run's own numbers, never from the backend id, and token or context figures, live figures, a card rebuilt after a gateway restart, the System page's Sessions table, the Telemetry Spend tab, the parent's completion, delivery-failure and wave-digest messages and native sub-agent cards are not part of it.

## Why

- On a dollar-billed harness the chat's own turn line already shows a dollar cost, while the subagent card said its usage was not reported, so a fan-out's spend stayed invisible where the person looks.
- The progress half of the original request is answered on main; the cost half was unanswered only where the run is billed in dollars, which is the setup it was reported on.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/13628#issuecomment-7083595525 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/13628 -- the issue.
