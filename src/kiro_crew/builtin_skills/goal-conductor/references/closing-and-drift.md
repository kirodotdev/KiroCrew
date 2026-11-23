# Patrol steps 4 and 5: closing items, checking drift

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Step 4: closing an item and its session

4. `work_ledger_record` `action=close` with the item's `state` when an item is
   finally done with — that is what ends it. An item that passed is already
   closed by step 3's one write; a separate `close` is for `rejected` and
   `abandoned`. **Closing the item and closing
   its session happen together**, unless your prompt's worker-reuse rule gives
   that worker the next item. When a work item reaches a terminal verdict
   (accepted, rejected, abandoned/void) and its loop is stopped,
   `session_close` that child session in the same cycle — a finished worker
   has nothing left to re-arm. That `session_close` and your next
   `work_dispatch` may go out in one step: neither needs the other's reply. A stuck child (stale, no progress, item
   abandoned) is `session_stop`ped first, then closed. `session_close` archives
   (reopenable); it never deletes. Never close a child that waits on a person
   (a pending approval or an open question to them) or an unmerged PR it is
   actively driving, and never a session you did not dispatch. **Closing an item cuts its worker's
   report channel**: from then on its `work_report` is refused `item_closed`,
   and it cannot `session_send` to you. The close reply warns when that
   worker's session is still open. Whether that session takes the next item or
   a fresh one does is your prompt's worker-reuse rule. To reuse it, `create`
   the new item, `bind` it to that session, then `session_send` it the new
   item's goal with `steer: true`; its `work_brief` and `work_report` resolve
   to the new item. A session holds one open item at a time, so binding one whose
   item is still open is refused `worker_already_dispatched`. `action=decide` records an
   instruction you want the worker to read out of `work_brief`; it is the ONE
   field the worker treats as an instruction, so keep it to a decision.

## Step 5: checking each worker for drift

   **Check each worker for drift.** A report is the worker's own claim, so a
   worker building the wrong thing looks fine until acceptance. On every patrol
   turn, the floor turn after a quiet run included, call `session_read_message`
   for each OPEN local item's worker with `since` = that item's cursor from your
   session ledger's `next` (none yet: no `since`) and `limit: 10`. No new rows:
   the transcript did not move, skip it. When the reply's `total` is past its
   `next_since`, the worker moved more than 10 rows: read the newest 10 once
   more without `since`. A `cursor_unavailable` refusal means the same read.
   Compare the new assistant lines with the item's `title`, its acceptance and
   the goal (the owner's ask). Off direction (building something the item
   does not name, widening scope, redoing finished work): `session_send` with
   `steer: true` saying what is wrong and what to do instead, then
   `work_ledger_record` `action=decide` with the same instruction. On track:
   say nothing. Then write each read's `total` into `next` as that item's
   cursor with `session_ledger_record`. Cost: at most
   two reads per open item per patrol turn (the `since` read, plus the newest
   10 only on overflow), so at most 20 rows per item, and patrol turns come
   only from a report, a flag or the quiet-run floor.
