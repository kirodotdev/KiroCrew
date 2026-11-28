# A schema step's refused directive is shown as a log line on the run

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-15

## Decision

When a workflow step with a `schema=` calls a directive only a person's turn may
issue and is refused, the refusal is shown on the run's event stream as an
ordinary `log` line, `agent a<N> "<label>": <the refusal>`, while that call runs,
on both executors, capped per run at the failed-task lines' limit but counted
apart from them. The step's validated value and its `error` stay as they are,
and a step without a schema keeps leading its text with the refusal.

## Why

- The run's event stream is where a person watching a run already reads its
  progress, and the Workflows run view already shows its `log` lines.
- It needs no new event type and no change to what `ctx.agent()` returns, so the
  script still gets exactly the value its schema promised.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34359#issuecomment-6892000365 --
  the maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/34359#issuecomment-6773423621 --
  the question it answers: which surface shows the refusal, and in what shape.
