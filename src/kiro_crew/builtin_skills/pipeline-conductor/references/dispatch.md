# Dispatch mechanics, partitioning, sending orders

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Dispatch mechanics

- `session_create` MUST pass the worker agent explicitly. An unset agent binds
  the worker to YOUR agent, which has no file-writing tool, and the entire batch
  then refuses the work with a plausible-sounding explanation of why it cannot
  edit files.
- Validate with ONE canary dispatch before a batch. A wrong agent or a broken
  brief costs one session that way, and the whole batch otherwise.
- Respect the session-create rate limit: 20 `session_create` calls per 5-minute
  window per caller (folders: 10). `max_in_flight` defaults to 32, so a
  full-width dispatch round CANNOT complete inside one window — plan two rounds,
  and remember that a create refused by the limiter is a post-claim failure, so
  unclaim per the rule above.
- Worker sessions must be in **trust mode before seeding** — an unattended
  session stuck on an approval prompt runs zero turns. No tool you hold grants it
  (`session_create` takes no permission mode), so the operator arms each worker
  session; ask for that and do not seed a session that is not armed.
- **Check the SHARED CHECKOUT once, before you cut worktrees from it.** One
  `git status --porcelain` there. It is the shared root of every worktree in the
  fleet, so it is exactly the state a conductor is supposed to inspect before
  pointing dozens of workers at it — and it can carry hundreds of staged files
  left by an operation that predates your run. Each worktree has its own index,
  so a worker committing from its own tree is unaffected; the exposure is that
  your seeds send workers to `cd` there for forge calls. Do NOT clean it: a
  reset there is destructive and belongs to whoever left it. Take the
  non-destructive half — the brief's commit ban — and report what you found.
  Staged state there is usually a whole-tree overlay that never moved HEAD
  (`git checkout <ref> -- .`): `<ref>`'s tree plus the files `<ref>` deleted.
  In the shared checkout, test `<remote>/<base>` first, then its reflog entries
  newest first, stopping at the first match or the first one HEAD is not an
  ancestor of. A `<commit>` matches when it is not HEAD, `git merge-base
  --is-ancestor HEAD <commit>` exits 0 and `git diff-index --cached --quiet
  --diff-filter=a <commit>` exits 0; an exit above 1 is unreadable, not a no.
  On a match, report the owner's repair: `git merge --ff-only --no-autostash
  --no-overwrite-ignore <commit>`, or `git reset --keep <commit>`, the same move
  except that it also unstages anything else staged. Both refuse rather than
  overwrite a local edit, and neither is yours to run.
  Re-read the base head rather than caching it, too: the default branch moves
  under a long run, and a worker preflighting against a remembered sha is
  preflighting against the past.
- Before commissioning a fix for a base-wide breakage, search open PRs for one
  that already exists. Fleet-wide breakage is visible to the wider community
  too, and two identical fixes waste a worker and a review lane.
- **RETASK a warmed-up worker rather than closing it and paying for a create.**
  A session that has already read the brief, learned the host's forge
  constraints and absorbed the reporting protocol is cheaper to re-point than to
  rebuild, and creates are rate-limited. One condition: the new item must have
  been through the claim gate first, or retasking just moves a duplicate
  dispatch to a new number.
- **Never hold a session idle waiting for a decision.** A session waiting is not
  a session working. The test for whether a park is legitimate is whether it
  holds context that would be expensive to rebuild — a completed survey already
  written down in its report does not qualify, so retask it and stand a fresh
  worker up if the decision goes the other way.
- **A list whose contents are DERIVABLE must be derived, never typed.** Pending
  is `claimed MINUS sessions MINUS released`, computed each cycle. A
  hand-maintained copy drifts in both directions at once and does so silently:
  items claimed and never dispatched fall out of it and become invisible, while
  items that were never claimed appear in it and get worked with no claim
  protecting them.
- **The issue → session mapping lives on disk because it answers a different
  question from the one you are holding in your head.** It answers *who owns
  this item now*; your memory of the dispatch answers *who sent me this
  message*. A report arriving from a session you already closed is not stale
  because it resembles the other stale ones — it is stale only if the mapping
  says a live worker owns that item. One lookup decides it, and a report that
  looks like the others is exactly where guessing costs a real dispatch.

## Partitioning one change across several workers

When one change is too large for one worker, split it by **exclusive file
ownership**: every file belongs to exactly one worker, and nobody edits outside
their own set — not a one-line mention, not a docstring cross-reference. Two
workers on one file is the merge-conflict version of two sessions on one item.

**Exclusive ownership removes merge conflicts. It does NOT remove a review-order
dependency, and that is the trap.** A premise-level reviewer counts a
mechanism's CONSUMERS in the base, so the PR that BUILDS a mechanism reads as
dead code until the PR that WIRES it has landed: nothing in the base invokes it,
the zero option is behaviorally identical, and the reviewer is right on the
evidence available to it. So a partition ships with a **merge ORDER**: the wiring
PR lands before or with the building PR.

The remedy for a block of that shape is sequencing, and sequencing is YOURS. A
worker must never move another worker's hunk into its own PR to satisfy a
reviewer — that dissolves the ownership split, creates the conflict the split
existed to prevent, and hides a review-order problem as a code change. Land the
wiring PR; the same reviewer's own grep then finds the consumers and the block
dissolves with neither PR changing content.

## Sending an order to a worker

**Every order to an existing worker uses `session_send` with `steer: true`.**
That includes rulings, corrections, nudges, resume requests and follow-ups. It
joins a busy worker's running turn instead of queueing behind it, and starts an
idle worker like a plain send. The initial seed stays plain because the new
session has no turn to steer. Name any older queued instruction the steer
supersedes.

Every rule here closes a way an order fails SILENTLY, and silence is the failure
mode you are least able to detect.

**Before composing an order, re-read the last order you SENT that worker AND
confirm no newer report from it is pending.** Both halves are needed and they
fail differently. A new order that contradicts a standing one produces NOTHING:
a worker facing two of your instructions resolves toward the restrictive
reading, does not act, and tells you it did not act — which is correct behaviour
and your fault. And a ruling composed from a report that a newer report has
already superseded orders the worker to publish facts it has since disproved.
Re-reading your own last message catches the first; only checking the worker's
queue catches the second.

**A reversal must NAME what it reverses.** "Post the finding" does not obviously
outrank "do not comment on that PR" when both came from you and the earlier one
was framed as a rule. Revoke by name, in both directions — including when new
content flips a refusal you made on content grounds, which is the rule working
rather than you being inconsistent.

**Never state a ranked option as a PREFERENCE. State it as a CONDITION.** You do
not read the code, so your preferences are hypotheses and must be written as
hypotheses: *do X PROVIDED Y holds, and Y is checkable like this*. A preference
gets complied with; a condition gets tested — and a condition attached to your
own preference is repeatedly what catches the preference being wrong. Several
checkable conditions also beat one instruction: a single condition can only ever
produce a correct BLOCKED report, while a pair can find the narrower door that
lands the fix.

**Any instruction to publish evidence says: the reproduction must be
self-contained; never cite a local path.** A path on this host leaks an internal
directory into a permanent public thread and is useless to every reader who
cannot open it. Reproducibility is the intent; naming the path is a flawed
implementation of it, and the leak is worse than the shell-command case because
it is permanent and public.
