---
title: Git branch switcher -- switch the project's checked-out branch from the dashboard, refused while an agent works in the repository
status: draft
author: inariku, with Kiro Crew
created: 2026-10-24
last-audited: 2026-10-25
audited-at: 1a7190c711
doc-pr: 23725
implementation-prs: [23102]
tracking-issues: [22670]
supersedes: []
superseded-by: []
---

# RFC: Git branch switcher

- Status: draft. Nothing is built on main. A working implementation of every
  phase below exists as one draft pull request,
  [#23102](https://github.com/kirodotdev/KiroCrew/pull/23102), which takes over
  [#17628](https://github.com/kirodotdev/KiroCrew/pull/17628). That pull request
  is held in draft until this document is accepted, and will be split along the
  phases in section 6.
- Tracking issue: [#22670](https://github.com/kirodotdev/KiroCrew/issues/22670)
  (`tier:T3`, `parked`).
- Why an RFC: the change adds the dashboard's second route that rewrites a
  repository's working tree, adds a rule that refuses a user action while any
  agent works in the same repository, and records agent prompts in flight at the
  ACP transport. Those are hard to reverse once other code relies on them, and
  the First Principles lane reads the decision from the base branch.
- Measured at `1a7190c711`. Every symbol cited below is on that commit unless
  it names #23102.

## 1. Summary

Let the dashboard switch the project's checked-out branch from a searchable
picker, opened from the branch name in the Git panel header and from the branch
segment of the project chip under the chat box. The switch runs `git switch` in
the project's repository, relies on git's own refusal to overwrite uncommitted
work, and is refused while any agent is working in that repository, naming what
it is waiting for. Nothing is ended, killed or stashed to make a switch possible.

## 2. Motivation

### 2.1 What exists today

- The branch is shown, never changed. `GET /api/project/git`
  (`api_project_git` in `src/kiro_crew/dashboard/file_api/project_dirs.py`)
  reads `.git/HEAD` directly to label the branch; `GET /api/project/git/status`
  and `GET /api/project/git/log` (`api_project_git_status` and
  `api_project_git_log` in `src/kiro_crew/dashboard/file_api/git_panel.py`) are
  read-only. No route under `src/` and no component under `website/src/`
  switches a branch: `git grep` for `git/switch`, `git/branches` and
  `repo_checkout_guard` returns nothing.
- Clicking the branch segment of the composer chip copies the name
  (`CopyBranchButton`, rendered by `website/src/components/ChatInput.tsx`).
- The one route that rewrites a repository today is `POST /api/worktree/create`
  (`api_worktree_create` in `src/kiro_crew/dashboard/handlers/worktree.py`),
  which serializes per repository on `_repo_lock` and runs git through the
  strict-sandbox helper `_run_git`, both in the same module.
- Nothing records which agent work is running in which repository. A chat turn
  starts at `_turn_open` (`src/kiro_crew/dashboard/chat_turn/turn_entry.py`)
  and claims its session in `_turn_admit` (`chat_turn/turn_admission.py`); a
  sub-agent runs from `_run_inner_impl`
  (`src/kiro_crew/subagent_manager/run_turn.py`); workflow steps open their own
  sessions outside chat slots (`src/kiro_crew/workflows/agent_exec.py`,
  `agent_pool.py`); and the ACP request writers (`_send_request` in
  `acp/client_transport.py`; `send_request`, `send_request_for_answer` and
  `_send_and_await` in `acp/runtime_wire.py`) do not know the repository a
  request will act on.

### 2.2 The problem

The issue's case: an agent works in a separate worktree and opens a pull
request; the user later checks that branch out in the main checkout to deploy
it; reopening the repository in Kiro Crew, it is still on that branch, and the
user does not want the agent to see it. The dashboard offers no way back, so
the user leaves for a terminal or asks the agent to run `git switch`, which
spends a turn on a one-word command.

The hard part is not running `git switch`. It is that a checkout rewrites files
under every session working in that repository. A switch that lands while an
agent is mid-turn means the agent read files on one branch and writes them back
on another. The picker can see its own chat's turn, but the same repository can
be worked on at the same time by another chat, a Slack thread linked to an idle
chat, a sub-agent, a workflow step, a cron run, or a turn the user cancelled
whose backend is still running tools.

## 3. Goals

- G1. Switch to a local branch, check out a remote-only branch as a tracking
  branch, or create a branch at the current commit, from one picker.
- G2. Never lose work: uncommitted and untracked changes either carry over (git's
  default) or the switch is refused with the files in the way named. Ignored
  local files the target branch tracks are protected the same way.
- G3. Never switch under a running agent. Refuse while any agent work may still
  change files in the repository, and say what to wait for.
- G4. End nothing to make a switch possible. A refusal is the whole answer; the
  user decides whether to stop the work.
- G5. The project boundary `POST /api/worktree/create` already draws, plus an
  owner-only switch, strict-sandbox git with hooks and fsmonitor off, every
  string redacted, and every switch in the SEL.

## 4. Non-goals

- fetch, pull, push, rebase, merge, delete or rename a branch.
- Switching a project folder that is a subfolder of its repository (the switch
  would rewrite files outside the project the user chose).
- Repositories whose config declares a checkout filter driver (`git lfs` and
  similar). The picker lists them and says why switching is off.
- Stashing, auto-committing or discarding changes on the user's behalf.
- Remote instances and peers.
- Changing what the agent does; an agent can still run `git switch` itself.

## 5. Design

### 5.1 Where the switcher lives

- The branch name in the Git panel header opens the picker.
- The branch segment of the composer's project chip opens the same picker
  upward. While this chat's response is running the segment keeps today's copy
  behaviour and its tooltip says to stop the response with the message box's
  stop button; a switch would be refused at that moment anyway.

The picker is searchable. It lists local branches newest first, then
remote-tracking branches that have no local branch of the same short name, and
offers "create branch" when the query is a valid new name. Each list is capped at
200 rows and says so when cut. Every row carries the commit subject, author and
date; local rows carry ahead/behind counts.

### 5.2 Routes

`GET /api/project/git/branches?path=<dir>` lists. `POST /api/project/git/switch`
with `{path, branch, create?, track?}` switches. Both live in a new
`dashboard/handlers/git_branches.py` and reuse the worktree route's boundary:
`path` must name or sit inside a live chat slot's project, and so must the git
toplevel it resolves to (`repo_root_outside_project`, 403). Listing takes the
dashboard-caller gate; switching is owner-only, as `/api/file-write` is.

The switch runs `git switch --no-recurse-submodules --no-overwrite-ignore` with
`--no-guess` for an existing branch, `--no-track -c` for a new one and
`-c <name> --track refs/remotes/<track>` for a remote one. Names are validated
before git sees them (no leading `-`, no `@{`, no whitespace; an existing name
must resolve under `refs/heads/`). Already-on-branch is a 200 no-op.

Failures are coded so the picker can render localized copy:
`git_switch_dirty` (with up to five redacted `files` and a `files_more` count),
`git_switch_session_busy` (with `busy`, section 5.3), `git_switch_filter_refused`,
`git_branch_exists`, `git_branch_in_worktree`, `git_operation_in_progress`,
`git_branch_not_found`, `invalid_branch`, and `git_switch_failed` carrying git's
redacted first line. git runs with `LC_ALL=C` because the codes are read from
git's English diagnostics.

### 5.3 The busy rule

A switch is refused with 409 `git_switch_session_busy` while any of these may
change files in the repository, matched on the repository root, a folder inside
it, or a folder containing it.

A folder inside the root that belongs to another checkout does not match. A
linked worktree created under the repository (`.worktrees/x`), a nested
repository or a submodule has its own working tree, which `git switch
--no-recurse-submodules` in the root does not rewrite. A folder is in another
checkout when, walking up from it toward the root, a `.git` file or directory
appears before the root is reached; the check reads the file system and runs no
git. The same match decides which work waits on a checkout's reservation. #23102
matches on the path alone (`_shares_tree` in
`dashboard/handlers/git_branches.py`, `_covering_root` in
`repo_checkout_guard.py`), so a session in a worktree nested under the
repository refuses a switch it cannot be affected by; Phase 3 changes that.

The sources are:

1. a dashboard chat turn, matched on every folder the turn works in: the
   project at its entry, the project its session was allocated for and the
   folder that session reports. A project picked mid-turn moves `project` while
   the session keeps its folder, so `project` alone is not enough;
2. a turn a linked channel (a Slack thread on a chat) runs on an idle chat's
   session;
3. a running sub-agent, through its own working folder, its ancestors' or its
   root chat's project;
4. a workflow step's session;
5. any prompt in flight to an agent process working in the repository, recorded
   at the ACP transport from before the request is written until the backend
   answers it or the runtime confirms the process tree exited (section 5.4).

A cron, heartbeat or monitor-wake turn, or an app's turn, has no rule of its
own: it runs on a chat slot, a channel session or a sub-agent, so it is caught
by whichever of rules 1 to 3 applies, and in every case by rule 5.
Work whose folder is not known yet counts as busy in every repository. A turn
is "unplaced" from the moment it starts allocating its session until the session
reports its folder, because the allocator may place it in a member's workspace
or a resumed conversation's stored folder that no `project` value names.

The refusal carries `busy`: the title of the chat or the label of the workflow
run that holds the repository, redacted and clipped to 120 characters, empty for
a sub-agent, a prompt in flight or unplaced work. The picker shows it ("Not
switched: “Fix the login redirect” is still running in this repository. Wait
for it to finish, then switch.").

Busy is checked under the repository lock, and the switch also takes a
reservation in a new registry, `kiro_crew/repo_checkout_guard.py` (below the
dashboard, so the engines that register with it import no dashboard code),
before the check awaits anything. Work that starts during a checkout waits:
`_turn_open` waits at its first await, the turn waits again before it claims a
session, sub-agents wait in a `pre_run_gate` the dashboard installs, and ACP
writers wait before reserving a request id. The reservation is released by the
checkout worker's own completion, so a cancelled HTTP request cannot drop it
while git is still rewriting files.

That wait is bounded by git, not by a timer of its own. Every git call the
switch makes goes through `worktree._run_git`, which kills the call after
`_GIT_TIMEOUT` (120 s) of wall-clock time, and the switch makes a fixed number
of calls (the toplevel probe, the filter-driver check, the name and HEAD checks
and the `git switch` itself). A git that hangs therefore ends in a timeout, the
worker completes, and the reservation is released; the switch answers with an
error and the SEL records it. While anything waits on a reservation it logs a
warning every 120 s naming what waits. A timer on the wait itself is rejected:
releasing the reservation while git may still be writing is the failure the
reservation exists to prevent. Whether that rule also binds the busy records
of section 5.4 is open question 3.

### 5.4 Prompts in flight

Rules 1 to 4 see work through the structures that start it. Rule 5 exists for
what they cannot see: a turn the user cancelled whose backend keeps running
tools, a channel turn with no dashboard slot, and a workflow step after its
caller gave up. Each request that starts agent work (`session/prompt`, a kiro
slash command, a steer, a KAS checkpoint restore) is recorded by
`AcpClient._send_request` and `AcpRuntime`'s three request writers, against the
session's folder. The frame readers end it when its response arrives, including
a response whose `result` is JSON `null`. A teardown that confirms the process
tree exited ends that process's prompts; one that cannot (a root it could not
prove dead, descendants left for the orphan sweep) hands the guard a probe of
the leftovers, which the busy check runs off the loop until they are gone. A
root that exits on its own (an OOM kill) gets the same treatment once the exit
is confirmed.

If the writer is cancelled before any byte reached stdin, the registration is
removed. Once `stdin.write` accepted the frame it stays, because the backend may
still consume it.

### 5.5 Security

- Owner-only switch, project allow-list on both `path` and the resolved
  toplevel, as for `POST /api/worktree/create`.
- git runs through `worktree._run_git`: strict sandbox, argv list,
  `core.hooksPath=/dev/null`, `core.fsmonitor=false`, scrubbed environment.
- A repository whose config declares a `filter.*.{clean,smudge,process}` driver
  is refused, as the Git panel's status route already refuses it (the probe in
  `git_panel.py` mirrors `worktree.py`'s `_checkout_filter`).
  `--no-recurse-submodules` keeps a submodule's own drivers out of reach.
- Every string in both responses is redacted. A branch name the redactor alters
  is listed as not switchable, since it could never be echoed back. The handler
  is registered as a redaction sink in `security_posture_sinks_delivery.py`,
  which makes the implementation a sensitive-path change.
- Every switch, refused or not, is written to the SEL, including one whose HTTP
  request was cancelled while git ran.

## 6. Migration plan

Each phase is one pull request and can be abandoned without the next.

**Phase 1: list branches (read-only).** `GET /api/project/git/branches`, a
read-only branch list opened from the Git panel header (each row shows its
subject, author, date and ahead/behind; clicking a row does nothing yet), and
the redaction-sink row. No new way to change anything.
Exit: the route lists local and remote branches with redaction and caps; a
redactor-altered name is `switchable: false`; a filter-driver repository lists
with `switchBlocked: "filter"`; a path outside every live chat slot's project
is refused with 403 and audited.

**Phase 2: the work registry, with nothing reading it.** The registry in
`kiro_crew/repo_checkout_guard.py`, the chat-turn, sub-agent, linked-channel and
workflow-step registrations, and prompt tracking at the ACP transport. No user
behaviour changes; the cost is bookkeeping on every turn and request.
Exit: tests show a cancelled turn whose backend has not answered stays
recorded; a `result: null` response ends its prompt; a root that dies on its own
ends its prompts once confirmed; a lingering descendant keeps the record until
it exits, across a respawn of the same runtime; workflow tests keep the
expectations they have on main (no session is ended); prompt tracking is
tested for every backend in `BASELINE_SELECTABLE_BACKENDS`
(`src/kiro_crew/agent_sdk/backends.py`), parametrized over that set so a
backend added later is covered without a new test, since every harness writes
through the same ACP transport; and the bookkeeping adds at most 1 ms at p99 to
an ACP request write and to a response read, measured with and without it on
the same host and recorded in the pull request.

**Phase 3: switch from the Git panel.** `POST /api/project/git/switch` and the
busy rule reading Phase 2's registry, the reservation and the waits, SEL
records, localized refusal copy.
Exit: each busy source in section 5.3 refuses with the right `busy` text; a
session working in a linked worktree under the repository neither refuses the
switch nor waits on it; a switch blocks a turn that starts during the checkout
until git exits; a cancelled
switch request still releases the reservation and writes its SEL record; dirty,
filter and invalid-name cases return their codes and change nothing.

**Phase 4: the composer chip.** The chip's branch segment opens the picker;
while the response runs it keeps copying and says how to stop it.
Exit: chip tests for both states; screenshots for the UX lane.

## 7. Backward compatibility

The routes and the registry are additive. The one visible change to existing
behaviour is the chip's branch segment, which opens the picker instead of
copying when the chat is idle (Phase 4). Phase 2 adds work to every ACP request
write and response; its exit criterion bounds that cost.

## 8. Alternatives considered

- **End the sessions working in the repository, then switch.** The design in
  #17628 ended a workflow step's session when the step failed or was cancelled,
  so the repository would read as idle. That loses the conversation of anyone
  who never uses the switcher, and the First Principles review blocked it.
  Refusing loses nothing and leaves the decision with the user.
- **Warn instead of refuse.** A warning the user clicks through still rewrites
  files under a running agent. The refusal is cheap to wait out; the damage is
  not cheap to undo.
- **Stash automatically.** Moves the user's work somewhere they did not ask for,
  and the stash is shared by every worktree of the repository.
- **Tell the user to ask the agent.** Today's answer. It works, costs a turn,
  and races the same way if another session is working in the repository.
- **Open a worktree per branch instead.** Already offered by the follow-up card
  (`POST /api/worktree/create`). It answers "work on another branch", not the
  issue's "put this checkout back on main".
- **Only check the chat's own turn.** The picker already disables itself while
  its own response runs. That misses every other source in section 5.3, and
  those are the ones a user cannot see from the chat they are in.

## 9. Open questions

1. Scope of the first release. The issue asks for local branches only;
   #23102 also offers remote-tracking checkout and create. Should Phases 1 and 3
   ship local-only and add the other two later?
2. Unplaced work counts as busy in every repository. That is safe but can refuse
   a switch in an unrelated repository while a session is still starting. Is a
   refusal that names "a session that has not reported its folder yet" enough,
   or should unplaced work be bounded by a timeout?
3. **Decide before acceptance:** on the Windows client path there is no pid to
   probe after a teardown that could not confirm the tree exited, so as
   implemented in #23102 that process's prompts stay recorded, and switching in
   that repository stays refused, until the gateway restarts. Options:
   (a) keep that, and say in the refusal that a gateway restart clears it;
   (b) hold the record for a bounded time (for example 10 minutes) after the
   unconfirmed teardown, then drop it and write an SEL record, accepting that a
   descendant still writing after that window is not seen;
   (c) keep refusing, but let the owner switch anyway from the refusal, with an
   explicit confirmation and an SEL record, for records left by an unconfirmed
   teardown only.
   This question also settles a conflict in the document. Section 5.3 rejects a
   timer on the reservation's wait, because releasing while git may still write
   is what the reservation prevents; option (b) ends a busy record on elapsed
   time while a descendant may still write. Choosing (b) makes it a stated
   exception to that rule; choosing (a) or (c) extends the rule to every busy
   record. (a) costs a refusal the user cannot end without a restart, (c) costs
   an override path the owner must judge. The document takes no position; the
   choice is the maintainers' at acceptance.
4. Should branches checked out in another worktree be marked disabled in the
   list, as the issue suggests, instead of being refused on click with
   `git_branch_in_worktree`?
5. How much of the busy rule ships. Three sizes:
   (a) all five rules, as proposed;
   (b) rules 1 to 4, without prompt tracking at the ACP transport, accepting
   the gap of a cancelled turn whose backend keeps running tools;
   (c) rules 1 and 3 only, close to an editor's branch picker: refuse while a
   chat turn or a sub-agent works in the repository, and let a switch through
   under a linked-channel turn or a workflow step.
   (b) drops the ACP part of Phase 2 and with it the per-request cost and the
   per-backend tests; (c) also drops the linked-channel and workflow-step
   registrations, roughly halving Phase 2. Each smaller size lets through the
   work the larger one names, and the user cannot see that work from the chat
   they switch from.
6. Should the busy rule be the shared guard for any future route that changes a
   repository (for example `POST /api/worktree/create`), and if so should a
   maintainer own `kiro_crew/repo_checkout_guard.py` as a module of its own
   rather than as part of this feature?
