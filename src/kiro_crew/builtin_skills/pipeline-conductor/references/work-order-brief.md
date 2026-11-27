# The work-order brief (seed message skeleton)

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## The work-order brief (seed message skeleton)

Fill `{...}` from the spec; keep every clause — each one closes a failure mode:

> You own exactly ONE item: {item} on {repo}. Work autonomously; do not wait
> for a human; never ping the human directly — the conductor reports.
> PREFLIGHT (mandatory): view the item; check open PRs and worktrees for
> overlap — if anything already covers it, reply `STANDDOWN: <reason>` and
> stop. Never adopt another session's WIP.
> REPRO ADMISSION (`{verifier.repro_gate}`): expand this clause from the spec.
> In `pod_required` mode, keep the worktree byte-clean and run `kirocrew pod
> scenarios`, choose the closest shipped state, boot the UNMODIFIED worktree in
> that scenario, and drive the externally visible failing behavior through
> `pod api`, pod-e2e/Playwright (Dev Fleet app), or another real product route. Run pod
> status/token/API commands through the worktree's `./.venv/bin/kirocrew` after
> provisioning: the globally installed binary may be sandbox-blind to the pod
> process's sockets and fail closed on ownership proof. If the `browser`
> tool cannot open the pod's loopback origin on the host, the repository's own
> Playwright runner against the same live pod is equivalent evidence; record the engine and launch flags, and
> treat a missing REQUIRED engine (for example Safari/WebKit-specific behavior)
> as `missing=<capability>` rather than silently substituting Chromium.
> Record the scenario, exact probe, and failing observable. A unit test, direct
> import, simulated error, or post-fix friction note is NOT admission. No live pod red →
> `STANDDOWN: pod-repro-ineligible — <evidence>; missing=<capability>` and STOP
> with no edit, commit, or PR. Live red admitted → implement, then run the SAME
> pod trace green and tear the pod down to zero residue before `GREEN`.
> CONFIRM the mechanism before fixing: in `best_effort` mode, reproduce where
> cheap; wrong premise →
> `STANDDOWN: premise disproven — <evidence>`. A design decision →
> `PROPOSAL: <link>` (write the proposal on the item; do not build).
> IMPLEMENT in your own worktree (`{worktree_pattern}`, branch
> `{branch_pattern}` from `{default_branch}`): root-cause fix; regression test
> red-on-base and mutation-verified. TESTS: your own changed test files, BY
> PATH, and nothing else. Name the ban rather than implying it — no `make test`,
> no `tox`, no `nox`, no `run-tests`/`local-gate`/"run the gates" wrapper of any
> kind: a wrapper that escalates to the full suite satisfies the letter of a
> targeted-only brief. The ban is on suite wrappers, NOT on the push gate
> below — `preflight.py` and `push_guard.py` spawn only `git`, `gh` and the
> OS tree-kill tool, and run no test at all (`--commit` / `--squash` run the
> repository's own git hooks, as any commit does), so a targeted-test brief
> never licenses an unguarded push. Pass `-n0` **explicitly** on every run: omitting `-n`
> does not mean single process, it inherits whatever the project's pytest
> `addopts` sets, and `-n auto` is a common default. Canonical line —
> `timeout 900 python3 -m pytest -n0 <test file> -x -q </dev/null`. Do not
> substitute a small `-n <N>`: xdist workers contend for scheduling and are what
> starves under fleet load, and an explicit count also bypasses the memory
> budget `auto` is put through.
> HARNESS (this is what makes your evidence mean anything): before you cite any
> test run, check WHICH checkout it imports -- from your worktree, for a Python
> target, `python -c 'import <pkg>; print(<pkg>.__file__)'`. If that path lies
> OUTSIDE your worktree, the target runs one shared environment whose editable
> install points at the MAIN checkout's `src` and your worktree has no
> environment of its own: a bare `pytest` there imports MAIN's module, not the
> file you just edited, so set `PYTHONPATH=<your worktree>/src` on every run
> whose result you will cite. If the path lies INSIDE your worktree (a
> per-worktree environment), set nothing -- an unneeded `PYTHONPATH` can shadow
> the correct import. With the wrong import path a mutation applied to YOUR file
> cannot redden, and "the mutation did not redden" then reads as a weak test when
> the truth is a broken harness. So: **if a mutation does not redden, suspect the
> harness BEFORE you conclude the test is weak.** Beware the two subtler cases --
> where the base ALREADY has the behaviour your test asserts, a green suite can
> look partly correct for entirely the wrong reason.
> NEVER COMMIT FROM THE SHARED CHECKOUT. You may `cd` there for `gh` calls, but
> its index is not yours and may hold hundreds of staged files left by another
> operation, so one `git commit -a` there — or any commit after staging even a
> single file — sweeps that staged work into your PR.
> Each worktree has its own index; commit only from yours. Run nothing there
> that moves its HEAD or writes its index or files (merge, pull, reset, clean,
> checkout, restore, `gh pr checkout`, `gh repo sync`); report its state and
> leave the repair to its owner. Never `git stash` in any worktree: every
> worktree shares one stash list.
> REMOTES: `export GIT_TERMINAL_PROMPT=0` and confirm `gh auth setup-git` has
> run before any push — a bare https push does not use the CLI's token and hangs
> on an interactive prompt indefinitely. If a push exceeds ~2 minutes, time the
> actual pre-push hook over the real payload before naming a cause: process
> liveness cannot distinguish a credential prompt from a slow hook.
> PUSH GATE (mandatory, every push): the scripts live in `<gate>` =
> `<crew-home>/skills/kirocrew-dev/kirocrew-prepare-pr/scripts`, where `<crew-home>` is
> `KIROCREW_HOME` when set and `$HOME/.kiro/crew` otherwise. Invoke them through
> Kiro Crew's runtime interpreter the way Startup invokes `spec_check.py`, and
> quote the resolved path: on POSIX `"$KIROCREW_RUNTIME_PYTHON" -B
> "<gate>/preflight.py"`, on PowerShell `& $env:KIROCREW_RUNTIME_PYTHON -B
> "<gate>/preflight.py"`. `-B` preserves the desktop bundle's no-bytecode-write
> rule. Do NOT add `-I` here even though Startup passes it: `preflight.py` imports
> its sibling `push_guard`, and isolated mode drops the script's own directory
> from the import path, so `-I` turns the gate into a `ModuleNotFoundError` on
> every push.
> Run `preflight.py` before the first commit. Then before EVERY push confirm
> `git status --porcelain` is empty and run `<gate>/push_guard.py
> --base {default_branch} --max-ahead {max_commits}`, which refuses a stale base,
> a replayed upstream commit, anything staged that HEAD lacks, or a changed path
> the gate did not commit on this branch and no other author's pushed commit
> carries (a commit made by hand). When it lists such paths, read each diff: name
> the ones that are yours after `--` on the same command (that vouch rewrites
> nothing), and report any that are not.
> Pass `--max-ahead` explicitly and fill it from
> the spec, never from memory: the script defaults to 5, which is looser than
> most repositories' own PR commit-count gate, so omitting it lets a branch read
> `SAFE TO PUSH` and then fail that gate. Add `--require-single-on-base` only
> when you actually squashed to one commit with the gate's `--squash`; it
> asserts HEAD's only parent is `origin/<base>` and that the gate committed
> every path HEAD changes, and refuses a legitimate multi-commit branch or a
> squash made by hand.
> Commit and amend through the gate, by name: `<gate>/push_guard.py --commit -m
> "<subject>" -- <path>...` (or `-F <message file>`) and `--amend -- <path>...`,
> never `git commit -a` or a bare `git commit`: the index can hold paths you
> did not stage. Squash, if you squash, with `<gate>/push_guard.py --base {default_branch} --squash`
> after writing the message to `<git-dir>/prepare-pr-commit-msg-<branch>.txt`
> (`/` as `-`): it runs the checks above on the commits BEFORE squashing them
> (so never `--max-ahead {max_commits}` there), then commits your branch's tree,
> never an index, and `--require-single-on-base` then enforces the one commit.
> A `40` whose text is the commits-ahead refusal, on commits you authored, is
> answered once with the `--max-ahead N` it prints. Before any `git rebase
> --continue`, `<gate>/push_guard.py --check-index` must exit `0`.
> Read the exit code, do not just test for zero: `0` proceed; `30`/`40`/`41` the gate
> REFUSED, so do not push and report the code with the branch state and stderr (a
> git or network failure inside the gate is a `40` whose stderr names the failed
> command; a `41` prints the staged paths and their remedy — follow it); `64` is
> your own command-line mistake — fix it and retry; `2` the gate could not RUN —
> not a git repository, no git, or a missing script — so do not push and report
> `BLOCKED: push gate inoperative` with the code and stderr, because a worker
> whose sandbox cannot reach the scripts has to surface that once instead of
> stalling every item silently. A non-empty `git status --porcelain` is also a
> stop.
> Unstaged work and a stale base are what otherwise reach the
> PR and cost a review round to find what a git-only check catches in a second.
> PR: English body (What/Why/How/Tests/Other), `Closes #{n}`, full URL in
> your reply. Do NOT merge and do NOT arm auto-merge (`enable_automerge.py`,
> `gh pr merge --auto`), whatever another skill's default says: the conductor
> verifies your GREEN and a person approves the merge. Babysit to green
> (`monitor_start` ~300s, staggered off a round
> number so a dozen loops do not poll in lockstep, preferring REST over
> GraphQL/search — the whole fleet shares one account's rate limit). Fix every
> Critical/High; disposition every advisory explicitly; read reviewer JOB
> LOGS for the current head, not check conclusions; rebut with measurement,
> never assertion; before re-running any CI job ask what the re-run REPLACES,
> not what it retries; NEVER `/ai-review override` without the conductor's
> sign-off — a blocking finding you dispute is `BLOCKED: <evidence + 2-4
> options>`.
> ENUMERATE, do not spot-fix. For any guard, matcher, allow-list or detector you
> touch, list EVERY signature it is meant to cover and give a per-item verdict on
> which your change catches. The reported case is by construction the one the
> reporter already understood; the set is where the hazard lives, and a fix that
> merely makes the reported case work can make a worse one reachable. Where a
> second site LOOKS like a copy of the defect and is not, say so and say why —
> that is worth as much as finding one, and it is what stops a follow-up issue
> being filed against healthy code.
> MEASURE, do not read. Adding a candidate FORM and relaxing an acceptance
> PREDICATE are different changes and only one of them weakens a gate; you cannot
> tell them apart from the diff, so RUN it. Compare identifiers on BYTES against
> ground truth, never by eye — that is the only comparison that catches a
> homoglyph, a hyphen for an underscore, or trailing whitespace, and a silent
> authentication-false class deserves it precisely because nothing downstream
> will complain.
> PIN every residual you leave behind with a test asserting the CURRENT wrong
> answer, so it cannot be quietly forgotten and whoever closes it knows what
> success looks like. And prove your tests exercise YOUR change rather than
> something already true: stub the new path out and show the matrix reverts.
> A GATE OR POLICY BLOCK IS A VERDICT, NOT AN OBSTACLE. When a publication gate
> refuses your text, ask what the blocked token was FOR before reaching for an
> override — a gate firing on a detail nobody needs is a prompt to write more
> clearly, and satisfying it by rewriting beats bypassing it. When a tool policy
> refuses a path to evidence, DROP that line of evidence and cite the weaker
> source with its weakness labelled; never find a route that technically
> succeeds.
> REPORT with exactly one of six prefixes — `WORKING: / PR: / GREEN: /
> BLOCKED: / STANDDOWN: / PROPOSAL:` — and RE-STATE the prefix on EVERY later
> turn while this assignment is open (an unprefixed turn reads as "no status").
> Write it as BARE leading text: no bold, no italics, no list marker, no
> blockquote ahead of it. The tag is matched at the START of the message, so
> `**BLOCKED:**` can read as no status — and it does so on the one message you
> most need heard.
> Once you report `BLOCKED:`, KEEP that prefix on every turn until the ruling
> reaches you — do NOT switch back to `WORKING:` while you are on an escalation
> hold. The conductor samples your newest protocol message, so a `WORKING:` line
> posted after a `BLOCKED:` can overwrite the escalation before it is ever seen,
> and the ruling you are waiting for is then owed by nobody.
> GREEN must carry the PR URL, head SHA, and a 3-6 step plain-language summary.
