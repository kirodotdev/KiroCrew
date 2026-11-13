---
title: A PR item stays open until its pull request lands, the work-ledger probe watches it, and a forced floor wake carries live facts
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: ee52eacb74
doc-pr: null
implementation-prs: []
tracking-issues: [36751]
supersedes: []
superseded-by: []
---

# RFC: Track a PR item until it lands, and give the forced floor wake facts

Status: accepted. The repository owner asked for this on 2026-11-13 in the
conductor chat and merges this document; it takes effect when it merges.
Nothing is built and it changes no code. The code lands in the phases of §7,
each in its own PR rebased onto the base that carries this file. Every code
reference was read at `ee52eacb74`.

This document lands on its own because it changes a ledger write contract (a
`pr_checks` bar is stored as a landed bar, §3.2) and what the work-ledger probe
reads and wakes on (§3.4), and the First Principles lane reads that decision
from the base branch.

Related: [`rfc-work-ledger-resource-pressure.md`](rfc-work-ledger-resource-pressure.md)
(the other host fact that wakes a conductor; same synthetic-entry pattern),
[`rfc-conductor-drift-check.md`](rfc-conductor-drift-check.md) and its
amendment from [#36507](https://github.com/kirodotdev/KiroCrew/pull/36507)
(the drift read and the mid-round re-plan a floor turn now has facts for),
[`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) (the ledger),
[`rfc-work-ledger-person-wait-hold.md`](rfc-work-ledger-person-wait-hold.md),
[`rfc-work-report-wake-at.md`](rfc-work-report-wake-at.md) and
[`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md).

## 1. The ask

The owner, verbatim: "我希望我们这些改动应该有一个可以解决下面的问题。当worker完成任务，将PR推绿，conductor并不知道在之后的轮询里要检查PR是否merge了。我觉得这里有两个问题，第一是验收条件不对，只把推绿当作目标而不是merge。这个不应该在我们的instructions里改，而是应该给出这个能力。第二个就是说的，在被强行wake，比如过了N轮，conductor缺少主动纠错和检查的动作"

In English: when a worker pushes its PR green, the conductor does not know to
check later whether it merged. Two problems. First, the bar is wrong: green is
the target, not merge, and the fix is a capability, not more instructions.
Second, a forced wake (after N quiet rounds) gives the conductor nothing to
correct or check with.

## 2. What exists, and the gap

| Part | Where | Today | Gap |
|---|---|---|---|
| Landed bar | `_pr_merged` in `goal-conductor/scripts/accept_eval.py` (same file under `goal-ledger-conductor`) | `gh pr view --json state`: `MERGED` pass, `OPEN` pending, `CLOSED` fail; `repo` required | Nothing picks it by default; the skill calls `pr_checks` "the common case" |
| Pending budget | `is_pending_overdue` in `work_ledger_rules.py`, `PENDING_BUDGET_SECS` (24 h) | A read flag after a day of `pending` | Flags; never wakes on the merge itself |
| Item states | `TERMINAL_ITEM_STATES` in `work_ledger_records.py` | `open` until `accepted` / `rejected` / `abandoned` | No "worker done, PR not landed" shape |
| Ledger probe | `WorkLedgerProbe.observe` in `probes/work_ledger.py` | Reads items and event tails; wakes on reports, stalls, undelivered reports, all-closed | Reads no PR state |
| PR reader | `fetch_state` / `_fetch_core` in `probes/gh_pr.py` | One `gh pr view` per PR through `_Transport` (budget, retry, `x-ratelimit-*` headers, SEL audit) | One PR per call, used by single-PR monitors only |
| Floor | `_quiet_floor`, `_IDLE_LEDGER_QUIET_FLOOR` in `autonudge_service/gate.py` | After 10 quiet ticks (40 when idle) the tick delivers anyway | `_work_ledger_wake_text` returns `""` with no briefs, so the floor turn gets the bare patrol text |

Evidence from 2026-11-13: the items for
[#36529](https://github.com/kirodotdev/KiroCrew/pull/36529) and
[#36302](https://github.com/kirodotdev/KiroCrew/pull/36302) were closed at
green. [#36540](https://github.com/kirodotdev/KiroCrew/pull/36540) and
[#36507](https://github.com/kirodotdev/KiroCrew/pull/36507) were found merged
only by floor turns that re-read the board.

## 3. Decision A: land tracking

### 3.1 One bar, two stages

`pr_merged` is the landed bar; no new kind. It gains one optional field,
`ready`, the stage at which the worker's `done` is due:

```json
{"kind": "pr_merged", "pr": 36752, "repo": "kirodotdev/KiroCrew", "ready": "pr_checks"}
```

- The item's bar is `pr_merged`. Only a merge passes it.
- The worker's bar is `ready`. `work_brief` returns it as `ready_when`, so the
  worker reports `done` at green and stops; nobody needs a rule telling it to
  keep a monitor armed through the merge.
- `accept_eval` on an open PR still answers `pending`, and its `why` names the
  stage from the same call (`--json state,mergeable,isDraft,statusCheckRollup`):
  `ready` (every check passed, mergeable), `running`, `red <failed>/<total>`,
  `conflicting`, `draft`. A `gh` that cannot answer stays `error`, never
  `fail` (the seam of [#33688](https://github.com/kirodotdev/KiroCrew/issues/33688)).

`ready` is metadata `accept_eval` reads only for the stage text, so
`ACCEPTANCE_READ_FIELDS["pr_merged"]` stays `("pr", "repo")`.

### 3.2 The default is landed

`create` and `accept` store a `pr_checks` bar that names a `repo` as
`pr_merged` with `ready: "pr_checks"`. The reply carries
`acceptance_rewritten: "pr_checks->pr_merged"`, so the conductor sees what was
stored. A `TBD` `pr` is kept as written and promoted the same way.

- Opt-out: `"until": "checks"` keeps `pr_checks` as the final bar, for an item
  whose job is green CI on a PR someone else lands.
- A `pr_checks` bar with no `repo` is stored as given. The gateway cannot read
  a PR without its repo, so the row carries `land_untracked: true`. The code
  carries `shortcut: no repo, no land watch; upgrade when the ledger records the
  conductor's repo`.

This is the capability the owner asked for: the ledger stores the right bar,
so neither conductor needs a new instruction. The two skill lines that call
`pr_checks` "the common case" are corrected to name the landed default, so the
text does not contradict the store.

### 3.3 Awaiting land

An item is **awaiting land** when it is open, its bar is `pr_merged`, its
latest worker report is `done`, and its latest verdict is `pending`. Derived
at read time, like `is_stale`; no new state or status.

- `work_ledger_read` shows `awaiting_land: true` and the stage on the row.
- It does **not** count toward the goal's `concurrency_cap`. It runs no worker
  turn, and the cap bounds parallel work. It still counts toward
  `MAX_ITEMS_PER_CONDUCTOR` (32), the store bound.
- A re-seeded worker reports `progress`, so the item counts again. The cap
  refuses only `create`, so a re-seed can overshoot it by one; that is allowed.
- `is_stale` already treats a `done` report as the conductor's move, so an
  awaiting item never raises a stall wake.
- `pending_overdue` still applies after 24 h, so a PR nobody merges is
  re-armed once and then closed `abandoned`, as today.

### 3.4 The probe watches the PR

`WorkLedgerProbe.observe` reads PR facts for every open item with a concrete
`pr_merged` bar, after the ledger read, in one batched call:

- New `gh_pr.fetch_states(pairs, transport)` beside `fetch_state`: one
  `gh api graphql --include` query with one alias per PR (at most 20 per query,
  so at most 2 queries for 32 items). Per PR: `state`, `mergedAt`, `mergeable`,
  `isDraft`, `headRefOid`, and the head commit's `statusCheckRollup { state }`
  with `checkRunCountsByState` and `statusContextCountsByState`. It reuses
  `_Transport`, so the budget, retry, header parse and SEL audit are the ones
  the single-PR monitor already uses.
- `repo` is validated by `ACCEPTANCE_GH_REPO` before it reaches the query.
- The facts are kept on the probe as `self.pr_facts` with `observed_at`, the
  way `self.revision` is kept. Decision B reads them; nothing else fetches.

Wakes, one per token per item, through the existing `_spend` (so the 12/h
item budget and the seen set apply):

| Fact | Items | Token |
|---|---|---|
| `MERGED` | every open PR item | `land:merged` |
| `CLOSED`, not merged | every open PR item | `land:closed` |
| `mergeable` is `CONFLICTING` | awaiting land | `land:conflict:<head>` |
| rollup `FAILURE` or `ERROR` | awaiting land | `land:red:<head>` |

Tokens are levels, not edges, so no previous reading is stored: a re-read of
the same fact is masked by the seen set, and a merge that happened while the
gateway was down wakes on the first tick after start. Conflict and red wake
only for awaiting items, because a working worker owns its PR and a second
driver would collide with it.

The brief text holds only enums and numbers, never PR-authored text:
`[work-ledger wake] item it_3a93: PR #36752 merged 16:40Z; its bar passes.`
That keeps the pointer-not-board fence `_work_ledger_wake_text` documents.

### 3.5 What the conductor does

Nothing new to learn. The wake names the item; the existing pointer says
`accept_eval(item_id=...)`; `accept_eval` returns `pass` on a merge and the
conductor closes it, as it does any passed item. On `land:conflict` or
`land:red` it re-seeds the worker, as it does any failed verdict. Both
conductors arm `watch="work-ledger"` and use the same `accept_eval` and
ledger, so Ultra Mode (team lead, `goal-conductor`) and Bot conductors
(`goal-ledger-conductor`, `crewmate_prompts.json`) get the same behaviour
with no prompt change. The gateway does not close items itself: the
conductor stays the only verdict writer.

### 3.6 GitHub cost and rate limits

- One GraphQL query per conductor per 300 s tick with any PR item: 12 an hour.
  A query this shape costs 1 point; ten conductors spend about 120 of the
  5,000 points an hour (2.4%).
- Today a worker keeps its own `gh_pr` monitor through the merge: every 600 s,
  `_fetch_core` plus the check pages and statuses, several REST calls a tick.
  With `ready`, the worker stops at green, so the total goes down.
- The calls use the operator's `gh` login. The CI review lanes' "API rate limit
  exceeded for installation" is the Actions installation token, a separate
  bucket, so the two do not starve each other.
- `_Transport` reads `x-ratelimit-remaining` / `x-ratelimit-reset`. At or below
  `_RATE_LIMIT_FLOOR` (2), or on a 403/429 rate-limit answer, the probe skips
  PR reads until the reset. The ledger tick stays `fetch_ok`; PR facts read as
  unread. An unread never wakes and is never shown as red.

## 4. Decision B: a forced floor wake carries facts

### 4.1 The digest

On every tick the probe builds `self.digest` from what it already holds: the
items, the liveness resolvers, and `self.pr_facts`. The gate keeps it on the
`MonitorState` in memory beside `ledger_revision`. When the fire is a floor
delivery on a work-ledger watch (`claimed_floor` in `firing.py`),
`_work_ledger_wake_text` returns `ledger_wake.floor_digest_text(digest)`
instead of `""`, followed by `WAKE_READ_POINTER`. A wake turn keeps its briefs
and no digest. A floor on a busy slot is declined as today; the digest never
rides the steer.

One line per open item:

```text
[board digest 16:40Z] 5 open
it_3a93 pr#36752 MERGED | worker idle | done 52m | transcript still | -> close
it_77ab pr#36700 OPEN red 2/97 | worker idle | done 20m | transcript still | -> reseed
it_c0de no pr | worker idle | progress 41m stale | transcript still | -> reseed
it_9f12 pr#36618 OPEN ready | worker idle | done 3h10m | transcript still | -> wait
it_51aa pr#36690 OPEN running 40/97 | worker running | progress 8m | transcript moved | -> none
```

- **Fields**: item id; PR number and state (`MERGED`, `CLOSED`, `OPEN` with
  stage and failed/total counts, or `unread` with the read's age); worker
  `running` / `idle` / `closed` / `remote`; latest status and minutes since it;
  `stale` when `is_stale` says so; whether the worker's transcript grew since
  the conductor's last delivered turn; a hint from a fixed set.
- **Hints**: `close` (merged), `decide` (closed unmerged), `reseed` (awaiting
  land and red or conflicting, or stale), `drift-read` (transcript moved but no
  report for 30 min: the `rfc-conductor-drift-check` read), `wait`, `none`.
  The hint is the gateway's reading, not an order; the conductor still decides.
- **Transcript moved**: the worker slot's transcript length (the index
  `session_read_message`'s `since` pages by) against the length stored when
  the conductor's last turn was delivered. Read through a new injected
  resolver, defaulting to unknown (`transcript ?`), like `worker_running`.
- **Order**: actionable hints first, then `wait`, then `none`.
- **Bound**: at most 20 lines, each at most 160 characters, the whole at most
  `MAX_WAKE_BRIEF_CHARS` (4,000). Past 20: `+N more: work_ledger_read`.
- **Content**: ids, enums, numbers and times only. No title, summary, decision
  or PR text, so no worker- or third-party-authored prose enters the prompt,
  and the fence `_work_ledger_wake_text` documents still holds.

### 4.2 What the floor turn can then do

The floor turn reads the facts first, so it can close a landed item, re-seed an
idle or red worker, run the drift read on a worker whose transcript moved
without a report, or re-plan one item under the #36507 amendment, without
first spending tool calls to rediscover the board. It still runs
`work_ledger_read` before any write, as the pointer says.

### 4.3 Cost

- No GitHub call: the digest reads A's cached facts. The transcript length is
  an in-process lookup.
- A digest is at most 4,000 characters (about 1,000 tokens), sent only on floor
  turns: every 50 min at the shipped floor (10 ticks of 300 s), every 200 min
  idle.

## 5. Non-goals

- The gateway closing items on a merge. The conductor stays the only verdict
  writer.
- A webhook. Polling at the watch's 300 s tick is enough and needs no inbound
  route.
- A new tool, work status, item state, event kind or config key.
- Digest text on wake turns or steers.
- Forges other than GitHub.

## 6. Tests the code PRs carry

Phase 1 (probe and wake):

1. `fetch_states` builds one aliased query for N pairs, splits past 20, and
   maps a missing alias to unread. Injected transport.
2. Probe: `MERGED` wakes once (`land:merged`), a re-read is quiet; `CLOSED`
   wakes once; `CONFLICTING` and rollup `FAILURE` wake for an awaiting item
   and not for one whose worker last reported `progress`; a new head wakes
   again.
3. Rate limit: remaining at the floor, or a 403 rate-limit answer, skips PR
   reads until the reset; the tick stays `fetch_ok`; no wake; the digest shows
   `unread`.
4. A wake brief carries no PR-authored string (a PR title with a marker never
   appears in the delivered text).
5. `accept_eval` `pr_merged` stage text for ready, running, red, conflicting,
   draft; a failed `gh` is `error`.

Phase 2 (default bar):

6. `create` and `accept` rewrite `pr_checks`+`repo` to `pr_merged`+`ready`, and
   say so; `until: "checks"` keeps it; no `repo` keeps it with
   `land_untracked: true`.
7. `work_brief` returns `ready_when` for a `ready` bar.
8. `awaiting_land` is true only for open + `pr_merged` + last `done` + pending;
   a `create` past `concurrency_cap` counts awaiting items out and is admitted;
   the `MAX_ITEMS_PER_CONDUCTOR` refusal still counts them.
9. Parity: Ultra Mode and Bot conductor read the same rewritten bar
   (`test/test_mate_conductor_parity.py`).

Phase 3 (digest):

10. Floor delivery on a work-ledger watch prefixes the digest; a wake delivery
    does not; a steer does not.
11. Bounds: 25 items give 20 lines and `+5 more`; a long id or count never
    passes 160 characters; the whole never passes 4,000.
12. Hints: each fact maps to its hint; order puts actionable first.
13. Transcript resolver absent gives `transcript ?`; a grown length gives
    `moved`.

End to end (pod-e2e, one run): one conductor, one PR item with a fake forge
answer. Worker reports `done` at green; the item reads `awaiting_land`; the
fake flips to `MERGED`; one conductor turn arrives with the merged brief;
`accept_eval` passes. Then force the floor and check the digest is in the
delivered prompt.

## 7. Rollout

A first, then B, each its own PR:

1. **Phase 1, watch.** `fetch_states`, the probe read and the four wakes,
   `accept_eval` stage text. Items already using `pr_merged` gain the merge
   wake at once; nothing else changes. About 150 lines.
2. **Phase 2, default.** The `pr_checks` rewrite, `ready` / `ready_when`,
   `awaiting_land`, the cap count, and the two skill lines. About 80 lines.
3. **Phase 3, digest.** `self.digest`, the floor prefix, the transcript
   resolver. About 120 lines.

Each phase is a revert of its own PR. Phase 2 rewrites only new writes; a
stored `pr_checks` item keeps its bar until it closes.

## 8. Known limits

- **Rollup counts every check.** A red non-required lane wakes `land:red`.
  Read on 2026-11-13: #36507 merged with rollup `FAILURE` (1 failed, 78
  passed, 18 skipped). Accepted: the token is per head, so it costs at most
  one turn per head, and the conductor reads the stage and re-seeds or waits.
  The code carries `shortcut: rollup state, not required checks; upgrade by
  reading the base branch's required contexts`.
- **No repo, no watch.** See §3.2.
- **One forge.** GitHub hosts only, as `gh_pr` is.
- **Restart.** The digest and the stored transcript length are in memory; the
  first floor after a restart has PR facts after one tick but shows
  `transcript ?`.
- **Remote workers.** PR facts read the same; liveness reads `remote`.

## 9. Security considerations

- Read-only GraphQL with the operator's existing `gh` login; no new token or
  scope. Every call goes through `github_runner.run_gh` with its SEL audit tag.
- `repo` and `pr` are validated before they reach the query, as
  `accept_eval` validates them.
- Wake briefs and the digest hold no text a third party or a worker can choose,
  so they add no prompt-injection surface and keep the existing fence.
