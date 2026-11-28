# Pickup and dispatch checks, and the queue filter

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Pickup and dispatch

Dispatch is **idempotent** — every check, every time (skipping them is how two
sessions end up on one item and mutual-yield deadlock):

1. The ledger carries no non-`queued` entry for the item — `dispatched`,
   `green_verified`, `done` or parked means skip. An ABSENT entry means NOT YET
   ADMITTED, which is eligible: the ledger holds admitted items only, so absence
   is the normal state of a backlog item and reading it as a veto would strand
   every item the entry cap could not hold. What prevents a double dispatch is
   not this check — it is the four-way collision check in step 4 and the atomic
   forge claim below, which is why the ledger being a cache is safe here.
2. The backlog/findings store (when the pipeline has one) still says the item
   is open — a queue snapshot goes stale the moment it is built.
3. `claim_preflight.py` returns `CLAIM` for the item (below). That verdict
   replaces the old one-question "is there an open PR" predicate, which was
   blind to a covering PR that already merged, to a claim written in prose, and
   to target code that does not exist on the base yet. **If the script is absent
   from your install** — an older build, or an install where it did not land —
   that is `UNKNOWN` for every question only it can answer, and `UNKNOWN` is
   never permission: answer the merged-PR and prose-claim arms yourself before
   claiming (they are the two the old predicate missed), or park the item and
   tell the operator the script is missing. Never fall through to a bare open-PR
   search, which is the predicate this step exists to replace.
4. The **four-way collision check**: no open PR, no merged PR already on
   `{default_branch}`, no branch matching `branch_pattern`, no worktree at
   `worktree_pattern`. The preflight answers the two PR arms; the branch and
   worktree arms are local and yours.
5. In-flight count < `max_in_flight`, this cycle's dispatches <
   `max_per_cycle`, this cycle's ledger reclaim has run and left a slot for it
   (see "How the ledger behaves" — reclaim, then admit), and admission admits
   (see governance).

Then **claim atomically** — the lock label and the assignee in ONE call, never
two. The forge is the cross-operator lock and your ledger is only a cache of
it; a claim written as two calls is a window another operator dispatches into.
Only after the claim lands: `session_create` (titled `{id}: {item}`), seed it with the work-order brief, record
`{state: dispatched, session, ts}` in the ledger. The seed is a `session_send`
when your prompt carries this line: "`session_create`'s `message` is refused
for this agent (`message_needs_send_approval`): create, then `session_send`."

**The claim is only valid while a worker holds it.** If ANY post-claim step
fails — session create refused, create rate limit hit, seed rejected, trust grant
unavailable — unclaim before you move on, label and assignee both, exactly as you
would on a stand-down. A claim with no session behind it is indistinguishable
from work in progress to every other operator, and nothing later in the cycle
looks for one: the ledger never recorded a dispatch, so no probe line, no SLA
timer and no reclaim path covers it.

On any stand-down, **unclaim promptly** — label and assignee both — and leave an
evidence comment on the item. An item released silently reads as still-yours to
the next operator, and an item disposed of with no evidence reads as abandoned
rather than as decided.

## Queue build exclusion: `coverage_filter.py`

The work source selects by label and excludes by label, and a contributor who
opens a PR carrying `Fixes #N` applies no label at all. So a queue built from
labels alone is mostly work already in flight: measured on this repo against the
documented selector, 25 of 29 label-clean candidates were referenced by an open
PR. Run this over the WHOLE candidate list before you record the backlog, and
again whenever pickup rebuilds it:

```
"$KIROCREW_RUNTIME_PYTHON" -I -B "<skill-dir>/scripts/coverage_filter.py" --repo <owner/repo> --items 10890,10849,9736 [--json]
"$KIROCREW_RUNTIME_PYTHON" -I -B "<skill-dir>/scripts/coverage_filter.py" --repo <owner/repo> --items -   # numbers on stdin
```

One forge call for the whole batch, up to 500 candidates — the same question
`claim_preflight.py` check 2 answers per item, asked from the other end so the
cost does not scale with the backlog. A larger batch is refused rather than
truncated, so page the queue build instead of trimming it.

| Exit | Line | What you do |
| --- | --- | --- |
| 0 | `COVERED <n> open-pr=#<pr> …` | Drop the item from the queue and record the PR as the reason. `unvouched=true` marks a cross-repository PR whose author has no standing: the item still leaves the queue, and that marker is your cue to review the subtraction rather than let it pass as routine. |
| 0 | `MENTIONED <n> open-pr=#<pr> …` | **Not a subtraction.** A PR references the item with no closing keyword, so it has not claimed to fix it — `Refs #N` is this repository's idiom for exactly that. **Keep the item as a candidate.** Carry the PR number into the dispatch brief as *somebody looked at this and did not fix it; establish what remains*. Occasionally it is work in flight whose author never wrote a keyword, which is what `claim_preflight.py` re-checks before the claim. |
| 0 | `UNCOVERED <n>` | **Not permission.** Keep the item as a candidate; `claim_preflight.py` still decides. A reference made in a PR COMMENT is in the item's timeline and not in this answer, so silence here is a smaller view, never a clean bill. |
| 2 | malformed | YOUR arguments are wrong, including a batch over 500 items. Fix the call — a bad call is not a finding about any item, and a batch this refuses was never scanned. |
| 3 | `UNKNOWN reason=<slug>` | The forge could not be read, so NO exclusion was computed. Keep every candidate and carry on; the per-item preflight still runs. Never read it as "none are covered" — the output prints no `uncovered` list for exactly that reason. |

This filter only ever SUBTRACTS, and that is what makes two evidence sources
safe. `COVERED` is a positive finding — a closing keyword aimed at the item in a
PR's own title or body — so acting on it can only remove an item. Nothing it
prints can ADD an item or certify one as free, so the cheaper evidence can never
widen what gets dispatched. `MENTIONED` and `UNCOVERED` both leave the item a
candidate and are still separate lines, because a reference the filter declined
to subtract on is worth a look and printing it as `UNCOVERED` would be exactly as
silent as the subtraction it replaced. If the script is absent from your install,
treat it as `UNKNOWN`: keep the whole queue and let the preflight carry the
coverage question, which is slower but never permission you did not have.
