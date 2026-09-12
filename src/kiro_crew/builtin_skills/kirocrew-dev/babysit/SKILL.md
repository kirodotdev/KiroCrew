---
name: babysit
description: Use when a user asks to babysit, monitor, keep checking, keep an eye on, or report when a pull request, CI run, ticket, deployment, or other changing target reaches an outcome.
inject_on_trigger: false
triggers: babysit, keep checking, keep an eye on, let me know when, monitor pull request, monitor pr, watch pull request, watch pr
tags: [skill, kirocrew, monitor, babysit]
---

# Babysit

General-purpose monitoring: this skill works without a Kiro Crew checkout or
prepare-pr installed. It owns loop mechanics, not a repository's repair policy.
For Kiro Crew PR CI AI comments ONLY, MUST load
[prepare-pr: Review repair routing](../prepare-pr/SKILL.md#review-repair-routing)
before any fix. That procedure requires model-pinned repair subagents; parent
self-fixing does not satisfy it. Prepare-pr owns that repo's local reviewers,
PR gates, dispositions and publication rules. Do not require it for other work.

## Choose one driver

Use a structured monitor only when its typed provider observes every fact needed
to decide the objective. Use the finite legacy path for evidence it cannot see.
Structured watches support dashboard, Slack, and Discord sessions.
On Webex, use the finite legacy path even for a supported pull request.

| Work | Driver |
|---|---|
| User is waiting, total work under 30 minutes | Bounded in-turn `wait` + poll |
| One public GitHub PR, readiness decided entirely by typed provider facts | `monitor_watch` |
| Generic comments/advisory findings, or a required final report or notification | Finite `monitor_start` with `gate=false` |
| Act on a schedule, multiple subjects, unsupported ticket or deployment | Finite `monitor_start` |
| Fresh-session work needing no approval-bound tools | `cron_add` |
| Post-merge cleanup after verified merge | Script cron, roughly every 5 minutes |
| External system calls back | `register_hook` |

Never use an agent cron or `HEARTBEAT.md` to fix and push review findings. Cron
has no owning chat slot's trust and can time out at tool approval while still
recording `last_status: ok`; heartbeat's allowlist has no shell or push.
The bundled `pr_watch.py` script remains for existing jobs only; do not copy or
register it for new babysit work. Use one session-owned driver, not two watchers.

### Structured GitHub watch

Use this only for pull-request lifecycle, mergeability, review decision,
unresolved review threads and check conclusions:

```
monitor_watch(kind="github_pull_request", target=<full PR URL>,
              objective="review_ready", interval_secs=300,
              max_runtime_secs=14400, wake_instructions=<actions and exit>)
```

Inspect the schema for positive `max_agent_turns`, `max_tokens` and
`max_provider_errors` budgets. Ordinary unchanged/pending/retry/terminal probes
spend no agent turn; a new actionable fingerprint wakes the owning session at
most once with a bounded summary. Fetch logs, comments or diffs only when needed
on that wake. The token cap applies only when usage is reported;
`token_usage_known` exposes that gap, while runtime and completed-turn caps
remain hard fallbacks. Provider errors are bounded too.

The reply is a pending application request, not proof that the monitor armed.
END THE TURN so the owning session can apply it. The operator can confirm it in
the dashboard; call `monitor_inspect()` at the start of a later user/wake turn,
not after arming in the same turn. It takes no monitor id, target or session key.
Its retained session-bound state is authoritative.

A terminal success uses zero model turns, so a structured watch does not create
a final reporting turn. If the user requires a final report or notification even
when no action is needed, use the finite legacy path.

The provider does not observe generic issue/pull-request comments or advisory
review findings outside review threads and check conclusions. When readiness
depends on those, call the finite legacy path directly with `gate=false`;
a typed fingerprint cannot stand in for missing evidence.

Use `monitor_update` without an id for cadence, positive budgets or
`wake_instructions`: these preserve the comparison baseline. Changing `target`
or `objective` starts a new baseline. Terminal records are read-only; restarting
requires an explicit new watch. Stop with `monitor_stop(reason=...)`, retaining
`user_stop`; `autonudge_stop` is a compatibility alias, not the preferred path.
A retained user stop cannot be replaced by rearming: ask its owner to use
**Clear stopped monitor** (legacy **Clear stopped goal**) or the dashboard's
explicit restart action. Never erase that evidence or retry the refusal.

## Example: legacy comment-aware recipe

```text
monitor_start({
  "message": "Watch https://github.com/kirodotdev/KiroCrew/pull/123. On each injected cycle, inspect current review comments and checks. Act only on a real change. Kiro Crew AI repairs MUST follow prepare-pr Review repair routing with model-pinned subagents and parent verification; commit and push only if authorized. If ready, terminal, blocked, stopped by the user or out of budget, report the outcome and any open findings, then call autonudge_stop with a reason.",
  "interval_secs": 300,
  "max_cycles": 24,
  "max_runtime_secs": 14400,
  "gate": false
})
```

Replace the example URL with the real target. Keep `gate: false`: provider-fact
gating cannot observe generic comments or advisory findings and could suppress
a cycle while that evidence waits. All three limits must be positive and finite;
never use `0` for unlimited work. Every delivered legacy cycle is a full model
turn even if nothing changed. Follow the execution steps below.

### Same-session timer

The legacy timer binds to THIS session. Slot-less subagents, cron, webhook and
task-runner turns cannot arm it; use bounded in-turn wait/poll instead.
Loops persist in the data home's `autonudge.json`. User turns defer a due fire
until their end but do not restart its countdown. Each delivered cycle's next
countdown starts after its own work, so cadence includes turn time. Busy cycles
can be skipped, not queued, and skips do not spend `max_cycles`. Gateway restarts
preserve state. Keep Slack/Discord unattended turns small; they have a 30-minute
bound and no blanket grant: normal PreToolUse governance and approvals apply.
A rejected or timed-out approval is a stall, not success or permission to loosen
security. Fix, commit and push only within the user's authorization.

- Naming exactly ONE public GitHub PR by full URL in `message` can select
  observation gating; a bare number or owner/repo shorthand cannot.
- `gate=true` is the default. Quiet subjects avoid turns, with eventual delivery
  after enough quiet intervals. Use `gate=false` for generic comments/advisory
  scans and duties that act despite silence, such as chasing a missing reviewer
  or tracking a moving base. `max_cycles` counts DELIVERED turns, including
  quiet-floor deliveries and fallbacks, not just useful changes.
- A short `banner` keeps a long instruction out of stored transcript rows while
  the model receives it whole. Dashboard only; channel loops refuse it with 400.
  `monitor_update(banner="")` clears it.
- `monitor_update(message?, interval_secs?, max_cycles?, max_runtime_secs?, banner?)`
  preserves the count and omitted fields. Do not mix these with structured-only
  `target`, `objective`, `max_agent_turns`, `max_tokens`, `max_provider_errors`
  or `wake_instructions`. On Webex, stop and create a new finite loop instead.
- One automation occupies a session. `monitor_start` is create-only; update an
  active loop rather than replacing it. A budget-paused legacy loop resumes only
  by raising the bound it reached with user authorization. Manual pauses and user
  stops stay preserved; retained evidence needs the owner action above. Do not
  rearm merged/closed work as if it still needed repairs.

## Execute the legacy loop

1. Write a self-contained instruction naming subject, allowed actions, success,
   blocker/stall conditions and stop tool. Include worktree/branch and whether
   pushes are authorized. Pass positive cycle and runtime bounds. For Kiro Crew
   preparation use prepare-pr's budget; never raise its cap yourself.
2. Load the MCP tool by exact `tool_search` ID before calling it. Report only
   that monitoring was REQUESTED and END YOUR TURN immediately. Application
   happens when the turn's result is processed, not synchronously with the call.
3. On a later turn, verify the applied transcript notice and session-bound state
   through `monitor_inspect()` where supported, the dashboard monitor/goal-loop
   surface or `GET /api/autonudge`. Prepare-pr's `monitor_armed.py` is an optional
   legacy helper. Check `cycle_count` advances on later cycles; never claim an
   acknowledgement alone proves active monitoring.
4. A create-only refusal means a loop may already be active: inspect state,
   never add wait/poll beside it. Missing hosting context permits bounded in-turn
   wait/poll. A retained-stop refusal needs the owner, not a retry or another driver.
   Read an absent/frozen loop's reason and report the blocker; do not silently
   substitute an unbounded poll or retry arming before the pending request applies.
5. Each wake reads durable state, checks the subject and acts only on real
   changes. Report real signals only; update stale instructions in place.
6. Persist the progress key AND streak below. Keep cycle output small; the same
   conversation grows each turn and compaction can erase an in-memory counter.
7. On success, terminal state, user stop, external blocker or spent budget,
   report the real outcome and any open findings, then call `autonudge_stop`
   with a reason. A cap is a backstop, never successful completion. Do not rearm
   automatically after a user stop or spent budget.

## Read state from the host

Read lifecycle every cycle: merged, closed or declined means report that outcome
and stop, regardless of check colors. Ask the host for its aggregate verdict;
never hand-roll a green result by filtering only check conclusions. Unknown or
unmapped states fail closed. Mergeability can be asynchronous: `unknown`,
`checking` or `unchecked` means wait, never pass; a closed object may never settle.
Collapse superseded attempts to the newest per workflow/check identity.

Green checks do not answer review threads or advisory findings. Establish once
per repo what its reviewer check means, and repeat when its fleet changes:
red may mean successful review with findings; green may carry findings; a job log
may hold a verdict that never posted; identical trees may receive different
verdicts; duplicate dispatch can inflate failure counts. If conclusions are not
trustworthy, use the current-head marker/comment body and needed job logs.
Record that finding in the repo's own tracker, not this general skill.

On GitHub, read the repo's aggregate commit status when present, otherwise the
full `statusCheckRollup` (`gh pr view <n> --json
state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup`), and read
review threads plus comments/reviews for the current head separately. The rollup
is a `CheckRun | StatusContext` union: classify `.conclusion` AND `.state`,
respecting the repo's named aggregate status. A same-named CheckRun is not that
commit status.

Other-host guidance is not live-verified here. On another host (GitLab, Bitbucket,
an enterprise forge) derive the same two answers, lifecycle plus aggregate merge
verdict and a separate review-thread axis, from that host's own verdict fields,
using its CLI or API help rather than field names remembered from elsewhere. Wait
states, hidden allowed-failure jobs and "no single merge verdict" hosts all fail
closed until you have confirmed the mapping on the actual host.

Conflict or `BEHIND` requires an authorized sync, not another unchanged poll:
GitHub cannot build a conflicted merge ref, so `pull_request` checks may never
start while old checks look green. Rebase unambiguous conflicts under the repo's
history rules, re-verify and push only with authorization; escalate ambiguous
conflicts. A draft or `CHANGES_REQUESTED` also survives waiting. Read the reviewer:
a product hold needs a human decision, not repeated patches. Report it once and
stop with the blocking review quoted.

### Optional GitHub helper

If prepare-pr is installed, run its complete script bundle from the TARGET repo,
not the skill directory. Resolve an absolute `SKILL_DIR` first from the active
installation; never use an unresolved default expansion as a path argument.

```bash
python3 "$SKILL_DIR/scripts/pr_status.py" <pr#> --json --reviewers <known-lanes>
python3 "$SKILL_DIR/scripts/pr_findings.py" <pr#>
```

Pin a known fleet with `--reviewers` / `PREPARE_PR_REVIEWERS`; discovery mode only
checks stamps it finds and cannot detect an absent lane. Set the repo's aggregate
via `--readiness-context` / `PREPARE_PR_READINESS_CONTEXT` where needed. The scripts
require sibling `_review_contract.py`; missing scripts are not permission to
pretend the helper ran. Use the host interfaces above for general monitoring.

- `0`: checks clean, but still apply the readiness conditions below.
- `10`: genuinely running. End the cycle silently, without rereading bot bodies,
  logs or diffs; it neither counts nor resets the stall streak.
- `20`: use findings and the reason before acting. Terminal PR state means stop;
  stale reviewer stamps can mean a comment-triggered bot has not posted yet,
  not a code defect. `?`/JSON `null` threads mean unknown, never zero.
- `2`: environment error, escalate rather than loop on it.

`--json` appends a last-line object without changing the exit code. Compare ONLY
`progress_key` for stalls. Its `advisory` fields include unresolved threads,
findings, stale/blocking reviewers and `elided_stamp_reviewers`. Report an elided
stamp once per head as a lane-quality note, not a new blocker; the script already
checks whether that shortened stamp matches the current head.

Always use the target checkout even for a full PR URL: thread lookup also uses
cwd, so a foreign checkout can mix two repositories. An unreadable/discarded CI
rollup is an environment blocker, not a bug count or a passing empty list.

### Readiness and handoff

For comment-aware legacy monitoring, declare review-ready only when all hold:

- The host aggregate (or optional `pr_status.py`) is clean for the current SHA.
- Threads are confirmed resolved; an unknown count needs inspection, not a guess.
- Every bot/human concern has an individual answer, including advisory comments
  on passing checks. The current head has every required reviewer's fresh verdict
  and no blocking marker. Do not wait for zero findings: a rebutted or deferred
  finding can remain visible after it is answered.
- No conflict, behind-base state, draft or changes-requested hold remains.
- No current-head finding lacks a disposition. For Kiro Crew, use prepare-pr's
  disposition contract rather than inventing a second ledger format.

Never stop silently with an unanswered finding. A blocker, budget or user stop
requires an open-findings handoff, not a success claim. Structured success proves
only the provider facts above; it does not satisfy this comment-aware contract.

## Stall tripwire

For legacy cycles, persist a JSON progress key AND an integer streak count,
read before each settled cycle. Use a durable file in the active data home's
`workspace/` beside loop state, e.g. `.babysit-key-<loop-id>`, NOT scratch/TMPDIR
or conversation memory. Resolve the home first; a tilde inside a shell default
does not expand. The engine does not track this streak for you.

For GitHub the optional helper emits these key fields; otherwise derive equivalent
fields from the host verdict above. GitLab and Bitbucket need their own equivalent
key before applying this rule:

| Field | Meaning |
|---|---|
| `head_sha` | Exact revision |
| `failing_checks` | Sorted workflow-qualified identities, not just names |
| `checks_failing` | Failure count |
| `readiness_kind` | Running, failing or unpublished |
| `exit_code` | Verdict |
| `status` | Verdict reason, so changed blockers count as progress |

On a settled cycle (`0` or `20`), matching key increments the streak; a new key
sets it to 1. Three byte-identical settled keys with no push means stop and
escalate when nothing can be changed without a human. Apply a diagnosed fix
within authorization instead of waiting for this tripwire. Running (`10`) cycles
are skipped, not counted AND not reset; environment (`2`) escalates. The wall-clock
budget bounds a wait that never settles.

Exclude finding counts (rewording/rebuttals keep changing them) and thread counts
(unknown API results are not progress). Preserve both key and count through
compaction. A deliberate stop reason names success, the blocker or the terminal
state; merely exhausting a cap is never success.

With an active loop do not also wait/poll. On a deliberate early end of `wait`,
read its end reason; do not reissue a wait the user stopped.

Kill switches: the driver's stop tool, dashboard automation popover, cycle/turn
cap and runtime budget. A STOP sentinel exists only if explicitly configured
through `stop_sentinel_path` on the HTTP arming path; `monitor_start` creates none.
Treat `[auto-nudge cycle N]` as your scheduled wake, not a new human request.
