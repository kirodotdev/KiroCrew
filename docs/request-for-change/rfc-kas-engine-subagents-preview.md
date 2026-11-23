---
title: KAS chats run the engine's own sub-agents behind one default-off Feature Preview
status: draft
author: bolichen97, with a kirocrew agent session
created: 2026-11-22
last-audited: 2026-11-22
audited-at: 47edb00308
doc-pr: null
implementation-prs: [39781]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: KAS engine sub-agents behind a default-off Feature Preview

- Status: draft. Nothing on main follows it. Acceptance requested from a
  maintainer. A maintainer records acceptance on this Status line, in the
  `status` field and in the index row.
- Implementation: draft
  [#39781](https://github.com/kirodotdev/KiroCrew/pull/39781), which stays a
  draft until this design is reviewed. It was read at its pushed head
  `969c78233a` (two commits, `47b9f42e42` then `969c78233a`). Main was read
  at `47edb00308`.
- Three marks say where each statement was checked. **main**: on main at
  `47edb00308`. **head**: on #39781's pushed head `969c78233a`. **next**:
  decided, not on the pushed head, lands with the next update to #39781. A
  **next** statement is a proposal until that update is pushed, and this
  document is re-audited then.
- "kiro-cli v3" means the Kiro CLI on its v3 agent engine, the engine KAS runs
  (`kiro-cli acp --agent-engine v3`, `AGENTS.md`, "What this is").

## Summary

On KAS, a person's dashboard chat on the default agent can run its short
in-turn sub-agents inside the KAS engine instead of starting a full Kiro Crew
session for each one. One switch decides it: "KAS sub-agents" under Settings >
Developer > Feature Previews, off by default. Off, or never set, a chat uses
Kiro Crew's own sub-agents as it does today. On, the bar is parity with
kiro-cli v3 in both directions: an agent file is neither held tighter nor run
looser than kiro-cli v3 runs it, and every place where Kiro Crew still differs
is listed in this document with its reason.

## Motivation

### Current state

Verified on **main**:

1. The default agent fans out with Kiro Crew's own tools. Its contract names
   `spawn_run` and `spawn_sub_agents` as the only sub-agent mechanisms
   (`src/kiro_crew/config/prompt.md`, the `spawn_run` bullet).
2. Each such sub-agent gets its own agent session
   (`src/kiro_crew/docs/subagents.md`, "How It Works"). It starts its agent's
   MCP servers, a dedicated one starts its own process too, and each start is
   admitted by the memory guard (same file, "Memory guard").
3. The KAS engine has its own sub-agent tool, and a person's own agent whose
   file mounts it can already use it. Kiro Crew registers the agents that
   file's `availableAgents` list names (`project_subagent_targets` in
   `src/kiro_crew/acp/kas_subagent_targets.py`), for at most
   `MAX_SUBAGENT_TARGETS = 8` pattern matches; with no such list the engine's
   own roster answers. A target whose limits only Kiro Crew's gate applies is
   not registered (`_GATE_ONLY`, same file).
4. The default agent does not mount that tool: the `tools` list of the
   shipped template (`src/kiro_crew/config/defaults.json`) names neither it
   nor a pattern that covers it. There is no `agent.subagents` key, no
   `kas_subagents` preview and no `kirocrew-helper` agent.
5. A sub-agent the engine runs for such an agent is held to that agent's
   shell and write allowlists and denied commands, and gets none of its grants
   (`_is_native_child_call` and `_denies_only` in
   `src/kiro_crew/acp/session_handle_permissions.py`, merged as
   [#41080](https://github.com/kirodotdev/KiroCrew/pull/41080)).
6. Feature Previews are named switches stored under
   `dashboard.feature_previews` (`FEATURE_PREVIEW_FLAGS` in
   `src/kiro_crew/config/sections_dashboard.py`).

### Problem

In-turn fan-out is the common shape: look up three things, then answer. Each
Kiro Crew child starts its servers, and a dedicated one a process as well, so
a few children take gigabytes and wait on the memory guard, and the chat waits
with them. An engine child runs inside the parent's turn and process and
starts neither.

Using the engine's tool for the default agent raises three questions that need
a recorded answer before code merges: who may be named as an engine sub-agent,
what such a child may do, and how far Kiro Crew follows kiro-cli v3 when the
two read an agent file differently.

## Goals

- One switch, off by default, that a person can find and undo.
- Off or never set: no change for anyone, beyond what Design §3 lists for
  that state.
- On: a file behaves as it does on kiro-cli v3, in both directions, except
  for the listed differences.
- An engine child is never held to less than the agent that started it.

## Non-goals

- Removing anything. `spawn_run`, `spawn_sub_agents`, the sub-agent manager,
  the memory guard and its queue all stay.
- Background work, Crew members, Bots, conductors and their workers, cron,
  workflows, the task runner and channel chats. They keep Kiro Crew's own
  sub-agents under both values.
- Any backend other than KAS.
- Making the preview the default. That is a later decision with its own record.

## Design

### 1. One switch

| | |
|---|---|
| Where | Settings > Developer > Feature Previews, "KAS sub-agents" (**next**) |
| Card | A title, one sentence and the switch (**next**) |
| Stored as | `dashboard.feature_previews.kas_subagents`; only a stored `true` is on (**next**) |
| Default | Off. Never set reads as off (**next**) |
| Read | Once, when a KAS session starts. A running session keeps what it started with (**head**: module docstring of `src/kiro_crew/acp/kas_engine_subagents.py`) |
| Writes | Nothing is written to an agent file (**head**: same docstring) |

The pushed head still carries the earlier shape: a two-value setting
`agent.subagents`, `"engine"` by default (the `subagents` field in
`src/kiro_crew/config/sections_agent.py`), with a row under Settings >
Developer > Configuration (`website/src/pages/overview/KiroCrewCfgTab.tsx`).
On that head the `"crew"` value also refuses every engine sub-agent to every
agent of a KAS session (`without_engine_subagents`). The next update removes
the key, the row and that rule, so there is one switch, its default is off,
and off leaves a person's own agents as they are on main. Section
"Alternatives considered" says why.

### 2. Which chats get the engine's tool

Only a person's dashboard chat on the shipped default agent, on KAS
(**head**: `engine_subagents_on` in `src/kiro_crew/acp/kas_engine_subagents.py`).
The tool is not mounted for a Crew member's chat, a member dispatch, a Crew
panel, a chat with the chat-runtime-sharing preview on (same function) or a
session in a remote workspace (`native_batch` in
`src/kiro_crew/acp/kas_native_batch.py`).

The switch has other readers, all on KAS: the three kiro-cli v3 readings in
the table of §3 and a `spawn_run` child's grants (§5). No other backend reads
it.

### 3. Off, never set, on

"Off" and "never set" are the same state.

| Surface | Off or never set | On |
|---|---|---|
| Default agent's dashboard chat on KAS | `spawn_sub_agents`, as on main | The engine's sub-agent tool, with `kirocrew-helper` (**head**) and up to 8 of the person's own agents (**next**). `spawn_sub_agents` leaves that agent's tool list, and the one contract sentence that named it names the engine's tool (**head**: `SPAWN_SUB_AGENTS_SENTENCE`, `ENGINE_SUB_AGENT_SENTENCE`) |
| `spawn_run` | Unchanged | Unchanged for that chat and every other |
| A person's own agent that mounts the engine's sub-agent tool | As on main, plus the holds below | The same |
| A `spawn_run` child on another agent, on KAS | Held to what every session above it refuses (**next**) | The same hold, and it runs unasked only what its founding chat runs unasked (**next**) |
| An own agent whose `excludedTools` name the sub-agent tool | Starts no engine sub-agent (**head**: `deny_excluded_subagent_tool`) | Read as kiro-cli v3 reads it; the chat says so when it starts (**next**) |
| Three `deniedCommands` forms that kiro-cli v3 reads wider than their text | As on main, plus hold 4 below | Read as kiro-cli v3 reads them; the session-start notice says what each refuses (**next**) |
| A `denyByDefault` shell allowlist beside a shell entry in `allowedTools` | As on main | The allowlist holds, as on kiro-cli v3 (**next**) |

Holds that apply under both values. None lets a call run that main refuses:

1. Kiro Crew's own agents can never be named as an engine sub-agent, by any
   parent (**head**: `deny_own_agents` in `kas_subagent_targets.py`, applied
   to every KAS batch by `native_batch`).
2. An engine child's calls are held to its parent agent's shell and write
   allowlists and denied commands. This one is on **main** already (Current
   state 5); the pushed head carries its own form of it
   (`test/test_kas_native_child_limits.py`), which the next update has to
   reconcile with main's.
3. The parent's `postToolUse` hooks run once for each finished child call,
   with the parent's redaction (**head**: `is_finished_child_call` in
   `src/kiro_crew/acp/kas_child_finished.py`).
4. A `deniedCommands` entry written as a group alternation reaches KAS as one
   deny per branch, up to 64, instead of being dropped (**head**:
   `deny_alternatives` and `MAX_ALTERNATIVES` in
   `src/kiro_crew/acp/kas_deny_alternatives.py`).
5. Stop all also stops a KAS turn that holds an open engine sub-agent card
   (**head**: `_open_engine_subagents` in
   `src/kiro_crew/dashboard/messaging_api/run_control.py`).
6. A `spawn_run` child is held to every session above it (**next**, row above).

The first commit of #39781 also carries fixes that are not part of this design
(an owner check on a skipped spec write, a profile reload that no longer counts
as a move, and which name a tool call keeps for its hooks). Its body lists them.

### 4. Who can be an engine sub-agent

| Agent | Result | Mark |
|---|---|---|
| `kirocrew-helper`, built in memory from its parent's entry | Runs | **head**: `KAS_HELPER_AGENT` and `helper_entry` in `src/kiro_crew/acp/kas_engine_subagent_target.py` |
| The person's own agents, user-level and project-level, first 8 in name order | Run | **next** |
| A person's agent file named `kirocrew-helper` | Never takes the helper's place; that chat keeps Kiro Crew's own sub-agents | **head**: `_HELPER_TAKEN` in `kas_engine_subagents.py` |
| Kiro Crew's own agents | Refused by name, always | **head**: `deny_own_agents` |
| An own agent with its own `preToolUse` hooks, a shell or file-write allowlist, or URL-scoped limits | Kept out and refused by name; `spawn_run` runs it with its limits | **head** for hooks, allowlists and blocked web addresses KAS cannot carry as a host deny (`_gate_only_limits` and `_GATE_ONLY` in `kas_subagent_targets.py`, `gate_refused_blocked` in `kas_spec_denies.py`); **next** for the other URL-scoped limits and the by-name refusal |
| An own agent past the eighth | Refused; `spawn_run` runs it | **next** |

The engine's own tool text lists every agent the engine finds, the refused
ones included, and Kiro Crew cannot shorten that list. The contract sentence
tells the model so (**next**).

### 5. What a child may do

An engine child (inside the turn):

- The helper holds the default agent's file, shell, search and web built-ins
  (`_HELPER_TOOLS`) and its parent's MCP servers, on the same running
  servers, under its parent's rules. What the parent runs unasked, is asked
  about or is denied, the helper is too (**head**:
  `shared_servers` in `kas_engine_subagent_target.py`, and
  `_child_is_the_shipped_helper` in `session_handle_permissions.py`).
- The helper holds none of Kiro Crew's own servers: memory, skills,
  artifacts, spawn, cron, messages (**head**: `own_servers` in
  `kas_engine_subagent_target.py`). An own agent run from the default
  agent's chat holds none either (**next**).
- A child that runs another agent's spec gets its parent's limits and none of
  its parent's grants on the pushed head (**head**:
  `_child_is_the_shipped_helper`). With the next update an own agent run
  this way is governed as kiro-cli v3 governs one: the chat agent's grants
  answer its calls, its own grants answer nothing, and the denies and asks of
  both hold (**next**).
- Each dispatch is one approval card unless the chat is trusted. With
  `hooks.auto_approve_subagent_spawn` on, an engine dispatch still asks
  (**head**: `test/test_kas_engine_subagents_approval.py`).
- A child cannot be stopped on its own. The turn's Stop ends it.

A `spawn_run` child on another agent, on KAS (**next**, all of it):

- Under both values it is refused what every session above it is refused.
- With the switch on it runs unasked only what its founding chat runs unasked.
  The grants in its own file do nothing. Every ask and deny on the chain, its
  own included, still holds.
- With the switch on, a founding agent's grant that names an MCP server
  reaches the child's own server of that name. A grant that names no server
  (a `*`, or an `mcp` allow rule with no `match`) hands the child no MCP grant.
- A chain that cannot be read leaves the child with no grant at all.

### 6. What the page shows (**next**)

- A closed "Sub-agents in this turn" line under the reply, kept after a
  reload: each child's agent, task, how it ended and its tool call titles.
- On an approval card for a child's call: which sub-agent asks, and the task
  that dispatch was given.
- One row per running child in the bar above the composer.
- Stop all, when Kiro Crew's own sub-agents run beside engine children, says
  that it also ends the answer. A chat whose only sub-agents are engine
  children shows no Stop-all control (**head**:
  `website/src/test/SubagentProgressBar.test.tsx`).
- Every model-written string on these surfaces is masked before it is cut to
  its display length, for every reader the redaction rules already cover. A
  cut made first could split a secret so the mask no longer sees it.

### 7. Rulings this document records

The repository owner made these rulings while #39781 was built. They are
restated here so the design review confirms or changes them on the record.

| # | Ruling | Applies |
|---|---|---|
| R1 | One switch under Feature Previews, off by default; the `agent.subagents` setting and its row are removed | Always |
| R2 | The bar for "on" is parity with kiro-cli v3 both ways, with every difference listed | On |
| R3 | A tool exclusion of the sub-agent tool is read as kiro-cli v3 reads it | On |
| R4 | The three `deniedCommands` forms are read as kiro-cli v3 reads them | On |
| R5 | An MCP grant by server name reaches a `spawn_run` child's same-name server, as on kiro-cli v3 | On |
| R6 | A `spawn_run` child is held to what every session above it refuses | On and off |
| R7 | A `spawn_run` child runs on its founding chat's grants | On |

## Differences from kiro-cli v3 with the switch on

These are the differences known at the heads this document read. Each makes
Kiro Crew refuse or ask more than kiro-cli v3, never less.

| # | Case | kiro-cli v3 | Kiro Crew | Why |
|---|---|---|---|---|
| D1 | Kiro Crew's own agents named as a sub-agent | Has no such agents | Refused by name (**head**) | They are built around Kiro Crew's own tools, which a child of these chats does not hold (D2) |
| D2 | Kiro Crew's own tools in a child of the default agent's chat | Has no such tools | None (**head** for the helper, **next** for own agents) | Each Kiro Crew server is launched with the parent session's identity, so nothing could refuse a session-bound tool for the child alone (**head**: module docstring of `kas_engine_subagent_target.py`) |
| D3 | An own agent with `preToolUse` hooks, a shell or file-write allowlist, or URL-scoped limits | Has no such rule | Kept out, refused by name (**head**, **next**; §4) | Kiro Crew applies those limits at its own gate, only to the agent a session runs. A child's request would reach the gate without them |
| D4 | More than 8 own agents | Lists every agent it finds | First 8 in name order; the rest are refused (**next**) | Each registered agent is a whole projection at session start (**main**: the comment on `MAX_SUBAGENT_TARGETS`) |
| D5 | An own agent's MCP server, inside the turn | The starting chat's grant applies | The grant applies only when both files set the server up the same way; any other server's tool is asked for (**next**) | A server of the same name that launches something else is another server |
| D6 | The founding chat's grants on a `spawn_run` child (R7) | The founding session's grants apply | They apply with three holds: a grant of commands or paths only in the founding chat's own directory; a founding agent with its own `preToolUse` hook hands grants only to a child whose spec carries the same hook; a server's `autoApprove` verbs need the same server on both sides (**next**) | Each keeps a child from running unasked what its founding chat is asked about or refused |
| D7 | An engine dispatch with `hooks.auto_approve_subagent_spawn` on | Has no such setting | Still asks, one card per child (**head**) | No new auto-approve path is added for a new kind of child |

D5 and R5 treat a same-name MCP server differently. That is Open question 2.

## Security considerations

- **The switch grants nothing by itself.** It chooses between two governed
  paths. On the pushed head the mount is the only widening: the default
  agent gains the engine's tool and the helper's entry joins the batch. Every
  other change to an entry's rules is a deny, never an allow (the body of
  #39781, "Backwards compatibility"). With the next update a
  child can also run unasked what the chat that started it runs unasked (§5,
  R5, R7). It never runs unasked what that chat is asked about or refused.
- **A child is never wider than its parent.** The helper is built from its
  parent's entry. Any other child gets its parent's limits. Kiro Crew's own
  agents are refused to every engine sub-agent tool, and a child of the
  default agent's chat holds none of Kiro Crew's own tools.
- **Limits that cannot travel keep an agent out.** An agent whose limits only
  Kiro Crew's gate applies is not run inside the engine (D3).
- **Fail closed.** A config that cannot be read mounts nothing
  (**head**: `native_batch`). A chain that cannot be read hands a
  `spawn_run` child no grant (**next**).
- **Where parity widens.** R3 lets an own agent that excluded the sub-agent
  tool start a sub-agent again when the switch is on, because kiro-cli v3
  does. The chat says so at its start. R5 and R7 change what a `spawn_run`
  child runs unasked from its own file's grants to its founding chat's, and
  an own agent run inside the turn is answered by the chat agent's grants in
  the same way (§5). All of these apply only with the switch on, and none
  lifts a deny or an ask.
- **What readers see.** Text an engine child wrote is masked before it is
  cut, on the card, the approval card and the kept record (§6).
- **Audit.** Each rule this design adds to an agent's entry is recorded as a
  security event (**head**: `audit_subagent_policy` in
  `kas_subagent_targets.py`).

## Rollout

1. This document merges as `draft` and a maintainer accepts or amends it.
2. #39781 is updated to the switch in §1 and the rest of the **next** items,
   then leaves draft. This document is re-audited against that head and its
   marks are replaced with citations.
3. The preview ships off. Exit criteria for the update: with the switch off
   or never set, a KAS session's projection equals main's plus what §3 lists
   for that state, pinned by a test; with it on, each row of the differences table has a
   test; the old key is gone from the schema, the settings page and the docs.
4. Making it the default, or removing the preview, is a separate decision.

## Backward compatibility

With the switch off or never set, the only changes are the holds and the
table rows in §3, and none lets a call run that main refuses. They reach existing agent files on
KAS in these ways: a person's own agent that mounts the engine's tool can no
longer name one of Kiro Crew's own agents (hold 1), its engine child's
finished calls fire its `postToolUse` hooks (hold 3; hold 2 is on main
already), a group alternation in `deniedCommands` refuses the commands
it names (hold 4), an agent whose `excludedTools` name the sub-agent tool
starts no engine sub-agent, and a `spawn_run` child on another agent is
refused what the sessions above it are refused (hold 6). The
`agent.subagents` key exists only on
the draft's pushed head, never on main, so removing it migrates nothing.

## Alternatives considered

- **Keep `agent.subagents` with `"engine"` as the default** (the pushed
  head). Rejected by R1: it changes what the default agent does for every KAS
  user before the design is reviewed, and it leaves a second place to look.
- **Follow kiro-cli v3 with no switch.** Rejected: R3 to R5 and R7 change
  what existing agent files do, so they need an opt-in.
- **Keep Kiro Crew's stricter readings when the switch is on.** Rejected by
  R2: a file would then mean two things on the same engine.
- **Let every own agent be an engine sub-agent.** Rejected: an agent whose
  limits only the gate applies would run without them (D3).
- **Give an engine child Kiro Crew's own tools.** Rejected: D1.

## Open questions

1. Does the reviewer confirm R1 to R7 as written? Each can be changed here
   before the update to #39781 is pushed.
2. D5 holds an own agent's MCP server to "set up the same way" inside the
   turn, while R5 hands a grant by server name on the `spawn_run` path.
   Should the two paths agree, and in which direction?
3. Is the differences table complete? The update to #39781 should carry the
   same table in the owning spec, and a difference found later is added to
   both.
4. What evidence moves the preview to on by default?
