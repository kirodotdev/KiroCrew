---
title: Conductor folder scope — an agent opens sessions and folders only inside its own folder
status: draft
author: zezhexu
created: 2026-10-22
last-audited: 2026-10-22
audited-at: 0be5bcd9ea
doc-pr: null
implementation-prs: []
tracking-issues: [22953]
supersedes: []
superseded-by: []
---

# RFC: Conductor folder scope

**Status:** draft. Nothing built. This is the one-pager
[#22953](https://github.com/kirodotdev/KiroCrew/issues/22953) (`tier:T3`) asks
for. It changes what `session_create`, `session_fork`, `session_revive` and
`chat_folder_create` accept from an agent, so the First Principles lane needs
the decision recorded here before the implementing PR opens.

## Summary

### The rule

**An agent creates sessions and folders only inside the folder it sits in.**

| The agent sits in… | It may create in… | It may NOT create in… |
|---|---|---|
| folder `F` | `F` and any subfolder of `F` | the top level; any other folder; a folder it created earlier outside `F` |
| no folder (the top level) | the top level | inside any folder |

"Agent" means any session that runs on the person's own memory store: a
conductor, the team lead, an ordinary agent tab. "Create" means
`session_create`, `session_fork`, `session_revive` and `chat_folder_create`.
The person, acting through the browser, is not affected. Apps and crew members
keep the fence they already have.

### The problem it solves

Today those agents can create **anywhere** in the person's sidebar tree. The
folder tree judges them as the person, so no fence applies. That causes three
things the person sees:

1. **Work lands in the wrong folder.** A conductor in `Ops` that mistypes a path,
   or reads a stale `[FOLDER]` line after the person moved it, puts its workers
   in `Finance/Q4` or another folder the person organises by hand.
2. **Empty folders pile up.** A conductor can create goal folders under any of
   the person's folders and at the top level. #13449 cleans them up after the
   fact; nothing stops them being made.
3. **A conductor's reach is not its folder.** The folder-conductor direction
   (#22500, #17060) treats a folder as the conductor's boundary. Without this
   rule a conductor grows past that boundary by creating a worker outside it.
   This RFC closes the create path only. Moving a session or itself
   elsewhere afterwards (`chat_folder_move_session`, `chat_folder_file_self`)
   still crosses the boundary; that is Open question 2, and the boundary is
   not whole until it is answered.

The conductor skills already tell the agent to stay inside its folder. A prompt
rule is not enforced, so it fails the first time the agent gets the path wrong.
This RFC moves the rule into the gateway.

## Motivation

### Current state (verified at `0be5bcd9ea`)

Who a folder write is judged as:

| Caller | `folder_principal` (`dashboard/token_auth_callers.py`) | May create a folder under… |
|---|---|---|
| person, browser | `""` | anywhere |
| App Kit agent | app name | top level; its own folders; its home folder |
| crew member | `member:<store>` | top level; its own folders; its home folder |
| person's agent (conductor, team lead, default agent) | `""` | **anywhere** |

The app/member fence is one test in `create_folder_record`
(`dashboard/chat_folders_records.py`, the `forbidden_parent` branch of
`_append`): a nest is refused when `request_app` is set, the parent's
`owner_app` differs, and the parent is not the caller's home folder
(`_slot_home_folder_id(home_slot)`, from `caller_home_slot`). With `request_app == ""` the condition is false and
the write lands.

What already tells the person's agent apart from the person:

- `agent_creator_key` (`dashboard/chat_folders_records.py`) returns the live
  `dashboard:` slot key for an internal (MCP) create and `""` for the browser.
  `api_chat_folder_create` (`dashboard/chat_folders_routes.py`) stamps it on
  the new folder as `created_by_session` (`CREATED_BY_SESSION`) and
  `claim_parent_for_person` clears the mark when the person nests under it.
- `chat_folder_delete ?if_empty=1` (the `if_empty` arm of `api_chat_folder_delete`)
  already uses that mark as a reach rule: an agent may delete only a folder its
  own session created and the person has not touched since
  ([#13449](https://github.com/kirodotdev/KiroCrew/issues/13449)).

How a new session reaches a folder:

- MCP `session_create` / `session_fork` / `session_revive` share
  `_resolve_folder_for_new_session` (`mcp_dashboard_session_runs.py`). It
  passes `_refuse_tree_shaping_if_unverifiable` (`mcp_dashboard_caller_scope.py`),
  then `_ensure_chat_folder_path` (`mcp_dashboard_references.py`) creates
  missing segments through `POST /api/chat/folders` under the verified key, with
  a `preflight` that dry-runs the session create before the first segment is made.
- The endpoint `create_session`
  (`dashboard/session_control_parts/session_create.py`) checks only that
  `folder_id` exists (`folder_not_found`) and re-checks its project under the
  lock (`folder_target_changed`). It never compares the target with
  the caller's own folder.

What the agents are told to do:

- `goal-conductor/SKILL.md`, "File yourself in the goal's folder": file yourself in the goal's folder in your
  first turn; if the person already put you in a folder, that folder IS the goal's
  folder; create workers under `<goal folder>/<agent>`.
- The team-lead prompt (`agent_parts/spec_prompts.py`) gives the same
  `<goal folder>/<agent>` shape.

So the shape the prompts ask for is "stay inside your folder". Nothing enforces
it.

### Problems

1. **A path typo or a stale `[FOLDER]` line lands work in the person's other
   folders.** A conductor filed in `Ops/wave2` that calls
   `session_create(folder="Finance/Q4")` gets a worker in `Finance/Q4`. A
   conductor whose `[FOLDER]` line predates a move creates `<old path>/<agent>`
   under the old parent.
2. **Empty goal folders scatter.** `chat_folder_create(parent=<any id>)` works
   under any of the person's folders. #13449 cleans up after the fact; nothing
   stops the scatter.
3. **The two folder-conductor directions assume a fence that does not exist.**
   rfc-folder-conductor ([#22500](https://github.com/kirodotdev/KiroCrew/pull/22500),
   not yet on main) G3 ("a conductor's reach is the folder's membership") and
   [#17060](https://github.com/kirodotdev/KiroCrew/issues/17060) (scope peer
   messaging to the folder's project) both bound what a conductor can *reach*.
   The *create* side is unbounded, so a conductor can grow its reach by placing a
   worker outside its folder.
4. **A prompt rule is the only control, and prompt rules do not hold.**
   [rfc-conductor-context-cap](rfc-conductor-context-cap.md) records the same
   finding for the context budget: a rule the gateway does not enforce is a rule
   that fails on the first long run.

## Goals

- G1. An agent on the person's store can file a new session, or create a folder,
  only inside the folder it is filed in (that folder or a subfolder). A folder
  it created earlier somewhere else is not in reach.
- G2. An agent filed in a folder cannot create at the top level. An unfiled
  agent can: it creates the goal's folder there, files itself in, and works
  beneath it.
- G3. The rule is one predicate, decided under the folder-store lock, shared by
  every create path (`session_create`, `session_fork`, `session_revive`,
  `chat_folder_create`).
- G4. A refused create leaves nothing behind: no session, no path segment.
- G5. The person's own actions in the browser are unchanged. Apps and crew
  members keep their fence as it is.

## Non-goals

- Move verbs: `chat_folder_move`, `chat_folder_move_session`,
  `chat_folder_file_self`. An agent entering its home folder is a move, and the
  goal-conductor skill's Round 0 depends on it. Scoping moves is a separate
  decision.
- Target verbs (`session_send`, `session_read_message`, `session_stop`,
  `session_close`): [#17060](https://github.com/kirodotdev/KiroCrew/issues/17060).
- What a conductor can *see*: `chat_folder_tree` and `GET /api/chat/folders`
  keep returning the whole tree to a person-store agent. A narrower view is
  rfc-folder-conductor's call.
- Channel and cron callers: `create_session` already refuses them
  (`unattended_caller`).
- Whether a conductor is a kind of agent. This document keys on *who created
  the call*, never on an agent-name list (see Alternatives).

## Design

### 1. The reach of an agent caller

For a call that creates a session or a folder, the **agent caller** is the live
dashboard slot `agent_creator_key` names: an internal request carrying a
`dashboard:` session key that is open. A browser request has no agent caller.

An agent caller's **reach** is a set of folder ids, computed from the live tree
under the folder-store lock at the moment the write is decided:

```
home  = caller slot's folder_id                 (may be "": unfiled)
reach = descendants_inclusive(home)             when home != ""
reach = { top level }                           when home == ""
```

`descendants_inclusive` walks `parent_id` with the same cycle guard
`_resolve_folder_steering_dirs` uses.

A create is **in scope** when its parent (for a folder) or its target folder
(for a session) is in `reach`. Anything else is refused. Two consequences:

- An agent filed in a folder never creates at the top level.
- An unfiled agent creates at the top level only, never inside an existing
  folder. To work beneath a folder it just made, it files itself there first
  (`chat_folder_file_self`, a move, outside this rule) — the order the
  goal-conductor skill's Round 0 already follows.

Who created a folder plays no part: a folder the agent created earlier
somewhere else is out of reach once the agent sits in a different folder.
`created_by_session` keeps its one job, the `if_empty` delete rule.

The folder a new session lands in is checked whether it was named or
inherited:

- `session_create` with no `folder` files the session in the caller's own
  folder, not at the top level. An unfiled caller's child stays at the top
  level, as today.
- `session_fork` and `session_revive` with no `folder` inherit as today; an
  inherited folder outside reach is refused, and the caller names one inside.

Worked cases:

| Caller is filed in | Call | Verdict |
|---|---|---|
| — (unfiled) | `chat_folder_create("Flaky tests")` | allowed, top level |
| — | `chat_folder_create("x", parent="Flaky tests")` | refused: file yourself in `Flaky tests` first |
| — | `session_create()` | allowed, top level |
| — | `session_create(folder="Finance/Q4")` | refused |
| `Ops` | `session_create(folder="Ops/kirocrew-worker")` | allowed |
| `Ops` | `session_create()` | allowed, filed in `Ops` |
| `Ops` | `chat_folder_create("wave3", parent="Ops")` | allowed |
| `Ops` | `chat_folder_create("Ops2")` at top level | refused |
| `Ops` | `session_create(folder="Finance/Q4")` | refused |
| `Ops`, earlier created `Goal` at top level | `session_create(folder="Goal/x")` | refused: `Goal` is not under `Ops` |
| `Ops/wave2`, moved by the person to `Archive/wave2` mid-call | `session_create(folder="Ops/wave2/x")` | refused: reach is read under the lock after the move |

### 2. Where the predicate runs

One function, `agent_folder_reach(folders, caller_slot)` in
`dashboard/chat_folders_records.py` beside `caller_home_slot`, returns the
reach set. Four writers call it, each under the folder-store lock. Fork and
revive do NOT route through `create_session`, so each one is its own call site:

- `create_folder_record._append`: after the existing app/member nest test, when
  `request_app == ""` and the request names an agent caller
  (`agent_creator_key` non-empty), refuse with a new `"out_of_scope"` result
  when `parent_id` (`""` for the top level) is not in reach.
- `create_session` (`session_control_parts/session_create.py`): in the late
  folder re-check that already refuses `folder_target_changed`, test the
  landing folder against the caller slot's reach.
- `fork_session` (`session_control_parts/session_tree.py`): beside both of its
  `folder_not_found` checks (the pre-allocation one and the late re-check).
- `revive_session` (`session_control_parts/session_revive.py`): beside its
  `folder_not_found` check.

In each of the three session writers the test runs on the **resolved landing
folder**, whether the caller named it or it was inherited from the source
session, and the top level counts as a landing folder. It does not sit inside
the existing `if folder_id:` arm, which only runs for a named folder.

### 2a. No stray path segments (G4)

`session_create(folder="A/B/C")` creates missing segments one at a time through
`_ensure_chat_folder_path`, and its `preflight` dry-runs the session create only
against the deepest folder that **already exists**. That is not enough: an
unfiled agent asking for `Goal/x` when `Goal` does not exist passes the probe
(the top level is in its reach), the walk creates `Goal`, and the next segment
is refused, leaving an empty `Goal` behind.

So the reach test runs on the **whole path** before the walk creates anything:

- The MCP layer resolves the path against the tree it just read: the deepest
  existing folder `D`, plus the list of missing segments.
- It asks the gateway one question, as part of the existing dry run: may this
  caller create a folder under `D`, and is every folder the walk would create
  inside the caller's reach? A dry-run `create_session` with
  `folder_id = D` and `new_segments = n` answers it. For a filed caller this is
  "`D` is in reach". For an unfiled caller it is "`D` is the top level and
  `n == 0`": an unfiled agent can create one top-level folder with
  `chat_folder_create`, but never a nested path through `session_create`,
  because it must file itself into that folder first.
- A refused probe ends the walk with nothing made, as the preflight does today.

`chat_folder_create(parent=P)` creates one folder, so the single
`_append` test above already covers it. The same pre-walk test applies when its
`parent` is a path with missing segments.

A race (the person moves `D` out of reach between the probe and a segment
create) can still strand at most the segments created before the move. The
locked test in `_append` refuses the next one. That is the residue today's
preflight already accepts.

### 3. The refusal

- Folder API: `403`, body `{"error": ..., "code": "folder_out_of_scope"}`, plus
  a denied SEL entry (`operation=chat.folder_create`, `source=folder_scope`),
  the shape `FolderOwnershipError` already has. A new `FolderScopeError`
  subclass of `FolderCreateError` carries it so the MCP path does not
  string-match.
- Session control: `SessionControlError(code="folder_out_of_scope")`.
- The message names the home folder's path, so the agent can correct the call:
  `cannot file outside your folder "Ops" — pass a folder under it, or omit folder`.
  It never suggests moving the agent into the target folder: that is the
  move-verb gap in the Non-goals, and a refusal must not teach it.
  An unfiled agent is told: `you are not filed in a folder — you may create one
  folder at the top level; file yourself into it with chat_folder_file_self
  before creating beneath it`. This is the goal-conductor skill's own Round 0,
  where the agent files itself into a goal folder it just created.

### 4. What the agents are told

- The four tool descriptions in `mcp_dashboard.py` gain one sentence: *You may
  create only inside the folder you are filed in, or its subfolders; an unfiled
  session creates at the top level only.*
- `goal-conductor/SKILL.md` and the team-lead prompt already describe this
  shape; they gain the refusal code and the one-line recovery.
- The folder-scope section in `docs/system-specs/modules/session-control.md`
  (beside "The fence propagates to what a fenced caller creates") and the
  folder-tree write rules in `docs/system-specs/modules/learn-cron-dashboard.md`
  are updated in the implementing PR.

## Migration plan

One phase, one PR.

**Phase 1 — the fence.** `agent_folder_reach`, the four writer tests, the
whole-path pre-walk test, the two refusal codes, the tool-description sentences, the skill/prompt lines, the spec
updates, and the pipeline conductor's self-filing: `pipeline-conductor/SKILL.md`
gains a `chat_folder_file_self` into the pipeline folder right after its
`chat_folder_create` and before the first claim, and
`_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS` in `spec_grants.py` gains
`@kirocrew-dashboard/chat_folder_file_self`. The security conductor's tuple is
built from the pipeline one plus that same grant, so it drops its own addition
and inherits the grant instead of listing it twice.

Exit criteria, each a test:

- An unfiled person-store agent creates a top-level folder `G`: succeeds. A
  folder under `G` is refused; after `chat_folder_file_self` into `G`, a
  session in `G/x` succeeds.
- A person-store agent filed in `A` creates a session in `A/x` and a folder under
  `A`: both succeed; a session in `B/x` and a folder at the top level are both
  refused `folder_out_of_scope`, and `GET /api/chat/folders` shows no new row
  after the refusals.
- A person-store agent filed in `A` that earlier created top-level `G` is
  refused a session in `G/x`.
- `session_create` with no `folder` from an agent filed in `A` lands in `A`.
- The person's browser create under any parent succeeds as before.
- An App Kit agent and a crew member keep today's verdicts on the existing
  ownership tests (`test_chat_folder_*`, `test_session_control_*`): no change.
- A folder moved out of the caller's subtree between the preflight and the
  locked write is refused `folder_out_of_scope`, not created.
- An unfiled agent's `session_create(folder="Goal/x")` with no `Goal` is
  refused, and `GET /api/chat/folders` shows no `Goal` row afterwards.
- A filed agent's `session_create(folder="A/new1/new2")` with `A` in reach
  creates both segments; with `A` out of reach it creates none.
- `session_fork` and `session_revive` with `folder=` outside reach are refused.
  Without `folder=`, an inherited folder inside reach succeeds and one outside
  reach is refused, each driven through `fork_session` / `revive_session`
  themselves, not through `create_session`.
- The person moves the source session's folder out of the caller's reach after
  the source was created; a fork or revive of it with no `folder=` is refused.
- An unfiled pipeline conductor following its skill creates the pipeline
  folder, files itself into it, and dispatches a worker `session_create` filed
  into that folder: the dispatch succeeds. The same dispatch without the
  self-filing step is refused `folder_out_of_scope`.

## Backward compatibility

**Breaking for two caller shapes:** a person-store agent that today files into a
folder outside its own, and an unfiled agent that files sessions into a folder it
created without filing itself there first (the pipeline conductor). Writer sweep
at `0be5bcd9ea`, the pipeline conductor re-checked at `d42544f622`:

- `goal-conductor/SKILL.md` and the team-lead prompt: file into
  `<goal folder>/<agent>` where `<goal folder>` is the caller's own folder — in
  scope.
- `pipeline-conductor/SKILL.md`: **breaking.** An unfiled conductor
  `chat_folder_create`s the pipeline folder at the top level (in scope), then
  `session_create`s each worker "filed into the pipeline folder". That folder is
  not in an unfiled caller's reach, so every worker dispatch would be refused
  `folder_out_of_scope`. The conductor never files itself, and it does not hold
  `chat_folder_file_self` (`spec_grants.py`: "The pipeline conductor's procedure
  does not file itself yet"), so it cannot take the recovery §3 tells it to.
  Phase 1 therefore adds a self-filing step to the skill and the grant to
  `_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS`.
- `kirocrew-dashboard-author`, `kirocrew-research`, `kirocrew-knowledge`: no
  `folder=` use in their prompts.
- A person who asks their own chat (team lead) "open a session in Finance/Q4"
  while that chat sits in `Ops`: refused. The recovery is the message in §3.
  This is the one refusal a person may notice; the Open question below asks
  whether it needs a release note.
- A filed agent's `session_create` with no `folder` now lands in the agent's
  folder instead of the top level. Every shipped prompt already names a
  folder, so none of them sees the difference.

Nothing on disk changes shape, and no stored mark is read: reach comes from the
caller slot's `folder_id` and the tree alone.

## Security considerations

- Reach is computed from the live tree under the store lock, never from the
  caller's `[FOLDER]` line or a body value: the same posture as the app/member
  nest test and the `folder_target_changed` re-check.
- Keying on `agent_creator_key` (an internal request naming a live slot) rather
  than on an agent name means a renamed or new agent template cannot escape the
  fence, and the browser cannot be caught by it (it never carries
  `X-Internal-Secret`).
- The fence narrows what an agent can do; it grants nothing. Apps and members are
  not widened: their existing test runs first and still refuses, and the new test
  applies only when `request_app == ""`.
- Reach depends only on where the agent sits now, so there is no mark an agent
  could collect to widen it later.

## Alternatives considered

- **Key on agent name (a conductor list).** `kirocrew-conductor`,
  `kirocrew-team-lead`, `kirocrew-pipeline-conductor`, … is a list that rots,
  and the team lead is the default agent for the person's own tabs, so "is a
  conductor" is not a property of the template. `agent_creator_key` already
  separates agent calls from browser calls with no list.
- **A `PreToolUse` hook on the conductor agent.** Cannot see which slot is
  calling in a re-keyed warm-pool process, and cannot read the tree under the
  store lock. Same reasoning as #17060.
- **Make the conductor a crew member to inherit the member fence.** Changes its
  memory boundary (a member has a private store). Far larger than the folder
  rule.
- **A config switch (`agent.folder_scope`).** rfc-folder-conductor makes the
  folder the conductor's identity; an opt-out contradicts the model. Proposed on
  by default with no switch.
- **Also reach folders the agent created (`created_by_session`).** Lets an
  unfiled conductor open a session in its new goal folder before filing itself.
  Rejected by the product owner: a conductor's place is the one folder it sits
  in, and a folder it made elsewhere earlier must not stay a back door. The
  unfiled start still works by filing first, which the skill already does.
- **Refuse `session_create` with no `folder` from a filed agent.** Safer-looking
  but breaks every caller that omits it; filing the child in the caller's own
  folder keeps it inside reach with no error.

## Open questions

1. Does the one person-visible change (asking your own chat to open a session in
   another folder is refused) need a release note, or is the recovery message
   enough? Proposed: the message is enough; the person can create the session
   from the sidebar.
2. Should the move verbs get the same fence? Today a filed agent can still
   create inside its folder and then `chat_folder_move_session` the session
   out, or `chat_folder_file_self` into another folder and create there. This
   RFC does not close that, so Problem 3 is only half solved by it. Scoping
   moves is a separate decision: `chat_folder_file_self` for an unfiled agent
   is the goal start this design depends on. Noted for rfc-folder-conductor.
