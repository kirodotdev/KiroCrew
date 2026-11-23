# Preflight: `claim_preflight.py`

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Preflight: `claim_preflight.py`

One call answers every cheap question about one candidate and returns ONE
verdict. Branch on the exit code, never on the prose:

```
"$KIROCREW_RUNTIME_PYTHON" -I -B "<skill-dir>/scripts/claim_preflight.py" --repo <owner/repo> --item <N> \
    [--default-branch main] [--repo-dir <clone of the base>] [--json]
```

| Exit | Verdict | What you do |
| --- | --- | --- |
| 0 | `CLAIM` | Dispatch it. `risk=high` on the line means self-claim collision risk: that item is NOT batched — it goes to the live recheck on its own, immediately before claiming. |
| 10 | `SKIP` | Covered, or not workable. Leave it alone; record the reason. |
| 11 | `CLOSE` | Triage debt, not work. Close the item with the evidence the script printed. |
| 13 | `REVIEW` | A closure request was READ in the item's prose. **Do not dispatch and do not close.** Open the comment the line names, decide yourself whether the item is really done, and then either close it or dispatch it. This verdict exists because prose is the weakest evidence the preflight collects and closing is the strongest response it had. |
| 2 | malformed | YOUR arguments or config are wrong. Fix the call — a bad call is not a verdict about the item. |
| 3 | `UNKNOWN` | A check could not be answered (forge unreachable, rate limited). **Never treat this as permission.** Re-run it later or park the item. |

Exit 3 is why the verdicts are exit codes at all: an unanswerable question is
not a green light, and partial data yields `UNKNOWN` rather than `CLAIM`.

Pass `--repo-dir` every time, pointing at a read-only clone of the base that
its owner keeps current (the script never fetches). Without it, an item whose
merged PR claims to close it, or whose text names a backticked identifier, reads
`UNKNOWN ... reason=no-repo-dir` (exit 3) on every call unless an earlier rule
already decides it, and re-running later cannot change that.

Six checks run on every call, and the verdict is the FIRST match down this
precedence list:

1. `merged_prs` — a MERGED PR that CLAIMS TO CLOSE the item (a closing keyword
   for this item in its title or body, not a bare cross-reference) whose merge
   commit is an ancestor of `{default_branch}` → **CLOSE** `already-fixed`. Two
   conditions, and both are load-bearing: a merged PR that did not land on the
   base a worker would branch from is not coverage, and a merged PR that merely
   MENTIONS the item is not closure. A mention decides nothing here — it is
   neither CLOSE nor SKIP, so it falls through to the remaining checks, because
   treating it as coverage closes live work and treating it as a claim starves an
   item whose fix was only partial.
2. `open_prs` — an open PR that CLAIMS TO CLOSE the item (a closing keyword for
   this item in its title or body, not a bare cross-reference), **fork PRs
   included** → **SKIP** `open-pr`. A fork PR from someone with no standing still
   SKIPs, but the line carries `risk=high` and an `untrusted-fork` marker — treat
   that as a triage signal to review rather than an item that simply left the
   queue, because opening a fork PR needs no permission and is therefore a
   suppression channel.

   **An open PR that only MENTIONS the item does not suppress it.** The timeline
   event this check reads fires on a bare reference, so `Refs #N` — this
   repository's own idiom for referenced-but-deliberately-not-closed, and the
   reason the PR template keeps `Related Issues` apart from a closing trailer —
   used to take the item out of the queue. Measured over one real candidate list:
   of 21 (item, covering PR) pairs, 18 carried a closing keyword and 3 did not,
   and all 3 of those PRs disclaimed the fix in their own words. Such an item now
   reaches **CLAIM** at `risk=high`, with the PR numbers under
   `evidence.open_pr_mention_only` in `--json`. Read that as *somebody has looked
   at this and did not fix it* — usually a real dispatch, occasionally work in
   flight whose author never wrote a keyword, which is why it takes the live
   recheck rather than the batch.
3. `prose_claim` — a closure request in the newest human comment, or in the body
   only when the item has no human comment at all ("this is resolved",
   "please close") **from the item's own reporter or a repository
   insider** → **REVIEW** `reporter-asked-close` at `risk=high`. **Prose never
   closes anything.** It is the weakest evidence this script collects — nine
   separate false-CLOSE paths reached review in one change, and a ratchet that
   stops a new unguarded PATTERN cannot stop the next unguarded PHRASING of a
   pattern already guarded, because the space of English that accidentally means
   "close this" has no edge. So the detection stays and the response is withheld:
   you read the comment the line names and you decide. The authorization
   condition stays too, for a different reason than it had — `REVIEW` writes
   nothing, but it does withhold a dispatch, and a suppression any passer-by can
   cast is the same denial-of-work channel rule 5 refuses to open. A closure
   phrase from anybody else is not a closure request — it falls through to the
   remaining checks.
4. `forge_claim` — the item's own ownership fields, read off the payload the
   script already fetched: a `claimed` or `in-progress` label (the vocabulary of
   the default `skip_signals`, matched by word so `crew: in progress` counts and
   `unclaimed` does not), or any assignee → **SKIP** `forge-claim`, with the
   label term and the assignees on the line. **Either field alone is a claim.**
   It is honoured for anybody but you: a label with yourself as the only
   assignee is the shape of your own atomic claim, so re-checking an item you
   hold still reads CLAIM; a label with nobody assigned is unattributed and
   reads as foreign; so does every claim when your own login cannot be read.
   This is the live half of the queue build's label exclusion — a claim that
   lands after the queue is built is exactly what the recheck immediately before
   the atomic claim exists to catch, and until this check existed it could not:
   four live items in three days carried another pipeline's `claimed` label and
   read CLAIM. It sits below rule 1 on purpose, so an item that is open, claimed
   and already fixed still reads CLOSE.
5. `prose_claim` — a self-claim ("I'm claiming this", "working on this") **from
   the item's reporter or a repository insider** → **SKIP** `prose-claim`. A claim
   written in prose is invisible to every label and field query that exists, which
   is why it is scanned for rather than inferred. From anybody else it is NOT a
   veto: annotate the item `risk=high` and let it take the live recheck instead.
   The reason is that a veto anyone can cast is a denial-of-work channel — a
   single comment would suppress a queued item indefinitely, and nothing in the
   pipeline would ever report that it had been suppressed. Downgrading keeps the
   collision protection where the claim is credible without handing an arbitrary
   commenter a mute button.

   A claim is retired by a later withdrawal from the same author ("dropping
   this"), including one written in the issue BODY — otherwise a claim nobody is
   honouring suppresses the item forever. Bot comments never claim and never
   close. Ownership is read from the newest STANDING claim, not from the last
   comment, so a passer-by's "any update?" does not clear a claim.
6. `symbol_on_base` — a symbol the item names is absent from
   `{default_branch}`. **Absence alone is not a SKIP.** Corroborated as
   bug-class, it is **SKIP** `symbol-absent`: the target code lives only on an
   unmerged branch, so that is a park, not a dispatch. Uncorroborated it
   downgrades to **CLAIM** `risk=high`, because a feature request names the
   symbol it PROPOSES to add — vetoing on absence alone would permanently park
   every item of that class.
7. any check errored → **UNKNOWN**.
8. otherwise → **CLAIM**, annotated with `risk` from the `recency` check (a
   recently opened item from an active contributor is a high self-claim risk).

**A PR that only MENTIONS the item is a POINTER you hand over, not a verdict you
drop.** This holds on both sides of the merge boundary, and for the same reason.
The script finds the reference — the timeline it reads is state-agnostic, so a
`Refs #N` with no closing keyword and no `closingIssuesReferences` is collected
and then correctly falls through to CLAIM, because a mention is neither coverage
nor a claim. But a pipeline that prefers `Refs` whenever a residue remains — which
is the right house style, since `Closes` would shut items whose remainder nobody
has addressed — manufactures precisely this shape, so the gate's blind spot is the
shape of its own output. That is how the OPEN side came to suppress: it read the
bare reference as coverage for a while, which quietly withheld every item a PR
had deliberately left for somebody else. Carry the PR number into the dispatch
brief as *a PR may cover part of this; establish what remains*, and the worker's
preflight starts where yours stopped instead of rediscovering it.

**A triage comment routing the item away from automated fixing is a POINTER TO A
QUESTION — and the LABEL is noise.** A `needs-human`-class label sits on the
majority of an aged backlog, so filtering on it removes most of the pool
including plenty of genuine obvious bugs; the signal is a comment that
explicitly routes the item, and only the comment. Then two properties come
apart, and they diverge in BOTH directions: a routing comment can be
citation-dead (every line number moved) and still be directionally right, or be
perfectly cited and VOID because the parent PR it declared the item blocked on
has since merged. So the test is not "is a routing comment present", and not even
"does its evidence still hold" — it is **re-answer the question the comment was
asking, against the current base**. On a repository moving hundreds of commits a
week, a week-old comment is routinely right and dead at the same time.

That test needs the code read against current `{default_branch}`, which makes it
the WORKER's preflight and not your claim gate. So your gate ANNOTATES and the
brief DELEGATES — *a triage comment routes this away from automated fixing;
re-answer its question against current `{default_branch}` and
`STANDDOWN: routed — <evidence>` if it still holds*. A stand-down there costs one
preflight and is a correct outcome, not a defect.

**A `claimed` label you find in the wild is evidence of INTENT, never of
ACTIVITY.** The test is whether a branch, a PR, or a commit exists. A claim whose
work never started becomes a lie that deters every other operator indefinitely,
which is also why a claim YOU release must be verified released — assignees back
to zero, not just the label removed.

**`risk=high` is a decision, not a note.** A high-risk `CLAIM` is NOT batched:
it goes to the live per-item recheck immediately before the atomic claim, on its
own. A `REVIEW` is always high-risk and is not a dispatch at all: it goes on your
own list, and it clears only when you have read the named comment and either
closed the item or dispatched it. An annotation nothing acts on is the same
defect as a prose predicate — it reads as caution and changes nothing.

**A batch snapshot is never the authority.** Preflighting a batch is how you
order a queue; a per-item live recheck immediately before the atomic claim stays
mandatory, because coverage can appear in the seconds between the batch and the
claim, and on a queue other operators work, it does.

**An item that is open, claimed and already fixed is triage debt, not a work
item.** Its verdict is CLOSE with the landing-commit evidence, and closing it IS
the work. Dispatching a worker to rediscover that the work does not exist spends
a whole session — create, seed, preflight, stand down, unclaim — to learn
nothing, and leaves claim churn on a repository other operators are reading.
**An item that merely READS as already fixed is not the same item.** A merged
commit that is an ancestor of the base is evidence; a sentence saying so is a
reading, and it comes back as `REVIEW` for you to confirm.
