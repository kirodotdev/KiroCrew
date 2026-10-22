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

An agent session that runs on the person's own memory store — a conductor, the
team lead, any ordinary agent tab the person opened — may today file a new
session or create a folder **anywhere** in the person's sidebar tree. Apps and
crew members are fenced to the folders they own plus the folder their own
session sits in; the person's own agents have no fence at all, because the
folder tree judges them as the person.

This document adds one rule for those agents: **a new session or folder goes
inside the folder the calling agent is filed in, or inside a folder that agent
created itself.** An unfiled agent may still open one top-level folder, which is
how a conductor starts a goal. The person, acting through the browser, is
untouched.

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
  only inside its own folder or a folder it created.
- G2. An unfiled agent can still start a goal: one new top-level folder, then
  everything beneath it.
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
home        = caller slot's folder_id                      (may be "")
own         = { f : f.created_by_session == caller key }
reach       = descendants_inclusive(home) ∪ ⋃ descendants_inclusive(f) for f in own
top_level_ok = home == ""
```

`descendants_inclusive` walks `parent_id` with the same cycle guard
`_resolve_folder_steering_dirs` uses.

A create is **in scope** when its parent (for a folder) or its target folder
(for a session) is in `reach`, or is the top level and `top_level_ok`. Anything
else is refused.

Worked cases:

| Caller is filed in | Call | Verdict |
|---|---|---|
| — (unfiled) | `chat_folder_create("Flaky tests")` | allowed, top level |
| — | `session_create(folder="Flaky tests/kirocrew-worker")` right after | allowed: `Flaky tests` is in `own` |
| — | `session_create(folder="Finance/Q4")` | refused |
| `Ops` | `session_create(folder="Ops/kirocrew-worker")` | allowed |
| `Ops` | `chat_folder_create("wave3", parent="Ops")` | allowed |
| `Ops` | `chat_folder_create("Ops2")` at top level | refused |
| `Ops` | `session_create(folder="Finance/Q4")` | refused |
| `Ops/wave2`, moved by the person to `Archive/wave2` mid-call | `session_create(folder="Ops/wave2/x")` | refused: reach is read under the lock after the move |

### 2. Where the predicate runs

One function, `agent_folder_reach(folders, caller_key, caller_slot)` in
`dashboard/chat_folders_records.py` beside `caller_home_slot`, returns
`(reach, top_level_ok)`. Two writers call it, both already under the lock:

- `create_folder_record._append`: after the existing app/member nest test, when
  `request_app == ""` and `created_by_session != ""`, refuse with a new
  `"out_of_scope"` result when `parent_id` is not in reach (or is the top level
  and `top_level_ok` is false).
- `create_session`'s late folder re-check (the `folder_target_changed` block), which
  already runs under `state.read_folders` to refuse `folder_target_changed`:
  add the same test on the caller's slot. `session_fork` and `session_revive`
  pass through the same folder gate and get it for free.

The MCP layer does not decide anything new. `_resolve_folder_for_new_session`'s
`preflight` already dry-runs `create_session` before the first path segment is
made, so a session create that would be refused is refused with no folder
created (G4). `_ensure_chat_folder_path` creates segments through the folder
endpoint, which refuses the first out-of-scope parent before any segment exists.

### 3. The refusal

- Folder API: `403`, body `{"error": ..., "code": "folder_out_of_scope"}`, plus
  a denied SEL entry (`operation=chat.folder_create`, `source=folder_scope`),
  the shape `FolderOwnershipError` already has. A new `FolderScopeError`
  subclass of `FolderCreateError` carries it so the MCP path does not
  string-match.
- Session control: `SessionControlError(code="folder_out_of_scope")`.
- The message names the home folder's path, so the agent can correct the call:
  `cannot file outside your folder "Ops" — pass a folder under it, or omit folder`.
  An unfiled agent is told: `you are not filed in a folder — create one at the
  top level first, or file yourself with chat_folder_file_self`.

### 4. What the agents are told

- The four tool descriptions in `mcp_dashboard.py` gain one sentence: *You may
  file into your own folder, its subfolders, or a folder you created; an unfiled
  session may create one top-level folder.*
- `goal-conductor/SKILL.md` and the team-lead prompt already describe this
  shape; they gain the refusal code and the one-line recovery.
- The folder-scope section in `docs/system-specs/modules/session-control.md`
  (beside "The fence propagates to what a fenced caller creates") and the
  folder-tree write rules in `docs/system-specs/modules/learn-cron-dashboard.md`
  are updated in the implementing PR.

## Migration plan

One phase, one PR.

**Phase 1 — the fence.** `agent_folder_reach`, the two writer tests, the two
refusal codes, the tool-description sentences, the skill/prompt lines, the spec
updates.

Exit criteria, each a test:

- An unfiled person-store agent creates a top-level folder, then a session in
  `<that folder>/x`: both succeed.
- A person-store agent filed in `A` creates a session in `A/x` and a folder under
  `A`: both succeed; a session in `B/x` and a folder at the top level are both
  refused `folder_out_of_scope`, and `GET /api/chat/folders` shows no new row
  after the refusals.
- The person's browser create under any parent succeeds as before.
- An App Kit agent and a crew member keep today's verdicts on the existing
  ownership tests (`test_chat_folder_*`, `test_session_control_*`): no change.
- A folder moved out of the caller's subtree between the preflight and the
  locked write is refused `folder_out_of_scope`, not created.
- `session_fork` and `session_revive` with `folder=` outside reach are refused;
  without `folder=` they inherit as today.

## Backward compatibility

**Breaking for one caller shape:** a person-store agent that today files into a
folder outside its own. Writer sweep at `0be5bcd9ea`:

- `goal-conductor/SKILL.md` and the team-lead prompt: file into
  `<goal folder>/<agent>` where `<goal folder>` is the caller's own folder — in
  scope.
- `pipeline-conductor/SKILL.md`: `chat_folder_create` the pipeline folder at
  the top level from an unfiled conductor — in scope.
- `kirocrew-dashboard-author`, `kirocrew-research`, `kirocrew-knowledge`: no
  `folder=` use in their prompts.
- A person who asks their own chat (team lead) "open a session in Finance/Q4"
  while that chat sits in `Ops`: refused. The recovery is the message in §3.
  This is the one behaviour a person may notice; the Open question below asks
  whether it needs a release note.

Nothing on disk changes shape. `created_by_session` already exists on folders
agents created since #13449; folders created before it carry no mark, so an
agent reaches them only through its home folder. That is the conservative
direction.

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
- `created_by_session` is cleared when the person claims a folder
  (`claim_parent_for_person`). After that the agent reaches it only if filed in
  it, which is the intended direction: the person took it over.

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
- **Home folder only, no `own` set.** Breaks the unfiled start: a conductor
  creates `Flaky tests`, and until it files itself it could not open a session
  there. The `own` set is the mark #13449 already stamps.

## Open questions

1. Does the one person-visible change (asking your own chat to open a session in
   another folder is refused) need a release note, or is the recovery message
   enough? Proposed: the message is enough; the person can create the session
   from the sidebar.
2. Should `chat_folder_file_self` be scoped later so a filed agent cannot move
   itself out of its folder? Out of scope here; noted for rfc-folder-conductor.
