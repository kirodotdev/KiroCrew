# Babysit PR Watch

Status: implemented (this PR)
Owners: babysit builtin skill (`builtin_skills/kirocrew-dev/babysit/`), on the
interrupt controller in `irq.py` (see `agent-interrupt-controller.md`)

## 1. Problem

A PR babysit spends most of its life waiting. The `monitor_start` loop that
drives it re-injects a full agent turn every cycle — session context included —
and on a quiet PR the turn's entire output is "nothing changed". Measured on
real babysit sessions: roughly two thirds of cycles were pure status checks
that needed no judgment, while a saturated session pays a context-window-scale
input bill to produce each of them. The check itself is deterministic
(`gh pr view` and a diff against the last observation); only the *reaction*
to a change needs a brain.

## 2. Solution overview

Split detection from judgment:

- **Detection** becomes a zero-token `script` cron
  (`babysit/scripts/pr_watch.py`) polling one PR every few minutes. Quiet
  ticks raise `Skip` — no delivery, no tokens, no transcript growth.
- **Judgment** stays in the babysit session. On an unexpected state the
  script raises `Report`, and the existing script-cron delivery path
  (`_deliver_script_result`) injects the brief into the session that armed
  the cron **as a real agent turn** (queued if a turn is running, spawned if
  idle). No gateway changes: the wake primitive already exists.
- **Terminal states** (merged, closed) raise `Done`: one final message, and
  the cron removes itself.

The babysit skill's decision table gains a watch-mode branch: `monitor_start`
for phases where the agent acts most cycles, the watch cron for pure-wait
phases, and an explicit composition pattern for switching between them.

The generic half of that split now lives in `kiro_crew.irq`, the interrupt
controller: state identity, masking, epoch resets, the coalescing window and
the error backstop. This file is its first probe and owns only the two GitHub
decisions — how to observe a pull request, and what counts as an anomaly. The
probe never raises `Skip` / `Report` / `Done`; it returns a `Tick` of
`Observation`s and the kernel raises the verdict.

## 3. Wake predicates

Each fires once per head SHA per dedupe window (a force-push resets the
memory immediately; while a condition persists on the same head, the alert
re-arms after a few hours — the script cannot observe delivery, so dedupe is
time-bounded rather than a permanent acknowledgement, and a delivery lost to
a gateway failure costs a bounded delay, never a permanently suppressed
signal):

| Reason | Trigger | Why it needs a brain |
|---|---|---|
| `conflict` | `mergeable` CONFLICTING / `mergeStateStatus` DIRTY | Checks freeze on a dirty PR; waiting observes nothing. Rebase needed. |
| `new-red` | A check in a failing bucket whose name is not in `known_reds` and not yet alerted for this head | Read the job log / reviewer comment for the current head; run conclusions alone are unreliable. |
| `ready` | Zero pending and zero failing after the `known_reds` filter, non-empty rollup | Verify reviewer verdicts on this head and tell the user. Suppressed with `wake_on_green: false`. |
| `watch-error` | `gh` failed several consecutive ticks | The watch is blind (expired auth, network); it says so once instead of rotting silently. |

`known_reds` carries the check names that are red on the base branch itself —
the inherited-breakage filter that a human babysitter applies mentally. The
watch never wakes for them, and counts them as green for the `ready`
predicate. The probe filters them inside `observe()` and does not return them
as observations at all. `known_reds` matches EITHER the bare check name (what
an operator reads off GitHub's UI) or the workflow-qualified spelling the alert
key uses, so a hand-written allow-list works while two workflows sharing a
check name stay distinguishable.

`new-red` and `ready` are **coalesced**; `conflict` is an `NMI` and fires
immediately. The distinction is not importance but whether waiting can produce
more information: a dirty PR dispatches no checks, so its `pending` count never
drains and delaying the conflict signal would strand the operator on something
already actionable.

Coalescing was added because of a measured defect, not a hypothesis. Before it,
`new-red` fired on the first failing check with no gate on `pending`. On this
repository — roughly sixty-five checks finishing over about twenty minutes — one
head woke the operator twice within four minutes, at 34 seconds for a body gate
and at 4m55s for a reviewer lane, while twenty-four checks were still running.
Neither wake could act: the first turn did not know whether more failures were
coming, and both would have been serviced by the same edit and the same push.
Notably a full read of the same file concluded it had no defects — the fault is
a property of real CI timing, not of the code, and only running it exposed it.

Deliberately not watched: reviewer comment bodies, marker freshness, human
discussion. Parsing those requires the judgment this design exists to stop
paying for per-cycle; the woken agent does that reading, exactly as in a
manual cycle. CANCELLED check runs are classified as noise (force-push twins
and re-run leftovers dominate); the rare real one surfaces through the checks
it cancelled around.

## 4. Mechanics

- **Script home**: `builtin_skills/kirocrew-dev/babysit/scripts/pr_watch.py`,
  synced to the user's skills directory by the builtin-skills loader like
  every other bundled skill asset. It is a cron-only script: never imported
  by gateway code. Cron scripts must live under `<config_dir>/crons/`
  (`resolve_script_path` enforces the root, and a symlink out of it is
  rejected after resolution), so the skill's arm recipe copies the synced
  asset into `crons/` on every arm — keeping the copy current — and registers
  `cron_add(script=".../crons/pr_watch.py:watch", every=300, timeout=120,
  message=<JSON>)`. The script body is security-scanned at registration and
  sandboxed at execution like any other script cron.
- **gh inside the sandbox**: the script-cron sandbox is a single-uid Linux
  user namespace, so every root-owned path component (`/`, `/usr`, `/home`)
  stats as the overflow uid and `resolve_gh`'s ownership walk would refuse
  ANY gh on the host. The gateway therefore pre-resolves gh with the full
  validation OUTSIDE the sandbox and hands the child
  `_KIROCREW_GH_PREVALIDATED=<path>|<st_dev>:<st_ino>`; the child re-checks
  what the namespace leaves intact (regular file, executable, not
  world-writable, outside the agent-writable tree) and pins the device:inode
  identity, so a binary swapped after the parent's check is refused. Hosts
  without a usable gh omit the variable and the child fails with the
  ordinary setup message.
- **Polling**: one bounded `gh pr view --json
  state,mergedAt,mergeable,mergeStateStatus,headRefOid,statusCheckRollup`
  call per tick (25s subprocess timeout). The rollup is bucketed tolerantly
  across the CheckRun and StatusContext shapes; unknown conclusion vocabulary
  buckets as failing — when in doubt, wake a brain.
- **State**: owned by the kernel at
  `<data home>/watch/gh-pr/<subject-fold>-<digest>.json`, where the digest
  covers `gh-pr#<repo>#<pr>#<cron job id>` — so two sessions babysitting one PR
  keep independent alert memories and neither can suppress the other's
  delivery. It holds the last head (the kernel's `epoch`), the per-head alert
  memory, the consecutive-error streak, and any open coalescing window.
  Corrupt or missing state reads as fresh; the cost of lost state is one
  duplicate wake, never a lost signal. **This path differs from the previous
  `<data home>/pr-watch/...`**, so the first tick after upgrading is a cache
  miss and any currently-open anomaly wakes once more. That is expected, not a
  regression.
- **Wake targeting**: the cron must be armed FROM the babysit session — the
  cron system captures the calling session key at `cron_add` time and the
  delivery path resolves it back to that slot (rehydrating it from history if
  the tab was closed). Armed headless, delivery degrades to a bell
  notification.
- **Wake brief**: names the PR, head, reason, and the caller-supplied `note`
  (worktree/branch orientation), and directs the woken turn to read the
  session work ledger when one exists — pairing with the session-ledger
  feature so a cold wake resumes from durable state.
- Check names are attacker-influenceable text (a workflow names its jobs);
  they are charset-folded before entering state keys or the wake brief.

## 5. Non-goals

- **Replacing `monitor_start`.** Active-fix phases — where the agent pushes,
  re-runs gates, and answers reviewers most cycles — keep the nudge loop.
  Watch mode is for the waiting between them.
- **Reviewer-verdict parsing in the watcher.** See §3.
- **Multi-PR watches.** One cron per PR; the state file and the wake brief
  are per-PR, and `cron_list` stays legible.
- **A new wake primitive.** The script-cron `Report` delivery path already
  injects an agent turn into the origin session; this feature adds zero
  gateway surface.

## 6. Failure modes

- `gh` failure → silent `Skip` per tick, one `watch-error` wake after the
  streak threshold, streak reset on recovery.
- Malformed cron message (bad JSON, missing repo/pr) → `Done` with the
  reason: a watch that can never succeed removes itself instead of retrying
  forever.
- State file unwritable → alerts may repeat (duplicate wake), never lost, and
  every wake carries a warning naming the directory. If the probe is failing
  *and* state cannot persist, the streak can never accumulate, so that case
  reports on the first tick instead of waiting for a threshold it will never
  reach.
- **Coalescing costs at least one extra tick**, because a window cannot open
  and fire within the same tick — `elapsed` is zero at the moment it opens. On
  a 60-second cron that is at least 60 seconds of added latency. Set
  `coalesce_secs: 0` in the cron message when latency matters more than being
  woken once.
- `pending` never draining (a check wedged in queued, a phantom pending row) →
  the window fires at the `coalesce_max_secs` wall instead, which is measured
  from the first anomaly and independent of `pending`. A new anomaly arriving
  after that starts a fresh window, so under a permanently stuck `pending`
  count the worst case is one wake per hard-cap interval — delayed, never
  dropped.
- Session tab closed → the delivery path rehydrates the slot from history;
  if the session's history was permanently deleted, delivery degrades to a
  bell notification.
