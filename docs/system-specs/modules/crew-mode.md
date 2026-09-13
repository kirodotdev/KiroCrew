# Crews

A **crew** is a named entry in the config's `agents` map. It binds a kiro-cli
agent template plus a workspace, a memory store, a model and a reasoning effort,
and it carries free-text `triggers` that decide whether the orchestrator may
route work to it. The selection path is the `select_crew` MCP tool.

This spec used to own a second thing spelled *crew*: **Crew Mode**, the
`"crew"` chat-slot mode whose control plane (`crew_chat.py`) fanned one
session's topics out to sub-sessions. It is retired — see
[Retired: Crew Mode](#retired-crew-mode) — in favour of the Crew Members page
(`/members`, served by `dashboard/handlers/members.py` and `members.py` in the
table below), where each crew is a standing agent with its own thread.

A crew is not a *Remote Instance* (see [instances.md](instances.md)), and not
an Issue Radar *crew*, which is that app's own repository work crew
(see [issue-radar.md](issue-radar.md)).

## Components

Legacy topic respawn requires its original run identity or surviving legacy
run state. If pruning removed both, continuation refuses with a named memory
error and leaves the queued request retryable; the owner must start a new topic.
Missing history must never silently turn a private topic into Global memory.

| File | Role |
|---|---|
| `src/kiro_crew/config/sections.py` | `KiroCrewAgentConfig` — the crew record: `kiro_agent`, `workspace`, `memory_store`, `model`, `reasoning_effort`, `description`, `triggers`, `source`, `session_color`, `avatar`, per-crew watchdog overrides |
| `src/kiro_crew/config/loader.py` | `resolve_agent_bindings` (crew to workspace / memory store / template) and `resolve_effective_model` (the default-model precedence) |
| `src/kiro_crew/mcp_core.py` | `_do_select_crew` — the roster and bind bodies |
| `src/kiro_crew/mcp_tools/control.py` | The `select_crew` tool declaration and dispatch |
| `src/kiro_crew/validation.py` | `SELECT_CREW_SCHEMA` — argument validation for that tool |
| `src/kiro_crew/members.py` | Per-crew member space: activity log, DM-thread binding, permanent rules, self-maintained briefing, the member turn chokepoint |
| `src/kiro_crew/subagent.py` | `_validate_agent` — what an `agent=` name is checked against, and `UNADVERTISED_AGENTS` |
| `src/kiro_crew/config/prompt-orchestrator.md` | The orchestrator prompt that names `select_crew` and the delegation rule |
| `src/kiro_crew/dashboard/handlers/agents.py` | Crew CRUD on `/api/agents`, and the roster row serializer |
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, `POST /api/members`, thread get-or-create, rules, activity |
| `website/src/pages/KiroCrewAgentsPage.tsx` | The Crews UI, mounted as the **Crews** tab of `CapabilitiesPage` (Agent Capabilities) |
| `website/src/components/crew/crewEditorSections.ts` | The crew editor's pane registry, including the Routing pane that edits `triggers` |
| `website/src/components/CrewWakeSection.tsx` | "What wakes this agent" — schedules, deliberately distinct from `triggers` |

## Crew records and binding

A crew lives only in `config.json` under `agents.<name>`. It is not a kiro-cli
agent file: `kiro_agent` points at one. `resolve_agent_bindings` turns a crew
name into `ResolvedBindings`, in this order:

1. the named crew, when it is a key of `config.agents`;
2. otherwise a **materialized** kiro agent of that name (an app-registered agent
   under the user's `~/.kiro/agents/`, or a project agent), which keeps
   dispatching itself with the default workspace and Global Memory V1;
3. otherwise `default_agent`, with `requested_resolved` set to `False` so a
   caller never advertises a binding that is not running.

An unresolvable workspace falls back to `default_workspace`. Memory identity
resolves exactly: the reserved `default` assistant uses Global Memory V1;
existing members keep their declared V1 binding until the owner chooses V2.
New and opted-in members own unique private V2 stores. Missing, unreadable,
shared or mismatched private identity stops execution with an actionable error.
Selecting a member as `default_agent` preserves that member's memory version and
binding. With no agents configured, the resolver returns the existing defaults.

Member creation automatically provisions empty private memory. Members cannot
choose a shared store or rebind their private store. Legacy members may continue
using V1 or explicitly choose empty V2 memory from their settings; former Global
or named V1 contents remain untouched. Config fields, atomic publication, ownership manifest and
recovery semantics are owned by [config](config.md#named-memory-stores-memory_storespy).

A new member DM inherits the member's configured workspace, falling back to
`default_workspace` when that name is undeclared. Its project directory uses the
shared `default_project_dir` validation, so provider cwd and project essentials
refer to the same workspace. Resolution finishes before publishing the slot;
the first slot broadcast includes its project directory. A concurrent opener's
existing slot is preserved. Reopening a live or restored
thread keeps its saved workspace and project, including an explicitly empty
project, rather than resetting a session choice to the member default.

Opting into V2 opens a fresh member conversation. Existing V1 conversation and
native provider context cannot become private context by changing the config.
The member-thread binding records its private store generation and reuses that
conversation across later opens and restarts. An already protected V2 thread
keeps its existing key. Old schedules and child runs retain their recorded store;
the opt-in does not relabel past or already assigned work.

Private memory also pins an active dashboard turn to its member in ordinary
chat slots. A provider-side agent switch stops the stream with a visible notice
and resets the provider before another turn; later events cannot continue under
another agent while using that member's memory. This covers member DMs and
ordinary V2 chats. Ordinary V1 chats keep their existing switch behavior. The
validation and reset contract is owned by
[session](session.md#private-member-session-ownership).

The member side panel's Crew summary tab and the editor link to
`/settings/overview?view=memory&store=<name>`. The private memory workspace has
Memories, Profile and Recovery tabs: browsing/search/correction/copy stay in
Memories, preferences and project anchors stay in Profile, and backups plus
retired experiences stay in Recovery. Advanced facet analysis is collapsed.
Profile and Recovery load on first visit; visited Profile stays mounted so tab
changes cannot discard its drafts. Changing the selected member requires explicit
discard while a profile draft or memory mutation dialog is open. Source references
are rendered as origin labels and item references rather than JSON payloads.

The workspace header, store picker and copy-source picker reuse the owning
member's exact avatar descriptor and name, including uploaded pictures. Returning
from the member editor refreshes that identity. Empty memory can open
`/members?member=<exact-name>` directly; this link selects the member by name,
then uses the existing verified thread-opening endpoint. A failed thread open
retains its localized error heading and structured diagnostic report. Details
reveals the redacted reason on demand; Ask the agent receives the same report
when navigation permits. The cached conversation and its drafts remain available.

Facts, rules and experiences all support correction and explicit forgetting.
Experience correction keeps the same record identity and provenance. A store
marked unavailable still makes a scoped read to obtain its actual refusal, with
Retry and Recovery actions; it never displays cached records as a successful
read. Recovery paginates retired memories and refreshes live recall after an
item is restored. Complete snapshot restoration stays visibly staged across
page visits until gateway restart, and the owner can cancel the pending stage
without changing current memory or its saved backup.

Inline schedules created inside the editor persist `member_id` separately from
the provider template. A legacy schedule carrying only `agent_id` stays in Global
Memory V1 even when that string matches a member alias. The editor lists private
member jobs by exact `member_id`, and an existing job's member is immutable.
Legacy jobs retain their previous template/sequence display attribution and show
Global Memory V1 in the member's Schedules pane. Displaying an old schedule there
does not migrate it or grant access to that member's private store.

`resolve_effective_model` is the single source of truth for what model a new
session on a crew starts with, highest tier first: the crew's own `model`, the
bound kiro agent's pinned model (skipped for the built-in `kirocrew` agent), the
global `agent.model`, then the installed agent file's model. A per-session pick
outranks all four and is not considered there.

The loader is defensive about hand-edited config: a non-string `model` or
`triggers` collapses to `""`, an unknown `reasoning_effort` collapses to inherit,
and a junk watchdog override collapses to `0`.

## Hire: `POST /api/members` (copy-on-hire)

A crew member is a Kiro custom agent plus the wrapper row; **hire** is the one
verb that makes a member from a definition. Source kind `local` adopts an
installed agent file (`~/.kiro/agents/<agent>.json`):

```json
{"source": {"kind": "local", "agent": "reviewer"},
 "display_name": "Checkout triage", "role": "Oncall Triage Engineer",
 "workspace": "default", "triggers": "", "session_color": ""}
```

`source.kind` must be a string in `_HIRE_SOURCE_KINDS` (an unhashable value is a
400 `unsupported_source_kind`, never a `TypeError`); `source.agent` must be in the
template-name grammar (`invalid_source_agent`) -- the source is a FILE the installed
listing offers, dots included (`reviewer.v2`), and the row is bound to the copy,
whose stem the copy writer mints from the member id, so the binding stays inside the
agent-name grammar; the optional text fields (`workspace`, `description`, `triggers`,
`session_color`) must be strings when present (400 `invalid_<field>`, before anything is
written -- the create core stores them as given, and a list there is a member that
persists and then raises when its thread opens or `route_crew` reads it). A display name whose minted id would
share another member's SLUG is 409 `slug_collision` before anything is written
(`_slug_collision_refusal`, the create's `admit` hook: it runs INSIDE the config-lock
hold against the snapshot the row is published from, with the id the create minted,
and AGAIN inside `persist_member_config`'s cross-process locked mutation against the
document's `agents` map as it is on disk at the write (`MemberAdmissionRefused`
carries the hook's own code back; nothing is written) -- the in-process lock does not
hold a second gateway or the CLI, and two processes' pre-lock checks could both pass
for `Triage` and `triage` and then serialize into two rows on one slug): the slug keys `members/<slug>/`, the rules file and the
DM binding, and it is lossy (`Foo` and `foo` share one), so two members on it would
inherit each other's briefing and be refused their thread and rules as a collision
-- the hire is where that is still sayable. The route composes the create
core, owner-gated once at the top, and is **atomic** -- the member either exists
with its own copy of the source or does not exist -- and **at no moment is a row
bound to the SHARED source readable**: the copy is made inside the create's
config-lock hold, before the row exists, and the row is published already bound to
it. The other order (publish, then fork and rebind) left a source-bound row on disk
between the two; a concurrent thread open in that gap resolves the source binding
and runs a session that keeps using the shared template after the hire completed,
which is the exact hazard the copy exists to remove. The whole thing runs as ONE
transaction under `chat_utils.drained` (the coroutine twin of `drained_to_thread`):
a cancellation of the request mid-way -- a gateway shutdown, a client that closed
the connection -- is absorbed until the transaction reaches its own end and
re-raised afterwards, so a copy is never left without its row:

| Step | Core | On failure |
|---|---|---|
| 1. resolve the source | `_load_template_specs` | 404 `template_not_found` / 409 `ambiguous_template_name`; nothing written. The create path tolerates a missing template (a crew may be bound ahead of an install); a hire may not, because its promise is a copy of that file |
| 2. copy-on-hire | inside `_create_crew(copy_source=…)`, after the id is minted, the slug admitted and the source has passed the foreign-private-copy check, under the config FILE lock (`update_config_locked` with a read-only mutate, the cross-process one the fork and publish paths hold for the same reason: a writer in another process cannot bind the destination between the bindings read and the file's first byte) and, inside it, the spec lock: `_write_private_copy` (the one writer of a private copy, shared with the editor's first-edit fork) copies the source into a member-owned file whose stem derives from the member id, re-reading the source in-lock, reserving every current binding and the boot-rebuilt stems, suffixing past collisions and reserved basenames, and records lineage in the `agent_state` sidecar in the same hold | 404 `template_not_found` (the source vanished between 1 and 2), 409 `ambiguous_template_name`, 500 `bookkeeping_failed` / `fork_failed`; nothing published |
| 3. publish the wrapper row | the rest of `_create_crew` (the body of `POST /api/agents`: private memory provisioned, the row persisted) with `kiro_agent` = the copy. The publication's `admit` hook (`_admit_doc`, every create -- plain and hire) re-runs the LINEAGE check inside `persist_member_config`'s cross-process locked mutation, against the sidecar and the document as they are at the write: the pre-lock check holds only the in-process lock, so a hire in another gateway can copy a template to exactly the name a plain create validated as "missing, tolerated" and record its member as the owner in between, and a row published anyway would bind a second member to one private definition; a foreign owner is 409 `foreign_private_copy`, an unreadable sidecar 409 `lineage_unverifiable`, and a row another process already bound to the private copy this member is about to bind is 409 `foreign_private_copy` as well (the sidecar names one owner; a row is the other half of the same fact). Pinned: `test_a_copy_recorded_between_the_lineage_check_and_the_write_is_refused`, `test_a_row_another_process_bound_to_the_copy_refuses_the_hire`. Then, outside the lock, the fork governance refresh (`_refresh_forked_templates`) re-runs exactly as the fork endpoint runs it after its rebind, so a pass that interleaved between the copy's lineage record and the row's persist -- and recorded the copy as an uncorroborated fork -- cannot leave the new member blocked at the spawn gate | its own 4xx/409, verbatim -- and the copy is **unwound** (`_unwind_private_copy`: file and lineage, unless a row already took the name -- reference check and unlink one critical section under the config lock, so a binder in another process cannot land between them), so a retry does not find a stranded file claiming the id |

The create body is this package's own contract (pinned by its tests); the hire
reads it strictly -- a missing `name` or `kiro_agent` is a 500 `hire_incomplete`,
never a guessed default. The answer carries `kiro_agent` (the copy's name) beside
the id for exactly this reader.
Why a server verb rather than the two client-reachable calls: a client that dies
between create and fork leaves a member bound to the SHARED source it was told
it owns -- the exact hazard copy-on-hire removes -- and only the server can roll
the first half back. Two members hired from one file therefore coexist, each with
its own copy and row; a second hire whose display name mints a taken id is a 409
`agent_exists` (the message names the typed name and the id).

Success: `{"ok": true, "id"}` -- the minted id (what `/members?member=` resolves);
the copy the member is bound to and what the caller sent (label, role, source) are
read back from the roster row, not echoed. The `GET /api/members` row carries
`template_origin` -- the template a member's own
copy was made from (`forked_from` where the sidecar's `private_to` is this
member), `""` when bound to a shared template directly -- so the drawer reads
`reviewer — customized copy` (the editor's own word for a forked copy is
"Customized") rather than presenting the copy's stem as a template. It passes the
same redactor as the other identity fields: a declared template name is text a
package or a hand-edited spec wrote. The lookup keys the sidecar by the row's
binding, itself free text in a hand-editable config: a non-string binding
(`kiro_agent: []`) lists with no lineage rather than turning the roster read into a
500 (pinned: `test_a_hand_edited_non_string_binding_lists_with_no_lineage`).

**Zero-config.** Only `source` is required. Without a `display_name` (absent or
blank) the member is named after its `role`, else after the source file, and the
row records **`named_by_user: false`** (`KiroCrewAgentConfig.named_by_user`,
default `true` -- every pre-existing row, every create with a typed name, and a
hire that took one are "named"; a hand-edited junk value reads as `true` so a
stray edit can never resurrect the hint). The create honours the flag: a defaulted
name is allowed to collide -- the id is suffixed the way the migration suffixes
(`code-reviewer-2`) and the label follows (`Code Reviewer #2`) -- because there is
no user to hand a 409 to; a typed name that collides is still 409 `agent_exists`.
The first rename (`PUT /api/agents/{id}` with `display_name`) flips the flag to
`true`; a PUT that leaves the name alone does not -- and neither does a save that
**echoes** the current name back (the Crew Manager form sends every field): the
flag flips only when the *effective* display name actually changes. `GET /api/members` carries
`named_by_user` (withheld from `GET /api/agents`, like `starred`), which is what
the thread header reads for its *Just hired · named after its role* hint and its
in-place rename (design step 6). This step ships the verb **API-only**: the UI's
one hire entry point is the **hire gallery** under the Crew Members page (step 6,
`/members/hire`), which lands with that step; until then the page's Add member is
the crew manager's plain create form, and the crew manager's own **New crew** stays
a plain create (bind to a shared template) either way. Pinned in `test/test_member_hire.py`
(the gate: two members from one file coexist; **zero-config**: a hire with only a
source and a role lands `Code Reviewer` / `named_by_user: false`, a second one
`Code-Reviewer-2` / `Code Reviewer #2`, a typed name is marked named, a hire with
no role is named after the file, a blank name is an absence, the first rename
flips the flag and an untouched-name or echoed-name PUT does not, a typed collision is still 409;
no moment exists where a row is bound to the shared source -- the copy exists
before the row, the row persists bound to the copy; a failed copy leaves no
member and a retry is clean; an unwind that cannot remove the file keeps its
lineage; the unwind's reference check and unlink run as one section under the
config lock (in-lock callers hand over their locked document); a name sharing another member's slug is refused, the check runs inside
the config lock and concurrent `Triage`/`triage` hires publish exactly one member;
the copy is reserved and written under the config file lock; a dotted template the
listing offers can be hired; a source that vanishes between resolve and copy is 404
with nothing written; a row that fails to persist unwinds the copy; a cancelled
hire finishes its transaction; the source must not be another member's private
copy; unhashable kinds and non-string text fields are 400; lineage on the roster) and
`test/test_agents_roster_contract.py` (`named_by_user` withheld from the crew
manager's roster).

## Selection: the `select_crew` contract

`select_crew` has two modes, both answered as JSON by `_do_select_crew`.

`route_crew` resolves each trigger-matched member independently. Healthy matches
retain their rank and owned store. Matching members whose memory cannot be
resolved appear in `unavailable` with a bounded, path- and credential-redacted
reason. No healthy match and no trigger match are distinct outcomes; unavailable
memory never authorizes substitution with Global memory. A named `select_crew`
refusal returns `crew` and `error` without a bound store or routing activity.

**Roster** (`crew` omitted or empty):

```json
{"default_agent": "default",
 "crews": [{"name": "oncall", "triggers": "incident, prod outage"}],
 "guidance": "Select a crew ONLY when its triggers clearly and specifically match…"}
```

Three rules define that list, and each is load-bearing:

- A crew whose `triggers` is empty or whitespace is **omitted entirely**. There
  is no fallback to `description`: no triggers means not a routing candidate.
- `default_agent` is omitted, because it is the caller.
- The response carries `default_agent` and `guidance` so the model has an
  explicit fallback and a high-confidence bar rather than inferring one.

**Bind** (`crew` names a roster entry):

```json
{"crew": "oncall",
 "bound": {"kiro_agent": "oncall-agent", "workspace": "/…/oncall",
           "memory_store": "oncall-mem", "model": ""}}
```

`crew` is the member's **id** (the `agents` key). A handle that is not a key is
tried once as a **display name** (`member_identity.members_named`, exact after
whitespace normalization): the free-text name that was the key before the id split
minted one from it -- `select_crew("case competition")` in a skill written before the
migration -- still binds, and `bound` reports the id, which is what `spawn_run`
takes. Two members may share a display name, so an ambiguous handle answers
`{"error": "ambiguous crew '…': N members carry that display name — select one by
id: a, b", "available": "…"}` rather than guessing (binding the wrong member's
memory is the worse failure). An unknown name answers
`{"error": "unknown crew '…'", "available": "…"}`. The membership test against
`cfg.agents` is the deny-by-default gate; `SELECT_CREW_SCHEMA` deliberately does
not impose a name grammar, because the display-name fallback needs the raw string
and a stricter schema would list a crew in the roster and then refuse to bind it.

A bind also records a routing-decision pointer through
`members.record_activity` with `via="select_crew"`. Two properties of that write
matter:

- The entry keys the session under `decided_in`, not `session`, because the
  decision is made in the parent session while the crew runs somewhere else. A
  consumer counting sessions a crew took part in therefore cannot miscount a
  session the crew never ran in.
- The caller's memory mode is resolved at the call, and only `persistent`
  sessions are recorded. An unreadable session degrades to the private spelling,
  so the failure mode is a missing entry, never a durably logged private session
  key.

These entries are **intent, not execution**: binding a crew does not oblige the
model to delegate to it, and no `via="spawn"` execution entry exists today.

## Delegating to a bound crew

Explicit member delegation uses `spawn_run(crew=<member>)`. The member alias
resolves its provider template and private memory together. The separate
`agent=` argument identifies a provider template, not a durable member identity;
it must not be used to infer access to a member's private memory.

A private member's own sub-tasks and schedules retain its store. It cannot select
Global V1 or a peer through `spawn_run` or `cron_add`. The trusted owner or Crew
coordinator assigns cross-member work; named tool delegation respects the
recipient's routing opt-in. HTTP spawning verifies the actual calling process
before accepting a parent session, and the run primitive checks the boundary
again before allocating a provider.

A named-but-unknown agent is **refused**, never silently answered by the default
agent, with the machine-readable code `agent_not_found`. That refusal is a
privilege boundary: the default agent frequently runs at broader approval, so a
typo'd or injected name falling back to it would be an escalation at the manager
primitive. An empty `agent` still means "use the default".

Crew Mode resolves the alias itself instead of relying on the coincidence:
`CrewOrchestrator._dispatch_agent` calls `resolve_agent_bindings` per dispatch
and passes `bindings.kiro_agent`. It returns the raw crew name when
`requested_resolved` is `False`, so an unknown crew is refused by
`_validate_agent` rather than quietly running the default agent under a stale
name, and it resolves an empty crew too so the concrete template stays inside
`capabilities.spawn.scopes.agents`.

## Boundaries

- A crew's `triggers` is free text read by a model. It is not a matcher, and no
  regex interprets it.
- `POST /api/agents` requires an explicit `kiro_agent`; the silent `"kirocrew"`
  default is refused, because it made every template-less crew an alias for the
  default agent. A template absent from the installed listing is accepted with a
  warning rather than refused, since an edition may resolve a row the listing
  cannot see.
- A credential-shaped crew name is refused at creation, and roster values are
  masked for every caller but the owner. An already-stored name is not renamed
  retroactively, which is why the owner keeps reading it verbatim: a name must
  be legible to be renamed.
- `kirocrew`, `kirocrew-conductor`, `kirocrew-pipeline-conductor` and
  `kirocrew-security-conductor` are in `UNADVERTISED_AGENTS`, so they never
  appear in a rendered roster.

## Tests that pin this

| Test | What it holds |
|---|---|
| `test/test_select_crew.py` | Roster excludes the default crew and every triggerless crew, carries `default_agent` plus guidance; a named crew returns its bindings; an old free-text handle resolves through the display name and binds by id; a display name two members share is refused as ambiguous, naming both ids; an unknown name returns `error` plus `available`; the schema accepts spaces and dots in a crew name |
| `test/test_crew_reasoning_effort.py` | Per-crew effort reaches a crew dispatch |
| `test/test_members.py`, `test/test_members_dm_thread.py` | Slug validation and containment, activity recording and dedupe, DM-binding canonicality, rules and briefing reads |
| `test/test_chat_send_agent_model_default.py` | The crew model default a new session starts on |

## Retired: Crew Mode

Crew Mode was the `"crew"` chat-slot mode: one session whose messages became
durable queue entries, a single-flight decision agent that routed each to a
topic, and one continuable sub-session per topic, with results forwarded back
under `↩ re:` attribution. Its control plane lived in `crew_chat.py`; its
design of record is
[`../../request-for-change/rfc-orchestrator-chat-sessions.md`](../../request-for-change/rfc-orchestrator-chat-sessions.md).

It retired in favour of the Crew Members page, which inverts the model: instead
of one nameless session fanning out to topics, each crew is a named member with
its own standing thread. What remains, and why:

- **No ingress.** `"crew"` is in neither `_CREATABLE_MODES` (`chat_handlers`)
  nor `_VALID_MODES` (`chat_folders`) nor the fork override allowlist, so a
  session can no longer be born or switched into it. A caller still sending
  `mode: "crew"` on auto-create gets a plain slot (the value is dropped like any
  unknown mode); on the create and switch endpoints it is `invalid_mode`.
- **Existing sessions come back as plain chat.** `chat_persistence._restored_mode`
  maps a persisted `mode: "crew"` to `""` on both restore paths. The transcript
  is untouched and still renders; nothing is migrated and nothing is deleted.
  The store the mode kept under `<data home>/crew/<folded key>-<digest>/`
  (`queue.json`, `topics.json`, `forwards.json`, `slot_key`) held only routing
  state — it is neither read nor removed, and a reader who wants the disk back
  may delete that directory by hand.
- **Old transcripts keep their shape.** The frontend's `TurnBlock.isCrewReply`
  still honours the persisted `meta.crew_reply` marker so a forwarded topic
  answer in an old session renders outside the collapse pane, as it did when it
  was written. Nothing writes the marker any more.
- **The autonudge crew/member boundary keeps its vocabulary.** `autonudge_authz`
  still lists `"crew"` beside `"member"` in the modes that refuse an outside
  arm. With no slot able to carry the mode the entry is unreachable, and it is
  left in place rather than re-litigating a security boundary in a removal PR.

The sidebar's create-menu entry that used to create a crew-mode session is now
a "Crew Members" door: it opens `/members` when `PREVIEW_CREW` (Settings →
Developer → Feature Previews) is on and lands on that flag's card when it is
off.
