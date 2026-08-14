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
it was watching is still unresolved. Real loop stores show this is the common
failure: two live babysit loops ended at exactly 24/24 and 20/20 delivered
cycles, neither having called `autonudge_stop`. Evaluate the exit condition
every single cycle and stop deliberately.

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
- "Babysit / monitor / keep checking" in THIS conversation → `monitor_start`.
- Reacting to review feedback or CI on a PR → `monitor_start` or in-turn
  `wait`+poll. **Never `cron_add`, never HEARTBEAT.md** (see below).
- Work belongs in a fresh isolated session each cycle, and needs no tools that
  require approval → `cron_add`.
- Cleaning up after a merge you have already verified → `cron_add`, as a
  `script` cron at roughly a 5-minute interval.
- External system will call back → `register_hook`.

### Never use cron or heartbeat to react to reviewer feedback

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
python3 "$SKILL_DIR/scripts/pr_findings.py" <pr#>   # only after 20: failed steps, log tails, threads
```

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
   **accepted-and-deferred** keeps being re-raised, so a zero-findings test
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
2. **Call `monitor_start`.** `interval_secs` default 300 suits CI/review
   polling. `max_cycles` defaults to 24 (≈2h of idle gaps at 300s); raise it
   for longer work, and pass `0` for unlimited only when the user explicitly
   asks for an unbounded loop.
3. **Confirm it armed.** Read `~/.kiro/crew/autonudge.json` and check your
   loop is there. The tool's reply is not evidence — see above.
4. **Tell the user monitoring is active and END YOUR TURN.** The loop wakes
   you — do not wait+poll on top of it.
5. **Each cycle:** do the check, act, and report only real signals. Don't
   post "nothing new" every cycle. If the instruction no longer matches
   reality, `monitor_update` it rather than working around it.
6. **On the exit condition** (or the user saying stop): report, then call
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
           and act on its exit code (10 = still running, report nothing;
           20 = drill in with pr_findings.py, or stop if the reason is a
           terminal PR state). If this repo's reviewer conclusions are on the
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
