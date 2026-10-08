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
| `reason` | worker | on `blocked` / `question`, when only a person can move it: `approval` / `needs_human` |
| `summary` | worker | the worker's own account, up to 500 chars |
| `artifacts` | worker | pointers to what it produced |
| `pr` | worker | a pull-request number it produced |

The conductor writes its half with `work_ledger_record` (one action per call:
`goal`, `create`, `bind`, `decide`, `verdict`, `accept`, `close`) and reads the
ledger back with `work_ledger_read`. A patrol cycle reads it with `compact=true`
(status columns and derived flags only); `item_id`, `state`, `since` and
`events` narrow a full read. A reply over the tool-result limit comes back as
valid JSON marked `truncated`: event tails go first, then oversized acceptances
are elided, then rows are dropped (closed first, then open oldest-created), and
the newest open item is always kept. A worker writes its half with
`work_report` and reads its own item with `work_brief`. A conductor whose ledger
files read as damaged or missing rewrites them from the crew log with
`work_ledger_rebuild`: every accepted write was recorded there, so the files are a
cache of that record. It takes no arguments, acts only on the caller's own ledger,
and is refused when the crew log is off.

The two sets are disjoint, and that is enforced by the tools rather than by a
rule: the reporting tool takes no parameter that names a conductor field, so a
worker cannot write a verdict, a state, or its own acceptance condition.

## The crew log must be on

Every write here is recorded in the crew log, so the whole board depends on it.
The crew log is on by default. With `KIROCREW_CREW_LOG` set to a falsy value (`0`,
`false`, `no`, `off`) or to any value it does not recognise at gateway start, `work_ledger_record` and `work_report` both
answer `409 crew_log_off`, and `work_ledger_rebuild` is refused for the same reason.
Reads are unaffected. To switch it back on, unset the variable (or remove it from
`~/.kiro/crew/.env`) and restart the gateway.

A deployment that sets the flag off has no board writes at all, so leave it on
wherever a conductor runs, rather than discovering the refusal from a worker that
cannot report.

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

Limits: 32 open items per conductor (an item in a terminal state -- accepted,
rejected, abandoned -- stays on the board, listed and readable, and does not count
toward the open cap) and 256 items stored per conductor in total, open and closed
together, which is also the crew log fold's per-board ceiling, so a board is
always folded whole. The stored bound is measured against `created_total`, a
monotonic counter in the conductor header of every item the board has created over
its life: each create bumps it under the conductor lock, deleting or losing an item
record does not reclaim capacity, and a create is refused rather than counted when
the header cannot be read. A rebuild from the crew log sets the counter to the
records the rebuilt board holds. Closed items stay on the board until that stored
bound; a board at it refuses further creates (`item_store_full`) until the finished
ledger is purged (see "Cleaning up finished ledgers"). A conductor may dispatch a
conductor only once — depth is capped at 2, so a second-level conductor's own
children are workers. A worker holds one open item at a time.

## Other refusals

With the crew log on, a write can still be refused. Each answer carries a `code`:

| Code | What happened | What to do |
|---|---|---|
| `503 crew_log_unrecorded` | The store took the write but the crew log did not confirm it, so the write is undone and counts as not done. If the undo itself fails, the board turns `cache_dirty`. | Retry the write. For a `create` whose answer carries an `item_id`, do not repeat the `create`; read the board first. |
| `409 cache_dirty` | An undo failed, so the board's files may not match the record. Reads and writes of the board (`work_ledger_read`, `work_ledger_record`, `work_brief`, `work_report`) are refused until it is repaired. | Run `work_ledger_rebuild`. To keep the files as they stand instead, remove the marker file the message names. |
| `400 work_entry_too_large` | The write does not fit one crew-log line. | Shorten the fields you sent. |
| `400 work_item_too_large` | The item predates the record and, written whole, does not fit one line; usually its acceptance is too large. | The conductor shrinks it with `work_ledger_record action=accept` and a smaller acceptance. |
| `409 crew_log_incomplete` | `work_ledger_rebuild` refused: the crew log cannot rebuild this board completely (an unreadable unit, or a goal the log never recorded). | Follow the message: repair those units, or record the goal again, then rebuild. |

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

**`reason` says when a `blocked` or `question` report waits on a person.**
`approval` means a tool approval timed out or is waiting, and `needs_human`
means only a person can unblock it. A wait on a build, service or other item
carries no reason. It is refused on `progress` and `done`, and every report
replaces it. When every open item waits on a person (`approval` or
`needs_human`) and nothing on the ledger changed since the conductor's last
turn, the conductor's `work-ledger` watch holds: no turns, no extension, and
the goal popover says it is waiting on you. A worker's new report releases it;
the conductor's own writes do not.

Reports belong at real milestones, not on a timer.
`summary` is capped at 500 characters.
A longer one is **cut to the cap, not refused**: the stored value carries a note
saying how many characters were dropped, and `work_report`'s reply repeats it
with the length the caller sent, so a worker learns it overran in the same
round-trip that accepted the report instead of spending another one rewriting
it. Evidence goes in `artifacts` as pointers — a branch, a commit, a path, a
pull request number. A `done` with no `artifacts` and no `pr` is refused
`done_without_evidence`; a check that could not run is still an artifact when
it says so, e.g. `{"tests": "not run: harness unavailable"}`.
`artifacts`, `pr` and `status` are still refused when they
are wrong or oversized: a truncated pointer is a broken pointer, while prose cut
at the cap still reads.

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

A conductor stops patrolling only when every item is terminal (accepted,
rejected or abandoned), or when the user says stop, in words or through a round
or time budget they set. An item that fails acceptance three times is closed
`rejected` and the rest keep going. A decision that needs a person parks only
that item: the conductor asks about it and keeps patrolling the others.

Spend is bounded in code, not only in the prompt. Each goal may create at most
`item_cap` items, closed ones and every round's re-plans included. The default
is 20; a `goal` write sets it (`item_cap`, 1 to 256). A `create` past it is
refused `goal_item_cap_reached`, and the board writes nothing. That is a
needs-human spend stop: the conductor pauses, asks the user, and raises
`item_cap` if they agree. The loop is not stopped. Only a goal write with
`new_goal=true` (a goal the user replaced) starts a new count; rewording the
goal, moving the round or raising the cap keeps it. A board that never records
a goal, such as a pipeline or security conductor's queue, has no goal cap. Two rounds in a row with nothing accepted also stop new dispatches
until the user answers.

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

## Crew board

The work ledger is storage the agents read and write, and the dashboard shows it
read-only. A session that owns a work ledger has **Crew board** in its session
actions menu; other sessions do not show the entry. It opens
`/crew-board?conductor=<session key>`, which lists the goal, the round and the
items: those needing your decision first, then open work, then finished items.
Each row shows what the worker reported and how the conductor ruled.

An item whose conductor session is gone is marked orphaned, because nothing reads
its reports any more. For such an item the board offers **Stop current turn**,
which cancels the worker's running turn and keeps its session, branch and
reports. There is no take-over. To ask where a goal stands, you can still ask the
conductor session — it answers from the record.

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
