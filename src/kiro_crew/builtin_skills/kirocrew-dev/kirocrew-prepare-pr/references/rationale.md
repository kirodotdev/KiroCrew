# kirocrew-prepare-pr — why the rules are shaped this way

`SKILL.md` carries only what the loop executes. This file carries the evidence and
the design history behind those rules. Read it when you need to justify a deviation,
audit a rule, or decide whether a rule still applies — not on every skill load.

`references/gate-floor.md` is the companion file for the gate list specifically.

## Why the loop is one bounded cycle, never a patch-in-place

Resolving findings locally first (Phase 2 mirrors the server reviewers before any
push) cuts CI cost and wall-clock, and keeps the findings and reasoning in-session
for better fixes. Local-green does **not** guarantee server-green — same model,
different harness — so Phase 3's server poll stays the backstop rather than the
primary gate.

A failed server check re-enters Phase 1 rather than patching in place because base
movement between rounds is the common case on a repo carrying 175+ open PRs. Patching
in place produces a fix validated against a base that no longer exists.

## Why retrospective and runtime bounds are separate

A recurring span needs a retrospective because a stable failing-check count cannot
show whether each repair adds another mechanism that attracts the next finding.
The retrospective rechecks the whole diff against the original intent; it is not
a mandatory stop at the third round. Local review passes, monitor cycles and wall
clock measure different costs. The current bounds and escalation rules live only
in `SKILL.md`; none is a target, and only the user may extend an exhausted budget.

## Why the retrospective decides and continues

In practice the loop stopped at every `--rounds` exit 30: the parent collected the
retrospective, posted the remove / replace / keep verdicts as a menu, and waited.
That turned an every-third-round review into a mandatory user gate, which is the
opposite of what the loop is for. The verdicts are almost always decidable from the
PR body's frozen goal and the defect the mechanism was added for, so the skill names
the pick order and a default, and lists the only four situations where a human
supplies something the agent cannot. Recurrence is the trigger for the review, not
a reason to hand the PR back.

## Why the two Phase 0 gates come before opening

Measured across the 20 slowest PRs on this repo, rounds spent before the decision
gate and the file-overlap gate were the largest single waste class. The decision
gate used to be a line count (~1k lines needed a sign-off); it is now the issue's
tier, because the tier is the record of how big the work was judged to be and a
`tier:T3`/`tier:T4` issue already waits on a person for `triaged`. The worst case
reached full green and was then parked by a one-line product hold — every round of
that work was discarded.

The file-overlap gate's `--limit 500` is load-bearing because `gh pr list` returns
30 rows by default. With 175+ open PRs the default silently checks about a sixth of
them and the gate reads as passing.

## Why a red main gets its own PR, and the original PR waits

A PR that is red only because main is red cannot be fixed by its author's diff,
and every open PR rebuilt on that main fails the same way. Fixing it inside the
first PR that noticed widens that PR past its goal and buries the fix where nobody
looks. A separate `tier:T1` fix lands once and unblocks everyone; the original PR
rebases onto it rather than overriding a red that is real. Searching first keeps
two agents from racing two fixes for one break.

## Why the full loop arms auto-merge and watches to the merge

A review-ready PR that nobody watches decays: the base moves, a conflict appears,
the approval goes stale, and the PR sits green-but-unmergeable. Auto-merge hands
the final step to GitHub, which still requires the human approval and every check,
so arming it grants no authority a reviewer did not give. The loop stays on for the
same 24-hour budget so a conflict found after arming is rebased the same day.

## Why an agent may override a false positive itself

Most rounds that never converge are a reviewer re-raising a finding the code
already answers, or asking for machinery the goal does not need. Waiting on a
maintainer for each of those costs hours and teaches nothing. The override stays
writer-only (the handler checks), SHA-scoped, posted after an evidence-backed
`rebutted` disposition, and reported afterwards so a human can reverse it. The
carve-out is fixed: a security, data-loss or corruption finding, a crash or a
removed guard is never overridden by the agent. Those are the findings the lanes
exist to catch, and the override record is read as proof a person checked the
finding, so even a security finding the agent judges *not a defect* goes to a
writer to verify and post.

## Why the fetch in Phase 1 must succeed

Operating on a stale `origin/<base>` ref was the root cause of the 2026-07-31
clobber, where a force-push replayed 114 duplicate commits. The fetch therefore fails
closed rather than proceeding on the cached ref.

## Why push_guard builds the commits, and never from the shared index

A soft reset onto the base plus `git commit` commits whatever the index holds,
and in a shared checkout the index is shared state: another session's
`git checkout -q origin/main -- .` once left a stale tree staged there. Squashed
that way, the commit sat exactly where a good squash sits, so every structural
check passed (ancestry, ahead count, patch-id replay, `HEAD~1 == origin/<base>`)
and the post-squash guard printed `SAFE TO PUSH (single commit on base)` for a
commit that changed 5607 files and reverted 1480 upstream commits. A plain
`git commit` after staging one file sweeps in every pre-staged path the same way.

A content heuristic over the diff was built first (count the paths put back to
an older base blob) and dropped. Measured on this repository's history, it let
most trees one PR stale through. It could not see a reverted hunk inside a file
the PR also edits. It refused re-lands, bulk deletions, low-similarity moves and
codemod undos. Its limits were calibrated on one repository, while the guard runs
on any. The real root cause is a shared checkout, so the fix is to stop each step
from reading shared state:

- **Nothing staged that you did not name.** Every mode except `--check-index`
  refuses (41) while the index holds a path HEAD lacks. For `--commit` and
  `--amend` that means a path that is not literally one of the names; a
  directory does not name the entries under it. Names are compared as real
  paths, so a symlinked prefix names the same file; an empty name (an unset
  shell variable would otherwise name the whole tree) and a path outside the
  repository, or on another drive, are usage errors (64).
- **A commit by name is built in a private index.** It is seeded from HEAD;
  each named path's worktree copy is added (`-f` only for a file, never for a
  directory, so a named directory never sweeps in its ignored files), except
  that a staged change the worktree cannot show (`git rm --cached`,
  `update-index --chmod`, a gitlink whose submodule is not checked out) is
  kept. `git commit` then commits that index, hooks and signing included,
  with the message from `-m` or from `-F <file>` (which keeps prose off the
  command line a shell fence reads). The shared index changes only after the
  commit lands, only for the committed paths, and only where no one staged
  anything since the run read it, so a rejecting hook leaves nothing staged
  and a concurrent `git add` is kept. The commit landed when git exits 0 AND
  HEAD is what was built: its parents, its author, and its tree outside the
  named paths (a hook may reformat those, never add others). A commit that is
  not is taken back with a compare-and-swap when the shape is unambiguous: it
  sits where it was built, or git parented it on a commit that landed in
  between. A failure that moved HEAD anyway is named as such, never as
  "nothing was committed".
- **The squash commit is built from a tree, with no index.** `--squash` runs
  the history checks first and pins HEAD and the fully qualified
  `refs/remotes/origin/<base>` once, so a commit or fetch during the replay
  scan cannot slip in. `commit-tree` makes the commit object from HEAD's tree
  on that base, signed with `-S` when `commit.gpgSign` asks. The message is
  handled as bytes: cleaned like `git commit -F`, passed through the
  prepare-commit-msg and commit-msg hooks (`git hook run`, with an index of the
  squash's tree and `GIT_EDITOR=:`), cleaned again as `commit.cleanup` says, and
  refused if that leaves it empty. A git older than 2.36 cannot run hooks by
  name, so it refuses when such a hook exists instead of skipping it.
  Pre-commit hooks do not run, because they ran when the squashed commits were
  made. The branch ref (named once, never HEAD) then moves exactly once, by
  compare-and-swap from the vetted tip, and the outcome is read back from the
  ref. An interrupt or kill at any moment, SIGKILL included, leaves the branch
  at its old tip or at the finished squash, never at the base. That property
  is what makes a kill safe.
  `commit-tree` also reads no MERGE_HEAD or CHERRY_PICK_HEAD, so a merge
  another session starts mid-squash neither leaks in nor is used up.
  Alternative rejected for the squash: `git commit` against a private index
  (it parks the branch on the base while hooks run, and consumes another
  session's state). A commit by name has neither problem: its branch never
  leaves HEAD.
- **A rebase stop is checked before it is continued.** `git rebase --continue`
  commits from the shared index, so the skill runs `--check-index` first. At a
  stop, the replayed commit's own paths are expected, including where the
  other side renamed them (`STATUS: CLEAN`); anything else staged is listed
  (41), with remedies that only unstage or abort, never one that discards the
  replayed change. A stop on unresolved conflicts names them. An operation
  counts as in progress the way `git status` decides it: its state directory,
  or a pseudo-ref `git commit` would consume; a sequencer-only stop is named
  from its todo list (a revert is a revert). A stale `REBASE_HEAD` left by a
  finished rebase does not count. Every mode checks this before anything else,
  so a squash at a stop names the stop, not a detached HEAD.
- **Every SAFE mode reads the build record.** The record lives in the common
  git dir, so every worktree of the repository shares it. It holds one file
  per commit the guard made, named by the commit's sha and listing the paths
  it committed. A commit by name is recorded only once it has landed, so one
  a hook rejected records nothing. The squash is recorded just before its one
  compare-and-swap move, so an interrupt after the move still leaves it
  recorded; a squash whose move failed is on no branch, so its record vouches
  for nothing. No two writers share a file, and no entry evicts
  another; entries older than 180 days are pruned. Every mode that can say
  SAFE refuses while HEAD changes, against the base, a path nothing accounts
  for. That is what a squash or commit made by hand from a stale index looks
  like. A path is accounted for in four ways:
  - it is recorded for one of the branch's own commits;
  - it is recorded for a past tip in the branch's reflog, which is what a
    re-sync rebase, a conflict resolution or a squash rewrote, followed
    through any rename the base made since. `--amend` reads the base as last
    fetched for this, so it accounts for a path exactly as the push check
    does;
  - it is published: a commit on the pushed branch (`origin/<branch>`) whose
    author is not this checkout's identity changes it, and HEAD holds
    exactly that copy, as with a maintainer's commit already on the pull
    request. The pushed branch counts only when HEAD or one of the branch's
    reflog tips reaches it, so a stale remote ref left by an earlier branch of
    the same name counts for nothing;
  - it is named after `--`, which is the agent vouching for it after reading
    its diff. In the default mode and `--require-single-on-base` that vouch
    is read-only, for that run alone, so an author's commit plus a follow-up
    pushes with both commits and authors intact. `--squash` and `--amend`
    commit what is named (an amend, its worktree copy) and record it, and
    their refusals say so.

  Keying by commit, with the reflog for rewritten tips, means a deleted and
  re-created branch, a name reset with `git switch -C`, `checkout -B`,
  `branch -f` or a `git reset` of the checked-out branch (the reflog is read
  back only to that reset), or a detached HEAD
  in another worktree, inherits nothing: none has those tips. A worktree
  carried from HEAD (`git worktree add -b`)
  carries them. The record proves who committed a path, not what a later hand
  edit put in it. A stale hunk inside a path the branch committed through the
  guard is the residual below.
- **A listed path is named back as printed.** Refusals print each path as
  `:(top,literal)<path>`, which git and the guard both read as that path from
  the top, from any directory. The guard reads such a name as written, never
  through a parent that is now a symlink, and refuses one with a `..`
  component. Each refusal also writes a NUL-separated list file, which
  `--paths-from-file` reads, for a name no terminal can show.

Every probe child runs with no terminal prompt (`GIT_TERMINAL_PROMPT=0`,
`GCM_INTERACTIVE=never`), no stdin and `LC_ALL=C`. `GIT_ASKPASS` is left alone:
it is also how a non-interactive credential source is wired. Output goes to
files, not pipes, so a hook's background child cannot hold a finished step open.
Children that run user code (the message hooks, `commit-tree`'s signing, `git
commit`) keep the caller's locale, prompts, stdin and stderr, so a passphrase
prompt can be answered and git's messages show as they happen. Every child
drops the variables that relocate the repository, keeps the caller's
`GIT_CONFIG_*` (identity, hooks path, signing), and runs with
`GIT_NO_REPLACE_OBJECTS=1` and a `GIT_GRAFT_FILE` that cannot exist (a name
under the null device), so neither a replace ref nor a legacy `info/grafts` line
can hide trunk commits from the count. A graft file git can open, even an empty
one, makes every child print git's "info/grafts is deprecated" advice, hooks'
own git calls included. Every
child stays in the guard's process group: whoever stops that group (a harness
timeout, a closed terminal, SIGKILL) stops the child too. A child that outlives
its bound gets SIGTERM, then SIGKILL after a grace period (on Windows, `taskkill
/T`, then `/T /F`). SIGINT, SIGTERM and SIGHUP stop the current child before the
guard reports, except that a signal the caller ignores (`nohup`) stays ignored,
for the guard and its children. The base fetch runs with `--progress` and is stopped only when
its output has not grown for `FETCH_STALL_S`: a killed fetch keeps nothing it
received, so a total bound would fail a slow link on every retry. Its refusal
names the one unbounded manual `git fetch` that gets past a link that stalls.
Each 41 writes its own list file, so a remedy reads exactly the list it
printed, and it lists every path whose worktree copy may hold edits (`(*)`);
the command that clears the list is offered only when none is marked.

Residuals, on the record:

- An exact re-land of a reverted change is still refused by the replay check.
  That check predates this design.
- A conflict resolved wrongly, for example `rebase -X theirs` dropping an
  upstream line, is the branch's own content. So is a stale copy of a file the
  replayed commit also touches, staged at a rebase stop, and a stale hunk
  committed by hand into a path the record already holds. No history or index
  invariant can tell any of them from an intended edit; the review lanes are
  the check.
- Vouching for a path by name is the agent's word. The refusal counts and
  lists the paths and tells it to read each diff first, and never to name one
  that is not its own; no shipped remedy lists a whole diff to vouch with.
- A path counts as published only on `origin/<branch>`. A fork's pull request
  pushed to another remote has no published paths, so a co-author's commit
  there is read and named. A commit of yours that was pushed without the guard
  is not published either, so it is read and named. A stale copy pushed by
  another author still counts as published, and pushing it again changes
  nothing on the pull request.
- The rewritten tips come from the branch's reflog. Where reflogs are off, or
  a rebased-away tip's entry has expired (30 days by default), the guard's own
  paths are refused and named again: the failure is a refusal, never a pass.
- A grandchild that left the guard's group and ignores SIGTERM can outlive a
  stopped child. On Windows the tree kill reaches it.
- push_guard's runner resolves git from PATH's absolute entries only and
  refuses a batch launcher fed cmd.exe metacharacters (`_which_git`,
  `run_child`). An absolute PATH entry that points INSIDE the worktree is
  still not refused there, unlike `preflight.py`'s worktree fence; moving that
  fence into push_guard is a follow-up.
- The replay scan runs one `diff-tree` and one `patch-id` per commit, each with
  its own bound. Batching them, and one overall deadline, are follow-ups, as
  are `diff_signals.py` and `green_age.py` still resolving short base names.

## Why a green expires, and why the check is client-side

A pull request's green is a verdict about `refs/pull/<N>/merge` at the moment the
run was triggered. Nothing re-derives it when the base moves, so a head that
passed and then waited for review can merge against a base its tests never saw.
Measured: a PR ran CI once at 15:39, waited about ten hours, and merged at 01:32
with no re-run; two PRs merged in between had added tests that the merged change
makes fail. The red surfaced two hours later on an unrelated PR, because this
repo's `main` CI is routinely cancel-evicted by the next merge — three
consecutive main runs read `cancelled` — so there is no per-commit verdict on
`main` to catch it either.

Two obvious server-side fixes were rejected by the constraint that matters here:
merge velocity. The median merge gap is about 1.4 minutes and CI takes about 19,
so a merge queue or strict up-to-date protection would serialise the repository
behind its own CI. That held until the repository adopted a merge queue (#15200):
CI, Fast Gate and Build now run on `merge_group`, described in
`docs/ci/ci-and-reviews.md`. `green_age.py` is the soft version of the same idea. It runs
client-side during the review wait, and it costs a rebase only on a PR whose
files actually collide with what the base gained — not on every PR, and not at
merge time.

That is also why the exit code is consumed by the skill and by nothing else.
`pr_status.py` prints the line and carries the summary in `advisory.green_age`,
but never feeds it to `decide()`: a client-side heuristic in front of a required
check would be a merge gate the maintainers declined, and a probe that cannot
measure (exit 2) must not be able to turn a readable PR red.

`babysit`'s `BEHIND` trigger does not cover this. `mergeStateStatus` reports
`BEHIND` only under strict up-to-date protection, which is off here
(`strict_required_status_checks_policy: false`), so on this repo that trigger is
dead and the exit-30 signal is what replaces it.

### What this does not close

The re-sync half only reaches a PR the loop is actively driving. A PR a human
merges by hand gets the `green age:` line `pr_status.py` prints and nothing else,
so the incident class is narrowed rather than closed: a merger who does not read
that line can still merge an expired green. Closing it outright needs a
server-side signal, which is the thing the merge-velocity constraint refuses.

Exit 2 is deliberately neither of the other two. A base that cannot be read is not
a fresh green and not a stale one, so it satisfies no criterion and blocks none;
after three consecutive 2s the loop reports the reason and hands the PR over rather
than polling forever on an environment it cannot fix.

## Why the Backwards compatibility section is not a required one

The same observed incident has a writer-side half, and it is the half that actually
broke. The PR that waited ten hours TIGHTENED a contract: it made a ledger entry's
`data` validate against a registry, so a payload that main accepted before now
raises. That is a `Breaking:` change, and the writers it broke are exactly the ones
the two PRs merging in between added -- `test/test_ledger_kinds.py`,
`test/test_ledger_retention.py` and `test/test_remove_slot_for_history_key.py` all
hand-write payloads without the new required fields. A writer sweep taken when that
PR opened would have found none of them, because none of them existed yet. So the
rule is not symmetry with the tests-side check: it is the same expiry, on the same
merge, read from the callers instead of the tests -- which is why a tightening diff
owes a `Breaking:` line plus a writer sweep re-run on fresh `origin/main` right
before the last push, with that sha written into the body. `green_age.py` asks that
question of the tests; the sweep asks it of the callers.

The section is deliberately absent from `REQUIRED_SECTIONS` in
`.github/scripts/pr-description-check.sh`, the list PR Hygiene and
`fork-pr-description.yml` both check. That list is matched against the body of
every open PR on each `edited`/`synchronize`, so adding a heading to it would fail
every body written before this change -- a red that says nothing about the diff.
The rule reaches the author through the template's own prompt and through
kirocrew-prepare-pr's Phase 1.5 instead, where it costs an existing PR nothing. The frozen
goal sections pay that cost on purpose: a PR with no goal is exactly what the
gate exists to stop, so its red does say something about the PR.

## Why force-with-lease must be SHA-pinned

The implicit form (`--force-with-lease` with no SHA) silently accepts a
just-fetched remote-tracking ref, so a maintainer commit pushed between iterations is
overwritten without the lease firing. The pin must record the remote tip *before*
any fetch/rebase/squash that could advance the ref.

The pre-squash ancestor check (`git merge-base --is-ancestor origin/<branch> HEAD`)
is not re-run after the squash because it can never pass on a rewritten branch — the
old remote tip is not an ancestor of the new squashed commit. Re-running it there
would fail every iteration for a structural reason unrelated to safety.

## Why the profile is read from the base ref

The profile declares which reviewers run and which contract each mirrors. A branch
that could edit its own profile could drop the lane that reviews it. Reading
`.prepare-pr.toml`, the Kiro Crew markers, and the workflow globs from the base ref
closes that. A ref resolving to nothing is a hard error rather than a silent fall
back to the checkout, because the fall-back case is exactly the attack.

## Why the gate list is data, not prose

A gate an LLM has to notice in a paragraph is followed exactly as unreliably as the
gates this loop kept missing. `test/test_prepare_pr_profiles.py` pins the floor to
`ci.yml` and `fast-gate.yml`: every script, npm script and tool they run must appear in `gates[]` or
be named exempt with a reason, and every gate must name a target that exists. CI
gaining a blocking scan therefore fails that test instead of surfacing as a review
round on a later PR.

Narrowing **within** a surface needs a real import graph, not a text scan, so
`run_scoped_tests.py` deliberately does not attempt it. It only replaces the *other*
surface's full suite with the cross-surface set `ci.yml` runs for a single-surface
diff — measured at 350 backend files or 146 frontend specs, against 62k collected
backend tests and ~1.4k frontend specs that are not worth re-running serially on ten
inner-loop iterations for a signal CI produces on the merge ref anyway.

## Why the extraction fallback must announce itself

`local_review.py` exists to stop the hand-written charters from drifting away from
the CI workflows they mirror. A silent fallback to those charters reintroduces
exactly the drift the script prevents, so exit 40 requires a visible WARNING and a
fix to the extractor rather than a quiet degradation.

The charter budgets are hand-copied from CI and that copy is what drifted before —
the skill claimed ≤2 BLOCKING long after CI moved to 5.
`test_charter_budgets_match_the_ci_workflows` now pins the wording to
`.github/review-prompts/opus-validate.md` and `gpt-review-core.md`. The GPT lane
carries no numeric cap on purpose: a numeric budget encouraged staging discoveries
across review rounds.

## Why proportionality is a separate question from legitimacy

Legitimacy is necessary but not sufficient. A finding correct in the abstract can
still demand a change out of proportion to the PR's purpose — speculative hardening
against inputs that cannot occur, robustness for a single caller, gold-plating an
internal tool as if it were a public API. Appeasing those is the mirror image of
appeasing a false positive and costs the same.

Both keep-the-code outcomes record as `rebutted` because the *action* is identical —
the code does not change and a reply goes on the thread. Only the argument differs.
Earlier drafts made push-back a separate disposition, which produced dispositions
that were indistinguishable in effect and a taxonomy the agent had to reason about
instead of applying.

## Why `needs-a-decision` is not `accepted-and-deferred`

`accepted-and-deferred` files an issue. An issue whose body asks the maintainer to
choose between options is not a task: no contributor can act on it until the choice
is made, so it occupies the tracker indefinitely while the question inside it goes
unread. `needs-a-decision` puts the question where it will be answered and files
nothing. `test/test_deferred_disposition_ratchet.py` enforces that every surface
offering the first also offers the second.

## Why every concern must be answered, including advisory ones

`Design Review 🟡 CONCERNS` and `UX Review 🟡 CONCERNS` post their concern *and* pass
the check. The readiness rollup will therefore never force an answer, and nothing
else in the loop nags. To a human maintainer a silently ignored concern reads as
"the author never looked at it", regardless of how green the checks are.

Design Review owns the long-term-reversibility lens. It is advisory and reds only on
a genuine `BLOCK`, but an irreversible choice it flags needs a written justification
a reviewer can read — which is why it is fix-or-justify rather than a raw Medium.

## Why the disposition comment is scoped twice

The reviewer's adjudication ledger keeps the marker, the `> ` rationale lines, and
the `- **...**` title bullets, and scopes a ruling's coverage by its recorded
rationale. So `target=` naming a single lane keeps a Design concern from riding along
on the GPT comment, and one-rationale-per-finding keeps a reused reason from silently
claiming findings it was never checked against. A rationale reused across several
findings is the blanket line from "Common mistakes" with a marker on top of it.

## Why review repairs are delegated by reviewer family

The routing table in `SKILL.md` records a maintainer-requested execution policy
for this repository's own PRs. It does not rest on a defect found in an earlier
parent self-fix, and it does not claim a delegated repair is better than a parent
repair. The acceptance condition is: each CI AI finding is repaired by a
model-pinned subagent from the same family as the lane that raised it, preferring
the stronger available member of that family, with disclosed lower-tier fallback,
while the parent keeps verification, consolidation and publication. The intent is
a repair that starts from the finding, the owning spec and the assigned files
rather than the parent's round history (a subagent can still inherit context, so
this is intent, not a guarantee), read by the family that wrote the finding, and
checked by the parent as a separate step.

The listing selects availability, the table states preference: the backend/account
model listing says what exists, and an entry there is neither entitlement nor proof
of service, which is why the skill still requires exact IDs and reports an
unverified served model as such. "No pinnable spawn facility means a blocker" is
part of the requested policy: the parent reports the blocker and hands off rather
than presenting its own fix as delegated. The names live once, in the `SKILL.md`
table; `docs/ci/ci-and-reviews.md` points at it, and
`test/test_review_repair_routing_skill.py` pins the row order deliberately, so a
generation change is one table edit plus the test that records the policy.

## Why both body checks are gates

`--check-body` gates completeness (exit 20) and length (exit 21) at once, and
the two pull in opposite directions on purpose. The accounting check is the leak
detector, not the prose: a long walkthrough hides a stray file better than a
short body does, because the reviewer trusts it and skips the diff. The length
check used to be a WARN, on the theory that a rename or a shared-helper migration
needs the words and a hard cap cuts true facts. In practice the WARN was never
read: the hard check rewarded listing, the soft one was ignored, and bodies grew
round by round into per-file recitals nobody could read. With the ledger
guaranteed complete, a cap cannot cut a true fact -- only a restated one. The
code is the evidence; the body says what changed and why. Short prose, complete
ledger -- paths, tables and pictures never count against the limit.

## Why the PR body must come from the template file

The maintainer's auto-approval bot greps for the template's exact heading strings.
`## Problem` instead of `## Problem / Motivation`, or `## Fix` instead of
`## What changed`, blocks workflow approval indefinitely. There is no bundled copy
because the repo's file is the single source of truth and the skill always runs
inside a checkout.

## Why screenshots are attachments, not commits

`docs/` and `src/kiro_crew/**` ship in the wheel, the sdist, and the desktop DMG, so
review images placed there ride into a shipped artifact; a dedicated directory keeps
them out of the package but still puts megabytes of pixels into every clone, forever.
An attachment puts nothing in the repository at all: `gh pr create|edit --attach`
uploads the file with the caller's own token and rewrites the body's local path to
`https://github.com/user-attachments/assets/<uuid>`.

That URL is tied to no commit and no branch. A raw-blob URL has to be pinned to a SHA
to survive branch deletion on merge, and then every squash, amend or force-push moves
the SHA out from under it, so the body needs a re-pin pass after each round and the
first missed pass leaves a broken image. The attachment URL survives all of those
without anyone touching the body, so the loop never re-pins. External image hosts
are the other alternative and are worse on both counts: they leak content, and GitHub
Camo blocks them on private repos.

The review lanes read the same URL the author wrote: the UX lane's blind read
downloads the `user-attachments` links from the PR body, and the screenshot-evidence
gate accepts them as visual evidence. Dragging a file into the description in the web
UI produces an identical URL, so a human reviewer and the lanes see one convention.

**The one scoped exception: a contributor with no write access on the repository.**
`gh --attach` uploads through an endpoint that answers read permission with a 404
(cli/cli#14302), so a fork contributor has no CLI path to an attachment at all; the
web-UI drag is their only one. For them, and only them, committing the media under
`temp-screenshots/<topic>/` (`git add -f`, past the ignore rule) and referencing the
repository-relative path from the body is admissible evidence:
`.github/scripts/pr-committed-evidence.sh` reads the blobs out of the object store for
the fork lanes, and the screenshot-evidence gate accepts a still-linked committed
path. The exception does not weaken the rule for anyone who CAN attach: every reason
above still holds for them, and the cost below is one they would pay for nothing.

**What the exception costs, and which part is irreversible.** When the PR merges,
the committed file becomes a tracked file on `main` and a blob in history. The tracked
file is the reversible half: the merging maintainer removes it in a follow-up
`chore(evidence): drop the committed review media of #<n>` PR, so review media never
accumulates on the tip again (the sweep that emptied the tree took out 1,739 files
and 286 MB). The blob is the irreversible half: a deletion commit leaves it in every
clone's pack, and only a history rewrite removes it, which that sweep deliberately
deferred. The cost is accepted because it is bounded — fork PRs are the minority, the
evidence script refuses a file over its size ceiling, and the guidance asks for two or
three shots — and because it never lands silently: the files are in the diff the
maintainer squash-merges. The merge-time steps are in `docs/ci/ci-and-reviews.md`.

## Why the closing-keyword check reads the API back

The host resolves the link at PR-open/edit time and exposes it as a field, so
`closingIssuesReferences` is ground truth and the prose you just wrote is not.
Merging with no closing keyword is the leak with the longest tail: nothing reconciles
it afterwards, so the work ships, the issue stays open, and the next person to read
that issue plans against stale information.

`pr_status.py` owns the parsing so the agent does not have to model Markdown:

- A trailer is read as a complete visible line of `<keyword> <reference>` pairs; a leading bullet, a trailing `.`, and a trailing HTML comment are tolerated.
- Text inside a fenced block, an inline code span, or an HTML comment is masked before classification, and a trailer indented four or more columns is never read as a declaration — four columns is Markdown's own code boundary, and a tab reaches it. That single bound is what makes a copied example safe regardless of what precedes it.
- The bias is deliberately toward refusing: withholding credit from an oddly-indented trailer at worst prints an advisory notice, while crediting an example silently suppresses one.
- Reconciliation runs in the inverse direction too, matching on repository **and** number. A bare `#<n>` resolves to this PR's own repository, so a stale `Fixes other/repo#7` no longer vouches for a resolved closure of this repository's `#7`. Where either side's repository is genuinely unknown the match stays wide, so the notice fires only on a real disagreement.

## Why the check rollup is fetched separately

`statusCheckRollup` needs Checks read access, which a fine-grained PAT structurally
cannot grant, and GitHub resolves each `gh ... --json` request atomically. Both
scripts therefore fetch the rollup in its own call. That call is a GraphQL read of the
head commit's rollup rather than `gh pr view --json statusCheckRollup`: the rows `gh`
returns name the workflow by its display label and nothing else about the run, while
`collapse_superseded` keys on the run itself -- its id, its triggering event, its
workflow definition's id and its own conclusion -- which only the check suite exposes.
On failure the scripts print `NOTICE: CI check status UNAVAILABLE ...` and continue
with an empty rollup rather than aborting. Every page of the read re-fetches
`headRefOid` and names the commit the rollup hangs off, and the whole read is
discarded (`NOTICE: CI check status DISCARDED ...`) when either disagrees with the
core read's head, so one head's metadata is never paired with another head's checks.
A board past the read's page cap is reported UNAVAILABLE rather than in part, since a
partial read could keep a displaced row whose successor was never fetched.

Both states are deliberately distinct from a genuine "no checks yet":
`pr_status.py` still fails closed at exit 20 but with a `CI status unreadable ...`
reason naming the environment cause, while `no CI checks reported` is reserved for a
healthy read that truly returned zero checks. A loop comparing `progress_key.status`
can then tell an environment gap from a code blocker.

## Why `${VAR:-default}` cannot appear in a path position

An agent safety filter resolves `$HOME` but cannot statically evaluate a `:-`
default, so it refuses the whole call as an *"unresolved shell variable in path
position"* and ends the turn before any script runs. The unresolved value taints
every path derived from it, so splitting the assignment across lines does not help.

## Why the driver is `monitor_start` and not a cron

A turn is capped at 2 hours and a CI round here costs 20–40 minutes, so an in-turn
poll loop reliably hits the cap around iteration 3–4 — losing the loop, though not
the work. `monitor_start` gives each round a fresh turn and survives a tab close or
gateway restart.

Cron and heartbeat cannot drive the fix loop, and both report success while doing
nothing. A cron has no owning slot, so its tool calls hit a deny-by-default approval
path and time out after 180s without a global auto-approve grant — and a denied tool
inside a completed turn still records `last_status: ok`. A real PR watcher logged 101
runs over 25 hours with 23 approval blocks, zero pushes, and a healthy-looking
registry. Heartbeat runs under a strict name allowlist (`HEARTBEAT_SAFE_TOOLS`) with
no shell and no `git push`, so it cannot amend a commit at all.

`monitor_watch` is exempted only for a pure-watch stretch because it never interprets
comment bodies: it digests them only to notice a change, so it cannot tell whether a
bot posted its verdict. A round is complete when every check finished **and** every bot posted, and
a provider-typed watch cannot see the second half of that condition. There is no
script-cron watcher to exempt: a cron holding a copy of the retired driver is
refused on every tick and auto-paused.

## Why arming cannot be confirmed from the reply

Arming is applied after the tool returns, so an acknowledgement alone does not
prove a loop exists. A synchronous refusal is definitive; other results need the
state check prescribed in `SKILL.md`. That check and its bounded fallback prevent
both silent abandonment and duplicate poll drivers. Monitor cycles are not server
rounds, so a wall-clock bound must accompany the cycle budget. The execution
procedure owns those budgets rather than a second copy here.
