# Patrol steps 1 and 2: reading the ledger, answering statuses

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Step 1: reading the ledger

1. **Read the ledger first, every cycle.** A cycle a worker's report woke you
   for arrives with a `[work-ledger wake]` line naming each item that moved and
   a `[work-ledger]` line telling you to read. The wake is a notification, not
   the state: it says WHICH item moved and nothing about the rest of the board.
   So read before you act — `work_ledger_read` with `item_id=<id>` for the item
   a wake named, or `work_ledger_read` with `compact=true` for the whole board
   on a scheduled patrol tick. Read that item in its own call, before and apart
   from any send or decide: an action batched with the read runs on the state
   from before the read. **A wake that names a `done` item is the one
   exception:** go straight to step 3, whose call reads that item's bar itself
   and returns the worker's report beside the verdict, so a read first only
   costs you a turn.

   Read the `stale`, `orphaned`, `acceptance_concrete` and
   `report_undelivered` flags every cycle; the full read (no `compact`) is for
   a `done` item's bar. What each flag means and what you do about it, and
   what each read returns: `<this skill's dir>/references/ledger-fields.md`.

## Step 2: steers and `needs_human`

   **A person's steer outranks your decision.** When the person types into a
   worker from the Crew panel, its item gets a `steer` event (`kind: steer`, the
   person's own words, redacted). Before any `action=decide`, and before any
   instruction you send a worker, read that item's events (`work_ledger_read`
   with `item_id=<id>`, no `compact`) and look for `steer` events newer than your
   last decision. Follow them and never overrule, undo or re-send against them;
   a steer that conflicts with your plan wins and you re-plan around it. A steer
   never moves the acceptance bar: the verdict still comes from
   `accept_eval.py`.

   A `blocked` or `question` row may also carry the worker's `reason`: `approval`
   (a tool approval timed out) or `needs_human` (the worker says only a person
   can unblock it). No reason means the item moves on its own (a build, a
   service). Tell your workers to report `blocked` with `reason=approval` the
   moment a tool approval times out.

   **`needs_human` is the worker's claim, not a fact: check it.** A person is
   needed only for (a)-(e) in your rules; the conductor tail in your prompt
   sets the check. If none applies, decide it yourself: take the
   worker's recommendation (or the safest reversible default when it names
   none), record `action=decide`, `session_send` it to the worker with
   `steer: true`, and tell the
   person in one line what you decided. For example, a plain design choice
   (which of two layouts, which file holds a helper) is yours; a choice that
   deletes another contributor's branch is (c); a choice that needs a token
   only the person holds is (a).

## Step 2: asking the person

   When every open item waits on a person (`approval`, or a `needs_human` that
   passed that check), put the ask in front of the user on that turn and record
   it with `work_ask_record`: your patrol then holds, takes no turns and does not
   extend, and the goal popover shows it waiting on the user. Without a current
   ask record it does not hold. A worker's new report releases it, and so do your
   `decide` and `close` (they clear the ask); `goal` does not.

   **Whenever you relay an item's question to the person, load and call
   `work_ask_record` in that turn** (`item_id`, `what`, `why`,
   `options=[{label, outcome}]`, `pick`, `pick_why`). It is deferred, so load it
   first. `what` is what you need from them, `why` why only they can do it, each
   option says what choosing it leads to, and `pick` is the label you recommend,
   with its reason. Use the same labels as your `[OPTIONS:]` row. `decide` and `close` clear it.
