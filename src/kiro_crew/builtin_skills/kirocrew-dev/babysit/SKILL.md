---
name: babysit
description: Same-session monitoring loop for PRs, CI runs, tickets, and deployments using the monitor_start / monitor_update / autonudge_stop MCP tools. The loop re-injects your check instructions into THIS session on an idle interval — same context, same tools — and works from dashboard chat, Slack threads, and Discord DMs. Use when the user says "babysit", "monitor", "keep checking", "keep an eye on", "loop on this PR", "let me know when", or wants polling that outlives a wait+poll window. NOT for fresh-session work (use cron_add) or external-system callbacks (use register_hook).
tags: [skill, kirocrew, monitor, babysit, autonudge, loop]
---

# Babysit (same-session monitoring loop)

## Overview

`monitor_start(message, interval_secs?, max_cycles?)` binds a monitoring loop
to **your current session**. Every `interval_secs` the message is re-injected
as your next turn. User messages defer a due fire until their turn ends but do
NOT restart the countdown, so the loop stays on schedule even in a session the
user is actively chatting in. You keep the full conversation context, memory,
and tools on every cycle. Loops persist to `~/.kiro/crew/autonudge.json` and
survive gateway restarts (the countdown resumes where it left off).

Works from:

| Surface | Binding | Cadence |
|---|---|---|
| Dashboard chat | bare slot key | deadline timer (user turns defer, never reset) |
| Slack thread | `slack:<thread_ts>` | fixed interval after each unattended turn |
| Discord DM | `discord:{agent}:direct:{user}` | fixed interval after each unattended turn |

`autonudge_stop(reason?)` stops the loop bound to the current session from
any of those surfaces. `monitor_update(message?, interval_secs?, max_cycles?)`
revises the loop already bound to this session in place, keeping its cycle
count — use it when the instruction you armed has gone stale, or to raise the
cap on a loop that is still doing useful work.

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
  instruction. A loop naming one public GitHub pull request is gated by default:
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

**Why the interval can be small.** A tick of this cron costs one bounded `gh`
call and no tokens at all, so the interval is limited by API politeness rather
than by spend -- 60s is reasonable, and 300s is a default rather than a floor.
An UNGATED `monitor_start` cycle, by contrast, costs a full agent turn on the
session's whole context, which is what forces its interval up.

Arm it **from the session that owns the babysit** — the cron captures that
session as its wake target; armed anywhere else, the wake lands in the wrong
chat. Cron scripts must live under `~/.kiro/crew/crons/`, so copy the synced
skill asset there first (re-copy on every arm — it keeps the copy current
with skill updates):

```
CREW_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
cp "$CREW_HOME/skills/kirocrew-dev/babysit/scripts/pr_watch.py" \
   "$CREW_HOME/crons/pr_watch.py"

cron_add(
  name="pr-watch #1234",
  script="~/.kiro/crew/crons/pr_watch.py:watch",
  every=300,
  timeout=120,
  message='{"repo": "owner/name", "pr": 1234,
            "known_reds": ["Frontend Tests (4)"],
            "note": "worktree ~/oss/wt-foo, branch fix/foo"}'
)
```

The `cp` runs in your shell, so it has to resolve `KIROCREW_HOME` -- an install
that moved its data home has no `~/.kiro/crew/skills` at all and a hardcoded
path fails with `No such file or directory`. The `script=` value is different:
the gateway resolves it against its OWN config directory (and forces the
`crons/` root), so the conventional spelling there is correct as written.

- `known_reds` — check names that are red on the BASE branch (inherited
  breakage). The watch never wakes for them and treats "everything else
  green" as review-ready. Populate it from what you verified against main's
  own CI, and update the cron message if main's condition changes.
- `note` — one line of context echoed into the wake brief. Put the worktree
  and branch here so the woken turn starts oriented; if this session keeps a
  work ledger, the brief also tells the woken turn to read it.
- `coalesce_secs` — optional convergence window, default 240, `0` disables it.
  Reds arriving while checks are still running are **coalesced into one
  wake** instead of one wake each. This matters on a repository whose checks
  finish over many minutes: a 34-second body gate and a five-minute reviewer
  lane both flipping red on one head used to be two separate wakes, each
  arriving before the other checks had even started. The window opens on the
  first anomaly and fires once `coalesce_secs` has elapsed AND either everything
  has settled or a 30-minute hard cap is hit — so a `pending` count that
  never drains costs a delayed wake, never a lost one.
- **A grace-gated wake always costs at least one extra tick**, because a
  window cannot open and fire within the same tick. On a 60s cron that is at
  least 60s of added latency. Set `coalesce_secs: 0` when latency matters more
  than coalescing.
- A merge conflict and a merged/closed PR **bypass the window** and fire
  immediately: a dirty PR dispatches no checks, so `pending` never drains and
  waiting observes nothing at all.
- Wakes are deduplicated **per head SHA**: one wake per conflict, per new red
  check name, per all-green. A force-push resets the memory, so the next
  anomaly on the new head wakes again. Quiet ticks deliver nothing at all.
- A conversation signal older than five hours is never treated as new. That bound
  is a constant in the script, deliberately not a cron parameter: the probe keeps
  no memory of its own, so without it arming a watch on a PR with forty comments
  would report all forty on the first tick — and as a knob it had no caller while
  its one real constraint (stay under the kernel's six-hour re-alert window) is
  now asserted in code rather than merely documented. The value sits close to that
  ceiling on purpose: too large costs one coalesced arm-time wake, while too small
  costs a silent permanent MISS whenever the watch stops ticking for longer than
  the horizon (laptop asleep, gateway down, cron auto-paused). Those two prices are
  not equal, so the horizon is pushed as high as the kernel allows.
- The watch reads PR state, the check rollup, AND the conversation: comments and
  submitted reviews. It reports **that**
  something was said -- who, and when -- and never quotes the body. Reading the
  text, judging whether it is a real finding, and deciding what to do stay the
  woken agent's job, done with this session's trust rather than a cron script's.
  This is what closes the gap `monitor_start` used to cover: a comment moves no
  check, and on this repository a reviewer lane can report success while its
  comment body carries findings, so a rollup-only watch would sit quiet on a
  green PR nobody had read.
- Conversation signals are deduplicated per comment/review id and **survive a
  force-push**, because a comment belongs to the pull request rather than to the
  commit under review. Check-derived signals still reset per head. Your own
  comments are ignored, or the watch would wake you to read the disposition you
  just posted.
- Merged or closed → the cron delivers a final message and removes itself.
  If you finish the babysit early, remove it yourself (`cron_remove`).
- Typical composition: drive the active-fix phase with `monitor_start`; when
  the PR goes quiet (checks running, awaiting review), stop the loop
  (`autonudge_stop`) and arm the watch. When a wake arrives, act on it; if
  heavy fixing resumes, re-arm `monitor_start` and remove the watch until
  things go quiet again.

### Never use an agent cron or heartbeat to react to reviewer feedback

Both are structurally incapable of it, and both fail in ways that look like
success:

- **Cron.** A cron job has no owning chat slot, so it can never earn per-slot
  trust: its tool calls land on a deny-by-default approval path and time out
  after 180 seconds unless a global auto-approve grant happens to be active.
  Worse, a denied *tool* inside a *completed* turn still records
  `last_status: ok`, so the job registry reports health while the job does
  nothing. Measured on a real PR watcher: 101 runs over 25 hours, 23 blocked at
  approval, hours of model time, zero commits pushed, and a green-looking
  registry throughout.
- **Heartbeat.** Its approval path is a strict name allowlist
  (`HEARTBEAT_SAFE_TOOLS`), deny-by-default, with no shell and no `git push`.
  It cannot amend a commit or push a revision, so it can never close the loop
  it was asked to watch.

Cron *is* the right tool for post-merge cleanup — but as a `script` cron, which
bypasses the LLM approval layer entirely, at roughly a 5-minute interval. An
hourly job loses the race: one observed merge-to-teardown window was 17
minutes.

## Reading PR/MR state — ask the host for its verdict, don't hand-roll a filter

Whatever you are babysitting, the read step is **not** yours to invent. These
five rules hold on GitHub, GitLab and Bitbucket alike; only the command changes.

1. **Ask the host for its own aggregate verdict.** Do not reduce a list of
   individual checks into a pass/fail yourself. Every host computes a merge
   verdict and exposes it; a filter you write over the raw list is a second,
   worse implementation of it that silently disagrees.
2. **Classify every state you see, and fail closed on the ones you don't.** An
   unrecognized or unmapped status must count as *not passing*, never as
   passing. Collapse superseded runs to the newest attempt per check identity
   before counting failures, or a stale cancelled run reads as a live failure.
3. **"Checks are green" is not "nothing is outstanding."** Unresolved review
   threads and *advisory* (non-blocking) results are separate axes that the
   aggregate verdict does not cover, by design. Read them separately, every
   cycle, or the loop will declare readiness over an open thread.
4. **Lifecycle state is terminal — read it every cycle.** Merged, closed, or
   declined means stop, report the real outcome, and call `autonudge_stop`.
   Do not infer this from the checks; ask for the state field.
5. **Mergeability is computed asynchronously.** "Unknown", "checking" or
   "unchecked" means **wait**, not pass — and on a non-open object it may never
   resolve at all (see the GitHub limits below).
6. **A conflicted PR's checks are stale, not signal — so a conflict means rebase
   NOW, not wait.** This is the mechanism behind rule 1, worth knowing because it
   is invisible in the status list: a conflicted PR cannot produce a merge ref, so
   the host dispatches **no** `pull_request` workflows at all, and every check you
   can see belongs to the old head. A status-only loop therefore reports "nothing
   new" indefinitely while the clock runs. On GitHub you do not read these fields
   yourself — `pr_status.py` already reads `mergeable`, `mergeStateStatus` and
   `reviewDecision`, and it ranks a conflict **above** in-flight checks precisely
   so this cannot happen: a conflicted PR exits **20** on the first poll rather
   than reporting "running" forever while nothing can complete. The same holds
   for `BEHIND`, a draft, and `CHANGES_REQUESTED` — each survives any amount of
   waiting, so each is surfaced immediately. What this rule adds is the
   response: on a conflict or `BEHIND`, re-sync and re-push instead of polling,
   and never report the previous head's green.
7. **A fully green PR can still be terminally blocked by a human decision.**
   `reviewDecision == CHANGES_REQUESTED` survives every push and is invisible to
   the checks rollup; `pr_status.py` reports it as 20 ahead of any in-flight
   check, so it surfaces on the first poll. The judgment the exit code cannot
   make is *what kind* of block it is: when the content is a product hold rather
   than a defect, there is nothing to converge on, so report it **once**, quoting
   the blocking reviewer, and stop rather than cycling.

### Where each host keeps those answers

| host | one-shot verdict | unresolved-thread axis | the local trap |
|---|---|---|---|
| **GitHub** | `pr_status.py` (below); optionally an aggregate status context | review threads via GraphQL (`pr_status.py` prints the count) | `statusCheckRollup` is a `CheckRun \| StatusContext` union — `.conclusion` vs `.state` |
| **GitLab** | `detailed_merge_status` on the MR (`glab mr view <iid>`, or `glab api projects/:id/merge_requests/:iid`) | `glab mr view <iid> --unresolved`, or the Discussions API | a pipeline reports `success` while its `allow_failure: true` jobs failed |
| **Bitbucket Cloud** | none — combine PR `state` with the commit's build statuses (`/2.0/repositories/{ws}/{repo}/commit/{sha}/statuses`) | PR comments/tasks on the PR resource | below Premium, unresolved merge checks only **warn**; the host still allows the merge |

GitLab specifics worth knowing: use `detailed_merge_status`, not `merge_status`
(deprecated since 15.6 and it does not account for every state). Its values are
themselves the loop's decision — `ci_still_running` / `checking` / `preparing` /
`unchecked` are *wait*; `mergeable` is clean; `conflict`, `need_rebase`,
`not_approved`, `draft_status`, `discussions_not_resolved`,
`status_checks_must_pass` and `requested_changes` are each a distinct blocked
reason worth reporting as itself. Note that `blocking_discussions_resolved` is
**not** an unresolved-thread count: it only tells you whether resolution is
required *and* satisfied, so on a project that does not require resolution it can
be `true` with threads still open. Count threads from the discussions, and treat
external status checks as their own axis, separate from the pipeline.

Bitbucket specifics: there is no single "can this merge" field to poll, so rule 1
becomes "combine the two sources the host does give you" — the PR's `state`
(non-`OPEN` is terminal) and the head commit's build statuses. And because merge
checks are advisory below Premium, a Bitbucket "green" is weaker evidence than
elsewhere: rule 3 is not optional there.

Provenance: the GitHub path below is exercised (including against an unrelated
public repo); the GitLab and Bitbucket rows come from those vendors' API docs and
are **not** something this skill has run. Verify the exact flag or field against
your host before trusting a value you have not seen come back.

### On GitHub: use `pr_status.py`

The `prepare-pr` skill owns the tool, and it is project-agnostic — stdlib Python
over `gh`, no repo-specific assumptions baked in. Call it by path from the target
repo (do **not** `cd` into the skill folder; the scripts read which repo they are
talking about from your cwd):

```bash
SKILL_DIR="${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/kirocrew-dev/prepare-pr"
python3 "$SKILL_DIR/scripts/pr_status.py" <pr#>     # exit 0 clean / 10 running / 20 blocked / 2 env
python3 "$SKILL_DIR/scripts/pr_status.py" <pr#> --json   # same exit code, plus the progress key on stdout
python3 "$SKILL_DIR/scripts/pr_findings.py" <pr#>   # only after 20: failed steps, log tails, threads
```

`--json` adds one machine-readable object to stdout and changes nothing else —
same exit codes, same prose above it. Use it for the stall tripwire: the object
carries the full 40-char `head_sha` (the prose only ever prints 12), the sorted
`failing_checks` list, and `readiness_kind`, so two cycles can be compared
byte-for-byte instead of by eyeballing a diff of human text.

Its `advisory` half is what you read when checking the exit conditions below:
`unresolved_threads` (`null` there is the same "could not establish" as a `?`),
`findings` per reviewer, `stale_reviewers` / `blocking_reviewers`, and
`elided_stamp_reviewers` — lanes whose freshness stamp MANGLED the head SHA
(the workflows have the model retype it, so a lane can drop the middle and
splice the head's prefix to its suffix). The gate verifies such a stamp against
the current head and accepts it, which is why it is not a stale reviewer; the
list exists so the emitter defect is still visible. Report it ONCE per head as
a lane-quality note — it never blocks, and re-running that workflow usually
produces a clean stamp. Nothing else is emitted — ambient PR state (mergeable,
merge state, review decision, check totals) stays in the prose above, which is
where those conditions already read it, so there is no second copy to keep in
sync.

Drive the cycle off the **exit code**, not off prose: `10` → report nothing and
wait for the next cycle; `20` → drill in with `pr_findings.py` and act; `2` →
environment problem, escalate rather than loop on it.

**Exit `0` is necessary but not sufficient — do not stop on it alone** (rule 3).
The script's decision is fail-closed about *checks*, but the unresolved-thread
count it prints is **advisory: it is not part of the exit code**, so a PR with
open review threads still exits `0`. Before you declare review-ready and call
`autonudge_stop`, confirm all five:

1. `pr_status.py` exits `0`;
2. its `unresolved threads (advisory)` line reads `0` — a `?` means the count
   could not be retrieved, which is not a zero, so treat it as unresolved and
   check the threads yourself with `pr_findings.py`;
3. every reviewer that raised something has an answer from you on the PR. An
   **advisory** reviewer posts its concerns *and* passes its own check, so its
   verdict appears in neither the exit code nor the aggregate. In this repo that
   is `Design Review` / `UX Review` reporting `🟡 CONCERNS` while green; in
   another repo it is whatever non-blocking bots and human reviewers comment
   there. See `prepare-pr`'s "Answer every concern".
4. its `mergeable=` / `mergeState=` / `reviewDecision=` line (the script prints
   all three) shows no conflict, no `BEHIND`, and no `CHANGES_REQUESTED` — exit 0
   already implies this, so read the line to know *which* to report, not to
   re-decide it (rules 6-7);
5. **no finding on the current head lacks a disposition.** Not "zero findings" —
   that can never be reached. A **fixed** finding disappears from the bot's
   in-place-updated body on the next review, but one you **rebutted** or
   **accepted-and-deferred** (or **needs-a-decision**) keeps being re-raised, so a zero-findings test
   deadlocks the loop against your own correct answer. The test is *unanswered*,
   which is also what the `autonudge_stop` prohibition below is keyed on. The
   mechanical half of this condition is the script's, not yours: `pr_status.py`
   reads the bot comments itself, holds every `[<NAME>-REVIEWED]` stamp to the
   current head SHA, and folds a stale stamp or a `[BLOCK-MERGE]` marker for
   the current head into exit `20` — so exit `0` already proves reviewer
   freshness and the absence of a blocking finding. On a repo with a known
   reviewer fleet, PIN it (`--reviewers NAME1,NAME2` /
   `PREPARE_PR_REVIEWERS`): a pinned reviewer must have a fresh stamp, so a
   bot that fails to post — or an emitter drift that stops stamps appearing at
   all — blocks instead of silently un-gating; unpinned discovery mode holds
   whatever stamps it finds to freshness but does not require presence. One reading note: a stale stamp maps onto exit `20`, and
   on a repo whose reviewer bots are comment- or cron-triggered (not in the
   check rollup) that `20` can mean "the bot has not posted for this head yet"
   rather than "author action needed" -- the reason string names the stale
   reviewer, so read it before treating the exit code as a fix signal. What stays yours is the judgment half: the script prints
   each fresh reviewer's advisory `FINDING` count but deliberately never gates
   on it, so read those from `pr_findings.py` (which lists each one with a
   stable `span=` identity), subtract the ones your own `ai-review-disposition`
   comments already answer, and disposition what remains.

**Never call `autonudge_stop` while an un-dispositioned finding exists for the
current head SHA.** If you must stop for another reason, post the open-finding
list so the handoff is visible to a human — otherwise findings sit unread for
hours while the loop looks healthy.

### Verify what your reviewer's conclusion actually means — once per repo

Rule 1 says ask the host for its verdict. That holds for CI, but a **review bot is
not a build**: its check conclusion is whatever its workflow chose to exit with, and
that is a per-repo implementation detail. So before you build an exit condition on
it, establish once what it means in the repo you are in, and re-check if the review
fleet is renamed or replaced. The failure modes to look for, any of which makes a
status-only loop unsound:

- **Red that only means "found something."** The workflow exits nonzero when the
  review succeeded and produced a finding. Red becomes its normal state.
- **A verdict that never posted.** An empty comment while the job log holds the
  finding — the loop sees no findings and concludes clean.
- **Green with findings in the body.** Worse than red, because red at least wakes a
  watch loop.
- **A verdict that is not reproducible.** Re-dispatch on an identical tree flips it,
  so bot-green is not terminal and one red is not a stable fact about the diff.
- **Inflated failure counts.** Per-SHA double dispatch, or runs reporting `failure`
  with zero failed jobs. Collapse to the newest run per (workflow, SHA) — rule 2 —
  before counting anything.

> **Establish this per repo before trusting a conclusion, and write down what you
> found.** If any of the five holds, resolve reviewer state from the **comment
> body for the current head SHA** instead — which is exactly what `pr_status.py`
> does mechanically: it never reads the review workflow's conclusion, only the
> per-SHA stamps and blocking markers in the bodies, so "stamp matches head AND
> no blocking marker" is already folded into its exit code. Which repos are
> affected, and the evidence for each, belongs in that repo's issue tracker — not
> in this skill, which ships to every install and cannot be corrected in copies
> already distributed. For kirodotdev/KiroCrew that record is #2548; #2550 moved
> the check itself into the script so the conditional is data, not prose.

Where a reviewer's conclusion *is* trustworthy, none of the above applies and reading
job logs every cycle is wasted work: check first, then decide.

If any of the five is unmet, the loop has not reached its exit condition —
keep cycling (or escalate), and do not report the PR as review-ready.

Two GitHub-shaped traps this closes, both of which produce a confidently wrong
reading:

- **`.conclusion` is not universal — this is a GitHub API shape, not a
  per-repo quirk.** `statusCheckRollup` is a union: **CheckRun** entries carry
  `.conclusion`, while **StatusContext** entries (the legacy commit-status API,
  still how many third-party integrations and any home-grown aggregate report)
  carry `.state` instead. So
  `gh pr view --jq '.statusCheckRollup[] | select(.conclusion==...)'` silently
  drops every status context in any repo that has one, and the PR reads cleaner
  than it is. `pr_status.py` classifies both shapes, and treats an aggregate
  status as authoritative over the individual rollup when one is published —
  naming it with `--readiness-context NAME` (or `PREPARE_PR_READINESS_CONTEXT`;
  the default is this repo's `PR Readiness`, and `resolve_profile.py` reports
  the right name for another project, which you then pass in). With no aggregate
  published it falls back to the full rollup, so it still works on a repo that
  publishes none.
- **The failing count is fail-closed, not a bug count** (rule 2). Any
  unrecognized COMPLETED conclusion counts as a failure, and superseded re-run
  attempts are collapsed to the newest run per check identity before counting —
  so every remaining `[fail]` line is live. Read the per-check lines before
  naming causes to the user.

Two limits worth knowing before you trust it on an arbitrary PR:

- **Run it from a checkout of the target repo.** A bare PR number resolves
  against your cwd's repo. A full PR URL works for any repo, but the
  unresolved-thread count re-resolves the repo from cwd (`gh repo view`), so a
  URL from a foreign checkout mixes two repos: usually that prints `?` (the
  number does not exist there), but if the cwd repo happens to have a PR with
  the same number you get a thread count for the *wrong* PR with nothing marking
  it as such.
- **A merged, closed or declined PR exits `20`** with `PR state is ... (not
  OPEN; terminal)` — that satisfies rule 4, so on that message report the real
  outcome and stop the loop rather than triaging it as a failure.
- **`?` is not `0`.** It means the count could not be established (auth, page
  cap, wrong repo). Treat it as unresolved.

## Workflow

1. **Write the message as instructions to your future self.** Include:
   - what to check (PR URL, job id, ticket),
   - what to do with findings (fix + push, summarize, escalate),
   - the exit condition, ending with: "when met, tell the user and call
     `autonudge_stop`".
2. **Call `monitor_start`, and always pass a wall-clock budget.**
   `interval_secs` default 300 suits CI/review polling. `max_cycles` defaults to
   24 (≈2h of idle gaps at 300s); raise it for longer work, and pass `0` for
   unlimited only when the user explicitly asks for an unbounded loop.
   **Set `max_runtime_secs` too.** It is the only bound that holds when
   per-cycle work grows: measured cycles ran 124s–823s of model time *on top of*
   the idle gap, so a 12-cycle cap took 4.1 hours. Cycle count does not bound
   spend. Budget the wall clock you would actually accept (e.g. `14400` for four
   hours) so a stalled loop dies on time rather than on arithmetic.
3. **Confirm it armed.** Read `~/.kiro/crew/autonudge.json` and check your
   loop is there. The tool's reply is not evidence — see above.
4. **Tell the user monitoring is active and END YOUR TURN.** The loop wakes
   you — do not wait+poll on top of it.
5. **Each cycle:** do the check, act, and report only real signals. Don't
   post "nothing new" every cycle. If the instruction no longer matches
   reality, `monitor_update` it rather than working around it.
   **Keep a no-signal cycle cheap.** On exit `10` the correct cycle is: read the
   exit code, write nothing, end the turn. Do not re-read review-bot bodies, job
   logs, or diffs on a cycle whose own status call already said nothing changed —
   that is where a watch loop turns into a spend loop. One `pr_status.py` run is
   6–8 `gh` invocations; the expensive reads belong to exit `20`.
6. **Persist the progress key and its streak count to a file** (not session
   memory — compaction eats both) and apply the stall tripwire above: 3
   byte-identical keys on *settled* cycles (exit `0` or `20`) with no push means
   stop and escalate. Exit-`10` cycles are skipped, not counted and not reset;
   exit `2` escalates.
7. **On the exit condition** (or the user saying stop): report, then call
   `autonudge_stop` with a reason. Do not let the cap do this for you.

## Example

User: "babysit PR #247 until it's review-ready"

```
monitor_start(
  message="Check PR #247. FIRST read
           gh pr view 247 --json mergeable,mergeStateStatus,reviewDecision —
           rules 6 and 7 of the babysit skill govern what each value means.
           Then run
           python3 \"${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/kirocrew-dev/prepare-pr/scripts/pr_status.py\" 247
           and act on its exit code, passing --json so the progress key is
           machine-comparable (10 = still running, report nothing and do not
           read bot bodies or job logs;
           20 = drill in with pr_findings.py, or stop if the reason is a
           terminal PR state). Keep the progress_key AND a consecutive-match
           count in the data home beside the loop's stop sentinel, reading that
           file rather than trusting memory: 3 byte-identical keys on settled
           cycles (exit 0 or 20) with no push in that span means stop and
           escalate. If this repo's reviewer conclusions are on the
           unreliable list you established for it, resolve the AI review lane
           from the job log plus the comment body for the current head SHA
           rather than the conclusion; where the conclusion is trustworthy, use
           it. Fix legitimate
           High/Medium findings and push, following this repo's history
           convention; before rebutting a finding that has returned a 3rd time,
           re-run the reviewer once on the unchanged SHA and re-derive the claim.
           Stop ONLY when all five exit conditions in the babysit skill hold, and
           never while an UN-DISPOSITIONED finding exists for the current head
           SHA — a finding you rebutted or deferred stays visible in the bot's
           body, so "any finding at all" would never let the loop finish. Then
           tell the
           user the PR is review-ready and call autonudge_stop.",
  interval_secs=300,
  max_cycles=20,
  max_runtime_secs=14400,
)
```

The example deliberately **points at** the five conditions rather than restating
them; a re-serialized copy inside the prompt drifts from the list above.

On GitLab or Bitbucket the shape is identical — only the first line changes (the
host's own verdict call from the table above), plus its own thread axis and its
own "green is weaker than it looks" caveat.

## Rules & gotchas

- **One loop per session** — a new `monitor_start` replaces the existing loop.
- **Busy sessions skip a cycle** (never queue) — a long-running turn delays
  the next check to the following interval; skipped cycles don't count
  toward `max_cycles`.
- **Unattended turns are bounded to 30 min** on Slack/Discord; keep each
  cycle's work small and incremental.
- **Slack/Discord loops auto-approve tools** on the unattended turn
  (Slack always; Discord follows the gateway approval mode — under
  interactive approval a Discord cycle cannot use tools, so prefer
  dashboard/Slack for tool-heavy babysitting or run the gateway with
  `--approval yolo`/`auto`).
- **Kill switches:** `autonudge_stop` (preferred), the dashboard 🎯 popover
  (dashboard loops), `max_cycles`, or the per-loop STOP sentinel file.
- Loops fire `[auto-nudge cycle N]`-tagged messages — treat them as your own
  scheduled wake-ups, not user input.
