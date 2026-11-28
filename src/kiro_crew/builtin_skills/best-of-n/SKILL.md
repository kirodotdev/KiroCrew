---
name: best-of-n
description: Use when the person asks to run one coding task on several models and compare the results. Each model works in its own git worktree, you show one comparison table, and only the attempt the person picks is applied. Costs credits, so it waits for a typed yes.
triggers: best of n, run this on three models, run this on several models, try this on several models, same task on several models, compare models on one task, models side by side
inject_on_trigger: false
---

# Best of n: one task, several models, compare, keep one

Run the same change on several models at once, each in its own detached git
worktree, then compare the attempts and keep only the one the person picks.

This is for comparing attempts at a change, on the person's explicit request.
It is not for debate or a second opinion on a question: use `llm-council` for
that.

## When not to use

- One model is enough, or the person did not ask for several models.
- The work folder is not a git repository.
- The task is a question or a review, not a change to files.

Never start this on your own because a task looks hard.

## Step 0: check the ground

1. The work folder is in a git repository with a commit at HEAD.
2. The repository, and the worktree that will sit beside it, are both under
   `agent.subagent_cwd_allowed_roots` (the defaults are `~/workspace`,
   `~/workspaces`, `~/workplace` and `~/workplaces`). If not, tell the person
   that setting decides it and stop. Never widen it.
3. A worktree starts from the last commit. Uncommitted changes are not copied.
   Ask the person to commit first, and wait. Never commit, stash or reset for
   them.
4. Say that a worktree separates files only, not databases, ports or outside
   services, so tasks that touch those can still collide.

## Step 1: models, count and cost

The person's own list wins. When they name none, list the models the backend
offers the way the `spawn_run` description says (on the kiro backend,
`kiro-cli chat --list-models --format json`; another backend accepts only its
own ids), and propose three spread across different vendors. Say why. Do not
reuse a model list from memory or from another skill: it goes stale.

Show the person, before anything runs:

- each model and its `rate_multiplier`;
- the count, at most four. If they ask for more, say four is the limit;
- the total as "about N times the cost of one run".

Then wait for a typed yes. A model on a task gives each worker its own process
(about 0.55 GB each), so call `resource_status` first and stop on `critical`.
This skill is not a spending limit: it only asks before it spends.

## Step 2: one call

Make one `spawn_run` call:

- `cwd` is the repository root, and `isolate` is `"worktree"`. `isolate` belongs
  to the whole call, never to an entry of `tasks`.
- `tasks` has one `{task, model, label}` entry per model, all with the same brief.
- `include_memory` is `false`, so no worker inherits another's framing.
  `include_lessons` stays `true`, because workers edit code.

Each brief says: work only in your own folder; make the change; run the test
command the person gave, if any; commit your work as one or more commits; never
push and never open a pull request; end with five lines (what you did, files
touched, test result, risks, what you would do next).

## Step 3: wait for every worker

Wait for every `[Subagent completion event]`. Do not do the task yourself while
you wait. Drop a worker that failed and carry on with the rest. Stop only if
none came back.

Each event names the worker's worktree and its commits (`base..HEAD`), or says
it made none. Report a worker with no commit as "no change" and do not compare it.

## Step 4: compare

For each worker, read the full diff and run `git diff --stat <base>..HEAD` in its
worktree. If the person gave a test command, run it in each worktree. Show one
table: model, files changed, lines added and removed, test result, and a
two-line note on the approach. Mark an attempt that touched files outside the
task. Say in one sentence which looks best and why, and say the person decides.

### The card, on the dashboard chat only

The dashboard chat renders `<mcwidget>`. There, show the same comparison as one
card instead of the markdown table (load the `widgets` skill first for its
markup and theme rules). On any other surface, such as a messaging channel, a
terminal or a background turn, the plain table above is the answer.

- One row per attempt that has a commit, numbered 1, 2, 3 in the order you list
  them, with the same columns and marks as the table. A "no change" worker gets
  no row and no button: name it in one line under the card.
- Each row has one button, `data-action="keep_attempt"`, labelled "Keep this
  one", with `data-payload='{"attempt":N}'`. N is the row's number and the
  payload holds nothing else: no model, path, commit or note.
- Every value that came from a worker, a diff or a test run (a note, a file
  name, a model name) goes into the card as escaped text: `&`, `<`, `>` and
  quotes become entities, and none of it is ever markup or an attribute value.
- A button press only pre-fills the composer with
  `[UI] keep_attempt: {"attempt":2}`. The person still presses send, and nothing
  is applied until they do, so never say a click kept or applied an attempt.
- When that reply arrives it names the winner, nothing more. Read N as the row
  number in your own table, take the worker from that table and never from the
  reply, and go on to Step 5 for the branch name.

## Step 5: keep one

Only after the person names the winner, and after they agree to a branch name,
create that branch in the main checkout and apply the winner's commits to it
with `git cherry-pick <base>..<winner head>`. On a conflict, stop and report it.
Never push.

The other worktrees stay where they are. Give their paths so the person can open
them. A clean worktree is removed by Kiro Crew when the run's record expires, and
one with commits is kept. Never remove a worktree or a folder yourself unless
the person asks.

## Refusals to relay, not work around

- `spawn_run` has no `isolate` field (an older gateway): say so and stop.
- `spawn_run` is absent because the engine's own sub-agent tool replaced it (the
  default-off `kas_subagents` preview). That tool takes no per-run model, so this
  skill cannot pin models there. Say so and stop.
- Any refusal from the tool (not a git repository, no commit at HEAD, a folder
  outside the allowed roots, the worktree folder already exists): quote it to
  the person as written.
