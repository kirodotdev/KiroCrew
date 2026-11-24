# The two ledgers, and working for a parent conductor

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## When a conductor dispatched you

A parent conductor can put a decomposable item on a second
`kirocrew-conductor`. If that is you, you are also that item's worker. Your
parent reads ITS ledger, not yours, so an item you never report on reads to it
as stalled.

So the worker contract applies to you on top of everything in this skill:

- **`work_brief` before Round 0.** Its `title` and `acceptance` are your goal's
  definition of done, and its `decision` field is your parent's instruction —
  the ONE field you treat as one. Everything else it returns is state. A root
  conductor gets `not_bound` here, and that answer is how you know you have no
  parent: proceed with the user's goal instead.
- **`work_report` at round boundaries, not on a timer.** `progress` when you
  dispatch a round or close one; `question` when a decision belongs to your
  parent and not to you (the needs-human checklist under Stop conditions,
  one level up);
  `blocked` when an external dependency stops the whole goal; `done` only when
  every item in your own ledger is accepted — put the evidence in `artifacts`
  and the pull request, if the acceptance names one, in `pr`.

Your own children are workers only unless the depth cap allows more (Which agent).

## What the ledger holds, and what your own does

**The work ledger** (`work_ledger_read` / `work_ledger_record`, with
`work_ledger_rebuild` for recovery) holds the items:
each one's `title`, `acceptance`, `round`, your `decision`, the worker's reported
`status` and `summary`, its claimed `artifacts` and `pr`, your recorded `verdict`
and `fails`, and its `state`. It is keyed to your session, it survives
compaction, and it is the only place an item's acceptance condition lives.
The ledger files are a cache of the crew log: when a ledger call is refused with
`cache_dirty`, or the ledger reads as damaged or missing, run
`work_ledger_rebuild` (no arguments) to rebuild it from that record.

**Your own session ledger** (`session_ledger_read` / `session_ledger_record`)
holds YOUR state, and nothing about individual items:

- `goal` — the user's goal, one line.
- `phase` — which round you are in and what it is waiting on.
- `next` — a resumable intent, not a status. "round 2: A awaiting acceptance, B
  still running" beats "monitoring".
- `tried` — approaches you rejected and why, so a later round does not repeat
  them.

**Do not encode items into `session_ledger` artifacts.** Two copies can
disagree, and the work ledger is the one the evaluator batch and the Crew page
read. Write only what changed to your own session ledger.

**Three mechanics of the snapshot still apply**, because your own ledger is still
what the composer renders:

- **The injected snapshot is a teaser, not the record.** On a nudge-driven turn
  the composer prefixes a `[work ledger]` block capped at **1600 chars**, each
  field truncated to **300 chars**, only the **last 3** `tried` entries. It tells
  you *what you were doing*; `work_ledger_read` is how you get *the items*.
- **The snapshot only arrives on nudge turns.** When the USER messages you
  mid-flight there is no snapshot — read both ledgers before answering anything
  about item state.
- **A terminal phase silences the snapshot.** `render_snapshot` returns empty
  once the phase is `done` or `abandoned`. Do NOT set either until the goal is
  genuinely finished, or you will silently stop receiving your own state on every
  later cycle.
