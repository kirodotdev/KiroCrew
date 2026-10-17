---
title: Mate-owned agent config — a copied custom agent in its own folder
status: draft
author: gjjnn
created: 2026-10-16
last-audited: 2026-10-17
audited-at: 8892889b91
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Mate-owned agent config — a copied custom agent in its own folder

- Status: draft — a proposal, not a description of what the code does today.
  "Exists today" claims cite symbols verified against the working tree at
  `8892889b91`; re-verify at implementation time.
- Author: gjjnn
- Related: [rfc-crewmates-launch.md](rfc-crewmates-launch.md) (the product
  vocabulary — "crewmate", "custom agent", "built from" — kept separate from the
  code keys this RFC uses).
- Grounding: the design below was exercised by an implementation spike against a
  live pod. Where the spike corrected an earlier reading, the correction is
  called out inline (search "spike"). The spike proved the resolution mechanism
  and the create/edit paths live; the one path still awaiting live confirmation
  is the in-place capability SAVE (§5.4, built and offline-proven, pending a pod
  it can run against).
- Amended 2026-10-17 after the team's conductor/worker architecture meeting.
  The amendment settles what each of the two agents IS (§5.0), makes the file's
  `prompt` load-bearing for the worker (§5.1), adds the conductor-raised,
  card-confirmed capability change (§5.4), and records that step 1 is a
  behaviour change rather than a sourcing move (§6). Where the amendment
  changed an earlier reading, the change is called out inline (search
  "2026-10-17"). The open step-1 pull request
  [#21463](https://github.com/kirodotdev/KiroCrew/pull/21463) predates this
  amendment: it names a `kirocrew-mate-conductor` spec but derives it from the
  default template, so its shape is the one §6 step 1 now says not to use.

## 1. Summary

A crewmate's binding record (`agents.<name>` in `config.json`) carries a
`kiro_agent` field that *points at* a shared custom agent in `~/.kiro/agents/`.
Everything that decides how the crewmate behaves — prompt, tools, MCP servers,
skills — lives on that shared file, so editing "the crewmate's tools" edits the
shared agent and the change reaches every crewmate pointing at it.

**A mate is two source-defined agents — a conductor and a worker — plus one
user-editable config file that extends the worker.** This is a restructure of
the mates feature that already exists, not a new concept: the conductor and
worker are how today's mate behaviour is re-expressed, with its definition moved
into source code. The conductor the user chats with has its system prompt,
tools, MCP servers and skills defined 100% in Crew's source code, and it **only
plans and dispatches** — it never does the work itself (§5.0). It delegates
tasks to a worker whose system prompt, tools, MCP servers and skills are *also*
defined 100% in source — that full set is the worker's base. Neither base is
user-editable. The one thing a user can change is the worker's **additional**
prompt text, MCP servers and skills, and those live in a per-mate file Crew
owns:

```
~/.kiro/mates/agents/<id>.json
```

To the user this still reads as "edit what my mate can do." Under the hood the
edit targets the delegated **worker**, and it is *additive*: the worker runs its
source-defined base plus whatever `prompt`, `mcpServers` and `skills` this file
adds.

This RFC gives each crewmate that file, written at creation time into a new
folder on purpose. It is a custom-agent file in the **exact format already
written to `~/.kiro/agents/`** — no new schema. In this design its `prompt`,
`mcpServers` and `skills` fields are load-bearing (they extend the worker);
every other field (`tools`, `model`, `permissions`) is written with a
default/placeholder value, reserved so a future design may use it without a
schema change. (2026-10-17: `prompt` joined the load-bearing set; the
pre-amendment text held only `mcpServers` and `skills` there.) The folder
relocation is the mechanism: it gives the mate a config Crew owns and can edit
directly, without touching any source and without adding the file to the
user-level agent catalog the session agent picker reads.

Crewmate-specific data (identity, avatar, memory store, workspace, model) does
**not** move into this file; it stays on the binding record, where it already
lives.

## 2. Motivation

### Current state

- A custom agent is a Kiro-wide config file (`prompt`, `tools`, `mcpServers`,
  `skills`, `model`) under the user-level agents directory. That directory is
  single-valued and is both the read scope and the write target:
  `kiro_agents_dir()` in `config/paths.py` documents it as the write target as
  well as the user-level read scope, and `list_agents()`
  (`agent_discovery.py`) scans it (plus a project-local read-only scope) for the
  agents selectable via `session/set_mode`.
- A crewmate's binding is a `KiroCrewAgentConfig` record (`config/sections.py`).
  Its crewmate-specific fields — `member_id`, `kiro_agent`, `workspace`,
  `memory_store`, `model`, `avatar`, `description`, `triggers`, `session_color`,
  `starred`, `source` — already live on the binding. `kiro_agent` is the pointer
  to a custom agent (its docstring: "Kiro agent name (modeId for
  session/set_mode)").
- Crew projects a config into KAS's `_meta.kiro.customAgents` array on
  `session/new`; `set_mode` then activates it. Crew does not pass a file path —
  it hands over the agent definition on the wire.
- A crewmate's chat runs the Kiro-wide default template: the first crewmate's
  implicit template is `kirocrew` (`first_crewmate._ORDINARY_TEMPLATE`), the
  New Crewmate dialog's default is `kirocrew`
  (`change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`), and `kirocrew.json` is the
  full default agent — every configured MCP server, every skill, `execute_bash`.
  So the agent the user chats with is also the agent that does the work, with
  the whole tool surface loaded into its context. The work it hands to
  `spawn_run` without naming an agent runs the member's OWN template again
  (`subagent_parts/agent_policy._inherited_member_agent`,
  `subagent_manager/run.py`: `info.agent or execution.template_id`) — for a
  `kirocrew`-bound crewmate, the full default once more. `kirocrew-worker`, the
  disk mirror of that default (`agent_materialization/worker_agent.py`), is
  what a plain session's unnamed spawn runs.
- A copy mechanism already exists, but it is **copy-on-write and lazy**:
  `api_agent_fork` (`dashboard/agent_admin/fork_publish.py`) forks a private
  copy on the *first edit*, names it `crew-<hex>`, writes it into the user-level
  agents directory, records `forked_from`/`private_to` lineage in an
  `agent_state` sidecar (the spec itself can't carry lineage — kiro-cli rejects
  unknown fields and drops the whole agent), hides it from the shared catalog,
  and rebinds. A reset re-points to the origin and deletes the copy. This fork
  then runs an **inheritance + review lifecycle** — the copy stays a child that
  tracks its parent — described in §9a.

### Problems

1. **Edits leak.** Several crewmates on one shared agent all see any edit to it.
2. **The edit path is confusing by construction.** "Edit the crewmate's skills"
   mechanically means editing the shared agent it depends on, so a crewmate-config
   editor can't be offered without that editor silently changing other crewmates.
3. **The lazy copy carries a live link back.** The fork's lineage plus
   reset-to-origin keeps a crewmate's config tethered to a shared parent until a
   reset severs it — the opposite of ownership.
4. **The copy lands in the shared catalog.** Because the fork writes into the
   same single-valued directory the picker reads, the private copy must then be
   *hidden* from the picker by a lineage filter.
5. **The chat agent is busy doing the work** (2026-10-17). Because the agent
   the user talks to is the one running the task, a new message arriving
   mid-task lands on a busy turn. The steer path's outcome is not guaranteed:
   the agent may try to serve both, prefer the new one, or drop work, depending
   on whether the two are related. A user cannot rely on the mate answering.
6. **Every user MCP server lands in the chat agent's context** (2026-10-17). A
   user with many servers pays for all of them on every conversational turn,
   whether or not the turn needs any.

## 3. Goals

- A mate owns its editable config outright: adding prompt text, an MCP server
  or a skill to one mate's file affects only that mate's worker, no other mate
  and no source.
- The conductor and the worker's base — system prompt, tools, MCP servers AND
  skills — stay 100% source-defined and are not user-editable; the disk file is
  strictly *additional* config layered on top of the worker's source base.
- The conductor stays responsive: it plans, dispatches and reports. Work its
  tool set excludes (a write, a shell, a user server) it cannot run inline;
  the rest it hands off as a best-effort prompt rule (§5.0).
- The conductor carries no user-configured MCP server. It can *see* what the
  gateway has configured, so it can ask for a worker addition, without loading
  any of it (§5.0, §5.4).
- Reuse the existing custom-agent file format and its whole projection +
  governance pipeline unchanged — no new schema, no field split. `prompt`,
  `mcpServers` and `skills` are load-bearing; the rest carry defaults.
- No live link back to a source: the file is a stamp, not a parent.
- The mate's config is **not** offered in the session agent picker.
- Crewmate-specific data stays on the binding, as today.

## 4. Non-goals

- **Message-array control.** This still projects a config into a KAS payload, so
  it inherits that mechanism's limits — no mid-session prompt change without a
  rebuild, no dynamic tool registration, nothing the payload schema can't
  express. Out of scope.
- **A shared/importable crewmate-definition format.** Copying severs sharing on
  purpose (§9); a sharing strategy is a separate effort.
- **Renaming code identifiers.** `kiro_agent`, `agents.<name>`, and the
  custom-agent format keep their names.
- **A data-model change for `member_id`.** The spike considered whether
  `member_id`'s empty-string-means-"not a member" sentinel should become a real
  nullable. Decision: leave it. `""`-is-unset is the house convention across the
  whole `KiroCrewAgentConfig` dataclass (`model`, `reasoning_effort`,
  `display_name`, `approval_mode` … all default `""`), and "is this mate-owned
  config?" is a DERIVED fact (does `kiro_agent` resolve under
  `mates/agents/`), never a stored flag — an `is_mate` boolean would be the
  actual mistake, able to drift from where the file lives.
- **Dynamic worker assembly** (2026-10-17). The conductor composing a worker
  per task from the gateway's live catalog of MCP servers and skills, with no
  predefined file, is the agreed direction but is P1. P0 is the predefined
  per-mate file this RFC describes. The P0 shape must not need a rewrite to
  get there: the file IS one assembled worker config, and the conductor's
  catalog view (§5.4) is the input P1 reads.
- **A dedicated decision model** (2026-10-17). A lightweight model for
  dispatch and routing decisions is in development. This RFC does not assume
  the conductor LLM holds all routing intelligence; the dispatch step is one
  seam, so that model can take it over later. Designing that model is out of
  scope.

## 5. Design

### 5.0 The two roles — a dispatch-only conductor and a worker that does the work

*(2026-10-17, from the architecture meeting. The pre-amendment text named the
two agents without saying what each is for.)*

```
Crewmate (container)
├─ runtime
│  ├─ conductor / main agent
│  │    prompt:  in source code, not JSON; the user cannot edit it
│  │    tools:   read-only file tool + spawn worker + work ledger
│  │             + the dashboard/widget servers; NO shell, NO user MCP server
│  │    job:     understand the user, plan, dispatch, report. Never does the work
│  └─ worker
│       prompt:  in source code + the file's `prompt`
│       mcp/skill: source base set + the file's `mcpServers` and `skills`
│       job:     one concrete task, reported back to the conductor. Never touches the file
├─ ~/.kiro/mates/agents/<id>.json    the mate's own copy, no parent link
└─ surrounding systems: goal, schedule, heartbeat, memory, workspace   configurable
```

**The conductor only dispatches.** Any substantive work — a code review, a
research pass, a code change — is spawned to a worker. The conductor
acknowledges, queues, dispatches and answers; it is free whenever a user
message arrives.

Two mechanisms carry that, and they are not the same kind. **The spec
enforces** what the conductor cannot do at all: it has no tool that writes a
file (no `fs_write`, no `code`), no shell (`execute_bash` is not mounted), no
user MCP server, and its one file tool is read-only (§10 question 7 is settled
read-only here, for this reason). A conductor so equipped cannot run a code
change, a build or a user-server task inline, whatever its prompt says. **The
prompt carries** the rest as a best-effort rule: that a long read (`fs_read`,
`web_fetch`) or a long think is handed to a worker too, so a turn stays short.
A rule in a prompt is weighed, not guaranteed, so §2 problem 5 is REDUCED by the
prompt and REMOVED only for the work the tool set excludes; the design accepts
that residue rather than pretending the prompt closes it.

**The conductor's prompt is a source-code string.** It names: who the mate is
and how it talks to the user; what Kiro Crew can do (render a widget, set a
schedule, keep memory); how to use the work ledger; how to spawn a worker and
what to put in the brief; and that the conductor never runs a task inline. It
is not a JSON file and it reads no `~/.kiro/crew/prompt.md` override. The
per-member inputs it reads are the member's own briefing, rules and goal, and
the project guides a crewmate's startup context already loads
(`member_essential_context.py`: AGENTS.md / SOUL.md in the member's workspace)
stay what they are — project context for the member's work, not a contract that
can replace or reorder the charter.

**The conductor's tools are a minimal set fixed in code.** A read-only file
tool, the spawn/dispatch verbs, the work ledger (`kirocrew-work`), the
member's own servers (`kirocrew-mate`, the dashboard/widget set), memory. No
user-configured MCP server is mounted on it, and no shell is mounted. The exact
grant tuple is the step-1 PR's to pin, following the shape the three existing
conductors already have
(`agent_materialization/conductor_agents.py::_conductor_spec`). The meeting
named "basic file read/write"; the design takes read-only, because a conductor
that writes files can do work inline, which is the case the tool set exists to
exclude (§10 question 7, settled).

**The conductor can see the gateway's catalog without loading it.** It needs
to know which MCP servers and skills the gateway has configured, so it can
notice a worker lacks one and ask for it (§5.4). That is a read of a catalog,
not a mount of a server.

**The worker does the work.** Its prompt (how a mate worker works, how it
reports back) and its base MCP servers and skills are source-defined. The
file's `prompt`, `mcpServers` and `skills` are layered on top at spawn time
(§5.4). A worker never edits the file; when it lacks a capability it says so in
its report.

**A worker does not promote itself.** A worker that finds its task too large
reports back to its conductor; the conductor decides whether to spin up a
sub-conductor. The dispatch tools a worker holds and the ones a conductor holds
are separate, so the roles cannot blur.

**P0 and P1.**

| | P0 (launch) | P1 |
|---|---|---|
| Worker configuration | predefined in `mates/agents/<id>.json` | the conductor assembles a worker per task from the gateway catalog |
| Dispatch decision | the conductor LLM | a dedicated decision model; dispatch keeps a seam for it |

### 5.1 The worker-extension config file

The file is a custom-agent JSON file — the same schema written to
`~/.kiro/agents/` today, no new fields — placed in a dedicated folder:

```
~/.kiro/mates/agents/<id>.json
```

It carries the full custom-agent shape (`prompt`, `tools` / `allowedTools`,
`mcpServers`, `skills`, `model`, `permissions`), but **in this design only
`prompt`, `mcpServers` and `skills` are load-bearing**: they are the user's
*additions* to the delegated worker. Every other field is written with a
default/placeholder value — the schema is kept whole on purpose so a future
design can start reading, say, `model` or `permissions` without a format
migration. The worker's own base system prompt and its tools come from Crew's
source code; the file's `tools` staying default is what makes the tool base
non-editable while the additions are editable.

**How `prompt` is applied** (2026-10-17). The worker's source prompt comes
first and whole. The file's `prompt` is appended after it as a clearly labelled
section ("instructions from this mate's configuration"). It adds to the
worker's instructions; it cannot remove or reorder the source base, because
the base is not in the file, and the source charter states that where the two
conflict the charter wins (the worker reports back rather than following an
appended instruction that contradicts its base — the same precedence a
custom-agent prompt has against the host contract today). Its trust level is that of any custom-agent prompt
today: user-authored text that reaches the model, with no grant attached to
it — grants come only from `mcpServers` and `skills`, under the ceiling (§8).
Nothing from the file reaches the conductor.

**The folder is machine-wide, alongside `~/.kiro/agents/` — a settled team
decision.** It sits under the Kiro home (`kiro_home()`), not under a per-instance
`KIROCREW_HOME`, so the mate config files live beside the custom agents they are
copied from rather than inside one Crew instance's data home. Because the folder
is shared across instances, `<id>` alone is NOT a safe partition: `<id>` is the
crewmate's `member_id`, and `memory_stores._allocate_member_id` only uniquifies it
within one instance's config, so two instances that each create a mate of the same
name produce the same `<id>` and resolve to the same `<id>.json`. The machine-wide
location therefore ships WITH a cross-instance write guard — the shared-folder rule
the repo already uses for `~/.kiro/agents/`
(`agent._decline_shared_agent_home` / `shared_kiro_agents_writable`), an
owner-identity field in the file, or a genuinely unique id — chosen in step 3,
which is the only step that writes this file (§6, §10). Creation writes the file
atomically (`0o600`); the guard is what prevents one instance from silently
overwriting another's additions, not the id by itself.

### 5.2 Crewmate-specific data stays on the binding

`KiroCrewAgentConfig` (`config/sections.py`) is unchanged. It keeps carrying
`member_id`, `kiro_agent`, `workspace`, `memory_store`, `model`, `avatar`,
`description`, `triggers`, `session_color`, `starred`, `source`. Only what
`kiro_agent` resolves to changes — the file in `mates/agents/` instead of one in
`~/.kiro/agents/`:

```
"zxc": {
  "member_id":    "zxc",
  "kiro_agent":   "zxc",            // resolves to ~/.kiro/mates/agents/zxc.json
  "workspace":    "member-zxc",
  "memory_store": "member-zxc-<uuid>",
  "source":       "kirocrew"
}
```

`member_id` is the immutable member identity (it is also the key the member's V2
memory store is owned by — `memory_stores.py` fails closed when
`owner_member_id != member_id`), not a boolean. The convention that identifies a
mate-owned binding is: **`member_id` is non-empty AND `kiro_agent == <the
member's own id>`, resolving to a file in `mates/agents/`.** A member whose
`kiro_agent` still names a shared template (e.g. the built-in `mate` member on
`kirocrew`) is a crewmate without mate-owned config — the two facts are
orthogonal.

### 5.3 Binding resolution reads both folders — and there are FIVE readers, not two

`kiro_agent` must resolve to the crewmate file, or binding resolution sees a
dangling pointer and heals it to the default agent. But the crewmate must not
appear in the session agent picker. The folder split answers both without a
lineage flag or a hiding filter:

| Reader | Reads | Crewmates visible? |
|---|---|---|
| session agent picker ("pick an agent for a scratch session") | `~/.kiro/agents/` only | No |
| binding resolution ("does `kiro_agent` name a real config?") | `~/.kiro/agents/` **and** `~/.kiro/mates/agents/` | Yes |

The folder *is* the privacy boundary. The existing fork's lineage and
catalog-hiding filter become unnecessary: a crewmate's config is excluded from
the picker because it lives in a folder the picker does not read, and resolved
by the binding because resolution reads both.

**Spike correction — "binding resolution" is not one reader, it is five.** An
earlier reading of this RFC named two readers. The live spike found that
"resolve `kiro_agent` → spec file" is implemented at **five** distinct sites,
three of which the two-folder table did not enumerate. Each must search
user-level then `mate_agents_dir()`:

| # | Reader | Surface | In the original reading? |
|---|---|---|---|
| 1 | `acp/harness/kas.py::resolve_projected_spec` | session projection | yes |
| 2 | `config/loader.py::refresh_materialized_agents` | declared-agent index | implied, but load-bearing |
| 3 | `member_essential_context.py::resolve_template_path` | crewmate startup context | **no** |
| 4 | `agent_capabilities.py::_source` | Capabilities editor LOAD | **no** |
| 5 | `dashboard/agent_admin/agent_detail.py::_agent_detail_candidates` | "Built from" pane (GET+PATCH) | **no** |

Reader #2 is the one that prevents the dangling-pointer self-heal: unless the
materialized-agent index unions `mate_agents_dir()`, a binding `kiro_agent=<id>`
reads as undeclared and the heal resets it to the default.

Normative rule this RFC adopts: **every site that maps a binding's `kiro_agent`
to a spec file MUST search user-level then `mate_agents_dir()`; a new site is
registered against a guard test so a sixth resolver added later cannot silently
skip the mate scope** (the guard test is T1's deliverable in the migration plan).
"Resolution reads both folders" is correct — it just has five mouths, not one.

`list_agents()` is already two-scope (user-level plus project-local), so adding
a crewmate read scope to *resolution* extends an existing pattern — but the
picker's scope stays user-level-only.

### 5.4 Execution paths

**Create (eager copy)** — the file is written at creation, not on first edit,
even when it starts effectively empty:

```
name the crewmate
→ write ~/.kiro/mates/agents/<id>.json                (atomic, 0o600)
   with the full custom-agent schema: prompt = "", mcpServers = [] and
   skills = [] (the user's additions, empty to start), every other field a
   default/placeholder
→ write the binding (kiro_agent = <id>, crewmate-specific fields set fresh)
→ grant the mate's own additions (see below) by editing that owned file IN PLACE
```

The file is deliberately created even with no user additions yet, so it is
present and ready the first time a user adds a skill or MCP server. There is no
"copy a source agent's payload" step any more: the conductor and the worker base
are source-defined, so nothing is copied FROM — the file holds only the worker's
*additions*. No shared-pointer phase, no first-edit fork, no lineage sidecar.

**Spike correction — any create-time addition and the eager copy must be
ordered, and the addition must not fork.** If create needs to seed the worker
file with a default addition (for example the `kirocrew-mate` MCP server), it is
written INTO `mates/agents/<id>.json` after the file exists, not minted as a
fork. The pre-spike path drove the capability editor with `put(enroll=True)`,
which **forked** the crewmate into a `crew-<hex>` draft and repointed the binding
OFF the mate id — undoing the eager copy. The create flow therefore MUST run
**eager copy first, any addition second**, and the addition MUST edit
`mates/agents/<id>.json` directly (append to `mcpServers` / `skills` in place),
never fork. If the eager copy did not land, the step leaves the binding alone
rather than minting a fork. This is the Phase-3 "retire the fork" work pulled
into Phase 1, because it is not deferrable: the fork actively breaks the eager
copy (see §6, §9a).

**Session start:**

```
crewmate chat opens; binding has kiro_agent = <id>
→ resolve binding → member_id
→ conductor: source-defined system prompt + tools + skills + mcpServers → session/new
   (the user chats with the conductor; nothing from the file reaches it)
→ when the conductor delegates, the WORKER is projected:
   source-defined worker base = prompt + tools + mcpServers + skills
   + resolve kiro_agent: look in ~/.kiro/agents/ then ~/.kiro/mates/agents/
     └ found in mates/agents/ → read its prompt + mcpServers + skills → ADD to
       the worker's base → project to KAS customAgents → apply security ceiling
```

The worker's effective config is `source base ∪ (file's prompt + mcpServers +
skills)`: the two lists are unioned, the prompt is appended (§5.1). The
projection and the governance ceiling are the existing ones — the file is the
same shape they already handle, so adding its three fields to the worker's base
needs no new projection path, only the union at delegation time. The ceiling
still applies to the merged result, so a user-added server or skill cannot lift
the worker above its governed grants.

**Edit — in place, never forked (the critical correction).** Editing a
crewmate's capabilities writes `mates/agents/<id>.json` directly: read the file,
apply the editor's operations, pin `name=<id>`, run the governance sanitizer,
write in place. The binding is never repointed and no `crew-<hex>` draft is ever
minted. None of the fork/inheritance/review lifecycle (§9a) applies, because
there is no parent to track.

This is a genuinely new edit path, not a destination swap: the existing
Capabilities editor **forks any binding on first edit** (the frontend pane runs
`agentFork` before `agentPatch`, calling `api_agent_fork`). So "edit in place"
requires, on both layers:

- **Backend:** when the binding resolves to a mate, bypass the
  fork/`enroll`/intent machinery and write the owned file directly;
  `api_agent_fork` and `put(enroll=True)` refuse a mate binding (no `crew-`
  draft, no lineage). The only guard kept is a plain optimistic lock
  (a `stale_revision`-style digest of the file) so two tabs editing the same
  mate file cannot clobber each other — that defends concurrent access, not
  parentage.
- **Frontend:** the pane does not call `agentFork` for a mate binding, and it
  suppresses the parent / "Follow the parent template" / "Review changes"
  lifecycle UI (there is no parent to show); the capability view reports a
  `mode: "mate"` the pane renders as an owned, in-place editor.

**Conductor-raised capability change — a card the user presses** (2026-10-17).
The second way the file changes starts from the conductor, not the editor:

```
conductor
  1. reads the gateway catalog: which MCP servers and skills are configured
     (a read-only tool, new; it mounts nothing)
  2. notices the worker it is about to spawn lacks X
  3. raises a change card: "add X to this mate"
     (kirocrew-mate.request_capabilities, the existing card-backed tool)
  4. the user presses → the gateway writes mates/agents/<id>.json in place,
     through the same backend path the editor uses (governance sanitizer,
     optimistic lock)
  5. the next worker spawn carries X
```

The conductor never writes the file: it runs inside the sandbox, and §8 keeps
every writer on the gateway's side. The card is the user's decision, as it is
for every capability change today (`crewmate.capabilities` is card-backed in
[rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md)); the
conductor only names the ask. Because an MCP server cannot be unloaded from a
running session, the change takes effect at the next spawn, never mid-session —
which is also why the conductor, not the worker, is the one that asks: the
worker's own session cannot gain X.

**Subagents** — a crewmate can spawn helper agents; a helper has no `member_id`,
so it resolves by name through the ordinary spawn path, unchanged.

### 5.5 Why a separate folder, not `~/.kiro/agents/`

A copy in `~/.kiro/agents/` re-enters the custom-agent catalog: picker-visible
(so it needs the lineage-hiding filter), rewritten on gateway start by the
managed-spec rebuild that writes into that same directory, and inside kiro-cli's
file lifecycle. A copy in `~/.kiro/mates/agents/` is Crew-owned, excluded
from the picker by location, and outside that lifecycle — while still being the
identical file format, so the projection pipeline needs no change.

## 6. Migration plan

Three independently shippable steps, each its own PR landing something testable.
Steps 1 and 2 do not read or write `~/.kiro/mates/agents/`, so neither can leak
config between instances. Step 3 is the only one that introduces the file, and
therefore the only one that carries the user-facing capability (configurable
worker additions) and the cross-instance write question (§8).

(2026-10-17: the pre-amendment text called steps 1 and 2 "behaviour-preserving".
Step 1 is not. Today the agent the user chats with does the work itself with
the full default tool surface; after step 1 it dispatches and keeps a minimal
tool set (§5.0). That is a user-visible change in what a crewmate chat does,
and the step owns it.)

### Step 1 — the conductor is source-defined and dispatch-only

Give the mate conductor (the agent the user chats with) its whole definition —
system prompt, tools, MCP servers, skills — from Crew's source code instead of
a file the user can edit, with the charter in §5.0: plan and dispatch, never
run a task inline, no user MCP server mounted. The spec is derived the way the
three existing conductors are (`conductor_agents._conductor_spec`):
`build_agent_config()` supplies the resolved MCP invocations and the security
hooks, and the prompt, `tools`, `allowedTools` and `mcpServers` are then
replaced — the server map narrowed to the Crew servers
(`_conductor_mcp_servers`), the grants filtered through the ceiling. One
narrowing beyond its siblings: `_conductor_spec` mounts `execute_bash`
un-approved for the goal conductor's bundled evaluator script; the mate
conductor has no such script, so its `tools` list omits the shell entirely
(§5.0). What it is NOT is a copy of the default template's surface, which is what a spec that
keeps `build_agent_config()`'s `tools` and `mcpServers` and changes only the
name would be: that shape hands the conductor every user MCP server and the
full tool surface, the shape §2 problems 5 and 6 describe.

**What rebinds, and what does not.** The three DEFAULTS move to the mate
conductor: the first crewmate's implicit template, the New Crewmate dialog's
"Built from" default and the `crewmate.create` card's template. A stored
binding is not rewritten: a crewmate whose `kiro_agent` names `kirocrew` or a
custom agent keeps it until step 3's migration (which carries its additions
into the per-mate file before it repoints anything). The mate conductor is
withheld from the "Built from" picker as a choice, but a binding that names it
still shows there, so the editor can rebind.

**Reversal.** Per crewmate: the crew editor's "Built from" still offers
`kirocrew` and every custom agent, so a crewmate whose conductor dispatches
badly is rebound to the full-surface template in one edit, with no restart.
Per install: step 1 is a sourcing change with no setting of its own, and
reverting it is reverting the PR; the step does not add a switch that would
keep two shapes of crewmate chat alive, because every later step builds on the
conductor shape.

Exit criteria:
- A mate's chat runs the source-defined conductor: its prompt text and grant
  tuple come from source, and editing `~/.kiro/agents/kirocrew.json` or
  `~/.kiro/crew/prompt.md` changes nothing about it.
- The conductor mounts no user-configured MCP server and does not mount
  `execute_bash` at all (a spec that copied `_conductor_spec`'s un-approved
  shell would fail this criterion).
- A mate-conductor-bound member's unnamed `spawn_run` runs `kirocrew-worker`,
  not the member's own template: the member-template inheritance that today
  re-runs `kirocrew` for a `kirocrew`-bound member would otherwise run the
  dispatch-only conductor as its own worker, with no write tool and no shell.
  The step maps that one template to the worker spec at the two inheritance
  sites; every other member template inherits as before.
- Asked to do substantive work, the conductor dispatches it (`spawn_run`)
  rather than running it inline — pinned by a prompt-contract test, the way
  the goal conductor's charter is.
- A crewmate bound to `kirocrew` or to a custom agent before the step still
  runs that binding after it; only the defaults moved.
- No conductor path reads or writes `~/.kiro/mates/agents/`.

### Step 2 — the delegated worker is source-defined

Make the worker the conductor delegates to — the one that does today's mates work
under the hood — exist as a source-defined base: system prompt, tools, MCP
servers AND skills, all from source code. Still no file on disk; the worker's
whole config is its source base. Its prompt carries the worker charter of §5.0:
do the one task, report back, do not promote yourself.

**The route does not flip here.** A crewmate's unnamed `spawn_run` keeps
dispatching to `kirocrew-worker` (the mapping step 1 introduces) — the mirror of
the default template, which carries the user's MCP servers today — until step 3
lands the per-mate file. Flipping the
route in this step would run every crewmate's work on a worker with no user
server for as long as the gap between the two steps lasts; the interim shape is
therefore: conductor from source (step 1), the user's servers still reached
through `kirocrew-worker`, and the source-defined worker installed but not yet
dispatched to. Step 3 flips the route and layers the file in the same change.

Exit criteria:
- The source-defined worker spec is installed and, dispatched to directly,
  projects exactly its source-defined base.
- No worker path reads `~/.kiro/mates/agents/` yet.
- A crewmate's `spawn_run` still routes to `kirocrew-worker`; no user-visible
  behaviour change in what a crewmate's work can reach.

### Step 3 — the worker reads its per-mate file as *additional* config

Now introduce `~/.kiro/mates/agents/<id>.json`. It is eager-created when a mate is
created (full schema, empty `prompt`/`mcpServers`/`skills`, defaults elsewhere —
§5.1), and at delegation the worker's effective config becomes `source base ∪
(file's prompt + mcpServers + skills)` (§5.4). This is the step that adds the
user-facing capability ("edit what my mate can do"), the folder resolution
(§5.3), the in-place edit path (§5.4), the conductor's catalog read and the
card-confirmed change it raises (§5.4), the create-time eager copy, and the
route flip: a crewmate's `spawn_run` moves from `kirocrew-worker` to the
source-defined worker in the same change that gives that worker the file, so
no release ships a worker without the user's servers. The governance ceiling
(§8) applies to the merged result.

**The cross-instance write guard is settled in THIS step, not before.** Because
only step 3 writes the machine-wide file, the leak the Design lane flagged —
`member_id` is the per-instance display-name slug, not a globally unique id, so
two instances that each make a mate of the same name resolve to the same
`<id>.json` — cannot occur until this step, and this step's PR owns the fix. The
repo already has the pattern: `agent._decline_shared_agent_home` /
`shared_kiro_agents_writable` refuse writes to the shared `~/.kiro/agents` folder
from a non-default data home, and the same rule (or an owner-identity field in the
file, or a genuinely unique id) resolves it here. The exact mechanism is an open
question for step 3's design (§10), not for steps 1–2.

Exit criteria:
- A newly created mate has a file in `~/.kiro/mates/agents/`, and a delegated
  worker projects its source base plus the file's `prompt`, `mcpServers` and
  `skills`.
- Adding prompt text, a skill or an MCP server writes only that mate's file; no
  other mate's file and no source is touched.
- A mate is **not** offered in the session agent picker (folder split, §5.3).
- No `crew-*.json` is written and `agent_state` records no `forked_from`: edits
  are in place, never forked (§5.4).
- The conductor can list the gateway's configured MCP servers and skills
  without any of them being mounted on it, and a capability it asks for lands
  in the file only after the user presses the card.
- The cross-instance write guard is in place: an instance with a non-default data
  home cannot clobber another instance's `<id>.json` (mechanism chosen per §10).

### Migrating existing crewmates (within step 3)

Existing crewmates move when step 3 lands. The conductor and worker base are
source-defined by then, so migration copies no tools; it carries over only what
the file may hold — the user's **additions**. For a crewmate whose `kiro_agent`
points at a shared `~/.kiro/agents/` agent, those are whatever
`mcpServers`/`skills` that agent carried beyond the worker's source base, and
that agent's `prompt` when it differs from the default template's — copied
WHOLE and verbatim into the file's `prompt`, not excerpted: the migration does
not read a prompt for intent, and the appended section's precedence (§5.1, the
charter wins on conflict) is what bounds a copied prompt that argues with the
worker's base. A crewmate with nothing extra gets an empty file. Leave the
shared agent in place.

Prove it before flipping pointers:

```
for each crewmate:
  additions = (shared agent's prompt, mcpServers, skills) minus the worker's source base
  write ~/.kiro/mates/agents/<id>.json  with those additions, defaults elsewhere
  after = project(worker source base ∪ file additions)
  assert after grants exactly: source base + the carried-over additions
  (only then) repoint kiro_agent
```

Exit criteria:
- Every existing crewmate has a file and its binding points at it; identity,
  memory, workspace and open chats are untouched.
- Each migrated worker grants its source base plus exactly the additions carried
  over — no capability silently gained or lost.

### The fork lifecycle, retired across the steps

Today's lazy copy-on-first-edit fork (`api_agent_fork`,
`_unlink_copy_unless_referenced`, the `agent_state` lineage sidecar, §9a) exists
for a mate only because editing a mate forks it. Step 3 removes the mate path's
dependence on it (edits write the file in place, never fork). Any **non-crewmate**
caller that still depends on the fork is a separate audit: enumerate its callers
and either remove the path once none remains or scope it to the survivors. That
audit does not gate steps 1–2 and is not blocking for step 3's own exit criteria.

## 7. Backward compatibility

- **Custom agents in `~/.kiro/agents/` are untouched.** This changes only where a
  *crewmate's* config lives.
- **kiro-cli unaffected** — a crewmate file never enters its machinery (it reads
  `~/.kiro/agents/`, not `~/.kiro/mates/agents/`). Crewmate derivation is a
  KAS wire payload, as today.
- **Session-picked custom agents are a no-op** — they already run from their
  `~/.kiro/agents/` file, and the picker still reads only that directory.
- **Existing crewmates** are migrated in Phase 2 rather than reinterpreted; their
  identity and memory are preserved and the dry-run proves each migrated worker
  grants its source base plus exactly the additions carried over.
- **A crewmate chat stops doing the work itself after step 1** (2026-10-17).
  The answers still arrive in the same chat; the worker's result comes back
  through the dispatch path the conductor already uses. What a user loses is
  the chat agent holding every user MCP server directly — those move to the
  worker, where the task runs. Between steps 1 and 3 that worker is
  `kirocrew-worker`, which mirrors the default template and so still carries
  the user's servers; a crewmate bound to `kirocrew` or a custom agent before
  step 1 keeps that binding until step 3's migration (§6).

## 8. Security considerations

- The crewmate file is user-editable, but the **governance ceiling is applied at
  projection** — the same ceiling a custom-agent payload passes through — so a
  hand-edited crewmate file cannot escalate the crewmate's real grants. The
  crewmate payload shares one projection + ceiling with the custom-agent payload;
  governance is not bypassed. The in-place edit path (§5.4) runs the governance
  sanitizer on the way to disk as well.
- **Who writes the file, and the disposition that follows.** The writers are
  gateway/dashboard handlers that run **unsandboxed** — the create path (§5.4),
  the in-place Capabilities edit path, and the change-card apply that a
  conductor-raised card runs (§5.4) — exactly like today's fork/publish/reset
  writers (`dashboard/agent_admin/fork_publish.py`). No in-sandbox agent code
  writes `mates/agents/<id>.json`; the conductor raises a card and the gateway
  writes after the user presses it. The file is written on the gateway's side
  of the sandbox and then projected onto the wire. So the earlier reasoning
  "`VISIBLE` because the in-sandbox agent writes it" is wrong: nothing in the
  sandbox needs write access to this folder. The only in-sandbox question is
  whether a sandboxed session must be able to READ the folder at all. Because
  the config reaches the session as a projected KAS payload, not by the session
  reading the file, the default disposition should be to **not expose the folder
  to the sandbox** — treat it like the other gateway-owned config under the Kiro
  home rather than a `VISIBLE` in-sandbox leaf. Pin the exact disposition against
  `sandbox.py` at implementation time, and if any in-sandbox read path is later
  found, ensure a session cannot reach a *different* crewmate's file than the one
  it is bound to.
- The file is written `0o600`, atomically, by those gateway writers.
- **The conductor's catalog read is a read, not a grant** (2026-10-17). The
  tool that lists the gateway's configured MCP servers and skills returns names
  and descriptions; it mounts nothing and approves nothing. A server reaches a
  worker only through the file, after the card, under the ceiling.
- **The file's `prompt` carries no grant** (2026-10-17). It is user-authored
  text the worker reads, at the trust level of a custom-agent prompt today. It
  reaches only the worker; the conductor's prompt is source and takes nothing
  from the file.
- **Cross-instance writes to the shared folder.** Because the folder is
  machine-wide and `member_id` is unique only within one instance (§5.1), two
  instances can resolve to the same `<id>.json`; left unguarded, one instance's
  write silently replaces another's additions. Step 3 ships a write guard for
  this — the `~/.kiro/agents/` shared-folder rule, an owner-identity field, or a
  unique id — and the mechanism is open question 1 (§10). This is a data-integrity
  guard, chosen before step 3 lands, not deferred past it.

## 9. Alternatives considered

- **Bespoke Crew-owned crewmate schema.** A new file format holding both the
  config and the crewmate-specific fields. Rejected: it needs a new schema, a
  split between "fields that reach the model" and "Crew-only fields," and a
  separate "resolvable but not selectable" name-set to keep it out of the picker
  — all of which this design avoids by reusing the custom-agent format and
  splitting by *folder* instead of by schema. It also moves crewmate-specific
  data off the binding, where it already lives correctly.
- **Shared pointer + per-crewmate overlay.** Keep pointing at a shared agent and
  add a small per-crewmate overlay (extra skills/MCP) merged at session start.
  Rejected as the primary approach: two sources of truth per crewmate, new
  merge/precedence machinery, and the shared base keeps the stale-until-rebuild
  behavior. Its one advantage — the shared agent stays a real template, so a base
  edit reaches every crewmate — is exactly what copying severs; teams that rely
  on sharing a base agent lose that under copying (a real tradeoff, and the
  reason a sharing strategy is a §4 non-goal rather than denied outright).
- **Shared pointer, read-only in the editor (today's model made deliberate).**
  Freeze the pointer, remove config editing from the crewmate editor. This design
  is a strict superset — it adds ownership on top — so shipping it directly is
  preferred.
- **Lazy copy-on-first-edit into `mates/agents/`.** Keep the existing fork
  trigger, redirect only its destination folder. Rejected in favor of eager copy:
  lazy leaves a shared-pointer-until-touched window and keeps the lineage/reset
  machinery alive; eager makes a crewmate self-owned from birth and lets the
  lineage path be retired.
- **Keep the fork/inheritance lifecycle, just point it at `mates/agents/`.**
  Considered during the spike: teach intent/overrides/drift-guards to target the
  mate folder with the mate's source as its parent. Rejected — it reintroduces
  the parent connection the design deliberately severs (§9a), so a later edit to
  the source would raise "parent changed / Review changes" on a config that is
  supposed to be standalone.
- **One identity for conductor and worker** (2026-10-17). The chat agent spins
  up copies of itself for complex tasks; same prompt, same tools. Rejected at
  the architecture meeting: the chat agent then holds every user MCP server in
  its context on every turn (§2 problem 6), and nothing stops it from running
  the task inline and going unresponsive (§2 problem 5). Two prompts and two
  tool sets are what make "always free to answer" enforceable.
- **Load the user's MCP servers on the conductor, with a dispatch rule**
  (2026-10-17). Keep one full-surface chat agent and tell it to delegate.
  Rejected: the context cost is paid whether or not the rule is followed, and a
  rule a model weighs is not a guarantee. The conductor gets a catalog view
  instead, which costs a tool result only when it asks.
- **Let the conductor write the file directly** (2026-10-17). Rejected: it runs
  inside the sandbox, and §8 keeps every writer of this file on the gateway's
  side; and a capability change is the user's decision, so it goes through the
  card the editor's own path already uses.

## 9a. The fork / inheritance lifecycle we deliberately do NOT use

*(Required section — records a conscious decision not to lean into an existing
mechanism, so a future reader does not mistake its absence for an oversight.)*

There is already a fork + inheritance + review lifecycle for a crew editing a
**shared** custom agent, and mates deliberately do not use it. What it does, for
a shared template:

1. **Fork** on first edit — copy the shared template to a private `crew-<hex>`
   spec in `~/.kiro/agents/`.
2. **Record lineage** in the `agent_state` sidecar: `forked_from: <template>`,
   `private_to: <crew>` (the spec file cannot carry these — kiro-cli rejects
   unknown fields).
3. **Rebind** the crew's `kiro_agent` to the `crew-<hex>` copy.
4. Thereafter treat the copy as **`parent baseline + this crew's overrides`**,
   staged as reviewable `intent` (draft → reviewed → saved), guarded by
   **drift checks** — `parent_identity_changed`, `private_template_shadowed`,
   `materialization_changed`, `stale_revision`, `foreign_private_copy`.

**Why that machinery exists:** it is the cost of inheritance. A fork is a copy
that keeps *following* a live parent, so the system must know the parent
(`forked_from`), the sole owner (`private_to`), and must refuse rather than
silently corrupt when the parent, the file, or the editor's view drift apart.

**Why a mate does NOT use it.** A mate has **no parent to track**: the worker's
base lives in Crew's source code, and `mates/agents/<id>.json` holds only the
user's *additions* to it — there is no forked template the file inherits from and
no `file://` link back to one. Every piece of the lifecycle defends a parent
relationship or shared-file access a mate does not have: no
`forked_from`/`private_to` (no parent, and the member is already 1:1 with the
file via the binding), no intent/override/draft cycle (the base is source, not a
baseline layered in the file), no parent-drift guards (no parent to drift). A
mate edit is a plain read-modify-write of its own additions. The one guard kept
is a plain optimistic lock so two tabs don't clobber each other — that defends
concurrent access, not parentage.

**The decision, stated plainly:** a binding that resolves to `mates/agents/` is
NEVER forked; it is edited in place. The fork/lineage/drift model stays in place
for shared custom agents, and mate-owned config does not lean into it. A mate is
not a child that follows a parent; it is the authoritative file. Running a mate
through the fork lifecycle would model a parent relationship the design severed,
re-expose the mate in the `/set_mode` picker, and orphan the owned file. Revisit
only if mates are ever redefined as living templates that track a source.

## 10. Open questions

1. **Cross-instance write guard for the machine-wide file (step 3).** `member_id`
   is the per-instance display-name slug (`memory_stores._allocate_member_id`
   uniquifies only within one config), not a globally unique id, so two instances
   that each create a mate of the same name resolve to the same machine-wide
   `~/.kiro/mates/agents/<id>.json` and could clobber each other's additions —
   the very leak §2 exists to remove. The machine-wide location is a settled
   decision; the guard that makes it safe is not yet chosen. Candidates: (a) adopt
   the existing shared-folder rule — `agent._decline_shared_agent_home` /
   `shared_kiro_agents_writable` refuse writes from a non-default data home, so
   only the default install writes mate config; (b) record the owning
   home/instance identity in the file and refuse a cross-owner write (a small
   schema addition); (c) make `member_id` genuinely unique. This is a **step-3**
   decision — steps 1–2 never touch the file — and step 3 does not ship without
   one of these.
2. **Edit → re-chat staleness.** A live KAS session reads its config when the
   backend session is built, so an edit to a crewmate file isn't seen until that
   session rebuilds. Does an edit need an explicit "apply now" that rebuilds the
   backend session, or is next-rebuild acceptable? This is the least-proven path
   — the spike confirmed the editor now loads and (offline) saves a mate's file;
   whether a live session picks up the edit on next rebuild is the one live-only
   unknown. (2026-10-17: for the worker this is settled — the file is read at
   spawn, so the next spawn sees it. The question remains only for the
   Capabilities editor's own live view.)
3. **Default additions for a new worker file.** The conductor and worker base are
   source-defined, so a new file starts with empty `prompt`/`mcpServers`/`skills`.
   Should create seed any default additions (e.g. the `kirocrew-mate` server),
   and if so, which — versus leaving the file empty until the user adds
   something?
4. **Harness abstraction.** `kiro_agent` is KAS/custom-agent-shaped. Should the
   binding abstract one layer up to support other harnesses before this lands, or
   after?
5. **Crewmate visibility in the session-start switcher (new, from the spike).**
   Crewmates appear in the session-start crewmate switcher via
   `/api/agents/catalog` + `useAgents.withoutCoveredCrewmates`. This is
   PRE-EXISTING member-listing — the switcher lists crew *members*, not
   `mates/agents/` files — and is independent of the folder split, which only
   governs the Kiro-wide `/set_mode` picker. Noted as out-of-scope-but-adjacent
   so it is not conflated with this RFC; hiding crewmates there is a separate
   crewmate-visibility product decision with global blast radius.
6. **Per-crewmate file isolation.** If a crewmate's in-sandbox session must be
   prevented from reading *another* crewmate's file in `mates/agents/`, that is a
   sandbox-scoping requirement §8 must pin, not assume.
7. **The conductor's file tools: read-only or read/write?** (2026-10-17, step
   1.) Settled: read-only (§5.0). The meeting named "basic file read/write";
   the three existing conductors carry no file-writing tool, and a conductor
   that writes files can do work inline, which is the case the tool set exists
   to exclude. Recorded here so the question is not reopened in review.
8. **What the catalog tool returns** (2026-10-17, step 3). Names and
   descriptions of the gateway's configured MCP servers and skills, or also
   the tool list each server exposes? The smaller answer keeps the conductor's
   context small, which is the point; the larger one lets it pick better. Start
   small.
