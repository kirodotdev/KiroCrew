# Crews

A **crew** is a named entry in the config's `agents` map. It binds a kiro-cli
agent template plus a workspace, a memory store, a model and a reasoning effort,
and it carries free-text `triggers` that decide whether the orchestrator may
route work to it. The selection path is the `select_crew` MCP tool.

This spec used to own a second thing spelled *crew*: **Crew Mode**, the
`"crew"` chat-slot mode whose control plane (`crew_chat.py`) fanned one
session's topics out to sub-sessions. It is retired — see
[Retired: Crew Mode](#retired-crew-mode) — in favour of the Crew page (Your Crewmates)
(`/members`, served by `dashboard/handlers/members.py` and `members.py` in the
table below), where each crew is a standing agent with its own thread.

A crew is not a *Remote Instance* (see [instances.md](instances.md)), and not
an Issue Radar *crew*, which is that app's own repository work crew
(see [issue-radar.md](issue-radar.md)).

## User-facing terminology

The UI names the feature **Crew** (sidebar rail entry and the create menu's door), the
roster page **Your Crewmates**, one row a **crewmate** (lowercase mid-sentence), the
roster's add action **Add a Crewmate** and the hire gallery **Hire Crewmates**, introduced
by: "Each crewmate is a persistent AI teammate with its own skills, memory, permissions,
and schedule." Those are LABELS only: member ids, the `agents` rows and their binding
fields, `/api/members/*`, persisted files and the Kiro custom agent vocabulary all keep
`member` / `agent`, as does every unrelated `crew` (the crew manager, `crew.*` runtime
config, apps). The lifecycle verbs stay hire / fire / detach / update role.

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

## Owner-reviewed capability inheritance

`agent_capabilities.py` resolves one verified Parent and explicit per-item
`set`, `remove` and `inherit` intent. The owner-only GET, POST preview and PUT
routes at `/api/agents/{name}/capabilities` use schema version 1. Preview ends
in `/preview`; PUT requires its opaque preview token and the GET revision.
Unknown fields, null sets, stale sources and ambiguous names are refused.

Enrollment is explicit. Shared members follow their selected Parent; a legacy
private snapshot starts with every existing row local and every absent Parent
row removed. Restoring one row leaves all other overrides intact. MCP transport
replacement is whole-value; autoApprove is separate. Skills preserve manual
resources and cannot change tool or approval lists. An exclusion still covered
by a wildcard or another approval list is refused instead of claimed effective.
Ordinary upstream changes reconcile through the same resolver. New capabilities,
transport changes and broader approvals stay pending owner acceptance. Local
conflicts retain their usable values: accepting a Parent row moves only the
accepted Parent baseline, an explicit local override on that row keeps applying
on top of it, and an `inherit` or restore on that row adopts the current
Parent value and advances that row's accepted baseline. Selected Parent rows can be accepted for
several already-enrolled members of the same exact Parent in one request. The
editor offers those members as a checklist drawn from the declared crew roster
(the current member excluded); the backend alone decides eligibility and
answers `member_parent_mismatch` for a member outside this exact Parent. The
checklist hint says only that the names are declared members and that
eligibility is verified at review; it never calls a listed member eligible. The
pane's mode badge names the persisted following mode without a `Saved:` prefix;
the separate Saved/"Unsaved draft" badge reports draft state, using the same
"draft" word as the footer, its confirmation and the stale notice. Ticking
Follow changes the draft, not that persisted mode badge. One muted legend above
the row list, rendered once rather than per row, defines the source select's
three states: Inherited follows the parent's accepted value, Override sets this
member's own value, Removed drops it for this member. A shared-reference row
says it references a shared skill or resource with no private copy; it promises
no propagation to running members. The locked Agent Template pane's navigation
button reads "Edit in Capabilities"; the pane title stays "Capabilities".
The preview lists every member the reviewed request covers, the current
member first, and states "no effective value changes" when the effective values
are unchanged, without implying that inheritance metadata is unchanged. Each
Parent checkbox names its save-time action from the draft's actual Source first,
then the saved row state: Override dismisses the update while keeping the override;
Removed dismisses it while keeping the row removed; Inherited takes the Parent
value or removal. An explicit `inherit` draft already takes the current Parent
and advances its accepted baseline even unchecked, so its checkbox only selects
that update for the chosen peers; a nearby accessible hint separates the current
member's save outcome from the checkbox's peer-only effect in two short lines.
Action labels lead with what is kept or taken.
This replaces the generic acceptance label and duplicated selection/outcome prose.
Parent change-kind badges say "Parent added/removed/changed this", separately from
the impact list's Added/Removed/Changed labels and the conflict badge. The shared
and legacy modes both say "Not following parent", with an independent-legacy-
snapshot qualifier for the latter. The visible `local` state label is
Override; its API value remains `local`. The receipt then
names each kept row under the current member, derived only from the reviewed
selection, the server view's conflict flag and the sanitized preview projection
(a row still `local` and present, or still `removed` and absent, with no impact
entry), and shows Override or Stays removed beside its reference; it
never prints a value, never reads the raw draft, and never invents kept-row
details for a peer member, whose rows the response does not project. An
Inherited row that follows a Parent removal is never labelled as a kept local
choice; it is an impact entry when its effective presence changes.
The Follow checkbox places the unchanged-until-save and value/empty-field
preservation guarantee beside the control; one state-carrying helper explains
checked means overrides are editable and unchecked means read-only, replacing
the duplicate enrollment callout. Both are accessible descriptions while the
checkbox's accessible name stays stable. The label explicitly targets the native
checkbox id, so clicking its text toggles enrollment. Conflict and receipt prose wraps at
word boundaries; long code references can break anywhere. The transport option
is "Command (local process)"; the conflict badge says "Conflicts with your
override", distinct from the Override row state. Reload retrieves the server view
without moving the draft's revision; "Use the new version for this draft" moves
that revision and invalidates a prior preview, but does not review or save.
Review and atomic save remain separate steps. Receipt counts use registered
locale-specific plural forms. The approval helper states that
making a tool available does not auto-approve it. A short live helper under the
list selector states where the selected list is stored and which references it
matches; the select and section labels stay short. Empty approval
section headings are hidden, but their selector choices and draft additions
remain available. Hidden transport leaves keep short placeholders; a separate
hint explains that typing changes only the draft, saved hidden values remain
until a replacement is saved, and discarding keeps the original. The footer's
Discard draft opens a nested, always-mounted Radix confirmation rather than
immediately clearing the draft. Cancelling keeps edits and the signed preview;
confirming clears the draft and preview without closing the editor or calling
the server. The editor's separate close guard states in its title that it closes
the editor, names the member whose edits are unsaved, and says no other member
is affected, since one editor holds one draft.

Each owner save writes new private spec identities and switches all selected
bindings through one config-delta publication. This includes changes to accepted
Parent baselines or local intent whose effective values stay unchanged; only a
save with unchanged spec and intent keeps its generation. Failed spec or config
publication keeps the prior bindings, specs, accepted baselines and local choices;
old generations may be marked pending, but staged choices remain private on new
generations. Reconciliation can clear that pending marker without accepting the
failed batch; the owner can review and retry the same selection. After binding
publication, a failed final receipt leaves all selected new generations pending;
reconciliation verifies them without minting replacements. Preview
values redact credential containers. The existing governance sanitizer still
runs at publication. Withheld shortcuts become tombstones, so a later policy
relaxation does not resurrect them automatically.

`prepare_member_capabilities(member, project_dir)` verifies the saved spec and
Parent identity without claiming that a provider loaded it. API runtime state
remains pending or unverified until runtime integration supplies observations.
The existing fork refresh delegates enrolled definitions to this resolver.
Legacy PATCH and direct rebind refuse an enrolled definition rather than
bypassing its intent. Unreadable authoritative state returns bounded
`503 capabilities_unavailable`; the final PATCH guard runs under the spec lock
before bookkeeping. A late legacy publish rebind refusal retains the old binding
and rolls back its staged destination, not a claim that no writes occurred.
Runtime views are projected from allocation-owned state through the public
`SessionManager.capability_runtime_view` facade; response rows expose no mutable
registry dictionaries. The existing whole-reset button explicitly restores the
verified current Parent through the capability transaction. Publish flattens
only the saved valid snapshot, without accepting pending Parent expansions or
exporting inheritance metadata. Publish records its member, source and target
snapshot identities in the existing sidecar before creating the destination or
committing the binding. The destination stays private until a second
config-first transaction verifies its binding, ownership, bytes and current
governance and clears only the temporary lineage. A failed final write returns
the committed template with `warning: publish_incomplete`, matching legacy
publish behavior. Retrying the same name completes that transition, including
after restart or a lost response; changed source/target bytes, a newer binding
or foreign ownership refuse without overwriting anything. Completed receipts
remain for idempotent retries and never enter the shared agent JSON.

Native permission policies that exactly match
the existing allowedTools derivation follow owner approval edits. Custom
permission policies and alternate toolsSettings shortcuts require a separate
review and are refused rather than silently bypassed. Cleanup of superseded
private generations is not implemented by this backend checkpoint, and no
generation is deleted today. A future cleanup must retain every generation that
a member binding names, that a live session's `LoadedCapabilities` stamp names,
that a `CapabilityPreparation` returned by `prepare_runtime` still references
between preparation and the loaded stamp, that a persisted resume record could
lead back to, or that a retained publish receipt names as source or target.
Because a preparation exists before any stamp and holds no registry entry, the
three visible references (binding, stamp, receipt) do not prove a generation
unreferenced. Deletion therefore requires a shared lock or explicit allocation
lease taken by the reconciliation seam that mints generations, plus an audit of
persisted resume references, and it fails closed: a generation whose absence of
references cannot be proven is kept. Binding
updates preserve config.local member overrides and write a narrow delta in the
active layer under base-then-overlay locks. A batch spanning both layers uses
one atomic overlay delta. Enrollment preserves absent fields, null prompt/model
values, custom hooks/settings and the original includeMcpJson choice. Removing
capabilities while provider-global MCP inclusion remains enabled is refused as
unrepresentable; enrollment alone never silently disables that existing source.

Parent selection reuses `agent_spec_path` with an explicit scope directory.
Unrelated malformed files are skipped; duplicate names, a broken exact-name
project claim and a changed pinned source still refuse resolution. Safe response
projection retains arrays and maps and masks credential values. URL userinfo
is masked from parsed username or password on every scheme, without relying on
the shared redactor's known-scheme patterns; revision-bound URL retention still
preserves the exact original bytes. Complete sensitive
`NAME=VALUE` argument assignments are recognized on both sides of `--`; that
terminator stops option inference, not assignment scanning. Retention preserves
the entire original argument, including additional equals signs in its value. The
`agent_capabilities.py` response boundary is registered in the security posture
redaction inventory, so the omission gate checks it with the other outputs. MCP `set`
accepts `retain_paths`, RFC6901 pointers into the complete replacement value.
Each pointer must address exactly `[REDACTED]` and the same redacted scalar leaf
in the current member transport. Empty/root, malformed, overlapping, duplicate
and out-of-range pointers refuse the whole request; any unretained placeholder
also refuses. Omitted fields are removed, not deep-merged. Retained bytes stay
server-side and are covered by preview/revision checks. MCP rows carry an
explicit `managed` flag; absent prompt/model rows remain editable. Rows do not
carry constant `editable` or redundant `locked_reason` fields: managed transport
fields stay read-only while Source and Enabled remain available. Runtime status
is one of pending, unverified, applied or failed; the separate Saved configuration
badge describes persisted configuration, not provider application.

Owned Parents retain the existing dynamic command, hooks and data-home refresh.
That pass cannot add omitted servers or tools and preserves local prompt/model
and resource choices. App namespace transports require a current enabled app's
exact declaration and use its authoritative transport. Safe ordinary Parent
fields (description, welcomeMessage and keyboardShortcut) follow updates; legacy
snapshots retain their explicit local baseline for these fields.

Reconciliation publishes changed bytes under a new private name and atomically
switches the member binding, leaving the old runtime's file unchanged. A no-op
keeps its generation. Failed writes retain pending intent; retry completes it
without modifying an earlier generation. Already-published pending receipts can
finish without another generation. Public revisions are random version ids;
source-content digests remain internal. The prepare seam refuses pending work
and never reports provider application from a successful save. Enrollment
intent lives in the shared `agent_model_state.json`, so an unreadable sidecar
cannot prove any declared member unenrolled: `prepare_runtime` refuses every
crew-member cold start with the closed code `capability_state_unreadable` (the
capabilities API answers `503 capabilities_unavailable`) rather than inferring
legacy mode, and a session that resolves to no crew never reads the sidecar.
An explicit `crew_agent` claim naming no `config.agents` entry refuses with
`capability_member_missing`; an implicit name outside the crew namespace
resolves to no crew and is unaffected.

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

The member side panel's Crewmate summary tab and the editor link to
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

**Named before it exists.** `display_name` is REQUIRED: a hire whose body has no
name, a non-string, or a blank/whitespace one is refused (400
`missing_display_name` / `invalid_display_name`) before anything is minted,
copied, provisioned or enrolled. There is no fallback to the `role` or to the
source file -- a crewmate is a colleague the owner names, and the role is at
most a suggestion the UI places in the field. The name may equal the role when
the owner typed it; spaces and Unicode are kept as display text, the id is minted
separately (`member_identity`). There is no persisted flag for how a crewmate was
named -- every name is a person's, so the row carries none and the thread header
shows no hint about it; a typed name that collides is 409 `agent_exists` -- the
create never suffixes a name. A later inline rename (`PUT /api/agents/{id}` with
`display_name`) changes the label only -- the id, the binding, the private store
and the enrollment record stay. The UI's hire entry point in this step is the Crew
page's **Add a Crewmate** (the empty roster's call to action and the header `+`):
it opens the crew form in the roster's words ("Add a Crewmate", "What this
crewmate uses", **Hire crewmate**), and that form -- template picked, name typed,
Hire pressed -- posts to `POST /api/members` (`KiroCrewAgentsPage` `createMut`,
`fromMembers`), never the plain create, so the roster it returns to lists what was
just added; the **hire gallery** under the Crew page (step 6, `/members/hire`) takes
the entry over, where selecting a card opens the name entry and only a confirmed
name hires. The crew manager's own button and sheet are **Add an agent** -- its
page's vocabulary, never "Add a Crewmate", which would promise a teammate that
never reaches the roster -- and stay a plain create (bind to a shared template)
that enrolls nobody (pinned: `CrewRoster.test.tsx`, a create via
`?new=1&from=members` calls `hireMember` with the picked template and the typed
name and never `createKirocrewAgent`; the crew manager's own button still
creates). Pinned in
`test/test_member_hire.py` (the gate: two named members from one file coexist,
both enrolled, `default` off the roster; a hire without a name -- with or without
a role -- is 400 and writes nothing; a blank name is refused the same way; a
rename keeps id, binding, store and record; a typed collision is still 409; no
moment exists where a row is bound to the shared source -- the copy exists
before the row, the row persists bound to the copy; a failed copy leaves no
member and a retry is clean; an unwind that cannot remove the file keeps its
lineage; the unwind's reference check and unlink run as one section under the
config lock (in-lock callers hand over their locked document); a name sharing
another member's slug is refused, the check runs inside the config lock and
concurrent `Triage`/`triage` hires publish exactly one member; the copy is
reserved and written under the config file lock; a dotted template the listing
offers can be hired; a source that vanishes between resolve and copy is 404 with
nothing written; a row that fails to persist unwinds the copy; a cancelled hire
finishes its transaction; the source must not be another member's private copy;
unhashable kinds and non-string text fields are 400; lineage on the roster) and
`test/test_agents_roster_contract.py` (`crewmate` is the one handler-added bool on
the crew manager's roster).

## Crewmate enrollment (explicit membership)

A **crewmate exists only after a confirmed hire**. Membership is decided by the
**enrollment record** -- `agent_state` sidecar key `::crewmates`,
`{"members": {<id>: {"generation", "template", "hired_at"}}}` -- and by nothing
else: not a row's presence in `config.agents`, not its `source`, its
`display_name`, a directory under `members/`, or a session that ran it. So a new
install with `default`, the built-ins, an app's materialized agents and local
agent files all available lists **no crewmates**: every one of them is an agent a
session can pick, none is on Your Crewmates until the owner hires it by name.
`/api/agents/sync`, template discovery, an app install or update, opening the
Crew page and a restart never write the record; the fire (step 5) removes it.

- **Where it lives.** The sealed sidecar (read-only to sandboxed agents, refused
  to the agent file tools), like the fork lineage and the pending-moves record:
  a record an agent could write would let a `config.json` edit enroll a member
  the owner never hired. `agent_state.get_crewmate_record` /
  `all_crewmate_records` / `set_crewmate_record` / `clear_crewmate_record` /
  `rename_crewmate_record` / `stage_crewmate_generation` /
  `move_crewmate_generation` / `unstage_crewmate_generation` are the only
  accessors; `record_covers_store` is the one predicate every reader applies.
- **The predicate** (`handlers/members.py::enrolled_member_ids`): a row is a
  crewmate iff it has a record that COVERS the row's `memory_store`
  (`agent_state.record_covers_store`: the record's `generation`, or its
  `pending_generation` while a store change is in flight, below). A row deleted
  and recreated under the same id (another store) is not the crewmate the record
  enrolled; a record with no row is inert. Reads are STRICT wherever the
  answer is the roster or a write for a member: `GET /api/members` (an
  unreadable sidecar is 503 `members_unavailable`, rendered as the page's load
  error -- never a clean empty list that reads as a fresh install; pinned:
  `test_an_unreadable_record_is_a_503_not_an_empty_roster`), the DM thread
  open and the rules write (503 rather than "not a member"). Lenient reads
  (logged, no members) are for callers that only DECORATE and have an honest
  empty.
- **Who reads it.** `GET /api/members` lists enrolled ids only;
  `POST /api/members/{slug}/thread` resolves the slug among enrolled members (a
  template or plain crew deriving the slug cannot acquire a DM); `PUT
  /api/members/{slug}/rules` requires enrollment; `GET /api/agents` rows carry
  `crewmate: true|false` so the session pickers group **Crewmates** apart from
  **Agent templates** (picking a template starts a session and enrolls nobody;
  picking a crewmate references the existing identity). The **Agent templates**
  heading is rendered even before the first hire, when it is the only group, so
  a row does not appear to change kind the day a crewmate joins the list
  (`AgentDropdownList.test.tsx`). The memory page offers
  **Open crewmate conversation** only when the private store's owner is on the
  roster (`MemberMemoryPanel` reads it); a plain crew with private memory is a
  session agent with no thread to open, and says so in one line; a roster read
  that FAILS withholds the door and renders the shared error notice (the
  agent hand-off offered only while nothing on the page is unsaved) -- a failed
  read is not "not a crewmate". Sessions, crons and dispatch bindings
  are untouched by membership.
- **When it is written.** Inside the hire's publication
  (`_create_crew(enroll=)`): after the private copy and the store are
  provisioned, `set_crewmate_record(id, generation=<the row's store>,
  template=<source>, hired_at=<UTC>)`, then the row persists; a row that fails to
  land clears the record it just wrote, so the two never disagree.
- **When it moves or goes.** The generation is the row's store, so a
  LEGITIMATE store change through `PUT /api/agents/{id}` -- private memory
  provisioned for an enrolled member on the shared store, a rebind -- moves the
  record with the row in TWO phases, so no failure or crash leaves an enrolled
  row uncovered: before the config write, `stage_crewmate_generation` adds the
  new store as the record's `pending_generation` while `generation` keeps the
  old one (a record that does not cover the prior store is a recreated row's
  and stays; an unreadable sidecar refuses the save, 503 `members_unavailable`,
  row unchanged); after the row landed, `move_crewmate_generation` makes the
  pending store the generation. A write that does not land is unstaged; a phase
  two that fails is logged and costs nothing (the staged record covers the
  landed store; the next store change finalizes it first). The crew manager's
  delete (`_delete_crew_record`) clears the record with the row, as the fire
  does -- inside the delete's locked publication (`update_config_locked`'s
  `after_write`, the cross-process hold still held, where the crew's uploaded
  avatar files are removed too: run after the hold either cleanup could land on
  a member another gateway recreated under the id meanwhile) and scoped to the
  deleted row's generation all the same: a row recreated under the same id,
  same store name included, is a session agent until a hire enrolls it. Pinned in
  `test_member_memory_creation_capability.py`
  (`test_an_enrolled_members_record_follows_its_store_when_private_memory_is_provisioned`,
  `test_a_sidecar_failure_after_the_store_landed_keeps_the_crewmate_on_the_roster`,
  `test_an_unreadable_crewmate_record_refuses_the_store_change`,
  `test_deleting_a_crewmate_through_the_crew_manager_un_enrolls_it`,
  `test_the_delete_clears_only_the_record_of_the_row_it_removed`).
- **Upgrade: nothing is enrolled.** No build before this one wrote a mark that
  only a hire could have written -- an earlier hire verb's private copy
  (`private_to == id`) and `member-` store are exactly what the crew editor's
  fork and private-memory provisioning give a plain crew -- so a roster read
  after upgrade enrolls no row: `default`, `kirocrew-*` built-ins, app- and
  file-synced rows, plain crews, and a private-forked crew on a `member-` store
  alike stay session agents until the owner hires them by name. Nothing is
  deleted, retired or rebound; a record the member-id migration must follow is
  re-keyed with the row (`rename_crewmate_record`). What the upgrader SEES:
  the Crew page's empty state, with a second line whenever the registry holds
  anything besides `default` (`empty_roster_templates`: the existing rows are
  Agent templates, and hiring one by name brings it onto the crew -- the
  **Add a Crewmate** button below it is that hire). A hired crewmate inherits
  a pre-upgrade member's private store the one way any row changes store: the
  crew editor's memory picker, which runs the two-phase store move above
  (`stage` before the config write, `move` after) so the record follows the
  store. **Hiring an existing row under its own name is 409 `agent_exists`**
  (the row still owns the id; the hire never suffixes a typed name), so the
  refusal carries the colliding row's kind — `existing: {id, crewmate}` — and,
  when that row is NOT a crewmate, its text states the collision and the one
  non-destructive remedy — choose another name for the crewmate — and says
  that the existing agent, its sessions and its memory stay as they are. It
  never advises deleting or moving the existing row and never promises a
  memory migration (none is verified for that path). The Add-a-Crewmate form
  renders that case in its own words (`member_id_taken_by_agent`) and the
  plain "a crewmate named X already exists" only when `existing.crewmate` is
  true (pinned: `test_a_hire_colliding_with_a_session_agent_names_the_path`,
  `CrewRoster.test.tsx`).

Pinned in `test/test_member_identity.py::TestExplicitEnrollment` (fresh state
with templates of every source lists no crewmates on repeated reads; a record
for another generation, or with no row, is not a member; an upgrade enrolls
nothing, a forked private copy on a `member-` store included; the two-phase
store move covers the row on either store) and in the hire tests above; the
DM-thread, rules and memory-ownership suites enroll the stand-in rows they
address.

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

It retired in favour of the Crew page (Your Crewmates), which inverts the model: instead
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
a "Crew" door: it opens `/members` when `PREVIEW_CREW` (Settings →
Developer → Feature Previews) is on and lands on that flag's card when it is
off.
