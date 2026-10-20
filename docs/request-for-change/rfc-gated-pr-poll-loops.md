---
title: Gated PR poll loops -- the prepare-pr and babysit recipes arm their pull-request loop gated, with the judge off
status: implemented
author: iamwhatever, with kirocrew-worker
created: 2026-10-20
last-audited: 2026-10-21
audited-at: 098bc0a174
doc-pr: 22440
implementation-prs: [22442]
tracking-issues: [22418]
supersedes: []
superseded-by: []
---

# RFC: Gated PR poll loops

- Status: implemented. Maintainer Joe Guo (@iamwhatever), who owns the
  `kirocrew-prepare-pr` and `babysit` skills, accepted this design on
  2026-10-21 (§ 10), and
  [#22442](https://github.com/kirodotdev/KiroCrew/pull/22442) implements it.
- Related: [rfc-wake-judge.md](rfc-wake-judge.md) § 8, PR F ("skills: `babysit`
  and `goal-conductor` author `wake_when` / `quiet_when` when arming"), which is
  still outstanding. This document arms the same loops gated, but with the judge
  off; a brief is a later step (§ 6).
- Stays compatible with the open RFC
  [#22165](https://github.com/kirodotdev/KiroCrew/pull/22165) (legacy-loop pull
  request reads, kept agent stops, declared waits); § 5 says how.
- Tracking issue: [#22418](https://github.com/kirodotdev/KiroCrew/issues/22418).
  Code references were read at `6432cad9ea` and re-checked at `098bc0a174`.

## 1. Problem

Both recipes arm their pull-request poll loop ungated:

- `kirocrew-prepare-pr` (`SKILL.md`, Phase 3 step 5):
  `monitor_start(..., interval_secs=300, max_cycles=288, max_runtime_secs=86400, gate=False)`.
- `babysit` (`SKILL.md`, "Example: legacy comment-aware recipe"): `"gate": false`.

An ungated loop delivers a full chat turn on every interval
(`autonudge_service/gate.py`, `_monitor_tick_is_quiet`: `if not loop.gate:`
returns "fire"), whether or not the pull request changed. Each such turn runs
`green_age.py` and `pr_status.py`, and on most of them reports nothing new.

Measured on one gateway's chat histories joined to usage rows, 2026-10-01 to
2026-10-19: 402 worker-chat turns opened 5 minutes after the previous one, and
206 of them came from ungated loops (155 carry the prepare-pr banner), most on a
pull request that had not moved. Ungated loops delivered on 99% of their ticks.

The reason both recipes give for `gate=false` is that comments and advisory
findings are outside the typed provider's evidence. That was true when it was
written, on 2026-09-11 and 2026-09-12
([#5186](https://github.com/kirodotdev/KiroCrew/pull/5186),
[#10293](https://github.com/kirodotdev/KiroCrew/pull/10293)). It stopped being
true for the gated loop on 2026-09-27:

- [#13759](https://github.com/kirodotdev/KiroCrew/pull/13759) made the gated
  tick read the pull request whole (`probes/gh_pr.py`, `PrObservation.as_facts`):
  state, head, mergeability, review decision, every check, and the recent
  comments and reviews by a digest of each whole body.
- When no judge runs, the gate already treats a whole reading that matches the
  last one as a quiet tick: in `_monitor_tick_is_quiet` only
  `judged is None and observation is not None and not reading_unchanged` fires.
  That is the path a loop armed with `judge=False` takes.
- [#22299](https://github.com/kirodotdev/KiroCrew/pull/22299) gave judged loops
  the same: a whole reading that matches the last judged one asks no judge and
  starts no turn (`_unchanged_reading_skips_judge`).

## 2. Goals and non-goals

Goals:

- An unchanged pull request costs these two recipes no chat turn.
- Every change the reading carries still wakes the loop, once.
- Silence stays bounded by the gate's existing quiet floor.

Non-goals:

- Changing the gate, the probe or the judge (`autonudge_service/`, `probes/`,
  `decisions/`). The behaviour this relies on is on main.
- A judge brief for these loops (§ 6).
- Other `gate=false` loops: a heartbeat, a reviewer chase, daily maintenance,
  the gateway-restart skill. Their ticks are the work.
- `monitor_watch`, and the legacy-loop reads of RFC #22165.

## 3. Design

### 3.1 Arming

Both recipes pass `judge=False` and leave `gate` at its default, `true`. The
interval, cycle cap and runtime stay as they are, so the rule from
[#22200](https://github.com/kirodotdev/KiroCrew/pull/22200) still holds:
interval × `max_cycles` ≥ `max_runtime_secs`, and the runtime ends the loop.

`judge=False` keeps the gate and drops the judge. `_judge_tick_is_quiet`
returns at `judge_is_off` before any judge is asked, so a changed reading takes
the gate's judge-less path and fires, and an unchanged one is a quiet tick.

### 3.2 What wakes the loop

| Change | Where the reading carries it | Cost |
|---|---|---|
| New review | `reviews`: id, verdict, body digest | one turn |
| New comment from someone else | `comments`: id, body digest | one turn |
| Edited recent comment | the whole-body digest | one turn |
| Review lane verdict | the lane's own check run | one turn |
| CI result | check buckets | one turn |
| Conflict | mergeability, merge state | one turn |
| New head | head revision | one turn |
| Merge or close | state | loop ends with a notice |
| Nothing | -- | no turn |

The reading keeps the newest 6 comments and reviews from the last 5 hours
(`_MAX_REMARKS`, `DEFAULT_REMARK_HORIZON_SECS`), so a new one is always in it.

A review lane edits its marker comment (`<!-- codex-ai-review -->`) in place, so
on a pull request older than 5 hours the edit itself falls outside the reading.
Each same-repo lane is one `pull_request` job whose check run finishes with that
edit, and each fork lane posts and closes its own check run on the head, so the
check moving from pending to a result wakes the loop.

The loop's own account's comments (`viewerDidAuthor`) are left out of the
reading on purpose, so a disposition the agent posts does not wake it again.

A change costs one turn, not two. A gh-pr reading never raises a kernel WAKE, so
a delivered change fire earns no follow-up tick (`firing.py` grants one only to
a claimed wake).

### 3.3 What waits for the quiet floor

The reading cannot see time pass. These duties wait for the floor turn, which
the gate delivers after 10 quiet ticks in a row, about 50 minutes at 300 s:

- main moved in this pull request's files without a conflict (`green_age.py`
  exit 30);
- a review lane that never posts;
- a finding whose text did not change, when the turn that carried it did not
  answer it.

Each of these was a 5-minute duty before.

### 3.4 The Report moves to the converged turn

On a dashboard loop, a merge or close settles the loop with the bell notice and
no turn (`gate.py`, the terminal branch). A Slack or Discord loop still gets one
final turn. So prepare-pr gives its Phase 4 Report on the converged turn
(`pr_status.py` exit 0), not after the merge.

### 3.5 Babysit

Changed: the comment-aware pull-request example, and the lines that send
pull-request comments and advisory findings to `gate=false` (the driver table,
the arming-preference paragraph, the structured-watch section and the
same-session timer bullets).

Unchanged: `gate=false` for work done while the subject is quiet (a heartbeat,
a reviewer chase, a moving base) and for a required final turn after a merge.

### 3.6 Tool description

`monitor_update`'s `judge` text says that after `false` "every cycle fires as a
plain timer again". On a gated pull-request loop that is not what happens: an
unchanged reading still costs no turn. The implementation corrects the sentence.

## 4. Measures

A replay of the 1,487 measured worker pull-request-loop turns from 2026-10-01 to
2026-10-19 (18.8 days), with an unchanged reading free and the 10-tick floor kept.
The first row is the loops that were already gated, which is what #22299 reached
on that gateway, where every judge call timed out:

| Option | Turns saved | Credits saved | Credits a day |
|---|---:|---:|---:|
| Already-gated loops (#22299) | 290 (20%) | 1,433 (12%) | 76 |
| ... and this change (prepare-pr) | 414 (28%) | 2,172 (18%) | 116 |
| ... and every other ungated pull-request loop | 432 (29%) | 2,379 (20%) | 127 |

The rows grant no follow-up tick after a change, per § 3.2. With one follow-up
tick per change the same rows read 212, 305 and 318 turns. Source: the
operator's study, `research/crewmate-perf/loops/l10_fix_a_savings.py`.

After it ships, the check is the same count on new loops: delivered cycles per
prepare-pr loop, and the gate's own counters on the loop record
(`quiet_ticks`, `floor_ticks`, `gate_fallbacks`).

## 5. Compatibility with RFC #22165

- Its § 2.2 reads an ungated loop's pull request between cycles only when the
  interval is longer than 300 s (`subject_reads.py` in
  [#21377](https://github.com/kirodotdev/KiroCrew/pull/21377):
  `if loop.gate or loop.monitor is not None or delay <= SUBJECT_READ_SECS`). Both
  recipes run at 300 s, so § 2.2 never read their pull request; gated, they read
  it every tick.
- Its § 2.1 (a held loop reads its pull request and settles on a merge or
  close), § 2.3 (an agent's stop keeps the row) and § 2.4 (a declared wait for a
  person) apply to these loops as to any legacy loop. This document changes none
  of them.

## 6. Later: a judge brief

[#22290](https://github.com/kirodotdev/KiroCrew/issues/22290) (every judge call
on that gateway timed out) is fixed by
[#22448](https://github.com/kirodotdev/KiroCrew/pull/22448). Once the judge's
latency and miss rate are measured on a loaded host, a brief written by these
recipes could also hold lane-by-lane progress. That is rfc-wake-judge § 8 PR F.
It is a separate change.

## 7. Risks

- The duties in § 3.3 slow from 5 minutes to about 50.
- A person editing an OLD comment (not a lane's) wakes the loop only at the
  floor. A new comment wakes it at once.
- No turn after a merge on the dashboard (§ 3.4). The notice says it merged.
- A person who comments from the loop's own account does not wake it.

## 8. Alternatives considered

- **Keep `gate=false`.** One turn every 5 minutes on an unchanged pull request
  (§ 1).
- **Gate with the default judge.** On the measured gateway every judge call
  timed out (#22290, since fixed by #22448), so each changed tick cost a judge
  call and the turn. A judge that answers may hold a change these recipes act
  on, and no test can pin that answer.
- **Read edits of old comments** with one more `gh api` call per tick. Not
  needed: a lane's check carries its edit (§ 3.2).

## 9. Rollout

One implementation PR,
[#22442](https://github.com/kirodotdev/KiroCrew/pull/22442), opened beside this
one and rebased after this one merged. It edits the two `SKILL.md` files inside
their byte budgets (the reasons move to
prepare-pr's `references/rationale.md`), the two spec passages that state the old
reason (`learn-cron-dashboard.md`, `monitor-architecture.md`) and the
`monitor_update` description, and re-pins the recipe tests. A new test arms each
recipe's own `monitor_start` call and drives it through the real gate.

## 10. Decision

2026-10-21: maintainer Joe Guo (@iamwhatever) accepted this design; implemented by #22442.
