# What the ledger reads and `accept_eval` return

Read this when a field or flag on a ledger read or an `accept_eval` answer is
one you do not know how to act on.

## The compact read

It returns the conductor record and, per item, the status columns plus the
derived `orphaned`, `stale` and `acceptance_concrete` flags — no events,
acceptance or `accept_batch`. This one read replaces the whole
transcript-reading cycle, and it stays small however many items the board
holds.

- **`orphaned`**: your slot is gone; it self-heals if the session reopens. The
  item keeps its binding and keeps taking its worker's writes, unread until
  someone takes it over or stops it.
- **`stale`**: never on the strength of silence alone. Its worker also has to
  own no proven live or queued work, and its last word has to leave the next
  move with the worker. An unreadable activity probe suppresses no stall. A
  background task does not answer a `needs_human` report; only a new model turn
  does.
- **`report_undelivered`**: its worker TRIED to report or read its brief and
  the harness never delivered the call. Check that worker's `session_status`
  and its pull request, and tell the user what you find ("couldn't send its
  report") instead of calling it stalled.

## The full read

The full read (no `compact`) adds every field, the newest events and a
ready-to-pipe `accept_batch`; take it when a `done` item needs its bar. A full
read too large for the tool-result limit comes back trimmed with
`truncated: true` and says what it left out.

## An `accept_eval` answer

It returns the evaluator's own `{"results": [...]}` document, one
`{id, verdict, evidence}` per item, with any `done` item it cannot judge under
`skipped` and the reason. Each answer also carries what a read of that item
would show you: `reported` (the worker's own `summary` and claimed `pr`: a claim
to read, never the bar), the `fails` recorded so far, the
`worker_session_key`, `pending_overdue` with your own `decision` when that flag
is set, and the stored `acceptance` of an overdue item or of a bar that is not
concrete yet.
