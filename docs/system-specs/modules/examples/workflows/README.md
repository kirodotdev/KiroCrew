# Dynamic-workflow example scripts

Three illustrative workflow scripts for the DSL specified in
[`../../workflows.md`](../../workflows.md). They exist to show what an authored
`ctx` script looks like: each declares a pure-literal `META` dict and an
`async def workflow(ctx)` entrypoint, and each demonstrates a different
orchestration shape.

**These are research examples, not tests.** Nothing here asserts anything, and no
test collects this directory as test cases. All three pass
`workflows.validate.validate()`, but validation only proves the sandbox and
authoring shape, not that a script runs green on the shipped host, so read the
per-file caveats below before copying one.

| Script | Shape it demonstrates |
|--------|-----------------------|
| `01_review_changes.py` | `ctx.pipeline` with two stages (review then verify) plus a nested `ctx.parallel` fan-out, and structured output via `schema=` |
| `02_loop_until_dry_bug_hunt.py` | loop-until-dry (keep fanning out finders until N consecutive clean rounds) combined with an early stop on `ctx.budget.remaining()` |
| `03_scheduled_triage_native.py` | the ports native to Kiro Crew: `ctx.memory` for cross-run state, `ctx.cron.ensure` to self-reschedule, a per-call `nudge=` dict, and `ctx.send_slack` delivery |

Caveats worth knowing before you copy one:

- `01_review_changes.py` runs end to end against a stub agent. It is the closest
  of the three to a working script.
- `02_loop_until_dry_bug_hunt.py` chains `.then(...)` onto a `ctx.agent(...)` call.
  No such method exists on the coroutine or anywhere in `workflows/`, so that
  `parallel` thunk raises, resolves to `None`, and the surrounding unpack fails
  once a finder actually returns bugs. Treat the loop-until-dry and budget-guard
  structure as the lesson, not that line.
- `03_scheduled_triage_native.py` references `ctx.memory`, `ctx.cron` and
  `ctx.send_slack`. The shipped gateway wires only the `nudge` port, so the
  runner's host-aware surface check rejects this script before exec with
  `where="validate"`. It documents the intended native-port surface, and it needs a
  host that wires those ports.
- The two `ctx.budget.remaining()` early stops read a counter nothing currently
  increments (see the Budget open question in the module spec), so on today's
  engine that branch never fires.

Two module docstrings here say the DSL "is not implemented yet". That predates the
engine; `src/kiro_crew/workflows/` implements it.
