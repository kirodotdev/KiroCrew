# Agent Interrupt Controller (`kiro_crew.irq`)

Status: implemented (this PR)
Owners: gateway core (`irq.py`), first probe (`builtin_skills/kirocrew-dev/babysit/scripts/pr_watch.py`)

## 1. Problem

A model turn is the expensive execution context in this system, the way a CPU
is in an operating system. Today it does its own polling: a `monitor_start`
babysit loop wakes on an interval, spends a full turn asking "anything new?",
answers "no", and sleeps. That is the arrangement OS designers abandoned —
having the expensive party poll — and the fix has a name: an interrupt.

Script crons already provide the cheap half. A `script` cron runs a Python
function in a subprocess with no model call at all, and communicates its
verdict to the gateway through one line of JSON: `Skip` (silent), `Report`
(deliver and keep running), `Done` (deliver and remove the job). A poller built
on that costs nothing on a quiet tick.

What is missing is the controller between the two halves. Every poller that
wants the shape re-implements the same machinery, and each piece has a failure
mode that looks like success:

- **Dedupe.** A permanent "already alerted" marker turns one lost delivery into
  a permanently suppressed signal — the script raises `Report` and exits, so it
  can never observe whether delivery happened.
- **State identity.** Two cron jobs watching one subject that share a state file
  let one job's dedupe suppress the other's delivery.
- **Error backstop.** A probe whose command starts failing skips quietly. It
  looks healthy and is blind. Measured on this codebase: a watch running in an
  environment where `gh` could not resolve produced five consecutive silent
  skips before anything was said.
- **Convergence.** Alerting on the first anomaly wakes the agent before the
  subject has settled, producing a turn that cannot decide anything.

The scale of the duplication is worse than it appears from the repository. Only
one poller (`pr_watch.py`) has an in-repo source; roughly fifteen others exist
only as agent-authored copies in the operator's data home, where they are not
version-controlled and not reviewed. Two of those have no persisted state at
all. That is a separate defect — see §7 — but it is why the in-repo poller is
the only one whose contract is correct, and why the correct contract belongs in
a shared module rather than in a file that gets copied.

## 2. Solution overview

`kiro_crew.irq` is an interrupt controller for agent sessions. It owns
everything generic and leaves the caller exactly two domain decisions: what to
poll, and what counts as an anomaly.

| Interrupt concept | Here |
|---|---|
| Interrupt source | a `Probe`, polled once per cron tick |
| ISR | the agent turn the gateway schedules on a wake |
| Masking | time-bounded dedupe, so one condition wakes once |
| Coalescing | several anomalies folded into a single wake |
| NMI | `Severity.NMI` — never delayed by coalescing |
| Clearing a pending bit | epoch reset, when the subject becomes another one |
| Stuck / spurious IRQ | the consecutive-error backstop |
| Unregistering an IRQ line | `Severity.TERMINAL` — the job removes itself |

The division of labour follows Linux's top half / bottom half: the probe is the
top half (fast, cheap, decides only *whether* something happened) and the woken
agent turn is the bottom half (does the real work, may be slow).

The seam is narrow on purpose. A probe returns data; the kernel decides
quiet-versus-wake and is the **only** place `Skip` / `Report` / `Done` are
raised. A probe that raised them would be re-deciding the policy the kernel
exists to own, so a probe does not import them at all.

## 3. Contract

```python
class Severity(Enum):
    WAKE      # an anomaly; masked, and folded into a coalesced wake
    TERMINAL  # subject reached an end state: deliver, then remove the job
    NMI       # an anomaly that bypasses coalescing (still masked)

@dataclass(frozen=True)
class Observation:
    key: str            # dedupe identity within an epoch
    severity: Severity
    brief: str = ""     # operator-facing text if this wakes

@dataclass
class Tick:
    epoch: str = ""     # identity token of the subject THIS tick
    observations: list[Observation] = ...
    pending: int = 0    # sub-observations not yet settled
    fetch_ok: bool = True    # False when the subject could not be observed
    detail: str = ""    # one line echoed into the Skip message

class Probe:
    def identity(self, ctx) -> tuple[str, str]: ...   # (subject_kind, subject_id)
    def observe(self, ctx) -> Tick: ...

def run(ctx, probe, *, realert_secs=6*3600, max_consecutive_errors=6,
        coalesce_secs=240, coalesce_max_secs=1800) -> None: ...
```

A probe filters out conditions the operator already knows about (a check red on
the base branch, a known-degraded dependency) inside its own `observe()` and
simply does not return them. An earlier revision carried an `expected` flag on
`Observation` that the kernel recorded but nothing read — a write with no
reader, which is the shape of state that rots — and it was removed: the probe's
own filter already did the suppression.

`identity()` raises `ValueError` for a configuration that can never become
valid; the kernel converts that to `Done`, because a malformed parameter cannot
self-heal and retrying it forever is a crash loop with extra steps. It is called
exactly once per tick, so the message is parsed once and every parse failure is
inside that conversion.

## 4. Coalescing

A window opens on the first non-NMI anomaly of the current epoch and fires when:

```
elapsed >= coalesce_secs and (pending == 0 or elapsed >= coalesce_max_secs)
```

`coalesce_secs` is a **floor, not a timeout**. The distinction is load-bearing:
immediately after a subject changes epoch its sub-observations may not exist yet
— a freshly pushed commit has an almost-empty check rollup — so `pending == 0`
can be briefly true while nothing has run. Firing then reports a convergence
that never happened. `coalesce_max_secs` is the wall-clock wall for a `pending`
count that never drains, measured from the first anomaly and independent of
`pending`, which is what makes the worst case a delayed wake rather than a
dropped one.

`NMI` and `TERMINAL` bypass the window entirely. For `NMI` the reason is
specific: the conditions classified that way are ones under which waiting
observes nothing further. A pull request with a merge conflict dispatches no
checks, so its `pending` count will never drain and the delay would strand the
operator for the full hard cap on a signal that was already actionable.

**Why coalesce at all**, given that this module's interrupt frequency is
minutes apart rather than a NIC's tens of thousands per second — the volume
argument does not apply. Two other things do:

1. **A wake raised before the subject settles cannot be serviced.** Waking an
   agent about one failing check while twenty-four others are still running
   produces a turn that structurally cannot decide anything: it does not know
   whether more failures are coming or whether they share a cause. That turn
   has no output regardless of what it costs.
2. **The follow-up action is usually shared.** Two failing checks on one pull
   request are fixed by one edit and one push. Servicing them separately means
   two pushes and two full CI rounds; the waste is wall-clock and CI capacity.

So the test for whether to coalesce is **whether servicing the signals shares
an action**, not how many there are. Signals on different subjects never
coalesce, because state is keyed per subject and per cron job.

Deliberately *not* claimed: a token saving. Per-request cached-versus-uncached
token accounting is not currently measurable in this codebase (usage records
carry credits only), and appending a turn does not invalidate the KV cache —
only compaction does. Both justifications above are wall-clock and
decidability arguments, which are verifiable without that instrumentation.

**Cost.** A window cannot open and fire within one tick — `elapsed` is zero at
the moment it opens — so a coalesced wake costs at least one cron interval of
added latency. On a 60-second cron that is at least 60 seconds.
`coalesce_secs=0` disables the window and restores fire-on-first-anomaly, for
callers that would rather be woken early than woken once. It is also the
migration setting for an EXISTING poller moving onto the kernel: it ports with
the window off, which is a pure structural change with no shift in wake timing,
and enables the window as a separate, attributable step.

The first probe is deliberately the exception, named here so the rule does not
read as violated by the change that introduces it: the window exists because of
a defect measured on `pr_watch`, so porting it with the window off would ship a
change that fixes nothing. The rule is about not bundling a timing change with
an unrelated structural move — here the timing change *is* the change.

## 5. Mechanics

**State.** One JSON document per watch at
`<data home>/watch/<subject_kind>/<folded subject>-<digest>.json`, mode `0600`,
written through `atomic_write` (a `mkstemp` temporary with an unpredictable name
plus rename, so a pre-planted symlink at a guessable `.tmp` path cannot
redirect the write). The digest is `sha256(kind#subject#job_id)[:10]`: the cron
job id is part of the identity so two watches on one subject keep independent
memories, and the digest covers the exact unfolded subject id so two ids that
fold to the same filesystem-safe characters cannot collide.

Fields: `epoch`, `alerted` (key → timestamp), `errors`, `coalescing`
(key → brief), `coalesce_started_at`.

**Degradation.** `load_state` coerces every field and returns fresh state for
anything malformed — hand-edited, truncated, or written by another version.
`json.loads` yields arbitrary-precision integers and accepts `Infinity` / `NaN`
literals, so a corrupt timestamp that overflows `float()` or is not finite drops
that entry. The cost of all of this is one duplicate wake; the alternative is a
crash loop, which auto-pauses the cron and takes the watch down entirely.

**Unwritable state directory** never removes or silences a watch. Dedupe
degrades to per-tick repeats and every wake carries a warning naming the
directory. One case is escalated immediately: if the probe is failing *and*
state cannot persist, the counted-threshold alert can never fire because every
fresh process reloads zero, so the kernel reports on the first such tick.

**Masking** re-arms after `realert_secs` rather than acknowledging permanently,
and treats a future timestamp (clock rollback, corrupt state) as stale so it
cannot suppress indefinitely. The error backstop fires at `>=` threshold, not
`==`: an equality gate turns one lost delivery into permanent silence, since the
persisted count passes the threshold and never equals it again. A recovered
streak clears the blind marker so the next streak alerts promptly instead of
inheriting hours of dedupe.

## 6. SDK for app authors

**Provisional.** There is exactly one probe today, so the contract has not yet
been tested by a second consumer, and the pollers it was derived from cannot
migrate until they are under version control (see §7). The surface is published
because an app that wants this shape otherwise hand-rolls the four things this
module exists to get right — but it is published as provisional, not stable:
expect `Observation` / `Tick` to gain fields once a second probe exercises them.

`kiro_crew.irq` is a supported surface for external apps. An app that needs to
watch something ships a script cron, subclasses `Probe`, and gets masking,
coalescing, epoch resets, atomic state and the error backstop without writing
any of them. `__all__` marks the surface; anything outside it is internal.

A complete probe:

```python
import json

from kiro_crew.irq import Observation, Probe, Severity, Tick, run


class DeployProbe(Probe):
    def identity(self, ctx):
        self.env = (json.loads(ctx.message or "{}") or {}).get("env") or ""
        if not self.env:
            raise ValueError('needs {"env": "..."}')   # kernel -> Done
        return ("deploy", self.env)

    def observe(self, ctx):
        status = read_deploy_status(self.env)          # your bounded call
        if status is None:
            return Tick(fetch_ok=False)               # kernel owns the backstop
        if status.finished:
            return Tick(epoch=status.id, observations=[
                Observation("done", Severity.TERMINAL, f"{self.env} deployed."),
            ])
        obs = []
        if status.rolled_back:
            # Nothing improves by waiting: a rolled-back deploy runs no
            # further stages. NMI, so it is neither masked nor delayed.
            obs.append(Observation("rollback", Severity.NMI,
                                   f"{self.env} rolled back."))
        for stage in status.failed_stages:
            obs.append(Observation(f"stage:{stage}", Severity.WAKE,
                                   f"{self.env}: stage {stage} failed."))
        return Tick(epoch=status.id, observations=obs,
                    pending=status.running_stages)


def watch(ctx):
    run(ctx, DeployProbe())
```

Rules an app author must follow:

- Never raise `Skip` / `Report` / `Done`. Return data; the kernel decides.
- A failed observation returns `Tick(fetch_ok=False)`, never an empty `Tick` —
  an empty tick reads as "nothing is wrong".
- Classify as `NMI` only what genuinely cannot improve by waiting. Using it to
  mean "important" defeats coalescing.
- Supply an `epoch` when the subject has an identity token. Without one there
  are no resets, so a re-triggered subject inherits the previous run's masks.
- Keep `observe()` to one bounded call. The top half must not be slow.

A probe may override the coalescing window by implementing
`tuning() -> dict[str, float]`, which the kernel calls after `identity()` so an
override can be derived from the cron message (`pr_watch` does exactly that for
`coalesce_secs`). It is a declared method rather than an attribute the kernel
reads off the probe: an implicit back-channel is a second, undocumented way to
configure the kernel, and the second probe would have copied it.

`coalesce_secs` is the only recognized key, because it is the only one a probe
produces. The kernel has three other bounds and the mechanism is generic over the
mapping, so admitting all four would cost nothing mechanically — but a recognized
key with no producer is a contract nobody has exercised, and the second probe
would build on a shape that was never tested. The other three stay settable
through `run()`'s own arguments, which is how the tests drive the error backstop
and the hard cap on small bounds. The returned value goes through the same bound
validation as `run()`'s arguments, so a probe cannot hand the kernel a number
that makes every tick raise.

## 7. Non-goals and known gaps

- **Not a scheduler.** Cadence, retries and job lifecycle stay with the cron
  service. This module runs one tick.
- **Does not read discussion.** The first probe deliberately does not parse
  reviewer comment bodies. Detecting "something changed and looks wrong" is the
  top half's job; reading carefully is the bottom half's, and a watcher that
  parsed prose would need the judgment this design exists to avoid paying for.
- **Migrating the other pollers is blocked on version control, not on this
  module.** Roughly fifteen script crons live only in the operator's data home
  because their skills instruct an agent to hand-write them, rather than
  shipping them as repository assets the way the babysit skill does. They cannot
  be migrated by pull request until that is fixed, and fixing it is worth more
  than the migration: it is why their contracts drifted.
- **The probe's own external call is not verifiable in an agent sandbox.** The
  first probe's `gh` resolution refuses inside the tool sandbox, whose user
  namespace maps every uid to `nobody`, so probe-level end-to-end behaviour must
  be verified by a real cron tick. This is not incidental: the defect that
  motivated the coalescing window was found by running a watch for four
  minutes, and was *not* found by a full read of the same file, because it is a
  property of real CI timing rather than of the code.
