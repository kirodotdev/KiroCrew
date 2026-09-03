---
name: babysit
description: Use when a user asks to babysit, monitor, keep checking, keep an eye on, or report when a pull request, CI run, ticket, deployment, or other changing target reaches an outcome.
inject_on_trigger: false
triggers: babysit, keep checking, keep an eye on, let me know when, monitor pull request, monitor pr, watch pull request, watch pr
tags: [skill, kirocrew, monitor, babysit]
---

# Babysit

## Overview

Use a structured monitor when its typed provider observes every fact needed to
decide the objective. Kiro Crew probes before invoking the model, so unchanged
polling does not consume agent turns. Use the finite legacy path when required
evidence is outside the structured provider.

Structured watches can be created only from dashboard, Slack, and Discord
sessions. On Webex, use the finite legacy path even for a supported pull request;
Webex does not have structured wake delivery and completion correlation.

## GitHub pull request: one bounded watch

Call this once with the canonical pull-request URL when review readiness depends
only on pull-request lifecycle, mergeability, review decision, unresolved review
threads, and check conclusions:

Two `monitor_start` parameters are worth naming because their defaults are easy
to inherit by accident:

- `gate` (default true) holds a cycle back until the thing you are watching has
  actually changed. Pass `gate=false` when the loop's duty is to act **while** the
  subject is quiet — refresh a heartbeat file, chase a reviewer who has not
  replied, keep a branch rebased on a moving base — because continued silence is
  invisible to the observation. A gated loop is never starved: it is delivered
  anyway after enough quiet intervals.
- `banner` — a short line such as `watching PR #123 for CI` — changes only what is
  stored and broadcast as a transcript row; the model still receives `message`
  whole every cycle. Pass one whenever `message` is long, or a multi-KB
  instruction is re-stored on every cycle. It is refused with a 400 on a
  channel-bound loop (`slack:` / `discord:` / `webex:`); `monitor_update(banner="")`
  clears one.

### For a public GitHub pull request, prefer the structured monitor

`monitor_watch(kind="github_pull_request", target=<PR URL>,
objective="review_ready", interval_secs?, max_runtime_secs?, max_agent_turns?,
max_tokens?, max_provider_errors?, wake_instructions?)` probes the pull request
cheaply and wakes the owning session only when a new revision needs action —
unchanged, pending, retry and terminal probes spend no agent turn, unlike a
`monitor_start` cycle which spends a full turn every interval. Put the compact
act-on-wake instruction in `wake_instructions`. Inspect it with `monitor_inspect`
(no arguments — it resolves your own session) and end it with
`monitor_stop(reason?)`, which retains the terminal outcome for inspection. One
structured monitor per session, occupying the same slot as a `monitor_start`
loop. Use `monitor_start` instead when you need to ACT most cycles, or when what
you are watching is not a public GitHub PR.

That retained record keeps occupying the slot, so arming a monitor for a
DIFFERENT subject in the same session is refused (`the session's stopped
automation is retained as evidence`). Only its owner can end it, by pressing
**Clear stopped monitor** in the session-automation popover (**Clear stopped
goal** in the legacy goal view). Read that refusal as a request to the user, not
as a transient error to retry: nothing armed, so say so instead of reporting a
monitor you do not have.

### `interval_secs` counts between the loop's own cycles

Each delivered cycle's countdown starts when that cycle's turn **ends**, so
the real cadence is `interval_secs` + however long each cycle's work takes. A
300s interval with 5-minute checks wakes you roughly every 10 minutes. Size it
for the gap you want *between* cycles. User messages in the session never
stretch this: a due fire waits for the user's turn to end, then delivers.

### You must stop the loop yourself

`max_cycles` (default 24) is a **runaway backstop, not a finish line**. A loop
that coasts into its cap did not complete — it ran out of rope, and whatever
it was watching is still unresolved. Evaluate the exit condition every single
cycle and stop deliberately.

**This is the dominant failure, not a rare one.** Measured on a real loop store
(4 loops, 84 delivered cycles, 14.9h wall clock, 7.5h of model turn time):
**4 of 4 ended at exactly their cap** — 24/24, 20/20, 28/28, 12/12 — with
`stopped_reason` either empty or `cycle_cap`, and **0 of 4 carried an
agent-supplied stop reason**. None set `max_runtime_secs`, so cycle count was
the only bound on spend.

The reason this is expensive rather than merely untidy: the cap is reached the
same way whether the loop **converged early** and then re-polled for nothing, or
**never converged** and stopped with the work still unresolved. The two are
indistinguishable from outside — the loop simply goes inactive — so a loop that
stops only at its cap tells you nothing about which happened, and bills you for
the difference.

### Stall tripwire — stop on no-progress, not just on success

An exit condition keyed only on *success* cannot end a loop that is stuck, and
stuck is the common case: a blocker needing a human decision, an upstream
outage, a flake nobody owns. So carry a second exit condition.

Each cycle, record the **progress key** — `pr_status.py --json` emits every
field of it:

| field | why it is in the key |
|---|---|
| `head_sha` | a new head means you pushed; work happened |
| `failing_checks` (sorted, workflow-qualified) | *which* checks are red, not how many — qualified by workflow because two workflows can publish the same check name, and a name-only list stays identical when one starts failing as the other stops |
| `checks_failing` | the count, as a cheap scalar |
| `readiness_kind` | distinguishes running from failing from unpublished |
| `exit_code` | collapses the whole verdict |
| `status` | the verdict *reason* — a conflict and a failing check are both exit `20` and can carry an identical check set, so without this a changed blocker extends a stall streak instead of resetting it |

If the key is byte-identical for **3 consecutive counting cycles** and you pushed
nothing in that span, the loop is not making progress. **Stop it**: report what is
blocking, why you could not move it, and what decision you need, then call
`autonudge_stop` with that as the reason.

**Only a settled cycle counts.** A cycle whose poll exits `10` is reporting work
still in flight, and waiting is exactly what the loop is for — so an exit-`10`
cycle neither counts toward the tripwire nor resets it; skip it and move on.
**Both exit `0` and exit `20` are settled, and both count.** A PR can sit at exit
`0` indefinitely — green checks, but not review-ready because a human-owned thread
or an unanswered advisory concern is outstanding — and a tripwire that counted only
`20` would never notice that kind of stall. Exit `2` is an environment fault:
escalate it rather than counting it.

This matters because a running PR yields a byte-identical key every cycle by
construction (`failing_checks` empty, `readiness_kind` `running`, `exit_code` 10),
so counting those would fire the tripwire after ~15 minutes of ordinary CI on a
repo where a single gate can legitimately run an hour. Skipping rather than
resetting is deliberate too: a PR that flickers between running and blocked would
otherwise reset forever and never be recognised as stuck. What bounds a genuinely
endless wait is not this tripwire but `max_runtime_secs`.

**Persist the key AND the streak count — session memory is not durable enough.**
A stalled loop is precisely the long-running case that walks into compaction (see
above), which can summarise away the counter at the moment it matters; the loop
then coasts to its cap exactly as before, which is the failure this tripwire
exists to prevent. Storing only the last key is not enough: that tells the next
cycle what the previous key was but not whether this is match one, two or three,
so the streak itself would still live in memory and still be lost. Write BOTH to
one file — the key plus an integer count — and read it first on every settled
cycle, then:

- key matches the stored key → increment the count, write it back, trip at 3;
- key differs → overwrite with the new key and a count of 1.

**Put that file where the loop's own state lives, not in temp.** A babysit runs
for hours and `TMPDIR` is periodically reaped by the OS, which would silently
reset the streak — the same erasure as compaction, just a different eraser. Use
the data home beside the loop's stop sentinel, i.e.
`${KIROCREW_HOME:-$HOME/.kiro/crew}/workspace/.babysit-key-<loop-id>`, which is the
convention `stop_sentinel_path` already follows and is not machine-cleaned. Write
`$HOME`, not `~`: a tilde inside a parameter-expansion default is not expanded, so
the literal `~` would survive and the state would land in a `~/` directory
relative to the current working directory — lost the moment that directory
changes.

Nothing in the monitor engine observes the key today, so that file is the only
durable record; moving the counter into the loop store, so the engine itself could
stop on it, is the follow-up that would make this enforceable rather than
advisory.

**The key is GitHub-only today.** `pr_status.py --json` is its only emitter, so on
GitLab or Bitbucket you must first derive an equivalent key from that host's own
verdict fields (the table above). The 3-cycle rule means nothing until something
emits a comparable value.

Two things deliberately stay OUT of the key:

- **Finding counts.** A finding you rebutted or deferred keeps being re-raised,
  so a key including it never stabilises and the tripwire never fires. Worse,
  the count moves when a bot merely re-words a comment. Read findings to decide
  *what to do*; do not let them decide *whether you are stuck*.
- **Unresolved-thread counts.** `?` means "could not establish", and a transient
  API blip would read as progress.

Escalating early is correct — a stalled loop does not become unstuck by being
re-run 18 more times; it becomes unstuck when a human answers the question. This
is **not** licence to park on a *fixable* blocker: the tripwire fires only when
nothing changed **because there was nothing you could change unilaterally**. A
diagnosed, verified fix gets applied and pushed, which moves `head_sha` and
resets the count by construction.

Deliberate stops are the goal. `autonudge_stop` should carry a real sentence
naming the exit condition, the tripwire, or a terminal PR state. A loop whose
store row later reads `cycle_cap` is a defect in that loop's instructions, not
a completed job.

### Context grows every cycle

Each cycle appends a full turn — tool calls, CI output, diffs — to the **same**
session. That shared context is the point of a same-session loop, but nothing
bounds it: long babysits walk into compaction, which can summarise away the
very instructions the loop keeps re-injecting. Keep per-cycle output minimal.

### Verify the loop armed — the return string is not evidence

`monitor_start` returns an acknowledgement whether or not the loop was
actually armed. The applier runs after the tool returns, and its failure
message is not visible to you, so a confident-looking success string is
consistent with nothing being scheduled at all.

Confirm against state, not the reply: read `~/.kiro/crew/autonudge.json` (or
`GET /api/autonudge`) and check the loop is present, then that `cycle_count`
advances on the next cycle. If it never appears, no monitoring is running —
fall back to an in-turn `wait`+poll loop and tell the user monitoring is not
active.

If `monitor_start` explicitly reports it could not arm, believe it. That
message is distinct from the transient MCP reconnects you retry through — do
not write it off as flakiness.

## Decision table

- User is waiting and total time < 30 min → `wait` + poll, no loop.
- "Babysit / monitor / keep checking" in THIS conversation, in a phase where
  you ACT most cycles (fixing findings, pushing revisions) → `monitor_start`.
- **Pure-watch phase of a PR babysit** — waiting on CI or reviewers, nothing to
  do until a signal → still `monitor_start`, and name the pull request in the
  instruction **by full URL** — `https://github.com/<owner>/<repo>/pull/<N>`.
  That form is the only one that selects a subject: a bare `PR #123`, or the
  `owner/name#123` shorthand, deliberately refuses inference, so the loop stays
  on the plain timer and every interval spends a turn. The user will usually say
  "babysit PR #123"; you write the URL. A loop naming one public GitHub pull
  request that way is gated by default:
  quiet cycles cost no agent turn, and it wakes only on a real change. The
  `pr_watch` script cron (below) is now only for what that cannot reach --
  an enterprise host, or detection with no owning loop.
- Reacting to review feedback or CI on a PR → `monitor_start` or in-turn
  `wait`+poll for the active-fix phase. **Never an agent (LLM) cron, never
  HEARTBEAT.md** (see below). The `pr_watch` script cron is fine: it is the
  detector, not the reactor — the woken agent turn does the reacting with
  this session's own trust.
- Work belongs in a fresh isolated session each cycle, and needs no tools that
  require approval → `cron_add`.
- Cleaning up after a merge you have already verified → `cron_add`, as a
  `script` cron at roughly a 5-minute interval.
- External system will call back → `register_hook`.

### Watch mode — a manual cron for what the default gate cannot reach

**Read this first: you probably do not need this section.** A `monitor_start`
loop whose instruction names ONE public GitHub pull request is already gated --
it observes that pull request each interval with one bounded `gh` call and
re-injects your message only when it actually changed, so a cycle where nothing
changed costs no agent turn. That is the default, on every arming surface, with
no steps to take. Use it, and skip to the end of this section.

Watch mode is the manual version, and only three situations still need it:

- the pull request is on an **enterprise host** -- the gate pins public GitHub,
  because choosing a host from data is not something a watch message may do;
- there is **no owning loop** to gate: you want detection without a babysit
  session, e.g. a fire-and-forget notification;
- you need the cron's own knobs -- `known_reds` to suppress failures inherited
  from the base branch, `note` to carry text into the wake, `wake_on_green`.

If none of those apply, arming this cron gives you a second watcher on the same
pull request, and the two will wake you separately for the same event.

```
script cron (zero tokens, every ~5 min)
  ├─ nothing changed / checks still running     → silent, no delivery
  ├─ merged / closed                            → final message, cron removes itself
  └─ unexpected state                           → ONE agent turn in THIS session
       (CONFLICTING · new failing check not in known_reds · all green
        · a comment or review someone else posted)
```

The reply is a pending application request, not proof that the monitor armed.
End the turn so the session-aware consumer can apply it. The operator can then
confirm it in the dashboard. The agent cannot inspect after ending that same
turn; at the start of a later user/wake turn, call `monitor_inspect({})` before
acting or reporting. Its retained session-bound state is authoritative and it
takes no monitor id, target, or session key.

The server performs ordinary probes. Unchanged state, provider retry, success,
and terminal blockers use zero model turns. A new actionable fingerprint wakes
the session at most once with a compact, bounded summary. Only then fetch logs,
comments, or diffs needed to act. The token cap applies only when the provider
reports usage; `token_usage_known` exposes whether it did. Positive runtime and
completed-turn caps remain hard fallbacks. Provider errors are also bounded.

A terminal success uses zero model turns, so a structured watch does not create
a final reporting turn. If the user requires a final report or notification even
when no action is needed, use the finite legacy path.

Use `monitor_update({...})` without an id to change `interval_secs`, positive
budgets, or `wake_instructions`. Those edits preserve the comparison baseline;
changing `target` or `objective` starts a new baseline. Terminal records are
read-only, so start an explicit new watch to restart. Use
`monitor_stop({"reason": "User ended the watch."})` for a deliberate stop; the
retained outcome is `user_stop`. `autonudge_stop` remains a compatibility alias.

The GitHub provider does not observe generic issue or pull-request comments or
advisory review findings that are not represented by a review thread or check
conclusion. When the user's definition of readiness depends on that evidence,
call the finite legacy path directly. Do not ask `monitor_watch` to represent
evidence its typed provider cannot observe.

## Example: legacy comment-aware watch

Use a prompt loop when `monitor_watch` does not support the target/objective, or
when required evidence is outside the structured provider:

```text
monitor_start({
  "message": "Watch https://github.com/kirodotdev/KiroCrew/pull/123. On each injected cycle, inspect current review comments and checks. Act only on a real change. If the pull request is ready, terminal, or needs a human decision, report the outcome and call autonudge_stop with a reason.",
  "interval_secs": 300,
  "max_cycles": 24,
  "max_runtime_secs": 14400,
  "gate": false
})
```

Keep `gate: false` for this fallback: provider-fact gating cannot observe the
missing evidence and could suppress cycles while new comments await review.
The finite loop checks that evidence on every scheduled cycle.

All three limits must be nonzero; use a larger finite value, never `0`, for a
longer request. Every delivered legacy cycle is a full agent turn that extends
session context and spends model/tool tokens even when nothing changed. The reply
is only an arm request; verify the loop through the dashboard monitor/goal-loop
surface or `GET /api/autonudge`.

Legacy tool calls remain under normal PreToolUse governance and approval policy.
An unattended Slack or Discord turn has no blanket grant: approval may be
requested, rejected, or time out, and the loop records an approval stall instead
of silently succeeding.
