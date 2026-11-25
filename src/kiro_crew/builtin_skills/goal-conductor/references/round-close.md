# Round close: dropping your chat, goal changes

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Dropping your chat

**Then drop your chat.** Every patrol turn re-sends your whole conversation,
and a conductor that runs for days pays for it on every quiet cycle. Your state
already lives in the two ledgers, and each wake carries their snapshot. So once
the next round is dispatched, end that same turn with two calls, in order:

1. `session_ledger_record` with everything the next turn needs:
   - `goal` — the goal, one line;
   - `next` — the round number, and for each open item its item id, its
     worker key, its PR number (or "no PR yet") and its drift cursor (step 5).

   Check the item ids, worker keys and PRs against a fresh `work_ledger_read`,
   not against memory: the work ledger is the record, `next` is the pointer.
2. `reset_conversation`, as the last call of the turn. It lands at the turn
   boundary; the next wake starts from the ledger snapshot alone.

Never reset when:

- a question to the user is unanswered — the answer would arrive in a chat
  that no longer holds the question;
- the round is still running — reset only at a round close;
- anything is queued — a queued item has no ledger row yet, so the chat is
  its only copy;
- the session ledger write failed — the reset would drop the only copy.

The gateway refuses a reset from a loop it cannot vouch for, or while a
question card or tool approval is pending. A refused reset changes nothing:
carry on in the same chat and try again at the next round close.

## Goal changes mid-flight

The user can message you any time. Apply a changed goal **at the round
boundary** — that is the re-plan point, and cancelling mid-round throws away
finished work. A request that adds separate work is not a changed goal for the
running items: make it its own item now, as your prompt's worker-reuse rule
says.

**Record the goal first, every time it changes.** When the user gives a new goal
or changes the scope, call `work_ledger_record` `action=goal` FIRST, before you
dispatch or reply: `new_goal=true` for a new goal, a plain rewrite for a scope
change. Write one plain sentence for the outcome, then one "Done when ..."
sentence, then the Plan and Changed lines. The re-plan itself still waits for the
round boundary.

One exception: if their message directly invalidates an item that is still
running, deal with that item now — `session_stop` it and `close` its ledger item,
or `session_send` the correction straight into it with `steer: true`. Do not
tear down the whole round for one item.

**Steer every direct follow-up; never queue it.** Follow your prompt's direct-worker
communication rule after the seed. For a fleet, use `session_broadcast` with
`mode: steer`. If `session_read_message` reports a non-zero `queue_depth`, name
which earlier instruction the steer supersedes.
