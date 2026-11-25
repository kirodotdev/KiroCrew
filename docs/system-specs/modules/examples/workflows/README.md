# Dynamic-workflow example scripts

Three illustrative workflow scripts for the DSL specified in
[`../../workflows.md`](../../workflows.md). They exist to show what an authored
`ctx` script looks like: each declares a pure-literal `META` dict and an
`async def workflow(ctx)` entrypoint, and each demonstrates a different
orchestration shape.

**These are research examples, not tests.** Nothing here asserts anything, and no
test collects this directory as test cases. They are still load-bearing: the
Workflows app serves these files to its example picker (`handle_examples` in
`src/kiro_crew/apps/builtins/workflows/server.py`), and `test/test_workflows_app.py`
and `test/test_workflows_authoring_eval.py` require each one to validate, so moving,
renaming or editing one changes what users see and can fail those tests. All three pass
`workflows.validate.validate()`, but validation only proves the sandbox and
authoring shape, not that a script runs green on the shipped host, so read the
per-file caveats below before copying one. See [`../../workflows.md`](../../workflows.md).

| Script | Shape it demonstrates |
|--------|-----------------------|
| `01_review_changes.py` | `ctx.pipeline` with two stages (review then verify) plus a nested `ctx.parallel` fan-out, and structured output via `schema=` |
| `02_loop_until_dry_bug_hunt.py` | loop-until-dry (keep fanning out finders until N consecutive clean rounds) combined with an early stop on `ctx.budget.remaining()` |
| `03_scheduled_triage_native.py` | the ports native to Kiro Crew: `ctx.memory` for cross-run state, `ctx.cron.ensure` to self-reschedule, a per-call `nudge=` dict, and `ctx.send_slack` delivery |

Caveats worth knowing before you copy one:

- `01_review_changes.py` runs end to end against a stub agent. It is the closest
  of the three to a working script.
- `03_scheduled_triage_native.py` references `ctx.memory`, `ctx.cron` and
  `ctx.send_slack`. The shipped gateway wires the `nudge` port and the
  session-control ports (`open_session`, `send_to_session`, `set_session_mode`)
  but not the `memory`, `cron` or `send_slack` ports, so the runner's host-aware surface check rejects this
  script before exec with
  `where="validate"`. It documents the intended native-port surface, and it needs a
  host that wires those ports.
- The `ctx.budget.remaining()` early stop in `02_loop_until_dry_bug_hunt.py` reads
  a counter nothing increments (see the Budget open question in the module spec), so
  that branch never fires.
