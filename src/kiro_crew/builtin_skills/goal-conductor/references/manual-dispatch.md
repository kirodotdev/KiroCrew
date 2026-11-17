# Dispatch by hand

Read this when you dispatch without `work_dispatch`, or bind an existing
session to a new item (Patrol step 4: skip step 2). Then make **one call at a
time**, each after the previous reply came back, in **exactly this order**:

1. `work_ledger_record` `action=create`, with the item's `title` and its
   `acceptance` condition — the same condition object `accept_eval.py` parses,
   stored verbatim. It returns the `item_id`.
2. `session_create` with a title that says what the item is FOR and **`agent`
   set explicitly** — see "Which agent" in the skill — and no `folder`. It
   returns the worker's session key.
3. `work_ledger_record` `action=bind` with that `item_id` and
   `worker_session_key`.
4. `session_send` the seed prompt into the new session — the item's goal, its
   acceptance condition, and the instruction to report through `work_report`.
   Its `started: true` means the target began a turn on your message,
   `started: false` that it queued; neither says the work succeeded.

Step 2 never carries a `message`: the seed is step 4, after the bind. Where a
`message` would be refused outright, your prompt says so:
"`session_create`'s `message` is refused for this agent
(`message_needs_send_approval`): create, then `session_send`."

**Bind BEFORE you seed.** The opposite order — seed first, record after —
protects against a ledger row with no session behind it. This one protects
against a running worker with no binding, and that is the failure the worker can
actually see: its first `work_brief` answers `not_bound`, which it takes as
having no item, so it does what the seed says and reports nothing. A bound item
with no seed is visible in your own `work_ledger_read` and you seed it next
cycle; an unbound running worker is neither visible nor recoverable.
