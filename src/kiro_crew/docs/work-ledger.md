# Work ledger (conductors and workers)

The work ledger is the shared record between a **conductor** session and the
**worker** sessions it dispatched. The conductor breaks a goal into items, stands
up one session per item, and then learns what each worker did by reading a
record — not by reading the worker's transcript.

That is the whole point. A transcript has to be interpreted, it grows without
bound, and it can carry an instruction. The ledger carries schema-bounded fields
instead: a worker writes a status, a summary, and pointers to what it produced,
and the conductor reads them as data.

## When you want a conductor

Use one when a goal is too large for a single session and splits into pieces
that can run at the same time — "clear the flaky-test backlog", "push these six
pull requests green". Each piece gets its own session, its own context, and its
own tab you can open and steer.

A piece qualifies as a work item only when all three hold:

1. **Independent** — it does not consume another item's output. Two pieces that
   hand off to each other are one item.
2. **Assertable** — you can name its completion condition before it starts.
3. **Long-running** — long enough that you would plausibly want to watch it.

Fewer than two qualifying items means one long session is the better shape, and
a conductor would only add overhead.

To start one, open a session on the **`kirocrew-conductor`** agent (see
[Agents](agents.md) for the per-session, per-thread and per-cron selectors) and
give it the goal. Its operating procedure ships as the `goal-conductor` skill.
`kirocrew-ledger-conductor` is a deprecated alias of the same spec, kept for one
release so an existing session or cron that names the old string keeps resolving.

## The item

An item is the unit of dispatch. It holds:

| Field | Written by | What it is |
|---|---|---|
| `title` | conductor | what the item is, up to 200 chars |
| `acceptance` | conductor | the completion condition, stored verbatim |
| `round` | conductor | which dispatch round the item belongs to |
| `decision` | conductor | what the conductor decided and why |
| `verdict` | conductor | the acceptance evaluator's answer |
| `fails` | conductor | how many acceptance attempts came back `fail` |
| `state` | conductor | `open`, or terminal: `accepted` / `rejected` / `abandoned` |
| `status` | worker | `progress` / `done` / `blocked` / `question` |
| `summary` | worker | the worker's own account, up to 500 chars |
| `artifacts` | worker | pointers to what it produced |
| `pr` | worker | a pull-request number it produced |

The conductor writes its half with `work_ledger_record` (one action per call:
`goal`, `create`, `bind`, `decide`, `verdict`, `accept`, `close`) and reads the
whole ledger back with `work_ledger_read`. A worker writes its half with
`work_report` and reads its own item with `work_brief`.

The two sets are disjoint, and that is enforced by the tools rather than by a
rule: the reporting tool takes no parameter that names a conductor field, so a
worker cannot write a verdict, a state, or its own acceptance condition.

**The acceptance condition is named before dispatch, not after.** It is one of
three kinds: `pr_checks` (a pull request's checks are all green), `file` (a path
exists), or `human_approval` (you accept it — legitimate for a design review,
and never machine-evaluated). There is deliberately no "run this command" kind,
so "the tests pass" is expressed as `pr_checks` on the pull request that carries
the work, and CI's verdict is the one that counts.

A condition may name a value that only exists once the item starts — a pull
request number is the common case. The conductor stores it as `TBD`, the worker
reports the real number, and the conductor promotes it into the condition by
hand after looking at it. A worker's claimed `pr` is never read as the bar,
because a worker that could fill in its own bar could point it at somebody
else's already-green pull request.

Limits: 32 items per conductor, and a conductor may dispatch a conductor only
once — depth is capped at 2, so a second-level conductor's own children are
workers. A worker holds one open item at a time.

## Dispatch order: create, bind, seed

The conductor mints the item, attaches the session, and only then sends the seed
prompt. That order is not cosmetic. Binding before seeding means a worker's
first `work_brief` always finds its item; the other order leaves a running
worker with no binding, which is neither visible in the ledger nor recoverable.
A bound item with no seed is visible, and gets seeded on the next cycle.

The seed is the worker's whole contract. A worker session inherits no context
from the conductor beyond it.

## `work_brief` — what a worker reads

A dispatched worker calls `work_brief` first. It takes no arguments: which item
you are bound to is resolved from your own session, never supplied. It returns
the item's `title` and `acceptance` — together, the definition of done — plus
the round, the conductor's latest `decision`, and your own last reported status.

**`decision` is the only field to read as an instruction.** Everything else it
returns is state. It does not return the conductor's goal or any sibling item: a
worker has neither.

A session that is not a dispatched worker gets `not_bound`, which is also how a
root conductor learns it has no parent.

## `work_report` — what a worker writes

One call, four statuses:

| `status` | Meaning | Who acts next |
|---|---|---|
| `progress` | moving, nothing needed from anyone | nobody |
| `blocked` | an external dependency stopped the work | the conductor clears or re-plans around it |
| `question` | a decision the conductor owns is needed | the conductor answers |
| `done` | the worker claims acceptance is met | the conductor verifies |

**`blocked` and `question` differ by who must act.** That is why they are
separate values and not one "stuck". A build the worker does not control is
`blocked`; a choice only the conductor can make is `question`.

Reports belong at real milestones, not on a timer.
`summary` is capped at 500 characters and is **refused rather than truncated**
when longer, so a report that lands is a report that landed whole. Evidence goes in `artifacts` as
pointers — a branch, a commit, a path, a pull request number.

## Why `done` is a claim

A worker's `done` never closes an item. The conductor reads its whole ledger
with `work_ledger_read` — every item, its own derived staleness flags, and a
ready-to-evaluate batch — runs the acceptance evaluator against the item's own
condition, and records the answer with `work_ledger_record` as a `verdict`:

- `pass` / `fail` — final for that cycle.
- `pending` — the condition is not true yet; keep waiting.
- `refused` — the evaluator will not answer the condition as written, and no
  amount of waiting changes that. A **draft** pull request whose checks have not
  finished lands here, because a draft is the author's own "not ready" and a
  readiness gate on the draft flag never resolves — so a `pr_checks` item is
  opened non-draft, or marked ready before the worker reports. A condition that
  names a command to run lands here too.
- `error` — a broken condition or environment, including a bar still set to `TBD`.

So the strongest true thing a worker can say is that it believes the bar is met.
The conductor's own read is also filtered: only items currently reporting `done`
are evaluated, because a world-state check can return a genuine `pass` on
unfinished work — a stub written before the real content, a pull request green
before the last commit.

A conductor stops when every item is accepted, when one item has failed
acceptance three times, when the round or time budget is spent, or when a
decision arrives that no acceptance condition can settle.

## Not the same as the session ledger, or subagents

Two records carry the word, and a third mechanism gets reached for instead of
either. Confusing them is the common mistake:

| | Work ledger | [Session ledger](session-ledger.md) | [Subagents](subagents.md) |
|---|---|---|---|
| Holds | work items shared by two sessions | one session's own goal, phase, next step | a task plus a retained transcript/result, but no shared acceptance ledger |
| Who writes | a conductor and its workers, disjoint field sets | the session itself | n/a |
| Survives | compaction, restart, and the worker's own session ending | compaction and restart | the retained conversation/result for its bounded grace window |
| Unit | an item with an acceptance condition | a phase and a next step | a task string |
| Completion | settled by the acceptance evaluator | the session marks its ledger finished | the parent reads the result |
| Steerable | yes — each worker is its own session you can open | n/a | yes while running via `spawn_steer`; follow-ups use `spawn_continue` while retained |

Reach for subagents for bounded background fan-out that needs no visible,
long-lived workstream or shared acceptance record. Reach for a conductor when
each piece needs its own long-lived session, its own acceptance bar, and a
record that outlives any one transcript.

A conductor keeps both ledgers: the work ledger for the items, and its own
session ledger for its goal, its current round, and the approaches it already
rejected. Items never go in the session ledger — two records that can disagree
is the failure that avoids.

## The `kirocrew-worker` agent

A conductor names `kirocrew-worker` for a leaf item. That spec is a **superset**
of your default agent, not a narrowed one: everything your default agent grants,
plus the two reporting tools. A worker writes files, runs builds and drives git,
so anything a narrowed spec withheld would be something some item needs.

Two things are subtracted. Cron **scheduling** grants, because a recurring job
would outlive the item, the session and the dispatch — the tools stay mounted,
they just no longer run unattended. And opt-in tool sets nobody assigned to the
worker itself, so a server you mounted on your own agent is not thereby handed
to every worker you dispatch.

Because the spec is derived from the default agent on disk, a server you mount,
a grant you add and the model you pick reach the worker at its next refresh. It
is re-derived every gateway start and re-checked before every worker session.
An explicit model pick on the worker file is the one thing carried across.

`work_brief` and `work_report` are auto-approved: a worker that must ask
permission to say it is blocked will not say it, and an unattended dispatch is
exactly the case the ledger exists for.

## No dashboard page

Like the session ledger, the work ledger is **storage the agents read and
write**, not a view you browse. To see where a goal stands, ask the conductor
session — it answers from the record.

## Cleaning up finished ledgers

Nothing reclaims the ledger store automatically — not on tab close, not at
gateway start, and no agent-reachable tool can delete it. Cleanup is one
operator command:

```bash
kirocrew ledger-sweep
```

That is a **dry run**: it lists the session and conductor-work ledgers that look
finished, with each one's kind, store directory, age, and why it qualified, and
deletes nothing. It prints the purge spelling for the window the report was
built with; running it is a second, explicit invocation.

Flags: `--purge` (irreversible), `--older-than-days N` (default 30) and
`--purge-unreadable`, which also removes records the sweep could not parse —
they are listed but kept without it.

The sweep is conservative by design. A conductor holding an open item is never a
candidate at any age, neither is one with no items at all, and each delete is
re-decided under the store's own lock, so a ledger that came back to life
between the report and the purge is refused rather than erased.

## Related docs

- [Agents](agents.md): the conductor and worker specs, and how to switch agents per session
- [Session ledger](session-ledger.md): one session's own durable record — a different ledger
- [Subagents](subagents.md): in-turn fan-out, for work that needs no supervision
- [Monitor loops](monitor-loops.md): the repeated-wake mechanism a conductor patrols with
- [Agent questions](agent-questions.md): how a conductor puts a decision that is not its own to you
