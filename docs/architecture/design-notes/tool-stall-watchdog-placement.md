# Where a stall detector belongs: in the read loop, or out of band

Kiro Crew has one stall detector, and it lives inside the code path it is
supposed to be watching. `AcpSessionHandle._dispatch_events` is an async
generator; its watchdog logic is the `except asyncio.TimeoutError` arm of

```python
msg = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
```

An async generator advances only when a consumer pulls it. When the consumer
parks inside its `async for` body, the generator is frozen at the `yield` and
that `except` arm never executes again — for the whole turn. The detector is
downstream of a failure mode it is meant to detect.

This note states the criterion for which failures belong in the read loop and
which must be judged from outside it, what an out-of-band hook may honestly
read, and how out-of-band action stays out of the in-band recovery path's way.

## The failure that made this concrete

`_dispatch_events` yields a permission event (`action == "permission"` →
`_build_permission_event`). The consumer, `dashboard/chat_runner.py`, handles it
in place at the `EVENT_PERMISSION_REQUEST` branch and awaits the user's decision:

```python
outcome = await asyncio.wait_for(fut, timeout=_approval_window)
```

At the time of the incident that window was a hardcoded `7200.0` — the same
value as the turn ceiling — so an unanswered prompt burned the entire turn. The
comment beside it already named the shape of the hazard: *"the turn then parks
here until the 2h timeout, holding the slot lock and silently dropping inbound
messages."* While that await is pending, the generator is suspended at its
`yield`. The frame-gap watchdog is not slow, not mis-configured, and not making
a wrong verdict — it is not running.

**Bounding that window did not close the class.** The literal is now
`tool_approval_timeout_secs()` (default 600s, cross-field clamped below the turn
ceiling, with a fast decline when no budget is left), so this particular await
releases in minutes instead of hours. The structural fact is unchanged: for as
long as it is pending, the generator is frozen and the frame-gap arm cannot run.
Every consumer-side await in that branch has the same property, and the fix
bounded one of them rather than making the detector reachable. That is what this
note is about.

The evidence is the absence of a log line. Between 08-08 16:00 and 08-09 01:00
the journal holds 611 lines for the affected unit and zero `Tool stall on
session` WARNINGs, while the commands issued in that window are provably never
executed (their redirection targets were never created on a tmpfs `/tmp` that
was not remounted). Had the arm run even once, a session with no tool child
process would have drawn an `UNKNOWN` verdict and acted at the suspect window,
emitting that WARNING. Silence is the proof of non-execution, not of a healthy
session.

## 1. The placement criterion

The general rule, which does not depend on this incident:

> **A detector must not be downstream of the failure it detects.** Put a check
> in the read loop when the failure leaves the read loop itself scheduled and
> waiting; put it out of band when the failure can freeze, delete, or starve the
> very code that would notice — or when the verdict requires comparing state
> the loop cannot see.

Applied to `_dispatch_events`, its arm runs only if two things hold: a consumer
is actively pulling it, *and* it is blocked on `self._queue.get()`. That gives a
clean partition.

**In-band class — upstream is quiet, our own consumption is healthy.** Every
existing branch is of this kind, and each belongs where it is:

| Failure | Signal it reads |
|---|---|
| Tool dispatched, no frames | `_tool_dispatched` + `last_data_ts` gap |
| Model silent mid-generation | `_stale_eligible` + runtime `_last_activity` |
| Runtime process died | `None` sentinel on the queue |
| Cancel sent, never acked | `_cancel_ts` past `_cancel_grace_secs` |
| Security filter aborted the tools | `_is_tool_interrupted_marker` on a text frame |

These are cheaper and far better informed in band. The arm holds
`_inflight_tool`, the per-frame `last_data_ts`, and the oracle — none of which
survive outside the turn. Nothing here should move.

**Out-of-band class — our own consumption is what stopped.** Four sub-classes,
distinguished because they need different remedies:

- **(a) Consumer parked at a `yield`.** Any await in the `async for` body. The
  approval future is the one that bit us, but it is not the only await in that
  branch: `post_linked_approval` is a network call, `hooks.on_tool_call` runs
  arbitrary user hook code, and the MCP Apps spool read is thread-offloaded.
  Each is a candidate for the same freeze, and each new await added to a
  consumer branch enlarges this class silently.
- **(b) Consumer task gone or never resuming.** `slot.task` cancelled without
  draining, an exception swallowed above the loop, a slot lock held by a turn
  that will not finish.
- **(c) Event loop starved.** A synchronous blocking call on the loop thread —
  a `/proc` read, a file lock, a hook that is a `def` rather than an `async def` —
  stops in-band code because it stops everything. **A loop-resident observer does
  not cover this class**, and it is a mistake to think otherwise: the cleanup
  loop is an `asyncio` task on the same loop, so it is frozen too. Only a thread
  or a separate process could watch it, and the cheaper remedy is not to block
  the loop in the first place — bound the outbound sends, keep user hook code off
  it. Listed here because it is a real class of the failure, not because the hook
  below answers it.
- **(d) Verdict is inherently cross-session or whole-process.** RSS growth,
  orphaned MCP children, pool leaks, N sessions each holding a lock. A
  per-session generator that can only see itself cannot form these judgements
  at all. This is the class `SessionWatchdog` was built for, and the reason the
  right home for (a), (b) and (d) is the same dispatcher rather than a new
  mechanism.

Two consequences worth stating plainly, because both were tempting and both are
wrong:

- **This is a second detector, not a relocation.** Moving the frame-gap logic
  out of band would trade a blind spot for a much larger loss of fidelity.
- **The two detectors need distinct names.** "Watchdog" already means two
  unrelated things in this codebase, and that collision is itself a recurring
  source of confusion. This note uses **frame-gap watchdog** for the in-band arm
  and **turn-progress hook** for the out-of-band one.

### The third shape: a pump task

Class (a) has an option the two above do not cover, and it deserves evaluating
on its merits rather than being skipped. Put a **pump task** between the
generator and the consumer: the pump owns the `async for`, pushes each event
onto a queue, and the consumer reads from that queue. The generator is then
pulled by the pump, so a consumer-side await no longer freezes it and the
frame-gap arm keeps running — with its full in-flight fidelity — during exactly
the parked-consumer case that bit us. If it works, class (a) disappears and the
out-of-band hook's job shrinks to (b)–(d).

It is not a free win, and the reasons are worth having on the table before
implementation review picks a shape:

- **It makes the arm run when its verdict is meaningless.** The dominant instance
  of class (a) is awaiting human consent, and there the backend is *legitimately*
  idle — it is waiting for our permission response. A reachable arm consults the
  oracle at exactly that moment, finds no shell child because the tool has not
  started, draws `UNKNOWN` or `DEAD`, and `DEAD` acts immediately regardless of
  the window: it cancels a turn that is correctly waiting for a person. The pump
  therefore needs the arm suppressed at precisely the moment it made it
  reachable, and once that suppression exists the net gain for this case is
  close to nothing.
- **Backpressure is NOT the objection**, though it looks like one. A pump would
  leave undrained frames sitting in a second queue, but `AcpRuntime._reader_loop`
  is already an independent task filling an *unbounded* per-session queue that
  the generator only drains — so frames already accumulate during a park, today,
  with no pump. The pump relocates that growth rather than introducing it.
- **It splits "where the turn is" in two.** The generator yields a terminal
  event and returns; recovery relies on that event being the last thing the
  consumer sees. Behind a pump, the arm can emit a tool-stall terminal event and
  return while the consumer is still parked on an *earlier* event's approval, so
  the terminal event queues up behind a decision that is now moot. Ordering and
  coherence have to be designed, not assumed.
- **It relocates the don't-fight-recovery problem rather than removing it.**
  A reachable arm will cancel the session (`_end_stalled_tool` calls `cancel()`)
  while the consumer sits on the approval future, racing the human's click. That
  is the same reasoning §3 develops for out-of-band, now needed in band too.
- **It does not subsume the out-of-band hook.** A starved event loop (c) stops
  the pump as well; a departed consumer (b) leaves the pump draining into a
  queue nobody reads, which is a new leak rather than a fix; and cross-session
  verdicts (d) are still unreachable from inside one session.
- **It is the larger, riskier change**, though not for the reason it first
  appears. The handle has exactly one `async for` over `_dispatch_events`, so the
  edit is small; what grows is semantics — the pump's own lifecycle and
  cancellation become new surface, and terminal-event ordering has to be designed
  rather than inherited.

So: the pump is the more *principled-looking* fix for (a), and it was weighed
rather than skipped — but it was not chosen. Its one benefit evaporates on the
case that actually bit, it needs the arm suppressed at the moment it made it
reachable, and it leaves (b) and (d) needing an out-of-band detector regardless.
The criterion and the constraints in §2 and §3 hold either way; see
"Decisions taken".

## 2. What the out-of-band hook may honestly read

The out-of-band carrier already exists and needs no new machinery.
`SessionWatchdog` (`watchdog.py`) is a stateless dispatcher over a fixed list of
`CleanupHook`s, ticked from `SessionManager._cleanup_loop` on its own
`asyncio.wait_for(shutdown_event.wait(), timeout=interval)` sleep, where
`interval = max(timeout // 6, 60)` (`SessionManager._cleanup_loop`). It depends
on no session's read loop. Three hooks are registered today: `idle_expiry`,
`orphan_mcp`, `rss_threshold`.

**Readable at that layer.** `SessionManager._sessions` under `self._lock`, and
per `_Session`: `semaphore.locked()` (the only in-flight signal at this layer),
`last_used`, `created_at`, `prompt_count`, `consecutive_failures`, `agent`,
`approval_policy`, the Slack `queue`, plus `get_pid(key)` and the process tree.

**Not readable, and no amount of plumbing makes it honest.** Everything
per-turn and in-flight: which tool is running, `last_data_ts`, `_inflight_tool`,
the ACP queue depth, the oracle's verdict for the current tool. Those are
`_dispatch_events` locals and `AcpSessionHandle` privates whose meaning is
scoped to one turn. A hook that reached for them would be reading a value with
no defined lifetime.

**Not reachable at all, structurally.** Anything in `DashboardState`. The
watchdog is in the session layer; slots, `slot.running`, `slot._approval_futures`,
`slot._tool_stall_retries` are in `dashboard/state.py`, and `SessionManager`
holds no reference to it. The session layer must not import the dashboard.

**The blind spot that matters — and why it was not a plumbing problem.** From
outside, a healthy 40-minute turn and a turn parked on an approval for 40 minutes
look identical: `semaphore.locked()` is True for both, and `last_used` is bumped
only by `get_or_create()` (the "Known limitation" note in `session.py`'s module
docstring), so it does not advance during a turn at all.

The tempting reading is that the signal has to be *injected* from the layer that
can see it — a consumer-supplied reader in the shape of
`SessionManager.on_session_expire`, or a progress ledger both layers write to.
That reading is wrong, and it is worth recording why, because it sends the
implementation across a layer boundary it does not need to cross.

Nothing was unobservable. The clocks were simply **locals in the generator's
stack frame**, and no observer can read another coroutine's locals. That is an
implementation choice, not a property of the system. Promoting them to attributes
on the handle — `parked_for_secs()`, `parked_since`, `awaiting_permission` — makes
the turn's park readable by anyone, with no cross-layer injection, no second
source of truth about whether a turn is alive, and no dashboard dependency in the
session layer. The three facts the hook needs are then all owned by the object
that already knows them:

- **How long the consumer has held the current event**, which is the park itself
  rather than a proxy for it. Deliberately *not* the ACP frame clock: a parked
  consumer has no frames arriving, so the frame clock reports the parked state as
  merely quiet — the same conflation that produced the blind spot.
- **Whether the turn is awaiting a human**, tracked by the handle because it is
  the thing that yielded the permission and will be told the answer. Waiting on a
  person is a legitimate state, not a stall.
- **Whether a turn is in flight at all**, which `semaphore.locked()` already
  answers at the session layer.

A callback remains in the design, but for *delivery* rather than observation:
`on_stuck_turn` exists so a surface that can reach a user decides what to do with
the signal. That keeps the reporting decision out of the session layer without
making the session layer depend on the dashboard to see anything.

The resulting verdict vocabulary is deliberately coarser than the in-band
one — one class, roughly *"this turn has produced nothing observable for N
seconds and is not waiting on a human"* — rather than the
`WORKING`/`UNKNOWN`/`DEAD`/`STUCK_INPUT` lattice. That is a feature, not a
concession: it keeps liveness judgement (see non-goals) out of a layer that
cannot see the evidence it would need.

## 3. Not fighting the in-band recovery path

In-band recovery already exists end to end. `_end_stalled_tool`
(`session_handle.py`) cancels **this session only** on a bounded 5s budget —
never the process, because co-tenant sessions share the runtime — and yields one
terminal `EVENT_COMPLETE` carrying `STOP_REASON_TOOL_STALL` plus the tool title,
redacted command, and evidence. The consumer's `STOP_REASON_TOOL_STALL` branch
in `chat_runner.py` then, for `_prompt_depth == 0 and
slot._tool_stall_retries < 3`, increments the
budget and `queue_insert`s a continue-nudge (`SYNTHETIC_RECOVERY_KIND` /
`RecoveryPayload.CONTINUATION`) naming the stalled tool. It no longer replays the
original message verbatim — that older routing re-ran the stalling command and
burned three cycles into "Session stuck". At `>= 3` it stops and says so.

Five rules keep the out-of-band path from colliding with it.

**The in-band detector has precedence, and it is checkable.** If the generator
is advancing, the turn-progress hook must not act — the frame-gap arm owns that
session and is better informed. Externally-visible progress moving is the
evidence that it is advancing; that is why the progress clock, not the frame
clock, is the input.

**Out-of-band action breaks the block; it does not end the turn.** This is the
load-bearing rule. Terminal events stay exclusively in band, because that is
where `_end_stalled_tool` and the `STOP_REASON_*` seam live, and a second
terminator would either double-emit or land a cancel ack on a turn that has
already completed and get misclassified. For the parked-consumer class the thing
to unblock is the consumer's await, not the generator — and at the approval site
that resolution path already exists and already has three callers (HTTP
slot-approve, the Slack click, the 7200s timeout). Resolving the pending future
lets the existing flow resume on its own. The out-of-band hook's job is to make
the frozen await finish, after which the in-band machinery works again.

**Recovery must not spend a budget it does not own.** `_tool_stall_retries` is
the in-band budget. An unblock consumes none of it, because it produces no
re-prompt: the user-visible outcome is the one the 7200s timeout already
produces (the approval is auto-declined and the turn proceeds), just sooner. If
some future class genuinely must end a turn from out of band, it routes through
the same stop-reason seam and therefore the same budget check — never a direct
`queue_insert`.

**Hold the existing non-lethality bar.** Every in-band probe is designed so a
wrong verdict costs a regeneration and never a session: a stale probe's cancel
ack is reclassified to `STOP_REASON_STALE_RECOVER` precisely so an oracle
mistake cannot surface as a silent user cancellation. Out of band must match
that. A wrong unblock costs one auto-declined tool approval, which the user can
retry. Killing a healthy long turn is not an acceptable failure mode, which is
also why the hook must reuse the guards the sibling hooks already established:
skip `semaphore.locked()` sessions it has no positive evidence against, collect
candidates under the lock and act after releasing it, and re-verify session
identity under the lock before acting (`reset(expect_session=…,
skip_if_busy=True)`, as `_rss_threshold_check` does).

**Log the decision not to act, at WARNING.** In-band already sets this
precedent: `_log_working_deferral` starts at INFO and escalates to WARNING once
idle passes the lower of `_WORKING_WARN_AFTER_SECS` and a fraction of the turn's
own deadline, rate-limited to one line per interval. It was INFO-only when this
was diagnosed, which is why the diagnosis had to rest on the absence of a
*different* log line — and that nearly did not work. A turn-progress hook that
declines to act past a threshold must say so at the same level, or it
reproduces the same ambiguity one layer out.

## 4. Non-goals

Explicitly out of scope here. Each is its own work item, and folding any of them
into this note would blur what is being decided.

- **The liveness verdict algorithm.** `liveness.py`'s mapping from process state
  to `WORKING`/`UNKNOWN`/`DEAD`/`STUCK_INPUT` — including whether an alive
  process blocked on a socket read should keep drawing `WORKING` indefinitely —
  is a separate workstream. This note takes the algorithm as given and asks only
  *where a check runs*. Notably, the incident above needed no liveness change:
  the arm never ran.
- **The approval window's own length.** How long a chat turn may park waiting
  for a human is `agent.tool_approval_timeout_secs`, bounded below the turn
  ceiling. Whether its default is right, and whether the dashboard's approval
  implementation should converge with the Slack/cron path that applies a fast
  deny, are decided there rather than here.
- **Watchdog window values.** The idle windows and their clamp against the
  prompt timeout are settled; picking numbers is not this note's business.
- **The turn-timeout ceiling.** Whether `CHAT_TURN_TIMEOUT` should change is
  orthogonal; a stuck turn that is detected does not need a longer ceiling, and
  a longer ceiling does not detect anything.
- **Relocating the frame-gap watchdog.** Not proposed. See §1.

## Decisions taken

All three questions this note opened are settled, and the hook they describe is
implemented (`SessionManager._stuck_turn_check`, registered on `SessionWatchdog`
alongside `idle_expiry` / `orphan_mcp` / `rss_threshold`).

**Where current behavior is authoritative:** `session.md` and `acp-client.md`, not
this note. A behavior change is contractually required to update the owning module
spec in the same commit; nothing forces a rationale record to keep up. So the
sections below describe *why* the shape was chosen and what was rejected — read
the specs for what the code does today, and treat any disagreement between them
as this note being out of date.

1. **Class (a) is handled out of band, not by a pump task.** The pump's stated
   benefit — keeping the in-band arm reachable during a park — turns out not to be
   one for the case that actually bit: during a human wait the backend is
   legitimately idle, so a reachable arm forms a verdict about a situation it does
   not model and can cancel a turn that is correctly waiting. See §1's third
   shape. The pump also leaves (b) and (d) untouched.
2. **The progress signal crosses no layer boundary.** Neither a reader callback
   nor a ledger was needed: the clocks were locals in a generator frame, so
   promoting them to attributes on the handle made the park readable directly.
   `on_stuck_turn` survives as a *delivery* seam, not an observation one.
3. **Awaiting a human is never a stall, but it is visible.** The wait is bounded
   by `agent.tool_approval_timeout_secs`, so acting on it here would put two
   components on different budgets racing to end the same wait. It is excluded as
   a mask rather than ignored, because a hook that cannot tell "parked on consent"
   from "parked on a hung send" cannot report either one honestly.

What deliberately did **not** follow from these decisions: the hook reports and
does not terminate. Ending a live turn stays with the in-band path that owns the
terminal-event seam and the non-lethal continue-nudge, and from the cleanup loop
there is no way to know what a park is blocked on, so there is no unambiguous
action to take. Turning a silent freeze into a named one is the whole of the
diagnostic gap that this was written to close.

Still open, and belonging to the *cause* rather than the detection: every
transport's per-event outbound send is an unbounded network await inside the turn
path, and `hooks.on_tool_call` is synchronous on the event loop. Both widen class
(a) — and (c), which as noted above no loop-resident observer can cover.
