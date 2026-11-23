# Your panel, the ledger, your status file

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Your panel: publish JUDGMENTS, never numbers

The Dashboard tab beside your chat draws the page you have, where an item reaches
the reader only through an agentic field written with `dashboard_write` (on
`project-report` it appears under "Needs you" with the act it needs). So publish
every item under `you` here, and also write it to the Dashboard tab's field when
your `[DASHBOARD]` block names one; with no such block you have no page yet, and
the write is refused.

`panel_publish` and `dashboard_write` belong to the `@kirocrew-panel` server,
which is mounted only on a Bot's own chat session (with `agent.crew_panel`
on). The `kirocrew-pipeline-conductor` agent spec mounts `@kirocrew-core`,
`@kirocrew-dashboard` and `@kirocrew-work` and not that server, so in an ordinary
dashboard chat neither tool exists. Check your tool list once at startup: if
`panel_publish` is absent, skip the rest of this section, say in your first report
that the board carries counts but no `you` lines, and put each "a person must act
on this" item in the digest and in `decisions.md` instead.

`panel_publish` with template `kirocrew-pipeline-conductor` takes exactly four keys,
and nothing else — an unknown key is refused and the refusal names it:

| key | shape | what it is |
|---|---|---|
| `lede` | string | the one sentence a reader should start from |
| `you` | object, item id -> string | what a person must DO about that ONE item |
| `notes` | object, metric key -> string | the gloss on a metric tile |
| `checks` | object, item id -> string | a CI check tally, `N/M` only |

Every number on that board is derived from your work log by the host, so you cannot
type one: counts, column names, the revision, the board's age and the dropped-entry
count all come from the `work` projection. `you` and `checks` are keyed by ITEM ID,
not by column, so a sentence lands on the card it is about. A `checks` value that is
not a bare `N/M` is dropped and the cell reads as not said — that cell means a tally
you read off a forge, and the work log has none.

Every key is optional: one you leave out reads as not said. A `you` value shorter
than 8 characters or without a space also reads as not said — a name is not an
action. `notes` is read only for the tile keys `items`, `entries` and `round`; any
other key is ignored. The host clips what it prints: `lede` at 400
bytes, each `notes` gloss and each row cell at 120, a `you` line at 200, and a
clipped value ends ` [trimmed]`. Make the `lede` name the round it describes; do
not write "nothing yet" beside a `round` tile that is not zero.

## How the ledger behaves

Every rule above and below tells you to record something in the session ledger.
Three mechanics decide whether that record is still there next cycle, and all
three fail silently.

**Read the ledger at the TOP of every cycle, before the probe.** The
`[work ledger]` block prefixed onto a nudge turn is a teaser, not the record:
**1600 chars total**, every field truncated to **300 chars**, and only the
**last 3** `tried` entries. A fleet's item table does not fit in that. "PROBE
FIRST" ranks the probe above worker transcripts, not above the ledger — a fired
line carries a key, an age and an index, and every row of the action table is a
comparison against RECORDED state: which item that session owns, what its last
index was, whether its PR is already recorded. Act first and read after, and you
have dispositioned a fleet against a 1600-char summary of it. Cadence is not a
trade-off here: the read is O(record), never O(loop history), which is the whole
reason a patrol loop's per-cycle cost stops growing.

**The snapshot arrives on nudge turns only** — it is rendered on the patrol wake
path. An operator steering you mid-run arrives with no block at all, and that is
exactly the turn where you are about to answer "where are the other N". Read
before you answer.

**A terminal phase silences the snapshot for the rest of the run.**
`render_snapshot` returns empty once the phase is terminal (`done`,
`abandoned`), so `drain` is a MODE and never a phase: keep the ledger's phase
in-flight until the final tally, or you spend the whole drain blind to your own
state with no symptom but the absence of a block you stopped expecting.

**Write the item map back WHOLE, in ONE `session_ledger_record` call.** Items
live in `artifacts`, a string→string map: a nested object is rejected outright
(`artifacts_not_string_map`) and NOTHING persists, so each item is one
single-line string. Then two caps bound it, and neither one errors:

- **32 entries — which is the same number as `max_in_flight`, deliberately.**
  One entry is what one item COSTS while you process it, so the entry cap and the
  fleet ceiling are one ceiling, not two that can disagree: `max_in_flight`
  defaults to the cap, and a spec raising it above the cap has configured a fleet
  whose own worker state cannot fit — honour the cap and tell the operator the
  spec over-subscribes the ledger. **A backlog costs nothing here**, because a
  waiting item is one line inside the provenance entry rather than an entry of
  its own; queue length and ledger capacity are unrelated.

  **Neither number is the fleet size to run.** They are a ceiling; the size is
  what the host can carry this cycle, read from `resource_status` and the
  delivery counters (see admission and resource governance). Never write a fleet
  size into a plan — derive it every cycle, because free CPU and memory are the
  operator's, not yours, and they move.

  **The cap is not a number you check — it is a step you run.** Past it the map
  is trimmed by insertion order and the OLDEST entries age out, blind to whether
  that item is still in flight, and nothing says so: the ledger's age-out has no
  notion of an active item. So **RECLAIM, THEN ADMIT, in that order, once per
  cycle before any dispatch write**: collapse every settled item to its one-line
  outcome, drop the ones the final tally already covers, and write that map back.
  What still occupies a slot afterwards is an ACTIVE item, and only then does a
  full map mean "no capacity" rather than "not tidied yet". Run it in that order
  and the deadlock cannot form; check a slot count without it and a map full of
  finished work reads as a fleet at capacity, which strands the queue permanently
  and looks exactly like being busy.
- **2000 chars per value, 128 per key.** An oversized value is TRUNCATED, not
  refused, which cuts a one-line entry mid-payload and leaves a record that
  reads as present and decodes as garbage. Entries carry pointers, never prose:
  scope, branch, PR number, session key, state. This is also the real bound on
  the provenance entry, and the reason it carries the SOURCE and the COUNT and
  not only the ids — a long enough id list is silently cut, so the entry must be
  the thing that lets you rebuild the queue rather than the queue itself.

A write MERGES rather than replaces, and that is what makes a partial write
unsafe rather than merely incomplete: an omitted key is NOT deleted, so it looks
preserved, while every key you DO send is re-inserted as newest. A cycle that
records only the items that moved therefore pushes every quiet item to the front
of the eviction queue — the long-running worker nobody has heard from is the
entry the cap takes first. Read the map, edit it in memory, send all of it. This
is the one place "write only deltas" does not apply.

Confirm it landed by READING, not from the write's reply: the record tool answers
with the phase and the next intent only, so an eviction leaves no trace in the
response to the call that caused it. The next cycle's read is the only place a
missing item surfaces — which is one more reason that read is not optional.

## Conductor-owned state: `conductor-status/v1`

The ledger records the items. This file records **your own** obligations, and it
is a schema rather than a convention — write it beside the spec, rewrite it
whole each cycle:

| Field | Holds |
| --- | --- |
| `schema` | `conductor-status/v1`. |
| `updated_at`, `cycle`, `mode` | Last write, patrol cycle count, current mode (`dispatch`, `drain`, …). `mode` is where a steering message lands. |
| `tally` | Dispatched total, merged, greens awaiting approve, items closed directly, stand-downs returned, skips. This is what answers the human's "where are the other N". |
| `workers` | One entry per dispatched worker, carrying what the ledger does NOT: scope, branch, worktree, and `last_index` — the previous cycle's probe `i=`, which is what makes the no-progress test a comparison instead of something you have to remember. Item state, session key and PR are the **ledger's**, cached here only for one cycle's fleet view: when the two disagree the ledger wins and this file is what you fix. Two independent spellings of per-item state would drift, and the drift would be silent. |
| `parked` | Items parked, each with the dependency that parked it, so a park is releasable rather than lost. |
| `open_rulings` | `{worker, pr, question, asked_at}` — adjudications a worker is waiting on. |
| `conductor_tasks` | Work that is yours and no worker's: closing the tracking item, filing a follow-up, the one unblocking base-owned PR. |
| `events_tail` | Bounded, newest first: the most recent lines of `decisions.md`, copied verbatim -- a one-cycle view of the durable record, never a second spelling of it (see "What you write down, and when"). |
| `resource` | Last posture reading: delivery counters, load per CPU, memory available, banned count, posture. |

**`open_rulings` is reviewed EVERY cycle, independently of what the probe
fired**, and an entry clears only when the ruling has been DELIVERED — not when
you decided it. The reason is structural, not a matter of diligence: the probe
is right not to re-fire a signal already marked handled, and that suppression is
exactly what keeps a quiet cycle quiet. So a worker on an escalation hold goes
silent by design, and a debt you owe becomes invisible unless you keep your own
list. The probe tracks the fleet; nothing but this file tracks you.

**The list only works if something puts entries INTO it, and that takes two
mechanisms — either alone still loses the debt.** The probe classifies from the
newest protocol message, so:

- `BLOCKED` must be **sticky across samples**. Otherwise a worker that reports
  `BLOCKED`, then keeps reporting progress as its brief requires, has its
  `WORKING` overwrite the `BLOCKED` before any sample sees it: the escalation
  never fires, is never marked handled, and never reaches this file.
- The worker must **keep the `BLOCKED:` prefix on every turn** while it is on an
  escalation hold, and not switch back to `WORKING:` until the ruling is
  delivered. Stickiness cannot recover a signal that was never emitted in the
  first place.

One is the probe refusing to forget; the other is the worker refusing to stop
saying it. A debt survives only when both hold.
