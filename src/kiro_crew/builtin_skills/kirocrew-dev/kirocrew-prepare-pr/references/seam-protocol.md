# Seam protocol

Read from SKILL.md's *Iteration budget and retrospective* and from Phase 2 step 3.
Nothing here asks a person. The loop picks and continues in the same turn.

## Class sweep, every fix round

A review finding is a class, not a line. Before you edit for it, write one entry per
span in `$(git rev-parse --absolute-git-dir)/prepare-pr-sweep.md` (same directory as
`prepare-pr-body.md`, never committed, rewritten each round). Hand the entry to the
repair subagent in its brief, so it fixes the class.

```markdown
## span=<12 hex>
class: a git hook can come from a source other than core.hooksPath
search: git grep -nE 'hooksPath|fsmonitor|includeIf' -- 'src/**/*.py'
sites:
- src/kiro_crew/git_config_hooks.py:118  fixed
- src/kiro_crew/dev_fleet/worktree_ops.py:240  covered-by test/test_x.py::test_submodule_hook
- src/kiro_crew/x.py:12  not-in-class -- reads the name only, never runs the command

## span=<12 hex>
class: a recovery path leaves state behind after one transient error
search: none -- ordering across three writers, no common token
branches:
- final drain, first PUT fails  covered-by test/test_a.py::test_drain_retry
- final drain, second PUT fails  fixed
```

- `class:` names the pattern, not the instance. Nothing compares it between rounds.
- `search:` is a `git grep` with `-E`, `-F`, `-i`, `-w` or `-n`, one pattern, and an
  optional `-- <pathspec>...`. It must hit the tree before your fix. A class with no
  usable pattern says `search: none -- <reason>` and fills `branches:` (two rows or more).
- Every site is `fixed`, `covered-by <test id>` (a test in this diff) or
  `not-in-class -- <reason of 20+ characters>`.
- Optional `risk: security|data-loss|corruption` marks a class that is never deferred.
- Optional `resolution:` records a strategy switch (below).

A disposition counts as a fix when its DECLARED outcome is `fixed`, `removed` or
`reverted`. `pr_findings.py` reads the outcome from exactly two places, never from prose
(sample and counts: `code-quality/impl/D5-FORMAT-SURVEY.md`):

- the **first quoted line** of the disposition: `> fixed in <sha>`, `> Fixed: ...`,
  `> removed in <sha>`, `> **fixed** in <sha>`, `> Rebutted: ...`, `> deferred #N`,
  `> accepted: ...`, `> Agreed, no change.`, `> Kept as ...`, `> Outcome: <word> ...`
- an **explicit label line**: `Outcome: <word>`, `Disposition: <word>`,
  `**Disposition: <word>**`, or a bold token that is exactly an outcome word and starts the
  record: `- **fixed** span=<id>`, `**rebutted** -- span=<id>`

So write the outcome first in the quoted rationale (`> fixed in <sha>: ...`). The words are
`fixed`, `removed`, `reverted` (fixes) and `rebutted`, `deferred`, `acknowledged`,
`accepted`, `agreed`, `intended`, `checked`, `kept`, `declined`. A bare fix word in the
quoted lead needs `in <sha>`, `:`, `.` or `,` after it ("> Fixed point iteration" and
"> removed sessions still listed" declare nothing).

Never read: a finding's title (`- **Deleted slot -- removed sessions still listed**`), a bold
title with other words, the plain `class:`, `mechanism:`, `self-added:` and `resolution:`
lines, any other sentence, any later quoted line, fenced code. A disposition that declares
nothing prints as `undeclared` in the view (comment id and spans); it is never counted as a
fix and never silently nothing. A marker without `target=` and `head=` (for example `span=`
inside the marker) cannot be placed in a round; the view counts those as skipped.

## Strategy switch

`pr_findings.py <pr#> --rounds` counts FIXES, not mentions: the distinct judged heads in
which one span (path + lane) was fixed. A span fixed in two earlier heads makes the next
fix its **3rd**: `SEAM REPEAT`. It exits 30 when the span is on the current head, or when
the latest round just made its fix count reach 2 or 4. A seam whose span is gone from the
current head, and was not just reached, prints as `seam history` and does not trigger again.
Do not patch a third spelling. Take the first that holds:

1. **Remove** the second writer or the hazard, when this PR added the mechanism.
2. **Narrow** the scope: revert to the last head before the mechanism, redo it smaller.
3. **Defer** with an open `deferred-finding` issue plus a pinning test or
   `xfail(strict=True)` naming it. Never for a security, data-loss or corruption class.

If none holds, one more patch with the full `branches:` table. A span fixed in four
earlier heads makes the next fix its **5th**: exit 30, `SEAM LIMIT`. Only narrow or
defer remain (narrow only, for the three classes above); removing is the narrowest
narrowing and is accepted. Write the choice in the entry:

```markdown
resolution: removed -- <reason of 20+ characters>
resolution: narrowed -- <reason of 20+ characters>
resolution: deferred #<issue> pinned-by <test id>
```

Put the same plain `resolution:` line (outside the quote) in the disposition you post for
that round. `--rounds` reads it from the thread: fixes up to and including that round no
longer count for the span, so a recorded narrow, removal or deferral does not trigger
again, and the count starts over from the next fix.

The count never looks at what a `class:` line says. Grouping fixes by how alike those
sentences are kept producing new edge cases, so the rule is the plain count of heads.
A span that really carries unrelated defects is answered by the `resolution:` line.

## PR-level count

The count above reads only dispositions an author wrote, and many PRs have none. When no
span is a seam, `pr_findings.py --rounds` and `sweep_check.py` also count PUSHES, read
from the PR timeline with GET calls (`_review_contract.blocked_push_count`). A push counts
once, when the head it replaced was blocked: a review lane (Opus, GPT, Design, UX or First
Principles) ended its latest check run on that head as `failure`, or a CHANGES_REQUESTED
review names that head and its reviewer has not since approved that head or had the review
dismissed (a comment does not clear it, and another reviewer's request stands). A rebase or a
red test shard on a green head, a cancelled or timed-out lane, a lane whose newest run on the
head is green, and a dismissed review count nothing.

- 3 pushes: `SEAM REPEAT (pr-level)`, exit 30. Take the first option of the strategy switch
  that holds. The class sweep and `resolution:` line work as for a span.
- 5 pushes: `SEAM LIMIT (pr-level)`, exit 30. Only narrow or defer remain.
- A plain `resolution:` line in any disposition, with or without a span, restarts the count in
  `--rounds` from the head that round judged (the last time the PR was on that head). `sweep_check.py` does not read it; a sweep entry's own `resolution:`
  answers that entry's finding. With no sweep file, or a file that holds no entry (empty,
  or only headings and notes), it prints the same finding beside any parse findings and
  still exits 0 while `SWEEP_ENFORCED` is False.
- The timeline names only the head a force push created. The head the first push replaced is
  recovered from a review that precedes the push (or a commit event); a first head that has
  neither stays unseen, so the count can be one low, never one high.
- A timeline or check read that fails is unknown, never zero pushes: nothing triggers.
- A marker seam (a span fixed in 2 earlier heads) is reported first; the push count is read
  only when none exists.

## Retrospective verdicts

On exit 30 the read-only retrospective rules on every mechanism first
(`references/retrospective.md`); the sweep asks "what else is in this class", the
retrospective asks "should this mechanism exist". Post a class-level `> ` disposition for
each subtraction. In doubt, smaller wins.
