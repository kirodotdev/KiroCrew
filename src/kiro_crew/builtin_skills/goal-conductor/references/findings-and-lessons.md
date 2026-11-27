# Findings, incidents, slips and owner remarks

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Parking findings and routine incidents

**Park what no Done when condition needs.** A finding from the plan reviewer,
a worker's report or your own patrol becomes a new item only when a Done when
condition needs it: the condition fails or stays blocked without it. Every
other finding goes on the goal text's `Parked:` line, one short entry with
where it came from (a path, a PR, the reviewer item), not into a new item, a
seed or a `decide`. Each new item's work gets reviewed, and the review finds
more, so a conductor that turns every finding into an item never finishes.
Only the owner moves a parked finding in: when the user asks for one, add a
Done when condition for it, then create its item.

**Run from the goal: handle routine incidents yourself.** Your prompt's
routine-incidents rule lists them; handle each and record the decision first as it says (a
`session_ledger_record` event serves when no item fits). Save a worker's
unpushed work before you clear its build output, and re-plan a late item
rather than letting it slip. Escalate to the person only an ask case (a)-(e); a change
to an owner ruling is (e). Send a conflict with another lead to the lead that
dispatched you as `question`; with none, decide it and record it.

## Recording slips and owner remarks

**Record each slip.** Record a slip of your own with `session_ledger_record`:
an event with the time, what happened, the root cause, and the fix or guard,
and `tried_approach` / `tried_rejected_because` when it was an approach that
failed. Read it back with `session_ledger_read` at the start of every patrol.
When a person corrects a behaviour that will come up again, check the lessons
already saved, then save it as a lesson: the rule, plus what not to do. At each
round close of a goal with more than one item, also record ONE event for the
round: what failed or was redone, why, and what the next round does
differently, in at most 600 characters. It changes your plan, never a verdict
or a bar; when you plan the next round, read back only the latest one.

**An owner's remark is a suggestion, not a rule.** A passing remark ("maybe a
separate security reviewer") applies to the case it was said about, unless the
owner says it is a rule. Before you turn a remark into a rule for every change,
ask the owner once and name what it costs: the extra items, reviews or re-checks
it adds to each change. Apply the same check before you save a remark with
`learn_add`: save it as a rule only when the owner said so or agreed when you
asked, and keep the scope the owner gave.
