# Admission and resource governance

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Admission and resource governance

**Before the instruments: the binding constraint is usually SUPPLY, not
capacity, and nothing below can see that.** A label-filtered candidate list
overstates available work by a large factor. On a repository with a fast PR flow,
an open item with a clear mechanical mechanism is usually ALREADY being worked,
so the population surviving the real gate — no covering PR, no live routing
comment — is a small fraction of the list. A fleet sized from the list therefore
runs mostly on items that should never have been admitted, and the duplicate
dispatch presents as the workers' fault.

So **never pad the fleet.** Admitting a covered or human-routed item to fill a
slot is the same failure as admitting a refactor, and it is WORSE than leaving
the slot empty: a claim on work nobody should be doing tells every other
operator to stay away from it. When the queue thins, the remaining value is in
driving the open items to green, not in finding one more item — and an idle slot
is a supply reading to report, not a gap to fill.

**Delivery capacity is the primary instrument.** The probe's `OK` line carries
`deliver init-timeout <a>, watchdog <b>`: `a` counts sessions in the cycle whose
tail shows an initialize timeout, `b` counts turns ended by the stall watchdog.
**Either counter appearing twice in one cycle means stop dispatching.**

Load and memory are secondary: both can read healthy while turns are killed by
the stall watchdog and sessions fail to initialize, because what saturates first
is request service and test-runner scheduling and neither field reports it.
Admission keyed on load alone therefore keeps dispatching into a fleet that
cannot deliver, and the failures then present as the workers' fault.

| Signal | Posture | Do |
| --- | --- | --- |
| Delivery counters both 0; load and memory healthy | `ample` | Dispatch up to `max_in_flight`, into what this cycle's ledger reclaim left free (see "How the ledger behaves"). |
| Either delivery counter ≥2 in one cycle | `saturated` | Stop dispatching until two consecutive clean cycles. In-flight work continues — what is short is delivery, not compute, so stopping the workers would waste their progress for nothing. |
| Load or memory tight, delivery clean | `tight` | No new dispatches; ask heavy workers to defer gate runs; postpone items marked expensive. |
| `critical` from `resource_status` | `critical` | Halt admission; `session_stop` the most expensive in-flight items (record as reclaim, not failure); handle violators; wait for recovery before resuming. |

Confirm with `resource_status` before batch dispatches. A delivery-saturated
fleet is also why an approval or transport outage presents as mass worker failure
— see outage recovery.

**Every ownership class is REPORTED; only `cwd=fleet` is ENFORCED with a stop.**
Keying this response on a single actionable-or-not boolean is what makes it
unanswerable, because it forces an unclassified line to be either a false stop or
a silent drop. Key it on the class instead:

| Class | Response |
| --- | --- |
| `cwd=fleet` | The heavy response: `session_stop` that worker, a ~5min cooldown, then restart it with the targeted-tests directive re-injected in the seed. |
| `cwd=unknown` | The probe could not attribute this line to the fleet: either the pid's cwd was unreadable, or the process incarnation changed between the probe's reads (a recycled pid), so the record's fields cannot be trusted as one process. NON-stopping: re-inject the directive to the owning session WITHOUT stopping it, and record the line. Never a stop, never a silent drop — a stale record can never trigger a stop against an innocent worker. |
| no `cwd=` field at all | The probe predates classification, so the line carries no ownership. Attempt attribution ONCE at action time (read that pid's cwd): resolved inside a fleet worktree → treat it as `cwd=fleet` and take the stop response above; not resolved → record the count and re-inject the directive fleet-wide as a reminder, stopping nobody. See the legacy-line fallback below. |
| `cwd=foreign` | Count only. Not the fleet's process to police, and never grounds for stopping a session. |

The no-field row is settled by measurement rather than by argument: a banned pid
is typically gone by the time anyone reads its line — cwd unreadable, cmdline
absent, the pid resolving to nothing — so a line carrying no ownership field
cannot identify a violator even in principle. Enforcing on it generates false
stops; dropping it removes the guard; a recorded count plus a fleet-wide reminder
is the response that is neither.

`unknown` is still not `foreign`, and the distinction survives the split: one is
a process whose owner could not be determined, the other one determined not to be
yours. Collapsing them would either police somebody else's host or discard a real
violation.

**Legacy lines: attempt attribution once, and enforce only on what resolves.**
For the no-field class only, read that pid's cwd at the moment you act. If it
resolves inside a fleet worktree the line is `cwd=fleet` after all and gets the
stop; if it does not resolve, take the recorded, non-stopping reminder. Failing to
resolve is the EXPECTED path, not an error condition — see the measurement below —
so the fallback is where most legacy lines land.

That ordering is what makes the whole cell answerable: nothing is enforced on
ownership that is missing or unknown, because the stop fires only on ownership
that RESOLVED; and no line is ever unmatched, because the reminder is always
available. One read, one class, at action time. Scan-time classification stays
the primary mechanism.

**The cwd class is captured when the process is scanned, not looked up when you
act on the line, and that ordering is what makes the class usable at all.** The
runs this rule catches are short-lived, and the gap between a probe returning
and a conductor acting on its output reliably outlives them: by then
`/proc/<pid>/cwd` is unreadable, `cmdline` is gone, and the pid resolves to
nothing. A line carrying only a pid is therefore unattributable, which leaves
"ignore it" as the only safe response — and an operator who learns to ignore one
such line has learned to ignore the whole class. The verdict has to travel on the
line, recorded while the evidence still exists. That is also why the legacy-line
fallback above is a bounded best effort and not the mechanism: it attempts the
same read, expects it to fail, and declines to guess an owner when it does.

What the pytest rule flags is **a run whose worker pool bypasses the budget**:
an explicit numeric `-n` of two or more (`-n 4`, `-n=4`, `-n4`, `-n 32`,
`--numprocesses 2`), because `xdist_budget.py`'s memory hook only ever sizes
`auto`: a number still claims the host's shared slots, but the host's live free
memory is never consulted about it. `-n auto`, `-n logical` and a bare `pytest` are quiet: the
bare form inherits the project's `addopts`, which supply `-n auto`, so its pool
is budgeted by the same hook. `-n0` and `-n 1` are quiet too, as single-process
runs with xdist inactive. Where `-n` is given twice the last one wins, as pytest
resolves it. The brief still mandates `-n0` on a worker's own test runs -- a
budgeted pool is still a pool -- but the probe only reports the shape that
escapes the budget. The other banned shape is a full-suite runner invoked with
no file argument.

A zero banned count is not proof that no such run exists. Known misses: an
unquoted separator (`|`, `&`) inside an option value ahead of `-n N`, a pytest
console script installed with `pip install --user`, and a count carried in the
`addopts` of a file named by `-c other.ini`. On a host with no `/proc` the scan
does not run at all, and the count prints `n/a` rather than `0`.

Standing constants: `session_ceiling` machine-wide, `-n0` on every worker test
run, targeted tests only, ≤2 subagents per worker. `-n0` rather than a small
`-n <N>` because `auto` is memory-budgeted by a conftest hook while an explicit
count bypasses that budget — so the safe form is zero workers, not a hand-picked
few.

## Reading an EMPTY answer under fleet load

A dozen pollers on one account trip SECONDARY (concurrency) throttling long
before quota runs out, and that failure is dangerous by shape rather than by
size: a throttled coverage query returns nothing, and nothing reads as "no PR
covers this item" — which manufactures exactly the duplicate dispatch the claim
gate exists to prevent. Four rules:

- **`gh api rate_limit` cannot see a secondary throttle.** Read against a fleet
  that is actively being throttled it reports full budget with zero used. It does
  not give a weak signal, it gives a CONFIDENT ALL-CLEAR THAT MEANS NOTHING,
  which is the most dangerous shape a diagnostic has. Never cite it as proof you
  have budget, and never point a worker at it as a diagnostic.
- **The signals that DO separate the cases** are stderr and the exit status on
  the actual call, plus a control query. `claim_preflight.py` already keys on the
  first — a non-zero `gh` exit becomes `UNKNOWN`, never an empty answer, which is
  why its verdicts are trustworthy under load. Any sweep you write by hand needs
  the same, and it needs the control too.
- **A control whose expected value you can state IN ADVANCE beats one you merely
  expect to be non-empty.** Predict the number — this item's comment count is 3,
  so after posting it must read 4 — and a correct answer proves the channel is
  live AND catches a stale or wrong-object read, which a merely-non-empty control
  does not. Every all-clear sweep gets one, including your own audits: a checker
  whose all-clear is indistinguishable from its own failure is worse than no
  checker, because it manufactures confidence.
- **On a GraphQL or search throttle, switch TRANSPORT before backing off.** REST
  frequently answers while GraphQL refuses the same question, and the `search`
  budget is far smaller than core, so `--search` queries starve first under a
  wide fleet. The fix for `UNKNOWN` is often a different transport rather than
  patience.
