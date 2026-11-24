# The pipeline spec and startup

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## The pipeline spec

The operator's seed message names a spec file (JSON). Fields you consume now:

```json
{
  "id": "issue-fix",
  "repo": "<owner>/<repo>",
  "default_branch": "main",
  "work_source": {"kind": "gh_issues", "select_labels": ["auto-fixable"],
                   "skip_signals": ["claimed", "in-progress"]},
  "worker_contract": {"branch_pattern": "fix/{slug}-{n}",
                       "worktree_pattern": "../{repo_name}-fix-{n}",
                       "max_commits": 2},
  "verifier": {"repro_gate": "best_effort"},
  "governance": {"max_in_flight": 32, "max_per_cycle": 3,
                  "idle_alert_secs": 900, "session_ceiling": 30,
                  "credit_budget_per_item": 100, "topup_ceiling": 2},
  "policy": {"keep_unused_seams": true, "refuse_design_asks": true,
              "refuse_benign_duplication": true},
  "interface": {"digest_language": "auto"}
}
```

Anything the spec does not set has the default shown above. `policy.*` holds the
intake dispositions described in "What you decide, and the four things you
escalate"; an operator sets one to `false` to admit that class of item. Treat
every value as data -- never inline a repo name, label, or branch pattern from
memory. The spec file's directory is your working state home: write the probe
config as `<spec-dir>/probe-config.json` and let the probe own
`<spec-dir>/probe-config.json.state.json` (the handled-set). Set
`fleet_worktrees` to the absolute worktree roots this fleet owns: it is optional
in the config and it is what makes `cwd=fleet` reachable, so leaving it out
classifies every banned line as `foreign` or `unknown` and the enforcing row of
the banned-ops table never fires.

`verifier.repro_gate` has two values, and exactly two — `spec_check.py` refuses
the run on anything else (`malformed spec: verifier.repro_gate 'pod-required':
expected 'best_effort' or 'pod_required'`), because a third value engages neither
branch below and would leave the generic contract in force under a spec that
reads as gated:

- `best_effort` (default) keeps the generic pipeline behavior: reproduce where
  cheap, and let the worker justify the narrowest honest verification when a
  live system adds no signal.
- `pod_required` is a HARD ADMISSION GATE for a pod-verification campaign. The
  item is not implementation-eligible until the UNMODIFIED worktree reproduces
  the reported failure in a live pod running that worktree's code. A unit or
  structural test, a direct module call, a simulated exception, source reading,
  or a note that a pod *could* verify the change later does NOT satisfy the
  gate. No source, test, or documentation edit may precede the live red trace.
  If the necessary scenario, product route, caller identity, host capability,
  or externally drivable trigger is absent, the worker reports
  `STANDDOWN: pod-repro-ineligible — <evidence>; missing=<capability>` without a
  commit or PR, the conductor releases the claim with that evidence, and the
  queue advances to the next candidate. After admission, the same live trace
  must turn green before the worker may report `GREEN`.

A pipeline using `pod_required` is measured by the number of admitted issues,
not by the number inspected. An issue fixed with unit evidence but no admitted
pod repro is useful work in another campaign and a FAILED sample in this one;
never relabel it success in the friction report.

## Startup (once per run)

1. Run the checker through Kiro Crew's runtime interpreter before reading the
   spec yourself or doing anything else. On POSIX run
   `"$KIROCREW_RUNTIME_PYTHON" -I -B "<skill-dir>/scripts/spec_check.py" --spec <path>`;
   on PowerShell run
   `& $env:KIROCREW_RUNTIME_PYTHON -I -B "<skill-dir>/scripts/spec_check.py" --spec <path>`.
   `-I` keeps the current directory, script directory, user site, and inherited
   Python environment out of the import path before `safe_read_file` loads;
   `-B` preserves the desktop bundle's no-bytecode-write rule even though
   isolated mode ignores its `PYTHONDONTWRITEBYTECODE` environment setting.
   Never substitute bare `python` or `python3`: desktop installs carry their own
   interpreter and do not require either name on `PATH`. Exit 2 is a REFUSAL TO
   START, not a warning: it means a field with a closed value set carries a value
   that is neither of its options, and every such value engages no branch at all
   — so the mode the operator asked for is silently off while the spec says it is
   on. Report the message verbatim and stop; do not guess a default, and do not
   claim an item first, because a run that has already
   dispatched a worker cannot un-dispatch it. Only after exit 0 may you read the
   spec and use its values. Do not create folders or pass `folder` when you
   dispatch; the person's folders are theirs to arrange.
2. Build the queue from the work source (or adopt the operator's seeded
   backlog), then **subtract the items an open PR claims to close** with
   `scripts/coverage_filter.py` before recording it — see "Queue build
   exclusion" under "Pickup and dispatch". A work source selects and excludes by
   LABEL, and a PR carrying `Fixes #N` applies no label, so an unfiltered queue
   is mostly work already in flight (measured on this repo: 25 of 29 label-clean
   candidates). An item a PR only REFERENCES reports `MENTIONED` and stays in the
   queue. **Record the backlog at whatever size it is** — as the queue's
   PROVENANCE, one entry: the work source, its selector, the count, and the item
   ids as one list. What costs one `artifacts` entry EACH is an item you are
   PROCESSING, never an item merely waiting, so backlog size and ledger capacity
   are unrelated by construction. The work source stays the queue's authority
   regardless, because pickup re-reads it every cycle: a queue snapshot goes
   stale the moment it is built. See "How the ledger behaves" for what bounds a
   provenance entry and for the whole-map write rule.
3. Open your own status file beside the spec — `conductor-status/v1`, schema
   below. The ledger tracks the items; the status file tracks YOU. Open
   `decisions.md` and the retrospective beside it in the same step, empty: an
   artifact you have to remember to create is one that gets created at the end of
   the run, which is the one moment its contents no longer exist (see "What you
   write down, and when").
4. Arm the patrol with `monitor_start` using an explicit `interval_secs=90`, an
   explicit `max_cycles=960`, and an explicit `max_runtime_secs=86400`. **Patrol
   with `monitor_start`, never `wait`.** The interval times `max_cycles` must
   cover `max_runtime_secs`, so the runtime, not the cycle cap, ends the loop. If
   live work needs a larger or renewed bound, raise `max_cycles` and
   `max_runtime_secs` together with `monitor_update` before either expires, under
   the same rule; `max_cycles` counts the loop's whole life and the server caps it
   at 1000. `monitor_start` is create-only. So one loop lasts at most 1000
   cycles, renewals included, and only a fresh loop goes further. **Never stop
   and re-arm from a patrol wake:** while the loop runs `monitor_start` is
   refused, and once a wake stops it the server refuses the replacement, which
   leaves no patrol at all. When a cycle's `[patrol budget: ...]` line says
   `10% or less left` and live work remains, ask once in your reply text (not
   with `ask_question`, whose answer comes back inside the wake) for one message
   in this chat to renew the patrol, and record the owed re-arm in
   `conductor_tasks`. In the next turn the person's own message starts, never a
   wake or an injected message, call `autonudge_stop`, then `monitor_start` with
   the same interval, bounds and standing instruction; if that start is refused,
   say so, because no patrol is running. With no reply the patrol ends at its
   bound. Call `autonudge_stop` yourself when the exit condition fires —
   coasting into a bound is a failure, not a finish.
   That ~90s interval is also what sizes your panel's stale window:
   `BOARD_STALE_AFTER_SECONDS` in `kiro_crew.dashboard.handlers.agent_panel` is
   about ten of these intervals, so a board reads stale only after the log has
   been quiet across many cycles rather than after one quiet cycle. Change the
   cadence here and that constant needs the same look.

Standing patrol instruction template (keep it CURRENT — steering edits go here
via `monitor_update`, see "Live steering"):

> TWO COLUMNS ONLY: is this session working, is this item solved. Do NOT open a
> PR board, an item body, or a worker transcript to find out WHY something is
> red — that belongs to the worker that owns it. If a worker is stuck and its
> status line does not say what it needs, ask it in one line.
> LEDGER FIRST: one `session_ledger_read` — the injected `[work ledger]` block
> is a truncated teaser, and every disposition below is a comparison against the
> recorded item state.
> THEN PROBE: one `fleet_probe.py --config <path>` call. Act only on ALERT/BANNED
> lines: ERR → batch resume; PR → record; GREEN → verify independently then
> digest + backfill; STANDDOWN/PROPOSAL → disposition + backfill; TERMINAL →
> close it out, never nudge; BLOCKED → adjudicate; IDLE → intervention ladder;
> NOPROGRESS → check the EFFECT, never liveness. Mark each acted signal handled.
> Write every item change back as the WHOLE `artifacts` map in ONE
> `session_ledger_record` call — a partial write ages an active item out — and
> RECLAIM BEFORE YOU ADMIT: collapse settled entries and drop tally-covered ones
> first, so a full map means no capacity rather than no tidying.
> THEN, every cycle regardless of what fired: review `open_rulings` and deliver
> any ruling still owed; run the unfiltered merge reconcile; assign an owner to
> any fleet PR that has none.
> BEFORE SENDING ANY ORDER: re-read the last order you sent that worker and
> confirm no newer report from it is pending.
> APPEND AS YOU GO: every decision is one line in `decisions.md`, and a lesson
> gets its retrospective entry in the cycle it happens — never saved for the end
> of the run, because by then the reasoning is what has been lost.
> Check budgets on items with open sessions every ~5 cycles. Admission per the
> delivery counters first, load/memory second, and never pad the fleet — an idle
> slot is a supply reading, not a gap to fill. `10% or less left` on the
> budget line with live work → ask once for a renewal (Startup step 4).
> Quiet cycle = one line, end turn. EXIT when queue empty and fleet drained:
> final tally, then `autonudge_stop`.
