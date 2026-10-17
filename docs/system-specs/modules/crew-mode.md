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

A crew is not a *Remote Crew* — that is another machine running its own Kiro Crew
gateway, which this one reaches through a tunnel (see [instances.md](instances.md))
— and not an Issue Radar *crew*, which is that app's own repository work crew
(see [issue-radar.md](issue-radar.md)).

See also: [context management § 5](../../architecture/context-management.md#5-crew-mode-crew-page),
which restates what a member's DM session carries in its context.

## Components

| File | Role |
|---|---|
| `src/kiro_crew/config/sections_session.py` | `KiroCrewAgentConfig` — the crew record: `kiro_agent`, `workspace`, `memory_store`, `model`, `reasoning_effort`, `display_name`, `description`, `triggers`, `source`, `session_color`, `avatar`, per-crew watchdog overrides |
| `src/kiro_crew/config/loader_agents.py`, `src/kiro_crew/config/loader.py` | `resolve_agent_bindings` (crew to workspace / memory store / template) and `resolve_effective_model` (the default-model precedence) |
| `src/kiro_crew/mcp_core_parts/crew.py` | `_do_select_crew` — the roster and bind bodies |
| `src/kiro_crew/mcp_tools/control.py` | The `select_crew` tool declaration and dispatch |
| `src/kiro_crew/validation_parts/messaging_session_schemas.py` | `SELECT_CREW_SCHEMA` — argument validation for that tool (imported through `kiro_crew.validation`) |
| `src/kiro_crew/members.py` | Per-crew member space: activity log, DM-thread binding, permanent rules, self-maintained briefing, the member turn chokepoint |
| `src/kiro_crew/subagent_parts/agent_policy.py` | `_validate_agent` — what an `agent=` name is checked against, and `UNADVERTISED_AGENTS` (both read and patched through `kiro_crew.subagent`) |
| `src/kiro_crew/dashboard/handlers/agents.py` | The agents handlers' import and patch surface: `GET /api/agents`, the model picker reads and the crew model-pin checks, composed with the `agent_admin` owners below |
| `src/kiro_crew/dashboard/agent_admin/` | Crew CRUD on `/api/agents` (`crew_records.py`, `crew_update.py`, `crew_removal.py`), the roster row serializer (`roster.py`), the Custom agents tab's detail, fork, publish and reset (`agent_detail.py`, `fork_publish.py`, `template_lineage.py`) and the default-agent write (`default_agent.py`) |
| `src/kiro_crew/dashboard/handlers/agent_catalog.py` | Read-only `/api/agents/catalog` execution choices, with separate member and template namespaces |
| `src/kiro_crew/dashboard/handlers/agent_templates.py` | The Custom agents tab's roster (`/api/agents/templates`), create, delete with reference guard, and the read-only rule the detail PATCH applies to definition edits |
| `website/src/pages/overview/AgentTemplatesTab.tsx` | The **Custom agents** tab of `CapabilitiesPage` (Customize): list by origin, edit the shared definition, create, delete, chat-with / enroll (an enroll invalidates `['kirocrew-agents']` and `['kirocrewConfig']`) |
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, thread get-or-create, rules, activity |
| `src/kiro_crew/crew_teams.py` + `src/kiro_crew/dashboard/handlers/teams.py` | Crewmate teams (`teams.json` in the masked gateway-only data-home directory `crew-teams`; `/api/teams`): the Crewmates page's roster grouping and team view — specified under [learn-cron-dashboard](learn-cron-dashboard.md) with the members handlers |
| `website/src/pages/KiroCrewAgentsPage.tsx` | The crewmate roster and crew editor, mounted as the **Crewmates** tab of `CapabilitiesPage` (Customize). The page holds the roster and editor queries, the editor's draft, opening, saving and deleting a crew, the deep links, the template-switch writes and the Danger zone, and composes its owners beside it: the roster cards, rows, toolbar and list (`KiroCrewAgentsPageRoster.tsx`), the binding fields every crew editor shares (`KiroCrewAgentsPageFields.tsx`; consumers import them from the page, which re-exports each), the editor header and the Model, Workspace & memory and Routing pane bodies (`KiroCrewAgentsPagePanes.tsx`), and the unsaved-work guard with its discard dialog (`KiroCrewAgentsPageDiscard.tsx`). The Crewmates page's own crew editor (`components/crew/CrewEditorNextDialog.tsx` + `useCrewEditor.ts`) mounts the same fields from here and keeps its own `INHERIT_MODEL` twin |
| `website/src/components/crew/crewEditorSections.ts` | The crew editor's pane registry, including the Routing pane that edits `triggers` |
| `website/src/components/CrewWakeSection.tsx` | "What wakes this agent" — schedules, deliberately distinct from `triggers` |
| `website/src/components/chat/crewmateBubbles.ts` | A crewmate's chat: what the transcript draws (`filterCrewmateChat`) and the run / corner rule for its bubbles (`crewmateRunPosition`, `crewmateBubbleClass`) |
| `src/kiro_crew/dashboard/warm_task_brief.py` | Owner-return presence, 30-minute in-memory away episodes, durable task-fingerprint claims, two ordinary crewmate summary messages, one bounded background call, and stale-result suppression |
| `website/src/pages/chat/CrewmateMessage.tsx` | One crewmate message in its chat: the bubble alone, placed by its run position (no author line, no avatar gutter) |

Crew creation reports `409 agent_exists` for both an existing name and a
concurrent name collision. The member-titled form uses its translated duplicate
message only for that status and code together. Other conflicts, including
memory and template-ownership failures, retain the API error message; missing
or malformed codes are not guessed to mean a duplicate. Failed creation leaves
the form open with its entered name and selected template intact.

## Execution-choice catalog

`GET /api/agents/catalog` lists configured members and discovered shared templates
without enrolling, pruning or allocating a member. Its one write is the owner's:
the owner's own fetch runs `refresh_materialized_agents(heal_default=True)`,
which may reset a `default_agent` whose template is gone. Any other caller's
fetch never writes config. Each row carries an explicit
`selection_kind` (`member` or `template`); a member and template with the same name
remain separate choices. This projection grants no execution or memory authority.
Member rows retain the existing roster's field allowlist and redaction rules.
Template rows expose only name, kind, scope, provider-template name, description
and source; they do not claim a member memory binding or expose spec paths.

Project discovery uses only the requesting chat's project, selected through
`X-Session-Key`. An unscoped chat or a request without a chat key never borrows
another slot's project. An unknown slot and an app request for a foreign slot
return `404 slot_not_found`. Project templates shadow same-named global templates
according to discovery's existing execution precedence, not member-name precedence.

Private copies and the runtime's two background-only specs, `kirocrew-lite` and
`kirocrew-guest` (`_BACKGROUND_ONLY_FILES`, matched on
the owned file, so a project checkout's own same-named spec stays an ordinary
choice) are withheld from standalone choices. The primary `kirocrew` spec is
offered and leads the template rows: a chat session is a template choice, and the
main managed agent is the default one. This is deliberately narrower than the sync
route's `source != "kirocrew"` exclusion, which decides enrolment as a crew member,
a different question. The other shipped specs (conductor, worker, knowledge,
research, heartbeat, ...) are ordinary template rows.
Lineage is read strictly in addition to discovery's optional display enrichment:
an unreadable lineage file cannot make a private copy appear shared. Discovery,
config or lineage failure returns `503 agent_catalog_unavailable`, not a partial
success that looks like an empty catalog. Existing member records remain listed
when their template is absent, and querying the catalog leaves their configuration
and memory unchanged (apart from the owner's dangling-default reset above). The member-management API (`/api/agents`) and the
synchronization route (`POST /api/agents/sync`) retain their contracts, but the
dashboard pickers no longer call sync: `useAgents` reads the catalog, so opening a
chat, the schedule form or the channel page enrols nothing. The hook returns the
typed list as `choices` (the chat agent pop-up renders it grouped under
**Custom agents** then **Crewmates** -- the template group leads because a session
created without a crewmate runs the default template, so the group holding the way
back from any switch is read first -- each member row wearing the same avatar the
roster draws for it, the origin badge dropped because the header already says what
a row is, and the custom-agents group carrying a one-sentence hint saying what a
custom agent is -- a reusable agent setup -- and nothing about where it runs) and
the same list folded to one row per name, member first, as `agents` for the
name-only consumers (cron `agent_id`, channel and project bindings, the cycle
shortcuts). The pop-up draws the group headers and the custom-agents hint only when it
lists more than one kind: a header that separates nothing is chrome, and the hint
contrasts a template against a crewmate the list must then be showing. The folded
rows keep their `selection_kind`, and `AgentSelector` (the shared roster picker)
groups by it under the same two headers when a caller passes `groupByKind`, in its
own order and with its own hint: the schedule job form does, so a cron's agent field
offers **Crewmates** then **Custom agents** in one dropdown, the custom-agents
group there keeping the longer hint (`group_templates_hint_default_workspace`: the
job runs the template on the default workspace with shared memory and enrols no
crewmate, which is the fact an unattended job needs), and a template pick stores
the bare template name -- the backend's name-first resolution runs an unaliased
template on `default_workspace` and Global memory, so no cron contract changes.
The chrome follows the same one-kind rule, decided on the unfiltered roster so a
filter that narrows to one group keeps its header; the `role="group"` label stays
for assistive technology either way. Callers that do not opt in, and any name-only
roster, render flat as before. Unless the config key
`dashboard.crewmates_in_agent_picker` is on (off by default, read from the shared
`/api/config/kirocrew` query), `useAgents.ts` withholds from `choices` every member
row a listed template already COVERS (`withoutCoveredCrewmates`): the member has no
memory of its own (`memory_store` is `default`) and runs a listed template, which is
the identical binding (the built-in `default` crew is this case). A shared name alone
does not cover: a template pick binds the shared `default` store, so a member with
its own memory stays pickable even beside a template of its name, as does one
running its own private copy, which the catalog never lists as a template.
Withholding those left the agent unreachable from any chat, since neither group
offered it. No crewmate is exempt for being "the
default": a session created without a crewmate runs the default TEMPLATE, which
the catalog lists as its own row (`default_agent` in the catalog response is
`default_template(config)`) and the badge marks, so the way back from any switch
is that template row. With the key off, a stock install's pop-up therefore lists
the **Custom agents** group with `kirocrew` wearing the default badge, and the
built-in `default` crewmate -- shared memory, the `kirocrew` template -- is
withheld as a covered crewmate.
Turning the key on lists every member; the folded `agents` list and the request
contract below are unaffected either way.
A pick sends `agent_kind` with the name on slot create and on
`/api/chat/slots/{slot}/agent`; the slot stores the committed kind, persists it with
the other slot-owned metadata (`SLOT_OWNED_META_KEYS`, so a restart restores a
template pick as a template pick and a later name-only pick retracts it) and the list
projection exposes it, so a same-name member and template are distinct sessions. A
local switch publishes the name and kind together and writes them to history in one
operation. Remote create, switch and adopt carry the kind across the gateway boundary
and mirror the same pair locally. A successful local metadata write makes that pair
restart-safe on either machine. If the peer commits but the local metadata write
fails, the response says `local_persistence: pending`, the live slot stays aligned
with the peer, and the dirty-slot flush retries the local record; until that retry
lands, a local restart can restore the prior pair. A
member DM thread's pin covers the namespace too: the same name picked as a template
is refused like any other re-bind (`409 member_thread_agent_pinned`).
Request and error contract: [learn-cron-dashboard](learn-cron-dashboard.md) → Chat.
The stamp also decides the RUNTIME read of a slot with no execution record yet
(`resolve_session_agent_bindings(..., agent_kind=slot.agent_kind)`): the owner's
create writes the record at once, but a non-owner create (an app token) leaves
the first send -- and the eager spawn -- to resolve from the stamp alone, and a
`template` stamp must never resolve a same-named crewmate's store and pin. A
record, once written, stays authoritative. The kind is part of every binding
tuple the race guards compare (`_slot_binding`, `_current_binding`, the
agent-conflict compare), so a kind change is a binding change.

## Template names the harness cannot activate

A crewmate's private copy is named after the crewmate, and a published or
created template after the user's choice. Three name classes are taken before
either lookup runs: a Windows device basename (`CON`, `NUL`, ...), a stem the
runtime rebuilds on boot (`OWNED_KIRO_AGENT_FILES`, present or not), and an id
the KAS engine keeps for itself (`agent_files.KAS_RESERVED_AGENT_IDS`:
`default` plus the built-in mode ids `vibe`, `spec`, `quick-spec`, `bug-fix`,
`plan`, `autonomous`). The last class exists because the seeded first crewmate
is itself named `default`, and because kiro-cli's v3 engine accepts a
`customAgents` batch under any of those ids without an error and then does not
run it: `default` is left out of `availableModes`, so `session/set_mode` finds
nothing to switch to; a built-in id keeps the engine's own definition
(`origin: bundled`), so `set_mode` would activate the built-in with the
crewmate's name on it. Measured on kiro-cli 2.23.0, with each injected entry
carrying a distinctive description: every case variant (`Default`, `Vibe`,
`PLAN`) and every collision with an on-disk `~/.kiro/agents/<id>.json`
(`kirocrew-conductor`) registered the client entry (`origin: client`, the wire
definition replacing the on-disk one), so the match is exact and
case-sensitive. The fork suffixes past a reserved stem (`default-2`), publish
and create refuse it as `400 template_name_reserved_by_engine` (its own code,
so the runtime-owned stems' `template_name_reserved` is not blamed on the
engine). A template that already carries a reserved id -- the seeded `default`
crewmate's private copy written before these writers refused it, or a kiro-cli
setup carried over when the operator switches to KAS -- needs no rename: the KAS
wire registers and activates it as `agent_files.kas_wire_agent_id(id)`
(`<id>-crewmate`), and every Crew-side record keeps the crewmate's id
([acp-client](acp-client.md), Guard (C)). The dashboard renders the
refusal in the user's language inside the dialog that sent the name
(`components.agentTemplateDetail.publish_name_reserved`,
`pages.overview.agentTemplatesTab.err_name_reserved`), naming the typed name.

## Custom agents tab

The **Built from** pane inside a crew editor edits that crew's PRIVATE copy of a
template (blueprint semantics, below). The **Custom agents** tab under
Customize (page title and sidebar label; route `/capabilities`) is the other
half: it manages the shared templates themselves — "custom agents" in every
user-facing string, the thing a crewmate is **built from** — the files under
`~/.kiro/agents/` a chat or a crewmate runs. The tab sits flat in the pane like
the Crewmates tab: no card, no title row (the rail tab and the page title
already say "Custom agents"), one toolbar row with the filter on the left and
the **New custom agent** primary button on its right (the Skills tab's rhythm),
then the list and detail panes. The roster on the Crewmates tab likewise shows
no page-level private-memory notice and no "New sessions use" default picker
any more; the default crewmate is still marked by its badge on the card / row (a
status stamp, nothing more — a badge that carried a verb ate the card's own name
on a 290px card), and the toolbar carries, once a second crewmate exists, a
**Change default crewmate ›** link beside the view toggle — its title is the
control's own sentence — to the Default crewmate row, ringed on arrival through
`useSettingHighlight`'s late-mount anchor `default-crewmate`; and the
per-binding memory tips stay. The default itself is changed in
Settings → Developer → Kiro Crew config (`KiroCrewCfgTab`), whose agents table
already badges the default: a "Default crewmate" select, rendered whenever a
crewmate exists (the roster link deep-links here even with one), calls
`PUT /api/config/default-agent` and reports a refusal in
an inline `ErrorNotice` beside itself — the table below it keeps the previous
badge until the write lands, so the page never shows a default it did not set.

`PUT /api/config/default-agent` writes one of two defaults, told apart by the
namespace the picker chose the name in (`agent_kind`, as the create and switch
routes take it). A name in the template namespace, or one that is no crewmate
alias, is the **default for new sessions**: the template a session created
without a crewmate runs. It is written to `agent.default_agent`
(`default_template` reads it back), enrolls no crewmate, and is logged as a
`default_template.write` SEL event. It must be an installed user-level
template the picker offers: a project agent, a background-only managed spec and
a name nothing installs answer `400 default_template_not_installed`; an app's
agent answers `409 app_registered_template` (the app removes the file on
disable); when `config.local.json` pins `agent.default_agent` to another
name, so the base write would change nothing a new session reads, `409
default_template_overlaid` names the file to edit, refused BEFORE the write
(a landed base value would take effect, unrecorded, the day the pin goes). The
locked write re-reads the directory under the agents-spec lock: the same owned
file must still declare the name, else `409 stale_binding` (deleted or renamed
between the probe and the write; when the lock FILE cannot be opened -- a
read-only `~/.kiro/agents`, probed before the lock -- the recheck runs unlocked,
since no writer can change that directory either; a lock that opens but is not
acquired within the bounded wait propagates, since the holder is a writer still
at work); another crew's private copy answers `409
foreign_private_copy`, an unreadable lineage `409 lineage_unverifiable`
(checked strictly in that same lock hold, as every binding writer does). A
crewmate alias (`agent_kind` `member`, or an alias name with no namespace) is
the roster's **default crewmate** (top-level `default_agent`: the badge, the
undeletable row) and a different setting; a non-alias name in the `member`
namespace answers `400 default_agent_not_alias` and is never rerouted to the
template write. `GET` returns both: `default_agent`, the crewmate alias (the
Kiro Crew config tab's select), and `default_template`, the default custom agent
(`default_template(config)`), which the shared `['default-agent']` query reads
so the Schedule page's agent column and the Worlds agent rail label an
agent-less job or slot with the template it runs, never the alias. The
chat picker's **Default for new sessions** row reads and writes the template
default: the execution catalog's `default_agent` IS `default_template(config)`,
the badge marks the template row of that name, the ✓ row names that agent
("`atlas` is the default for new sessions"), and a crewmate's chat shows no such
row (a crewmate is never the default for a new session) -- a slot stamped
`member`, or one with no kind whose agent the roster lists as a crewmate; a
kind-less slot writes with no namespace, so the server routes the name. A default template whose spec is GONE falls back to `kirocrew` at
resolution (`default_template`): "gone" is a removed name in the landed,
complete materialized snapshot that the edition does not supply, the evidence
`reset_dangling_default_agent` acts on; absence alone is not evidence, since a
project checkout or the edition can supply a name the directory never declared.

Every binding writer answers the same `409 lineage_unverifiable` when it cannot
verify whether its target is another crew's private copy: crew create
(`POST /api/agents`), both crew-update paths (`PUT /api/agents/{name}`), the
default-agent write, publish and reset. Its `error` sentence depends on the
cause, which `_UnverifiableLineage.reason` classifies, because only one cause
clears by itself:
`ambiguous_template_name` (two or more spec files resolve to the target; the
sentence lists their paths and asks to keep one or rename the others),
`ownership_record_unreadable` (the lineage sidecar `agent_model_state.json` is
not a plain JSON object within the reader's size cap, is symlinked or
hard-linked, or refuses the read; the sentence names the file and the error,
asks to restore it, and warns against deleting it, which would make every
private copy read as a shared template) and `read_failed` (any other read
error, including a Windows sharing or lock violation while another process
holds the file open; the sentence keeps the retry hint). The refusal still
fails closed in every case and writes nothing.

`GET /api/agents/templates` returns every global discovery row, every
externally controlled string rendered through `_roster_mask` — the control
`GET /api/agents` and the chat catalog apply — so a credential- or
exfil-URL-shaped description, model, package, skill, MCP server name or
reference label arrives as the sentinel, and a row whose `name` or `filename`
would itself be masked is left out (as the catalog leaves it out of the
picker); the delete refusal's `references` list is masked the same way. It
adds two fields. `read_only` is `null` for a template the user owns, or names why it
cannot be edited or deleted here: `package` (the package rewrites the file on
its next install), `runtime` (`OWNED_KIRO_AGENT_FILES`, refreshed by the
runtime — and any row with no spec file beneath the agents directory, such as
an edition catalog row whose `filename` is empty or foreign: the runtime
supplies it and no action here has a file to write, so it is never offered an
edit or delete that could only answer 404), `markdown` (a JSON round-trip would lose fields), `private_copy`
(it belongs to one crew's pane, where reset and publish keep its lineage
straight). `used_by` lists what still points at the template — each crew whose
`kiro_agent` resolves it, the default agent (`agent.default_agent`, a TEMPLATE
name; the top-level `default_agent` is a crew alias `load()` normalizes onto
`agents`, and the template that crew runs is already counted as that crew, so
a template merely sharing the alias is not "the default"), each schedule that DISPATCHES it
(through `cron.dispatched_agents_from_disk(loadable_only=True)`, the ONE walk
that encodes the dispatch rule for both this guard and `kirocrew doctor`'s
`job_agent_names_from_disk` wrapper — a multi-entry `agent_sequence` over a
dormant `agent_id`; otherwise the captured `execution_context.template_id`
when the record carries one, which is where the dispatcher reads the template a
job runs (a schedule created from a template chat with no `agent` argument
names its template ONLY there, `agent_id` staying empty), and `agent_id` only
for a legacy record without one; a script or command job over neither; the guard asks only
for records the scheduler could build, since a record with no `schedule` never
fires and must not pin a template, while doctor reads every name on disk; one
row per job), each chat folder whose `default_agent` pins it (what every new
session filed there starts on), each webhook token whose `agent` pins it (its
calls are refused once the agent is gone), each private copy forked from it —
and is the same list the delete guard evaluates, so the tab shows before a
delete what a refusal would say. Each holder in the tab's usage line is a link
to where it is held (a crew or a private copy to that member's pane, the
default to the Crewmates tab, a schedule to the Schedule page, a folder to the
chat sidebar, a webhook to the Webhooks page — `referenceHref`), so clearing a
reference is one click away rather than a hunt. The folder pins are snapshotted from the
dashboard's folder store on the event loop (`state.read_folders`) before the
roster is built in the discovery executor. An open chat slot that picked the
template (`agent_kind: "template"`) is deliberately NOT a reference: a slot is
a conversation on screen, not a configuration that redirects future work, and
a guard whose answer depended on which tabs are open in which window could not
be reasoned about from the roster — the decision and its consequence are
stated in the module docstring of `handlers/agent_templates.py`.

The delete guard's reference check and unlink are ONE off-loop critical
section under every lock the reference stores' writers take — the shape
`_unlink_copy_unless_referenced` in `dashboard/agent_admin/template_lineage.py` established. The
folder store lock is held across the whole section (`state.hold_folders`, a
snapshot-handing hold that may hop off the loop, added for this), so no folder
pin can commit between the check and the unlink; inside it, the `config.json`
advisory lock and, nested, the `config.local.json` overlay's own lock are held
through `update_config_locked` (writing nothing), which is cross-process — a
CLI `config set` or another gateway is excluded, not just this loop's
handlers — and the EFFECTIVE config (base with the overlay merged) plus the
fork sidecar (whose writers run inside the same config hold) are read under
them; the spec lock wraps the unlink itself. Nothing in the section runs on
the loop, and both mutations — the create's write and the delete's section —
run through `drained_to_thread`, so a request cancelled mid-write drains the
worker before the locks unwind: the file is never committed while the cache
refresh, the dangling-reference report and the audit row are abandoned. The
create's locked check re-scans by stem AND declared name (`_find_infos`), so
a package install landing `Pkg-foo.json` declaring `foo` after the pre-lock
probe refuses `foo.json` (`409 name_taken`) instead of writing an ambiguous
name.
Webhook tokens are read inside the section and their WRITERS commit under
the same spec lock: `hooks._commit_pinned_token` re-verifies the pinned agent
inside `agents_spec_lock` before `token_store().create` / `.update` (a re-pin;
a label or enabled change has nothing to serialize with), so a mint cannot
validate against a file the delete is about to remove — the lock order (spec
lock, then the token store's file lock) matches the delete's. Schedules are
pinned the same way, with the scheduler's OWN lock: the store's cross-process
advisory lock (`.crons.lock`, the ONE implementation `cron.cron_store_lock`,
which `CronService._file_lock` and so every store mutator delegates to) is
held from the walk that finds no dispatching job through the rename, innermost
in the delete's order (folder hold → config → overlay → spec lock → store
lock; the scheduler's writers take only the store lock, so no inversion), so a
schedule cannot be saved against the template between the check and the file
going. The hold is bounded like the mutators' (`CronStoreBusy` after the spin
budget → `503 schedule_store_busy`, nothing unlinked, retryable). A writer that
bypasses the lock (a hand edit) is still caught observably: once the file is
renamed the store is re-read, and a schedule that landed anyway is named in a
WARNING and a SEL row (`agent_templates.delete`, outcome `dangling_reference`,
the holders in `resources`), so the operator can repoint it before it fires. A
`crons.json` that is PRESENT but unreadable is
not "no schedules": `cron.dispatched_agents_from_disk` raises
`CronStoreUnreadable` and the delete answers `503 schedule_store_unreadable`
with nothing unlinked, since a repaired store brings its jobs back naming
whatever they named — but only BEFORE the unlink: once the file is gone the
post-delete re-read can only WARN (the delete has happened, and the client
must not hear "failed" about a file that is gone). The unlink itself is a
rename: the spec is retired to a one-deep tombstone beside itself,
`<name>.json.bak.<epoch>` (`_tombstone`: the rename to a fresh grave comes
first — a same-second twin gets a `.<n>` suffix — and earlier graves of the same
file go only after it succeeded, so a refused rename leaves both the live file
and the previous recovery copy in place; the sweep matches a genuine grave only,
`<name>.bak.<digits>[.<digits>]`, never a spec — a glob's `*` crosses dots, and
`foo.json.bak.5` is a legal template name written to `foo.json.bak.5.json`), a suffix discovery never reads and the agents-directory
janitor already treats as an aged backup, so the tab's one irreversible action
is undoable by hand; the SEL row names the tombstone. A row's `filename` is a discovery string,
never a path: the
delete resolves it only as a plain basename to a regular file directly under
the agents directory (`_spec_file_beneath`, checked before the locks and
again under the spec lock) and answers 404 otherwise, so an edition catalog
row carrying an absolute or traversing `filename` names nothing to unlink.
The name is resolved the same way a PATCH resolves it — `_find_infos` walks
the raw spec files (`iter_agent_spec_files`, the walk
`_agent_detail_candidates` uses), never the deduplicated roster, and returns
EVERY file that reaches the name (by declared `name` or by stem); the delete
answers `409 ambiguous_template_name` when more than one does, never the
first hit: `foo.json` declaring `bar` beside `bar.json` declaring `baz` would
otherwise let a delete of `bar` unlink whichever file the scan listed first,
and two files both declaring `bar` — which the roster collapses to one row —
would let it unlink the survivor as if it were alone. A row pairs a name with a filename, and both are
provider strings, so under the spec lock the FILE decides: it is re-read
(`_read_agent_spec`) and classified from its own contents the way discovery and
the definition PATCH classify a file (`_global_agent_info` plus the fork
sidecar); the requested name must be one the file itself answers to (declared
name or stem, else 404), the ambiguity check is RE-RUN under the lock (the
name must still reach exactly the file about to go — a second claimant landing
after the probe, a package install say, is `409 ambiguous_template_name`, not
an unlink of the user's own file), the file must be deletable from this tab
(else `409 template_read_only` with the file's reason), and the file's own
declared name joins the aliases the reference guard evaluates. An edition row saying `rogue`
over `victim.json`, or calling a package file plain, unlinks nothing.
Create applies the same two-layer binding check before writing:
a name bound only in the overlay is refused (`409 name_bound`) like one bound
in the base, and a duplicate re-reads its SOURCE inside the spec lock (the
fork/publish shape) so a save that lands between the pre-lock probe and the
write is what gets copied. Both mutations also keep the DISPATCH snapshot
(`_materialized_kiro_agent`, what "Chat with this custom agent" and every
template-bound turn resolve through) current, not only the roster cache: a
create publishes the new name at once (`publish_materialized_agents`, a
loop-safe set union) and then schedules the off-loop rescan, so a slot created
before the rescan lands is not normalized — and durably stored — onto the
default agent; a delete AWAITS an off-loop `refresh_materialized_agents`
before answering, since a removal has no publish shortcut and a deferred
rescan would leave the deleted name bindable until it ran. A successful create or delete emits its own
operation-labelled SEL line (`agent_templates.create` / `.delete`, outcome
`ok`) beside the middleware's request-level record; the owner gate logs only
denials.

The tab groups rows as Mine / Crewmates with their own copy / From packages /
Built-in (`pages.overview.agentTemplatesTab.group_private_copies`; the group
carries a one-line gloss under its heading, `group_private_copies_hint`), and a
private-copy row is described in the tab's own words — "Crewmate X’s own copy
of Y" (`override_line`) — not the fork-written "private copy" sentence, so one
object has one name on one screen. Its read-only lead is "Crewmate's own copy"
(`read_only_lead_private_copy`), and Duplicate yields "Your custom agent"
(`your_template`), an editable custom agent of the user's own. The shared
`lib.templateSource` badge the crew's **Built from** pane shows for the same
file still reads "Crewmate override" (`lib.templateSource.private_copy` in
`lib/templateSource.ts`). The tab lets an owned template's description, model,
prompt, tools and auto-approved tools be edited as one draft saved through the
detail PATCH — sending ONLY the keys the draft changed against its baseline
(tools and their marks together), because every key the server receives is a
statement: a non-empty `model` is read as an explicit pin and flips a managed
template's `model_managed` off, so a prompt-only save must not resend the
concrete model string a managed spec carries (the definition keys are refused on a read-only spec, `409
template_read_only`; when two files claim the name EVERY PATCH — model and
skills included, since each rewrites one file — is `409
ambiguous_template_name`, checked before the locks and again under the spec
lock so a claimant landing after the scan refuses rather than overwrites the
stale match; each tool entry is capped at `MAX_TEMPLATE_TOOL_CHARS`),
and edits skills through the same `AgentSkillsEditor` the crew pane uses.
Skills save on their own and are NOT part of the draft: on each save the tab
writes the returned list into the detail cache (keyed by the name the save was
for) BEFORE invalidating the `['agent-templates']` prefix, so a second toggle
landing in the invalidate-to-refetch window starts from the saved list rather
than the stale one and cannot PATCH the first edit away; the detail query
shares that prefix, so the editor
reseeds from a refetch only while the draft is clean — a dirty draft is never
overwritten by a background refetch. Creating a template is a row switch and
is guarded like one: **New custom agent** asks before discarding a dirty draft,
and a successful create drops the previous draft and baseline BEFORE
selecting the new row, so the reseed runs and Save can never write template
A's edits under template B's name. A successful delete drops the draft and
leaves the detail pane (`closeDetail`), so on a narrow viewport the list
returns instead of an empty detail over a hidden list with no Back control,
and says "Deleted “{{name}}”." in the save bar's slot over the next row — named,
so the line cannot read as describing the row the list selects next. The
tab's own in-app links — a holder in the usage line, **Open crewmate**, the
refused-delete dialog's Open — ask the shell's leave gate first
(`useGuardedLeave`, with the target so a link to the current page skips the
ask), since the layout's guard covers only the exits the shell owns; a
`beforeunload` warning is armed while the draft is dirty and only then. The
lazy tab chunk sits inside an `ErrorBoundary` in `CapabilitiesPage`, so a
stale chunk request after a deploy degrades to the tab, not the dashboard.
The read-only rule for a PATCH is decided
from the targeted FILE (its name and declared `name`, classified the way
discovery classifies a row, plus the fork sidecar), never by looking the
declared name up in the deduplicated roster: `atlas.json` beside
`SomePkg-atlas.json` keeps only the package twin there, and a lookup would let
the package file through as if it were the plain one. Resources and MCP
servers are shown read-only — as plain rows, not chips, since a chip reads as
something to click: skills are a computed view over `resources`, and
an MCP server is a capability grant with its own admission path. Auto-approval
marks are advisory: the governance sanitizer still withholds an entry the
ceiling may speak to. A read-only template's banner leads with a short bold reason
(**From a package** / **Built in** / **Markdown file** / **Crewmate's own
copy**) so the four states read apart at a glance, carries the reason
once and **Duplicate to edit** beside it; a read-only prompt renders as a
visibly locked dashed block (padlock, `<pre>`), never a disabled textarea that
looks like a normal editor; creating (`POST /api/agents/templates`,
blank or a lineage-free copy of any installed template) refuses a name an
installed spec or a crew binding already resolves, and answers `409
ambiguous_template_name` when two files declare the name — for the SOURCE
too: the pre-lock probe only chooses a path, and under the spec lock the
source name is re-resolved and must reach exactly that file (a second claimant
or a replacement landing after the probe refuses rather than copying a
definition the probe never saw). The detail header
holds two controls — **Chat with this custom agent** and an overflow menu (enroll,
duplicate, delete); **Chat with this custom agent** creates a slot with
`agent_kind: "template"` (its title says it is a one-off chat that creates
nothing; while the draft is dirty it stays enabled and asks the same discard
confirm a row switch does, rather than greying out with the reason in a
title); **Enroll as
crewmate** is the ordinary `POST /api/agents` with the template as
`kiro_agent`, and its menu row says what it starts (a crewmate with its own
memory, nothing running). A successful enroll invalidates `['kirocrew-agents']`
and `['kirocrewConfig']`: the QueryClient sets `staleTime: Infinity` and
`POST /api/agents` pushes no `refresh` frame, so without it the Crewmates tab
would keep serving a cached roster that lacks the new crewmate. The unsaved-changes bar names how many crewmates a
save affects and says in one line what a save does and does not reach (save;
new chats use it at once; chats already running keep what they started with) —
the Customize header carries no Apply & Restart button, so nothing on the page
contradicts that line (the button lives on Connections, where MCP server
changes — the edits a running chat cannot see — are made). The save
bar wraps its buttons onto their own row below a readable text-column minimum
rather than crushing the text at a 320px viewport. A successful save is confirmed in the bar's own
place (the bar unmounts; a `role="status"` line takes its slot for a few
seconds, cleared by the next edit or a row switch), and a refused save is
reported IN that bar, in place of the instruction and beside the Save button
that produced it (an
inline `ErrorNotice`, per `errors-use-error-notice`, with the no-hand-off
comment naming the unsaved draft;
cleared by Discard — which, while the draft is dirty, asks the same
"Discard unsaved changes?" a row switch does, so one click cannot erase a long
prompt edit for good — a later success or a row switch): the pane above scrolls
and the bar does not, so a notice at the top of the pane would be off-screen
for anyone who was editing the prompt or tools. A rejected detail read
renders its `ErrorNotice` ahead of the loading state (the draft stays null on a
rejection, so a draft-gated loading branch would mask the error); MCP server
rows render only string-valued `url` / `command` / `type` fields, since a spec
is a hand-editable file. The skills editor carries its own heading, so the tab
adds none over it. A tool pill is three legible parts — the name, a worded
state tag (`auto-approve ✓` / `asks first`, with a switch glyph and the
shared button recipe — strong border, shadow, press scale — so it reads as a
control before hover) that IS the auto-approval toggle, and a
divider-separated remove control — so neither click can be mistaken for the
other without reading the caption; the caption still says, in visible text,
that clicking a tag switches the tool and that removing or toggling changes
nothing until saved, for the reader who does not try or hover — on a read-only
template, whose tags are inert spans, the caption says "read-only here" instead
(like Resources), never an instruction to click. The list badges say what a count is ("Runs 2 crewmates" / "1 crewmate
with its own copy" — the same words as the group heading and the banner, so one fact is
not phrased three ways), not only how many, and the usage line uses the same words for the same fact ("Runs 2 crewmates", not "Runs as"), and both counts wear the same muted pill as the model (a normal state, not a caution — and a colored pill worded like the usage line's link would read as a second control); the read-only reasons and the missing-prompt note say outcomes ("{{product}}
replaces this file when it updates"; "{{product}} supplies it when the
agent runs — duplicate it to write your own") rather than mechanism. (`RestartButton`,
the Apply & Restart control with its confirm that names what a relaunch keeps
and stops, is mounted in the Connections page header — right of its
Services / MCP servers tablist, outside the `tablist` role — not on Customize:
a saved custom-agent edit reaches new chats without it, whereas a newly enabled
MCP server reaches an already-running chat only after a relaunch.) The MCP servers heading carries a plain-word gloss beside
the acronym ("external tool connections"). The Add tool
input keeps the words "Add tool" visible as its label while open (one control
in two states, not two controls), is wide enough for its example placeholder (`fs_write or @github/…`)
and offers a datalist of kiro-cli's native tool names, Kiro Crew's
background-subagent tools (`@kirocrew-core/spawn_run`, `spawn_list`,
`spawn_status`, `spawn_steer`, `spawn_continue`, `spawn_release`), plus every name the
template already grants (offered, not enforced). Until an editable template
gets the whole spawn family (it grants `@kirocrew-core`, or `*` with
`kirocrew-core` declared, or all six refs), a muted line under the
tools says what background agents give it (`spawn_tool_hint`) beside a one-click
**Allow background agents** button that adds every spawn ref the draft still
lacks (`spawn_tool_add`, with `@kirocrew-core/spawn_*` as its tooltip rather than
in the label), so
`spawn_status` and `spawn_release` come with `spawn_run`. A tools save that names a
Crew control-plane server (`kirocrew-core`, `kirocrew-cron`) by an explicit
`@server` or `@server/tool` ref -- with or without `*` beside it -- that the spec
does not declare also writes that server's managed `mcpServers` entry
(`agent_templates.control_plane_declarations`, added to the spec re-read
under the spec lock so a concurrent `mcpServers` edit survives): kiro-cli mounts a server
only when the spec both grants and declares it, so the grant alone would mount
nothing, and with both a custom agent's `spawn_run` is the same native
session-attached spawn the built-in agent has (inline task box with Stop, an
explicit `agent` / `crew` pin, completion delivered to the chat). The entry
carries `"type": "registry"` while `agent.mcp_registry_mode` is on, because a
registry-mode client drops every unmarked entry. Only the
control plane is declared this way, because its launch is re-derived from the
managed source at every session start; a third-party or opt-in server keeps
its own admission path, `*` alone declares nothing, an existing declaration is
never replaced, and removing the grant leaves the declaration in place
(ungranted, it mounts nothing). A save whose named server cannot be declared --
`mcpServers` is not an object, or the install cannot resolve the managed launch
-- is refused `409 control_plane_not_declarable` with a sentence naming the
server and the fix, shown in the save bar, and nothing is written (the check
runs before the model sidecar is touched): a grant that mounts nothing is never
saved silently. The grant
is a mount, not an auto-approval: each spawn still asks unless the author ticks it. The enroll row says where
the result lands (under Crewmates); the save bar's two buttons never wrap or
shrink, and its instruction names the button by its label ("Save custom agent"). A private copy is never a dead end: its
banner offers **Open crewmate** (the crew's **Built from** pane, where the copy is
edited, reset or published) instead of Duplicate to edit, and the refused-
delete dialog names the copy with its crew as a gloss and links to the same
pane, and its usage line says what it is ("Crewmate X’s override of Y")
rather than that nobody enrolled it; its banner ends "open the crewmate to
edit it", the same words as the button beside it, not a fourth surface name.
A list row's model badge with no stored model carries the editor's "auto
(backend default)" wording as its title, so `auto` is not read as a model
name. The create dialog opened from a Duplicate
affordance hides the blank/duplicate choice. A list row's package provenance
is plain text ("Package X"), not a pill, and its padlock carries the read-only
reason as a title; the per-template box carries the shared-edit warning and
the tab has no glossary — the rail tab's description ("What a crewmate is built
from. Pick one when you add a crewmate.") is the one definition, and the
override is defined under its own group.
The delete confirm states the blast radius the guard already knows (nothing
points at it; only the file goes; chats keep their history) rather than a
bare file-removal warning — and is asked only when the row shows no holder:
when `used_by` is non-empty the claim would be false and the server would
refuse anyway, so Delete opens the reference list directly with the holders in
hand, no confirm and no request (a holder that lands later still surfaces
through the server's own refusal). A read-only template
with no stored prompt shows a note that the runtime supplies it, not an empty
disabled editor, and no "0 characters" count beside it. Nothing on this tab enrols a member as a side effect.
The usage line under the header
names every holder kind the guard counts (crewmates, default agent, schedules,
chat folders, webhooks, private copies), so nothing is first heard of when a
delete is refused; **Create and edit** refetches the roster before selecting
the new row, since the auto-select effect replaces a selection the roster does
not list; the create dialog is titled **Duplicate <name>** when opened from a
Duplicate affordance, so the three duplicate entry points read as one flow.

## Owner-reviewed capability inheritance

`agent_capabilities.py` resolves one verified Parent and explicit per-item
`set`, `remove` and `inherit` intent. The owner-only GET, POST preview and PUT
routes at `/api/agents/{name}/capabilities` use schema version 1. Preview ends
in `/preview`; PUT requires its opaque preview token and the GET revision.
Unknown fields, null sets, stale sources and ambiguous names are refused.
Following is reversible: POST `/detach` with only the GET revision drops the
inheritance intent and leaves the member on its private copy
(`legacy_snapshot`), whose bytes are the saved effective spec, unchanged, so
no value is dropped or loosened and later Parent changes stop reaching it.
It works with the Parent missing, and refuses `not_inherited`, a stale
revision, a pending save (`materialization_pending`) and a hand-edited file
(`materialization_changed`), so only reviewed bytes become the member's own.
It also refuses a setup an independent copy cannot keep:
`detach_tool_limits_unkept` when a launched server turns single tools off
(chat start checks that against the backend only for a saved setup), and
`detach_removal_unkept` when the Parent is a template Crew owns and the
member removed something (the fork refresh re-applies that template's
plumbing). Owned is judged the way the fork refresh judges it
(`fork_origin_is_owned`: the lineage's origin NAME, whatever scope the
Parent came from), not only by the global-scope parent check, so a
project-scoped Parent at an owned name is refused too. A copy that still reads the global mcp.json (`includeMcpJson` unset) needs the owner's confirmation (`accept_mcp_json` in the request; without it
`detach_mcp_json_unkept`), because the refresh pins that merge off once no
intent is saved; the view reports it as `detach_mcp_json_loss` and the confirm
dialog says so. The view carries the refusal that
would apply as `detach_blocked`, so the pane disables the button and shows the reason before the dialog opens. The pane renders
these refusals as the save that clears them (drop the limit, or set the
removed rows back to Inherited, save, then stop), not as draft advice: no
draft can be open when stopping is offered. The only write is the sidecar
entry (the spec file is never touched), so a failed write leaves the intent
and the reviewed bytes in place. A governance answer newer than the save is
not refused, since a member whose Parent is gone could never reconcile it:
the fork refresh that follows governs the kept copy like any private copy
without a saved intent, and fails closed if it cannot. The spawn gate
(`hold_spawn_gate`) is held from before the write until that refresh ends, so
a fork-backed session cannot start on grants the refresh has not re-filtered. For a copy of an
owned template that still tracks the shipped model and chose its own, the
same write turns that tracking off so the refresh keeps the chosen model.
The pane's **Stop following the parent** button
sits beside the mode badge while a member follows, with a confirm dialog that
names the crew editor's template pane as where the kept copy is edited (this
pane shows an independent member read-only); enrolling again keeps every
local choice, as a legacy enrollment does.
The pane shows its stale notice ("The saved version changed ... Reload from
server") only for the 409 codes a reload can resolve, which the backend names
`stale_*` or `*_changed` (`stale_revision`, `stale_binding`,
`parent_identity_changed`, `materialization_changed`, ...).
Any other 409 refuses the draft itself and repeats on reload, so the pane
names it instead: `alternate_permissions_require_review` (an approval edit on
an agent file whose permission settings Crew did not derive) gets its own
copy, and every other code is shown by name with the draft kept.
A Review or Save 400 with a code is a refusal of the draft's own shape and
also repeats on reload, so it gets the same named-code copy;
`invalid_approval_ref` has its own copy naming the refused entry, which the
400 body carries as `ref`.
Only a Review or Save 409 or 400 gets that draft-refusal copy. A 409 on the load
itself (`parent_missing`, `foreign_private_copy`, `private_template_shadowed`,
`source_unreadable`) can arrive with no draft at all, so it keeps the load
notice.

The preview view carries `withheld_approvals`: each `allowedTools` or
`autoApprove` entry the draft sets to Override that the projected spec will not
carry, as `{section, id, reason}`. `reason` is `governed` (the governance
filter left a remove tombstone), `not_connected` (the projected spec has no
enabled MCP entry for that server: missing or `disabled`), `managed` (a
`MEMBER_PLATFORM_SERVERS` server, whose transport is rebuilt without
approvals) or `unexplained` (none of those). The pane lists them first in the
review; when any is `not_connected` it offers a button that opens the MCP
Servers tab with the draft kept.

The GET, preview and PUT views carry `backend_warning`: `null`, or
`{code, backend, setting}` when a member chat on the backend that setting
resolves to would refuse the saved (or previewed) spec. Two settings are read,
as `members.select_provider_backend` resolves them: `agent.member_acp_backend`
(member DMs) first, then `agent.acp_backend` (the member's Slack threads, cron
jobs and spawned sessions, which reach the same gate through
`resolve_crew_identity`). A spawn with an explicit backend override is chosen
per call and is not covered.
`harness_unsupported` means the backend is outside
`ACP_BACKENDS_MEMBER_CAPABILITIES`; `tool_limits_unsupported` means the spec
turns off single MCP tools (`servers` lists them) and the backend is outside
`ACP_BACKENDS_MEMBER_TOOL_LIMITS`; `servers` is present whenever the spec has
such servers, a harness refusal included. `member_backend_gap` is the rule, read from
the same sets as the chat-start refusals, and `tool_limited_servers` is the one
definition `prepare_member_capabilities` also uses. It is a warning only: the
save still lands, because the settings can change afterwards, and the copy says
so first. An unenrolled member gets none, since chat start gates only a saved
spec. The pane shows it on the Review section and on the saved state, in three
forms. `harness_unsupported` on `agent.member_acp_backend` shows the
`config set agent.member_acp_backend kas` command on its own line with a copy
button, since no dashboard page edits that setting and kas (the member default)
loads saved capabilities, plus a second step to clear `servers` when present,
since kas would refuse those next. It never suggests the Kiro backend, which
takes member dispatch away. `harness_unsupported` on `agent.acp_backend` names
the affected entry points (Slack threads, scheduled jobs, spawned tasks), says
member chats are unaffected, and offers no one-line switch, because that setting
moves every session. `tool_limits_unsupported` names the same entry points when
its setting is `agent.acp_backend`, and in both forms carries a button that opens
the MCP Servers tab of the same pane. Every form opens "Saving works", which is
true on the Review section and on the saved state alike.

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
no propagation to running members. The locked **Built from** pane's navigation
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
mode says the member runs the parent template as-is with no overrides, since it
runs the Parent's own file and has no intent; the legacy mode says "Not following
parent" with an own-copy qualifier. The visible `local` state label is
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
preservation guarantee beside the control; one helper names checking the box as
the way to follow the parent and add the member's own overrides on top, and says
the fields below stay read-only while it is unchecked, replacing the duplicate
enrollment callout.
Both are accessible descriptions while the
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

A member agent file that changed since its last review (its digest no longer
matches the stamped `materialized`) makes `prepare_member_capabilities` raise
`materialization_changed`. The chat error row carries that code and the
member, withholds Resume (a retry repeats the same check), and offers a fix
that opens `/capabilities?tab=crews&crew=<member>&pane=capabilities`. There,
Review and Save with an empty draft restamps the file when every changed key is
one the review can vouch for (`_unvouched`); a changed key the review cannot
show refuses with `409 unreviewable_drift`, naming the file
(`_stamp_reviewed`).

## Crew records and binding

A crew lives only in `config.json` under `agents.<name>`. It is not a kiro-cli
agent file: `kiro_agent` points at one. A **session** is started from three
things -- a template, a folder and a memory store -- and a crewmate pick is the
shorthand that fills all three from its row. `resolve_agent_bindings` turns the
selection into `ResolvedBindings`, in this order (#18328):

1. the named crew, when it is a key of `config.agents` and the stated namespace
   is not `template`: its template, workspace, memory store and model pin;
2. otherwise a **template session** on `default_workspace` and Global Memory V1,
   with no crewmate model pin and an empty `resolved_alias`: the **materialized**
   kiro agent the name declares (an app-registered agent under the user's
   `~/.kiro/agents/`, or a project agent), else the **default template**
   (`default_template(config)`: `agent.default_agent`, `kirocrew` when unset).
   A name that matched neither sets `requested_resolved` False so a caller never
   advertises a binding that is not running; the default template itself always
   resolves, even on a cold materialized snapshot.

A plain session -- no name at all -- is therefore the default template, never
the `default` crewmate: that row is one crewmate among the others, talked to in
its DM thread, and may bind a different template than `agent.default_agent`.
The top-level `default_agent` alias names the roster's default crewmate and is
not consulted for a session that picked nobody; an agent-less slot create and an
empty `/agent` switch stamp the default template in the `template` namespace.
An unresolvable workspace falls back to `default_workspace`. Memory identity
resolves exactly: the reserved `default` assistant uses Global Memory V1;
existing V1 members keep their declared V1 binding.

A new crew gets a **workspace of its own**, the way it gets its own member memory
(`member_workspace.py`). A create that names no workspace (`POST /api/agents` with
`workspace` absent or `""`; `kirocrew agent create` without `--workspace`) binds
the crew to a fresh `member-<slug>` workspace whose directory is
`<data home>/workspace-member-<slug>`; a same-slug collision takes a numeric suffix.
Naming a workspace is the explicit opt-in to share one, stored as given the way
every create path always stored it (an undeclared name resolves to the base
directory at use, per the fall-through above). `provision_member_workspace` records
the allocation on the caller's config snapshot only; `persist_member_config(create=True,
new_workspace=<name>)` publishes it in the SAME locked mutation as the record,
re-checking the name and directory against the document under the lock
(`workspace_exists` / `workspace_dir_in_use`, 409), making the directory through
`materialize_workspace_dir` and tightening it to owner-only like everything else a
boot step creates under the data home. A create that fails afterwards calls
`retire_unpublished_workspace`, which restores the snapshot; a directory the locked
step already made is left in place by the same rule as every other workspace
create. Every create path takes these steps: the dashboard, the CLI, the first
crewmate and an app's `ensure_team`. Crews created before this rule keep the
binding they have; deleting a crew leaves its workspace, like its memory store,
on disk and in `workspaces`. The workspace is the crew's own data directory; it
does not become the `project` of the crew's sessions.
A template has no workspace of its own, so a worker template a crew opens with
`session_create` (`agent="kirocrew-worker"`) inherits the crew's workspace along
with its store; naming another crew bound elsewhere stays refused
(`agent_workspace_mismatch`, see session-control.md's refusal table).
The first crewmate (config key `mate`, `agent_files.ASSISTANT_MEMBER_NAME`,
shown as Mate while its `display_name` is empty) is an ordinary crewmate. Every
install, fresh or upgraded, creates it once (`agent_materialization/first_crewmate.create_first_crewmate_once`,
called from `rebuild_agent_config`) bound to the same template a user-created crewmate
gets (the configured `agent.default_agent`, else `kirocrew`), with its own private V2
store and its own workspace from the start: the row is built in memory, given a store by
`provision_member_memory` (the ordinary, collision-reserving identity: a deleted
predecessor's retained DM binding or another legacy member's slug is never inherited),
a workspace by `provision_member_workspace`, and
published by `persist_member_config(create=True, new_workspace=…)`, the steps every explicit member
creation takes. It is never written on Global memory. A creation that fails leaves no
row, no store and no sidecar, so the next start tries again. It carries no tool, Global-memory access, template or prompt of its own:
its identity block is the ordinary member identity, and it can be renamed, rebound to
another template and deleted through every ordinary path (`DELETE /api/agents/{name}`,
`kirocrew agent delete`, a change card's undo), and its key and label are free for another
crewmate to use. The `mate_member_created.json` sidecar records the attempt and
whether it created the row (`created`), so a deleted first crewmate is never
re-created. The creation is skipped when the key already
exists in `config.json` or the `config.local.json` overlay, or when the overlay supplies the
whole roster; an empty base roster is seeded with the same `default` row the loader's
migration writes, so the implicit default member survives. The reserved `default` member
is never changed.

No row, template or spec file other than
the one it creates is read or rewritten on its account: a member or a template under any other name is an
ordinary member or a foreign template.

The only thing that sets the first crewmate apart is its first welcome (see the
first-welcome paragraph below), and it is the crewmate the Crewmates page opens when there
is no previously used conversation.

Guides, change cards, `find_ui` and `search_docs` are platform capabilities of the
default `kirocrew` template, every crewmate built from it, the worker derived from it, and
every dashboard session on them, on the always-mounted `kirocrew-guide` server
([MCP architecture](../../architecture/mcp.md)). The conductor and background templates
keep their own narrow server sets and do not mount it. The guide and card tools answer
only a turn the user sent from the dashboard; a message from Slack or another channel is
refused even when its conversation is mirrored into a dashboard slot, and a turn the person
did not send (a loop wake, a schedule, `session_send`, a subagent completion) cannot start a
guide or propose a card. The rules an answer through them depends
on travel in the tool descriptions and in each result's `next` line, not in any agent's
prompt: call `find_ui` first for a where-is question and quote only what it returns; on
`no_match`, search again with the control's own words before saying a control does not
exist; never say a guide or card is shown unless the tool returned one; answer a control
that decides what an agent may do without asking (Settings → Security, approvals, the
Approval mode picker, Computer Use, Secrets) in words, without a guide; describe what a
removal deletes only from the page's own `caution_text`; and propose a denied-command,
approval or secret change only when the user asked for that exact change. The operating
contract every session receives (`config/prompt.md`, section `KiroCrew Capabilities`) carries one
line pointing at these tools.
In a dashboard turn, a change to Kiro Crew itself that the user asked for (settings,
schedules, crewmates and their capabilities, templates, MCP servers, connections, secrets,
app trust, denied-command rules) is preferably offered as a change card, which the user
confirms; a card is never a way around a denial. Cards do not replace the direct tools:
an agent that schedules its own work, a conductor or a scheduled run keeps using
`cron_add` and the other tools it is granted, under their own approvals. An agent proposes
one registered kind with `propose_change`, after
`list_change_kinds` when the kind is unclear, `find_setting` for a setting, and
`get_member_capabilities` for a crewmate's tools and approvals; `get_change_status` reads
an outcome. The card's `risk` (`normal`/`tighten`/`widen`/`code_exec`) is the single
badge label, but the "runs without asking" acknowledgement rides a separate `widen` flag,
so a capabilities card that both launches code and expands auto-approval reads `code_exec`
and still requires the acknowledgement; an old signed record missing the flag derives it
from its capability impact. A setting whose value is a command this machine runs
(`dashboard.terminal.shell`), or that picks the harness program every session runs
(`agent.acp_backend`), or the agent spec a warm-pool session starts with
(`session.pool_agent`), is `code_exec` whatever its before/after. Every card-editable config
path (`_EDITABLE_CONFIG`) is in exactly one of `change_card_catalog._SETTING_ORDER` (values
ordered tightest to widest, so a move toward the end is `widen`), `_CODE_EXEC_SETTINGS` or
`_CANNOT_WIDEN_SETTINGS`, and `test/test_change_card_setting_classes.py` fails on a new path
until it is placed in one. `dashboard.tailscale.pin_scope`, which the route takes as any short string, is ranked on
the value the loader applies (`config.sections.effective_pin_scope`: case, spaces,
fallback to `node`). A stored card whose `risk` differs from the one its kind and params
now derive, because a setting was reclassified, keeps loading with the newly derived
`risk` rather than being dropped; every other derived field must still match. A reminder that runs once is a `schedule.create` card with `at` (a local
ISO-8601 date-time, no offset) instead of `cron_expr`. `search_docs` searches and pages
through `kiro_crew/docs/*.md` only, by the directory's own listing, and `find_ui` searches
the packaged dashboard location index
([mcp](../../architecture/mcp.md#find_ui-and-the-ui-location-index)); its labels follow the
language the user's dashboard renders (the tab's `X-UI-Lang`, read back through
`GET /api/guide/agent/language`), not the conversation's, and `find_setting` rows carry the
on-screen label the same way. A result's `guide_ref` (`settings.show`, `mcp.open_add` or
the generic `ui.show`) is offered as is with `guide_start`; a new offer replaces the chat's
unfinished guide by itself. `diagnose_settings` returns read-only symptom probes from
`diagnose_probes.py` (`findings`, each `{id, status: ok|warn|problem|unknown, summary,
evidence, fix?}` where `fix` is a change card to propose or steps to tell the user), then
every config and dashboard-config key whose value differs from its dataclass default,
joined to its Settings entry (credential-like keys report only whether they are set), plus
the newest 50 "Dashboard: ..." lines from Global memory history; an optional `topic`
keeps matching rows, returning the unfiltered result with `topic_matched: false` when
nothing matches. The card renders in the proposing chat as a row of the conversation, and
only the owner's click applies it through the existing settings route, verified by the
card hook. A secret's value is typed into the card and never passes through the agent.
Card outcomes reach the agent's next turn as a `[CHANGE CARD RESULTS]` block that never
starts a turn, and every owner change through those routes, card or manual, is a
names-only "Dashboard: ..." line in Global memory history.
An Undo never overwrites a change another tab made after this card wrote. A card's
Undo baseline is the value the card itself wrote, read back at the revision the write
reported, never whatever the post-apply read happens to find. For a `crewmate.capabilities`
card the apply plan is a preview `POST` followed by a `PUT`, and only the `PUT` writes:
a `PUT` that never landed (for example `stale_revision`, because another tab edited after
the preview) counts as nothing applied and leaves no Undo, and a successful `PUT` binds the
inverse to the revision that `PUT` returned. If the post-apply read is at a different
revision than the `PUT` returned, another tab wrote after this card and there is no Undo
(`changed_since_apply`). A `schedule.create` card's Undo deletes the schedule it made as a
compare-and-delete under the cron store lock (see
[Cron Service](learn-cron-dashboard.md#cron-service-cronpy)): the live job is re-fingerprinted
and removed only while it still matches what the card left, so an edit another tab lands
between the pre-delete snapshot check and the delete is refused rather than clobbered.
A `crewmate.create` card's Undo is two destructive deletes guarded the same way. Its
schedule DELETE compares `after.schedule` under the cron store lock exactly as a
`schedule.create` Undo does, and a cron DELETE in an Undo with no recorded fingerprint
fails closed rather than deleting unconditionally. Its crewmate DELETE is name-scoped,
so it compares the immutable `member_id` the card's own create returned against the live
crew's own, re-read inside the config deletion lock before the row is removed: an owner
who deleted and recreated the same name between apply and Undo leaves a different crewmate
wearing it, so the Undo answers `409 changed_since_apply` and keeps the replacement and
its crew log rather than erasing it. The remaining destructive reverses get no Undo at
all: an `mcp.install` or `mcp.add_custom` (uninstall), a `connection.connect`
(disconnect), a `trust.app` (revoke) and a `denied_command` add (rule delete) all delete
through stores that take no revision or fingerprint, so an Undo could not tell the entry
the card wrote from one the owner put back under the same name. Those cards still apply;
their preview and result carry `undo_unavailable_reason: no_identity_check`
(`change_card_catalog.offers_no_undo`), and the change is reversed from its settings page.
A `denied_command` toggle keeps its Undo, which is a `PATCH` rather than a delete.
A `schedule.update` also offers no Undo (`no_identity_check`): its inverse is a cron
`PATCH`, which writes unconditionally and could overwrite an edit another tab saved. A
`crewmate.update` Undo is a `PUT` of the old fields armed with the `member_id` its apply
snapshot recorded; the update route compares it with the live crew's under its config lock
and answers `409 changed_since_apply` when the name now belongs to a different crewmate.
Its title and scope line name the crewmate by the label `read_context` resolved
(`change_cards._crewmate_label`: the display name, else the key), so a card raised in the
crewmate's own chat names the crewmate the user knows; the key stays the identity in the
plan's path, and the label rides in the context, not the compared snapshot, so a rename
between propose and apply is not a conflict. The `crewmate.capabilities` card does the same.
Its rows name their `field` so the dashboard labels them and their values in the user's
words (`CardChanges.FIELD_LABELS`; an `approval_mode` value reads as the picker's word for
it, with a one-line hint of what that mode means under the value,
`CardChanges.changeHint`), and a record that pins no approval mode is read as the mode its thread opens in
(`config.sections.MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE`, the value the profile's picker
shows): the card's before column says that mode, and asking for it is refused `no_change`.
An `approval_mode` move is ranked like `agent.approval_mode` (`MEMBER_APPROVAL_MODES`,
tightest first, through `_order_risk`): toward `trust` the card's `risk` is `widen` and the
dashboard requires the widening acknowledgement, toward `normal` it is `tighten`.
An `approval_mode` write is a permission, so both the card's apply and its Undo are
compare-and-set at the update route (`change_card_catalog._crewmate_put_body`): the apply
sends `expected_approval_mode` = the stored mode its snapshot read (`""` for unset), the
Undo sends the mode the card wrote, and a profile save that landed in between answers
`409 approval_mode_conflict` under the config lock rather than being overwritten. An Undo
of a card made from an unset mode restores the unset-effective mode explicitly, since the
route accepts no `""`.
Crewmate proposals are
`crewmate.create` cards; the card builds the crewmate from `kirocrew`
(`change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`, the Crewmates page's own default)
and sends `first_greeting`, so the crewmate opens its chat by asking about the
proposed goal exactly as one created on the Crewmates page does (see the goal
welcome below); the `/members?create=1&name=<encoded-name>&goal=<encoded-goal>`
link to an editable UI draft remains for a user who wants to fill it in; opening a draft
never creates a member or starts a schedule.
Creating a crewmate is never a first-run chapter: neither the end of the tour nor
a Crewmates visit opens anything by itself, and the page's New crewmate card is
the one place a crewmate is created on the Crewmates page.
Explicitly created members own unique V2 stores identified by an immutable persisted `member_id`, independent of their editable label.
Automatically discovered agents start on Global V1 without member allocation.
Missing, unreadable, shared or mismatched member identity makes memory operations
unavailable without choosing Global. Rules and briefing remain usable without
the learned database. Member isolation is routing for built-in tools, not secrecy
against arbitrary code running as the same OS user.
Selecting a member as `default_agent` preserves that member's memory version and
binding for its own DM thread and explicit picks. With no agents configured, the
resolver returns the template session above.

Member creation automatically provisions empty member memory. Members cannot
choose a shared store or rebind their member store. Legacy members may continue
using V1; member updates never initialize a V2 database. Global and named V1
contents remain untouched. Config fields, exclusive database creation, immutable database identity and
recovery semantics are owned by [config](config.md#named-memory-stores-memory_storespy).

A member DM has no project concept. The slot's `project` field is only where
the dashboard (Files tab, terminal cwd, project essentials) and the provider
cwd read a directory from, and for a member thread that directory IS the
member's configured workspace, falling back to `default_workspace` when that
name is undeclared, through the shared `default_project_dir` validation. It is
derived from the crew's binding in config on EVERY open of
`POST /api/members/{slug}/thread` (the only creator and repairer of member
slots), never a per-thread choice read back from the transcript: a fresh mint
resolves before publishing the slot, so the first slot broadcast carries the
directory, and a live or rehydrated thread is re-pointed at the binding (off
the event loop; compare-and-set, so an already-bound thread is neither
re-marked dirty nor re-pushed; a changed binding reaches disk through the
periodic flush and the client through a push). This is what makes a thread
written before member slots carried the directory -- restored with an empty
`project`, so its pinned Files tab read "no project directory" -- converge on
the right one, and what makes a crew whose binding is later edited follow it.
When the directory changes -- including from empty to bound, since a thread
that ran while its workspace directory was missing has a provider warm in the
fallback work dir -- the open arms the deferred provider reset every other
`slot.project` writer arms: a warm provider keeps the cwd it was spawned in,
the reset never tears down an active turn, and the next message cold-starts in
the new directory. An unresolvable
binding (workspace directory missing or fenced) leaves the thread as it is
rather than clearing a directory the user can see. A concurrent opener's
existing slot is preserved; both openers derive the same binding.

A newly created V2 member starts a fresh conversation. Existing V1 conversation
and native provider context cannot acquire member memory by changing a label.
The session execution record binds its member ID and store ID across later opens
and restarts. Old schedules and child runs retain their captured member/store.
A provider-side template switch changes persona behavior without selecting a
new memory owner. Ordinary owner/app, capability, native-history and governance
checks still apply to selection changes; see [session](session.md#agent-selection-provenance).

The crew editor links to
`/settings/overview?view=memory&store=<name>`. The member memory workspace has
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

The Crewmates page (`/members`, titled "Crewmates") creates a crewmate in place.
Landing rule: with no crewmates the page
shows a single empty-state hero (ghost avatar, "No crewmates yet", one line,
"New crewmate") in place of a roster call to action and a "pick a member" pane;
a roster containing only the first crewmate (`mate`) is not an empty state.
With crewmates and no `?member=`, the page opens like a messages app: Mate, the
first crewmate (key `mate`), while no message has been exchanged with it
(`resolveMateLanding`, at most once per page mount, at every width, so its
welcome is the first look at the page); else the crewmate the user last OPENED
in this browser (`mc-members-last-member`, written on every open the user made
-- click, link, or the page's own default open; sending a message is not
required, #19341) if it is still on the roster, hidden rows included;
else the conversation holding the newest message (greatest `last_active_ts`,
whoever sent that message; ties keep roster order; a roster with no stamps keeps
its first row). The built-in `default` crewmate is a row like any other here. A
roster holding only `default` still shows the hero. The first crewmate's key,
thread and private V2 memory store are its own, and an explicitly configured
display name takes precedence over the label Mate. Explicit member and team
links take precedence. While a create's follow-up is opening the crewmate it
just made (`openCreated`), the fallback stands aside, so a roster whose other
threads hold messages does not land on one of them first. Below md nothing
auto-opens -- the roster is the page -- except Mate's first-visit landing, so
the thread's Back returns to the roster and stays there. A `?member=` naming a
crewmate that is gone falls back the same way, under the existing swap notice.
Loading, a failed roster read and a genuinely empty roster remain distinct. The
page's copy says crewmate / Crewmates and "Built from"; the crew record, its API
and its identifiers are unchanged.

With the Crewmates preview off, Mate still exists in the background, but every list
that offers crew records leaves it out (`hideMateWithoutPreview` in
`lib/assistantMember.ts`: the agent pickers fed by `useAgents`, Customize and a
template's crew list), so nothing names it until the preview is on.

Mate's chat has no static welcome card. Mate speaks first instead: once the thread
endpoint has confirmed Mate's pinned thread, with the Crewmates preview on, the page
calls `POST /api/members/{slug}/greet` (owner-only; app tokens get 404), once per
thread per tab (`pages/members/useFirstGreeting.ts`). A failed request shows
nothing: the chat works without the greeting, and the next open of the thread asks
again. A crewmate the user creates on the Crewmates page's New crewmate card
greets through the same route and the same dispatch (see the goal welcome below),
and the page asks for it on every dashboard-created row (`dashboard_created`), so
each create path has one greeting. The server (`dashboard/mate_welcome.py`) starts a welcome only
when the slug's DM binding names a configured crewmate other than the reserved
`default` member, the live slot is that member's pinned (`mode="member"`) local
thread, it holds no rows or queued entries, no turn is running, and the member owes a
welcome. Two creations record that debt, at creation, in
`members/<slug>/welcome_owed.json` (`members.mark_welcome_owed`, naming the member):
the first-crewmate creation (Mate's welcome, no `kind`), and a `POST /api/agents`
whose body carries `first_greeting: true` (exactly `true`; any other value owes
nothing), which records `kind: "goal"` after the record is published. The
Crewmates page's New crewmate card sends it, and so does an applied
`crewmate.create` change card. A member
that reached the roster any other way (the crew manager's create, the CLI,
discovery, an import, an app, a hand edit) has no record and never greets here
(`not_owed`), and neither does a record whose `kind` this gateway does not know.
A failed record write is logged and leaves the create successful, with a chat
that simply opens empty. It
then claims the once-only marker `members/<slug>/welcome_claimed.json` with
`O_CREAT|O_EXCL` before dispatch, so two tabs, a reload or a retry after a failed
turn never greet twice; a declined request leaves the marker unclaimed. The welcome
is one real turn of that crewmate: `_run_chat` with a hidden kickoff
(`_synthetic_payload=True`, actor `gateway`) and NO transcript row for it, so the
user sees only the crewmate's own reply, which stays in its session context. The
kickoff carries the whole welcome, since no system prompt explains it
(`mate_welcome.welcome_kickoff`, wording in one place): it introduces itself by its
label in everyday words with no developer terms; says it is the user's first
crewmate (`FIRST_WELCOME`) -- or, when the roster already holds a crewmate besides
Mate and `default`, one more teammate on their crew (`FIRST_WELCOME_WITH_CREW`) -- a
teammate who remembers what they work on together and keeps at it over days; says it
can help right now with getting Kiro Crew set up, sorting out anything not working in
it, or showing the user around (those three only); and finishes by asking what the
user would like to call it (the name in question is the crewmate's own). The kickoff
is written as plain principles, and the greeting itself is the model's own words.
When the user answers with a name, that later turn says plainly that the crewmate now
goes by it and calls the `kirocrew-mate` tool `update_self` with `display_name`
(`POST /api/member/self/update`, `handlers/member_self.py`), which renames ONLY the calling
crewmate -- the member is derived from the caller's own pinned thread, never from an
argument -- and only from a turn the user sent in the dashboard (`_turn_user_sent`,
so the hidden welcome kickoff, a wake or a cron turn is refused 403
`not_user_turn`). The name is cleaned (control and format characters dropped, spaces
collapsed, refused when empty) and checked by `validate_member_name`
(`MEMBER_NAME_MAX_CHARS`); the reserved `default` member and the configured default
crew are refused, and so is a name `members.check_crew_label` refuses: one another crew
already shows (`400 name_taken`) or a credential-shaped one (`400 invalid_name`). The rename is a `member/config` event, so the open chat header,
the roster and the switcher show the new name at once through the roster projection,
with no reload and no confirm card. The kickoff says the user has typed nothing,
frames it as the crewmate's first conversation with the user, so it greets as someone it is meeting for the first time, and allows a user name only when the User Preferences block
or a memory result states one, never one inferred from a username, home path, email
or host name. The route answers `{"outcome": ...}` (`started`, `already_greeted`,
`not_empty`, `busy`, `not_crewmate`, `no_thread`, `not_owed`). A welcome turn that
fails surfaces through the ordinary turn error path once; the marker is already
claimed, so it never loops, and the composer stays usable.

**Fresh start.** A quiet "Fresh start" pill under the crewmate's name, in the Profile
card's head (on every screen width; the DM header carries no button; no danger colour;
the label is the action alone, with no ellipsis and no sub line -- the confirm carries
both the warning and the line saying the messages stay) asks once in the page's themed confirm.
The action has ONE name in every place it appears -- the pill, the confirm's own button and
the fold divider all read "Fresh start", in each locale its own single wording -- so the
divider names the same thing the user pressed rather than a third phrasing of it. It
closes the card, then calls `POST /api/members/{slug}/fresh-start` (owner-only, app
tokens get 404, same gates as the greet route). The route resolves the slot through the
DM binding and never makes one, so the slot key, its monitor loops, the work ledger and
the crew log stay bound. A thread with queued messages or unconsumed steers answers 409
`slot_queue_pending` before anything is stopped, and again if one arrives during a stop
wait: a soft stop keeps them, and they would then run on the old conversation.
Otherwise it runs existing paths in order: `stop_slot_turn` on a running turn, a second
`stop_slot_turn` when the turn has not ended within the wait (its hard-kill escalation,
which frees a stuck process), then the typed `/clear` path
(`_answer_slash_without_channel`): the discard with replay off, the plan dropped, the
eager respawn, and its transcript line saying the agent no longer remembers what is
above it. A turn that outlives both stops answers 409 `turn_in_flight` and nothing is
cleared; a session that still refuses the discard keeps it queued, as `/clear` does;
the route passes on the outcome `/clear` returns, and the reply says
`outcome: "queued"`. The reply carries `reset_at`; on `cleared` the pane folds rows
written before it under one closed divider that names the reset time and the count
("Fresh start at … · Show earlier messages (N)") and opens them again ("… · Hide
earlier messages (no longer remembered)"). On `cleared` the route also appends
`member/reset {reset_at}` to the crewmate's own event log (best-effort, off the
response path), which the roster projection folds into `reset_at`; `GET /api/members`
serves it back on every row, and `MembersPage` reads it into `foldBefore` on load, so
the fold survives a reload instead of living only in page state -- a queued reset that
has not actually cleared yet folds nothing. The reply says whether that append landed,
as `reset_persisted`; a `cleared` reset whose append failed still folds for the session
and shows the page's own `ErrorNotice`. That notice speaks in the confirm dialog's own
words -- the crewmate forgot the conversation and the messages are folded away -- rather
than calling the conversation cleared, which would contradict the dialog's promise that
the messages stay; what it reports is the one thing that went wrong, that the fold time
was not saved, so the messages unfold again after a reload. The `/clear` is
not retried for that fault: the conversation is already cleared and only the record is
missing. A refused press shows a notice by cause:
`slot_queue_pending` says to wait, `turn_in_flight` says to stop the turn first.

A crewmate created on the New crewmate card owes a goal welcome (`kind: "goal"`).
Its first turn runs the same once-only claim, emptiness and busy checks and the
same "no transcript row" dispatch, with the hidden kickoff
`mate_welcome.goal_kickoff` (`GOAL_WELCOME`): the crewmate's first message is
two short sentences of everyday words, a hello with its name and then a question
asking what its goal is, with no plan until they answer. The user chose a name
and a look, not a template, so the kickoff also keeps its template, role and
agent type out of that message. When the record carries a `description`, or
failing that the crewmate's goal record already holds goals (what a
`crewmate.create` card writes), the kickoff appends it as a first draft of the
goal, which the crewmate restates and asks the user to confirm or change. An
unreadable goal record only means the kickoff asks for the goal. Once the user
states or confirms the goal, the crewmate proposes it with `set_goal` (a
`crewmate.goal` card) rather than offering a schedule: follow-up on a goal is
the check-ins, which the goal-setup nudge (`dashboard/goal_setup.py`) leads to
with `set_check_ins` after the goal is saved. The member identity sentence
(`context_assembly/member.py`) carries the same principle on every DM turn: a
long-term aim the user describes is proposed as a goal, not only remembered.

**Every new crewmate gets its own-record tools.** A crewmate's template need not
carry `kirocrew-mate`: only the default `kirocrew` spec is built with it, and an
owner may bind a crewmate to any template. So each create path -- the dashboard
create (`dashboard/agent_admin/crew_records.py`), the first-crewmate step
(`agent_materialization/first_crewmate.py`) and `kirocrew agent create` -- calls
`member_mate_tools.grant_mate_tools_to_new_member` once, right after the member is
published. It enrolls the member through `CapabilityService` (the same preview and
save the Capabilities pane runs) with three kinds of row: the `kirocrew-mate`
server, the `@kirocrew-mate` tools reference and each `agent._MATE_AUTO_GRANTS`
approval; every other row keeps inheriting its template. It writes nothing when
the template already mounts or mutes the server, when the template is `kirocrew`
itself, when POLICY ∩ PROFILE withholds the server (asked fail-closed), when a
fresh build would not emit it, or when the previewed copy carries a
`backend_warning` (a backend the member's chats start on cannot load an enrolled
member's spec). An existing crewmate is never enrolled by this, and a create that
landed is never reported failed over it. The capability editor accepts that
server on a member whose Parent lacks it (`agent_capabilities.MEMBER_PLATFORM_SERVERS`):
its transport is never the request's but the default template's entry
(`managed_mcp.managed_member_transport`), and every later save refreshes it, or
drops it when a fresh build would no longer emit it. The copy's entry carries
`disabled` only as `true` (`agent_capabilities._platform_entry`): an enabled entry
is byte-for-byte the default template's, because a `"disabled": false` is a key a
per-session element cannot express, so the session mount
(`session_mcp.native_mount_withholding`) would withhold the server's identity
element and every mate call would refuse `identity_unattested`.

Whether the crewmate's sessions load those tools
(`member_mate_tools.binding_has_mate_tools`, read from the spec they run) picks
the wording Crew writes for it. With them, the identity sentence, the first
welcomes and the goal nudges name `update_self`, `set_goal`, `set_key_results`
and `set_check_ins`; without them, they name no mate tool, have the crewmate
write proposed goals, milestones and check-ins in the chat, and point the user at
the crewmate page and its Goals tab. A spec that cannot be read keeps the
with-tools wording.

The primary New crewmate action (the header **+** menu's one row, the empty
roster's hero and the switcher's New crewmate) opens the
`NewCrewmateDialog` as a modal: one card headed **New crewmate** with the avatar tile, a
**Name** field under its own visible label, an **Advanced settings** row, and
**Create**; no line under the name explains what happens after
Create, since the new crewmate's first message says it. Create builds the
crewmate from `kirocrew`, and the folded card does not name that template. The
tile shows a name-seeded icon (a pose and background colour derived from the
name) until the user presses it (or its pencil badge),
which opens `CrewAvatarBuilder` with `iconOnly`: a pose and a background colour,
no tier strip and no reset link, since a crewmate that does not exist yet has no
record for a picture or a pack. Apply pins the pick and sends it as
`avatar: {kind: 'icon', pose, bg}`, and it survives a renamed draft; with no
pick the create still carries that name-seeded icon as its `avatar` (frozen into
a stored choice at create, never re-derived from a later rename), so a new mate
is born an icon rather than wearing the name's ghost. The **Advanced settings** row unfolds inline below
the avatar, in the same card: Built from, What it looks after, and the crew
editor fields (workspace, model, triggers, session colour), ending with a second
Create. It is folded by default; `startExpanded` opens it unfolded, which is what
the hero's Advanced link and the crew manager's door pass. Folding unmounts those
fields but keeps their values, and every create sends them. The "+" menu holds no
Advanced row of its own.

The card is the page's one creation surface. A drafted proposal (a crewmate's
create link `/members?create=1&name=…&goal=…`, clicked in its chat or reached by
address, or the `crewmate.create` guide, whose enter step is that link) opens the
same card with the proposal filled in: the name in **Name**, the goal in **What it
looks after**, with Advanced settings unfolded when a goal is proposed so it is in
view (`initialDraft`). The page strips the link's parameters once read and keeps
the chat the link was followed from as the one behind the card. A proposal left as
proposed is not the user's draft: it leaves without asking, and a later proposal
replaces it at once; a card the user changed is replaced only once its own leave
guard agrees, or when the guide that brought the proposal already asked. The card
is a modal: a centered card over a viewport scrim with a focus trap, portaled
above the page. The current chat (and the docked side panel) stay mounted and
visible behind the scrim rather than being replaced, and the chat is marked
not-viewed while the modal is open so messages arriving in it still badge;
closing the card returns to it. The guided door (`guided`) carries the guide's anchor on its Create button,
and its create request carries the guide headers so the gateway credits the
guide's one commit step from the crewmate it actually created. When the crewmate
runs is not asked on the card: its goal welcome settles that in conversation.

That card is the ONE "New crewmate" dialog
(`website/src/pages/members/NewCrewmateDialog.tsx`): it is
also mounted by the crew manager's "New crewmate" tile / header button and its
dashed roster card (`pages.kiroCrewAgentsPage.add_crew_member` in
`website/src/pages/KiroCrewAgentsPageRoster.tsx`, the Crewmates tab of Customize), which
has no separate create sheet. Both doors use the
existing owner-gated `POST /api/agents`; editing an existing member still uses
the crew manager. The crew manager's door opens with Advanced settings unfolded
and `kirocrew` as the default **Built from** (labelled as the default), and shows
no first-greeting hint, since it asks for none. What happens after a create
depends on the door. On the Crewmates page the card sends `first_greeting`, the
page re-reads the roster and opens the new crewmate's verified thread (a failed
re-read keeps its retry), and that open's greeting request starts the hidden goal
welcome; nothing is sent in the user's name. From
the crew manager the door is CONFIG mode: the dialog closes and the crew manager
re-reads its roster so the new card appears in place; it does NOT navigate to the
crewmate's chat and asks for no greeting. The dialog's own success path (cache
invalidation, the post-failure reconcile, "what it looks after" stored as
`description`) is identical on both doors; only the caller's `onCreated`
differs. `NewCrewmateDialog` imports its field frame and field components from
`KiroCrewAgentsPage` (which re-exports them from `KiroCrewAgentsPageFields.tsx`),
so the two pages cannot drift; the resulting call-time
import cycle is resolved because both reference the other's bindings only inside
component bodies.

The roster lists a row unasked only when its DM thread holds a message
(`has_dm_message` on `GET /api/members`: the thread's transcript has a row, or
the live slot holds one) or the user starred it -- whoever sent the message,
the user, the crewmate, a scheduled run, an app -- or when it is Mate, the
first crewmate (key `mate`), listed from the start so its welcome has a row to
open. An empty thread is not listed,
as a messages app lists no empty conversation; the search still reaches it, the
crewmate open in the thread stays listed while open, and a remembered crewmate
is restored even when the rule hides it. The built-in `default` crewmate, a
dashboard-created crewmate and an app's crewmate follow the same rule. Recent
order is by `last_active_ts`, the crew log's fold of the thread's last
`member/message` (see `_recency_for_row`), so the list reads newest conversation
first like a messages app. The client stamps a send on the row bound to the
sending slot (`noteUserChat`: `has_dm_message` and `last_active_ts`), so a
crewmate the user just messaged lists and moves without waiting for the crew
log's push or a refetch. There is no separate record of the user's own sends: a
plain session is the default custom agent and is nobody's conversation, and a
chat with a crewmate is its DM thread. The star, origin and status filters
narrow the rows the roster shows, so choosing an origin does not reach a hidden
row; the search is the one door within the roster list. A team's view
(`?team=`) and the team dialog are built from the whole roster, so they still
list every crewmate the user put on that team -- placing a crewmate on a team is
itself a choice to use it -- and a team whose crewmates are all hidden keeps its
(empty) roster header. Where nothing auto-opens (below md) and every row is
hidden, the roster says so and names the search. A row from an older gateway
that carries no `has_dm_message` is listed. The header count and the filter
tallies count the listed rows plus any hidden row the search reaches.
Before any of the landing rules, a bare visit with the Crewmates preview on
opens Mate while no message has been exchanged with it (`resolveMateLanding`:
Mate's row has neither `has_dm_message` nor a live `last_message`), ahead of a
remembered, the last-chatted or a more recently used crewmate, at most once per
page mount; once Mate's thread holds anything, that rule answers nothing and the
rules above apply unchanged. A create link followed while a chat is open lands
on that chat, never on this rule.

Deleting a crewmate is available on this page: the crewmate's edit controls
open `CrewEditorModal` in place, and its Danger pane deletes the
crewmate. A row the listing rule hides is reached through search, or through
the Customize page's Crewmates tab.

Reopening a running Member DM, including a turn awaiting tool approval,
reuses its captured execution record. The canonical session key, selected
member, live slot store and execution record must agree. This read does
not pin or repair memory while work is active; missing, mismatched or unreadable
identity still refuses. The handler rechecks slot identity after the off-loop
store read, and a link to another session remains a conflict.

The member's presence indicator includes active child runs even while its own
turn is idle. Completion of the member's planning turn does not imply its
delegated work has finished. The roster presence indicator remains active while
only child runs remain. Profile Sessions still lists dashboard sessions created
by the member; child runs do not become dashboard sessions.

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
Global Memory V1 in the member's Profile Schedules pane. Displaying an old schedule there
does not migrate it or grant access to that member's member store.

`resolve_effective_model` is the single source of truth for what model a new
session on a crew starts with, highest tier first: the crew's own `model`, the
bound kiro agent's pinned model (skipped for the built-in `kirocrew` agent), the
global `agent.model`, then the installed agent file's model. A per-session pick
outranks all four and is not considered there. The session layer
(`session._session_model`, run by `get_or_create` for a caller that names no model)
answers through the same function, so a bound template whose `model` is `auto`
inherits the global on the chip and in the session alike.

The loader is defensive about hand-edited config: a non-string `model` or
`triggers` collapses to `""`, an unknown `reasoning_effort` collapses to inherit,
and a junk watchdog override collapses to `0`. Every other field declared `str`
on the agent record (`kiro_agent`, `workspace`, `description`, `source`,
`telegram_account`) collapses to its declared default on a non-string value, so a
wrong-typed key reads exactly like an absent one. Two identity fields are the
exception. `memory_store` defaults to the shared store, and `member_id` defaults
to `""`, which reads as "no identity yet". Collapsing either would switch off a
fail-closed check in `memory_stores.py`, so both load as written and the
resolvers refuse them; `kirocrew agent list` shows a malformed `memory_store` as
`(invalid: <type>)`. The roster and config views still mask a non-string on their
own side as defence in depth.

### Crewmates page code: the facade and its owners

`website/src/pages/members/MembersPage.tsx` is the page's one import path (App's
lazy route and the capture entries import its default export; tests import its
named exports). It composes owners beside it, calling each one where its code
sits in the page's hook order: React runs a component's effects in registration
order, so the order is part of the behaviour, and `MembersPage.owners.test.tsx`
pins it together with the facade's exports and the rule that no owner imports
the facade. The owners are stretches of the hook sequence the page had as one
file, so a few pieces sit where their registration falls rather than with the
responsibility they serve (the Schedules guard state below, say); a change that
regroups them reorders registrations deliberately, reviews that as a behaviour
change, and edits the order pin (follow-up #20512 for the Schedules guard state). New work goes to the owner of its responsibility:

| Owner | Holds |
|---|---|
| `MembersPage.tsx` | the composition; the page's selection state (open crewmate, crew being edited, open Profile card); the New crewmate card and its post-create follow-up; the guide facts and hand-offs; the exit guards (the native confirms the lint allowlist permits); the face-flight overlay |
| `membersPageModel.ts` | pure rules: `resolveMateLanding`, `resolveDefaultMember`, `memberMemoryDisplay`, `panelSitsBeside`, `panelChrome`, the withheld view sets, `createDraftFromHref`, the `?member=` param and the roster width bounds |
| `useMembersPageRoster.ts` | the roster read and its projection merge; per-row live readings (unread, patrol, loop, needs-you); applying the filters to the held order (`sortedMembers`) and the filter menu's tallies, which need those readings |
| `MembersPageRoster.tsx` | the roster column (header and add menu, search, filter menu, chip, team groups, resize grip) and the roster view state: presence, the open crewmate's projected record and DM identity (`activeRoster`, `activeView`, `crewmateIdentity`), the persisted filters and sort with the question they ask (`rosterFilterQuery`) and the population it lists, starring, the held order |
| `MembersPageRosterRows.tsx` | `MemberRow` (one roster row, reading its own `roster` projection) and `CrewmateEmptyHero` (the empty roster's call to action) |
| `useMembersPageCrewEditor.ts` | the in-place `CrewEditorDialog`: its registry reads, the admitted-roster latch, the unsaved and in-flight stakes |
| `useMembersPageTeams.ts` | the team read, collapsed groups, the team dialog, and the team view (open, close, grouping) |
| `useMembersPageLayout.ts` | column placement: roster width, pin and fold; the phone top bar; the side panel docked or overlaid |
| `useMembersPageDrawers.ts` | the phone roster drawer, the side panel slide, the swipe bindings |
| `MembersPageThread.tsx` | the open thread (open POST, first greeting, Fresh start, reconnect, confirmed slot), its on-screen readings (pill activity, header insets, greeting card, read marking) and the thread column |
| `MembersPageThreadHeader.tsx` | the floating DM header: switcher chip, back control, identity pill, panel opener |
| `MembersPageProfile.tsx` | the Profile card: open, close and placement (`requestCloseProfile`; `togglePanel` and the `toggle-activity-panel` listener that folds the card when the panel is revealed), the Schedules draft and its guard, the About line's first-goal read (`profileGoalsQuery`), the face flight with the page's reduced-motion reading, the card's clock, the card render with its Goals tab |
| `MembersPageSidePanel.tsx` | the SidePanel binding to the confirmed slot and its render; the Sessions list and Schedules retention state declared in its stretch of the hook order |
| `useMembersPageNavigation.ts` | `activate` / `openMember`, the drawer pick, in-thread preview links, and the URL sync that decides the open crewmate |

The Profile card's Schedules draft is guarded at every exit that would unmount
it. Each piece of that guard registers at its own place in the hook order, so the
pieces sit in different owners:

- `useMembersPageProfile` (`MembersPageProfile.tsx`) declares `schedGuardRef` and
  `schedAtStakeRef`, the two refs every in-page exit reads. It also holds the exits
  that close or fold the card: `requestCloseProfile` (the card's close and the
  header's identity pill) and `togglePanel` (the header's panel opener and the
  `toggle-activity-panel` listener). Two effects there keep a dirty card where it
  is rather than ask: a cross-tab panel reveal, and the phone re-placement of a
  docked card as the floating one.
- The other exits read the same refs where they live: the card's Back
  (`requestProfileBack`, `MembersPageProfileCard`); the phone back / roster
  control (`MembersPageThreadHeader.tsx`); `openCrewWorkLog`, `openCrewView`,
  `openSessionGuarded` and the Dashboard tab's `onOpenProfileTab`
  (`MembersPageSidePanel.tsx`); a crewmate pick (`openMember`,
  `useMembersPageNavigation.ts`); and a team open (`openTeam`,
  `useMembersPageTeams.ts`).
- `MembersPage.tsx` assigns both refs once `schedulesMounted` is known, and keeps
  the exits the page owns: the route-leave guard, the published navigation stake
  (`usePublishNavigationStake`, which `NavigationBackGuard` arms off) and the
  `beforeunload` guard. It also holds the Goals tab's draft state (`goalsDraft`,
  `goalsAtStake`, `onGoalsDraftChange`, `mayLeaveGoals`), which the same guards
  ask before the Schedules question.
- `useMembersPageSchedules` (`MembersPageProfile.tsx`) holds the draft's refs
  `schedDraftDirty` and `schedSaving`, `releaseSchedRetention` with the effect
  that re-arms it, and `mayLeaveSchedules`, the question the guards ask.
- `useMembersPageSidePanel` (`MembersPageSidePanel.tsx`) declares, at their place
  in the hook order, `schedAtStake` (the state mirror `useMembersPageSchedules`
  sets; it feeds the published stake and the `beforeunload` guard), `schedReleased`
  and `schedulesMountedRef`.
- `useMembersPageCrewEditor` declares `releaseSchedRef`; `useMembersPageSchedules`
  points it at `releaseSchedRetention`, so a confirmed crew delete
  (`onCrewDeleted`) stands the retention down.

### Crewmate conversation layout: switcher, Profile, Dashboard, Files

When a crewmate conversation is open, the standing roster is hidden at every
desktop width and the thread receives that space. `CrewmateSwitcher` replaces it
in the thread header: the closed Glass chip stacks up to three faces and ends in
a chevron. The page's name (`pages.membersPage.title`, "Crewmates") is the
chip's accessible name and slides into view on hover, keyboard focus or while
the list is open (a `grid-cols-[0fr]` to `[1fr]` column, cut under reduced
motion); it is not drawn at rest, and neither is the roster count (#19045):
what the page is and how big the crew is are not what a reader comes to this
corner for. The action ("Switch crewmate") is the tooltip.
Its popover lists the roster's own population, not every crewmate: the rows
`listedByDefault` keeps (chatted-with, starred, Mate, the default crew) plus the
open crewmate (`rosterPopulation`), the same rule the roster column reads, so
the chip can never disagree with the column. A typed search decides alone and
falls back to the full roster, so a hidden row -- a background or app-driven
crew, a sync-generated template nobody has chatted with -- is reached by typing
exactly as from the column's search (`rosterShows`). It marks the current one,
switches
the thread through the existing verified `openMember` path, and opens the same
`NewCrewmateDialog` as the roster's create entry. The stacked faces are the OTHER
crewmates' in roster order — the open crewmate's face is already the identity
pill beside the chip, and a second copy there read as a duplicate rather than as
the rest of the crew; it joins the stack only when it is the whole roster, so the
chip never draws empty. Below `md`, the roster remains the first level and the
thread's Back control returns to it.

The switcher lists crewmates only. Team headers (and the team view they open),
the per-crew star, the filters and the sort live on the roster
column, and on desktop nothing else reaches them once a DM is open (the Back
control is `md:hidden` and a bare `/members` reopens a crewmate). So the
popover's footer carries a second action beside **New crewmate**: **Show the
full roster** (`roster_show`) pins the roster column back beside the thread —
page state (`rosterPinned`), not persisted, since the folded roster is the
desktop norm and a pin is a detour from it. While pinned the aside is `hidden
md:flex` (still folded below `md`, where Back is the way), it reserves its width
again for `panelSitsBeside`, and its resize handle returns. The switcher chip
fades out in place (`opacity-0 invisible`, 200ms, a cut under reduced motion)
while the pin holds: the open column is the list it folds, so a second copy
beside the identity pill only repeated it, and its kept box holds the identity
pill where it was. The chip's footer therefore only ever offers **Show the full
roster**. Picking a row from the pinned roster switches the thread and keeps the
pin, and the roster header carries its own close control (`member-roster-hide`,
`md+` only, shown only while pinned beside a thread) that lifts it and fades the
chip back. Each of those two controls disappears under its own press, so focus
moves to what replaced it: the column's search on pin, the chip on close. A TEAM view is not folded: `TeamView` draws no
switcher and its Back is `md:hidden`, so on desktop the roster column is the only
way out of `/members?team=<id>`, and the aside is `hidden md:flex` there whether
or not the pin is set (the pre-switcher behaviour); the pin's close is withheld in
a team view because lifting the pin would change nothing on screen. A failed
post-create roster re-read with no crewmate open also keeps the aside `hidden
md:flex`: `rosterShown` reserves its desktop width, so hiding the aside there
would leave a blank reserved column beside the retry notice. The roster column
carries roster-read, patrol-registry, default-crewmate, team-read, and roster
action failure notices. While a DM folds that column away, the visible main
column repeats those notices above the thread; those copies withhold the agent
hand-off because Profile may hold an unsaved schedule draft. Pinning the roster
back hides the duplicate main-column copies at `md+`. The team pane separately
repeats roster-read and team-read failures where the roster column is not on
screen — below `md` — and the pane's copy is `md:hidden`, exactly the column's
own hidden range.

The team-read and default-crewmate notices each carry a Dismiss control
(`ErrorNotice`'s `onDismiss`). One press removes every copy of that notice for
the rest of that outage: it stays away while the read keeps failing, and only a
failure after a good read (`dataUpdatedAt` past the dismiss) shows it again; a
good read clears it unpressed. Both reads ride
out an unanswered request before they fail (`retryThroughRestart`: a fetch
rejection, a proxy 502/504 or the gateway's 503 retries three times, 1s/2s/4s;
every other failure keeps `retryPolicy`). A failed read then re-reads every 30s
(`refetchWhileFailed`), so the notice clears on its own: nothing else refetches
either read on an idle page, and the WebSocket may never have dropped.

Because a bare `/members` reopens a crewmate, the folded roster is the desktop
norm, so the switcher also carries the live per-crewmate signals the roster column
used to. The page hands it the same `signalsOf` resolver its roster status filters
read (`MemberSignals`: running / needsYou / unread / patrolling), so a cue here can
never disagree with the filter that counts it. Each popover row shows the roster
row's own cues — the `ok` presence dot on the face while working (named by the
row's "Working" line), the accent unread dot on the right edge — plus a `warn`
needs-you dot for a turn parked on an approval or a question; the two right-edge
dots are named images, so the state is in the option's accessible name, and both
labels reuse existing catalog keys (`filter_status_needs_you`, `unread_message`).
Each right-edge dot also carries its state as a small muted word beside it
(`team_needs_you` "Needs you", `switcher_unread` "Unread"): colour alone is not a
state every reader can tell apart. The word is `aria-hidden`, because the named
dot already puts the fuller sentence in the option's name and a spoken word
would say it twice.
The closed chip shows a small `warn` needs-you dot over the face stack, and adds
that label to its accessible name, only while a crewmate OTHER than the open one
needs you: the open crewmate's parked turn is already on screen in its thread.
The dot is absolutely placed, so the chip's width is the same with and without it.
Working and unread elsewhere never light the chip. With no resolver passed, a row
shows only its own `running` flag and the chip stays quiet. The full roster's rows
(`MemberRow`, shown while pinned or with no thread open) carry the same needs-you
cue on their right edge, leading the unread dot — the same `warn` named dot plus
the same `aria-hidden` word, read through the same `signalsOf` resolver
(`isNeedsYou`) — so the full roster, the folded switcher and the status filter
never disagree about who is parked on a question.

Unread is also the one signal drawn OUTSIDE the closed chip (#19045). Under it,
in the header's floating band, hangs one small Glass pill per crewmate with an
unread message who is not the open one (`crewmate-switcher-unread-pill`, roster
order): the face with the accent unread dot on its corner, then the display
name; the button's name is the crewmate and the state (`fmtList`, "oncall,
Unread message") and the tooltip is `unread_message`. A pill opens that thread
through the same `onPick` as a list row, the page clears the unread, and the
pill goes. Past `UNREAD_PILLS` (4) the rest fold into one "+N" pill
(`switcher_more_unread`, `crewmate-switcher-unread-more`) that opens the list,
whose rows carry the same dot, so nothing is lost to the cap (the "+N" pill
bumps the page's `openRequest` counter, which the chip reads to open its list).
No unread, no column. The column (`CrewmateUnreadColumn`, exported beside the
chip) hangs from the thread header's bottom edge, absolutely (`absolute left-3
top-full -mt-2`, the header's own `py-2` pulled back so the first pill sits a
gap under the chip): it adds NOTHING to the header's measured height, so the
thread's top inset stays the chip row's and the conversation keeps scrolling up
under the band — and under these Glass pills — the way it scrolls under the
identity pill. Pills float over the text; they never push it. Each pill sits in
its own `motion.div` row inside an `AnimatePresence`: it grows in from the row
above (height and opacity, 220 ms) and shrinks out the same way when its thread
is read; the gap above a pill rides inside the animated row. Reduced motion
cuts to the end state. The column shares the chip's slot rule: desktop only (a phone's
Back returns to the full roster with its unread dots) and faded out with the
chip while the roster column is pinned. The chip's own corner dot stays
reserved for needs-you.

The thread header (`member-thread-header`: the switcher, the identity pill, the
panel opener) floats OVER the top of the thread: an absolute row with no
solid background, so the conversation scrolls under the Glass pill the way it
scrolls under the composer dock, and the pill reads as a chip on the conversation
rather than a bar above it. Every control on the row is a Glass chip — the
switcher and the panel opener wear the pill's material at their own size, the
phone back button too — because a bare button on a floating row sat on scrolled
text and read as text printed over text; behind the three, a fade filling the
header's own box (`from-bg via-bg/70 to-transparent`) dissolves rows toward the
page colour before they reach the chips, so the chips sit on half-faded ground
while the pane's top edge stays glass. The pane pays for the band with its
scroller's top padding — `ChatPane`'s `topInset`, the header's measured height
(`useMeasuredHeight`, never a constant) plus the pane's own 12px — so at scroll
top nothing hides under the pill (a crewmate's chat draws no pinned-prompt band,
#18346, so nothing else hangs under it). Whatever can sit between the header and the pane (notices, the
greeting cards) stays in flow in a measured inner box wrapped by an outer box that is padded
below the header by that height only while the inner box has a height (no
`empty:hidden`: a `display:none` child measures 0 but is not `:empty`), and while that box has a height the pane's inset is 0: a card
with controls is never under the glass, and the pane is under the header only
when it is the thing directly below it. The row and its cells pass input through
(`pointer-events-none`); only the controls catch it, so a wheel beside the pill
reaches the transcript. It sits at z-20 — above the pane's own chrome (its
dock), below the floating Profile card (z-30), whose scrim covers it. No hairline under it.

The identity pill shows the crewmate's face (with its loop mark), its name and
its activity line at rest, and ends in the chevron. The words never fold away
(#19045): this pill is how the reader knows which crewmate the page is talking
to, and a face alone does not say that. The loop mark (`CrewLoopIndicator`, "On
watch") is a solid accent ring with one dot orbiting it; under reduced motion
the ring stays and a still dot replaces the orbit. It never uses the green of
the working presence dot.

The centred identity pill opens `CrewProfilePanel`, not the editor. It ends in a
small `ChevronRight` (decorative, `aria-hidden`): the one visible sign that this
Glass chip is a door, since the switcher chip beside it is another Glass chip
with faces in it. The pill is a toggle, as its `aria-expanded` says: with Profile
already open a press CLOSES it through the same schedule-draft guard as the
card's own close control (`requestCloseProfile`). It must not re-open on the
default tab: `profile.tab` is part of the card's React key, so a card the reader
moved to another tab (Schedules, say) would be remounted and a New schedule
draft inside it destroyed with no question asked. Pointer users rarely reach the
pill under the floating card's scrim; keyboard users reach it every time. The
pill's title row is the display name plus the exact ID in mono when a label
covers it; a label that differs from the ID only in case ("Kiro" over `kiro`)
covers nothing, so the ID line is withheld and the ID rides the name's `title`
(the agent-ID tooltip) instead. When
the chat
SidePanel is closed on a non-phone viewport, Profile takes a 34%-of-row in-flow
column and narrows the thread. On phones it always floats over the thread: the
composed column is wider than a 320px viewport and must never be added there. The
pill unmounts in the non-phone column state and its face flies to the card's head:
one face moves rather than two faces cross-fading. The flight is `CrewFaceFlight`, a
copy of the face portaled to `document.body` (`fixed`, `z-[60]`, `pointer-events-none`,
`data-testid="crew-face-flight"`) tweened on framer's own default layout
clock (`FACE_FLIGHT_SECS`, the `defaultLayoutTransition` the `layoutId` flight ran on) from the departing face's box to the landing face's box, which is
re-read every frame because the landing face moves while the column reveals or folds;
both real faces hold their place with `visibility: hidden` until it lands, and reduced
motion or a face with no box (jsdom) is a plain swap. It is NOT a framer `layoutId`
shared by the two faces: a shared element travels inside whichever surface
owns it, so the card's face was clipped by the card's rounded `overflow-hidden`
shell, its scrolling body and the width-revealing aside until it was already inside
them, and the pill's face was painted under the thread and the folding card, both
later siblings of the header. The docked column's `AnimatePresence` keeps one slot
in both placements, so closing it runs its width exit and the leaving card's face
stays measurable as the flight's origin; the presence is keyed on the card's
placement, so a column RE-PLACED as the floating card (the window crossed below
`md`) is dropped at once with no exit and no flight — a phone-width viewport never
holds the column beside the floating card. Opening SidePanel collapses that Profile
column and restores the pill. If SidePanel is already open, or the viewport is a
phone, the pill opens Profile as a full-height, rounded hover card centred
horizontally in the chat width the open SidePanel leaves; both surfaces remain
visible. The pill stays, but with its face slot held empty (`visibility: hidden`):
the face is IN the card. Opening flies it there with the same `CrewFaceFlight`
copy the docked card uses while the card fades in on the flight's clock, and
closing flies it back while the card fades out — the floating portal is mounted
through `AnimatePresence` so the close plays. The flight fires on the
card opening or closing in either placement; a re-placement (column <-> floating)
flies nothing. The card matches SidePanel's vertical bounds but is not a SidePanel tab
or a second panel. Escape is handled on the card subtree, not the window, so it
cannot close Profile from the roster switcher or another composer. While a
pushed page is open, the covered tab subtree is both `aria-hidden` and `inert`,
so keyboard focus cannot reach controls behind the pushed page. Pushing About or
Notes moves focus to the back control (New schedule focuses its own form), so
Escape still pops the page, and popping returns focus to the control that opened it.

The Crewmates page's SidePanel visibility has two flags, one per placement
(`panelChrome`). Docked beside the thread, it reads the persisted
`mc-members-panel-open` (`usePersistedBool`, default shown), and the header's
panel opener is withdrawn while the docked panel is open, because the panel's
own strip carries the close control. As an overlay it reads a separate
per-visit flag that starts closed and is reset whenever the panel docks, and
the opener always stays. The panel chord toggles only while a crewmate is
open.

The Profile tab holds the crewmate's own settings (`CrewProfileSettings`):
permission (Normal / Reads / Trust), model and effort. They are changed there,
never in the chat. Each pick writes the crew record (`approval_mode`, `model`,
`reasoning_effort` on `PUT /api/agents/{name}`) and then the live DM slot, so
the open thread follows at once. `approval_mode` is `""` until the user picks one, and while it is `""` the
thread opens in `trust` (`_seed_member_approval` seeds the slot from `mode or
"trust"`); picking `Normal` is what opens it without trust, and `trust_reads` /
`trust` are applied as stored. A stored choice is applied as stored, a grant already on the slot is
kept, and the seed runs once per in-memory slot (`_member_approval_seeded`).
YOLO is never a crewmate setting: it is process-global.
The permission PUT is compare-and-set: the profile sends
`expected_approval_mode` (the value it last read), and the server refuses a
stale write with 409 `approval_mode_conflict` carrying the stored value, which
the profile then shows. An applied write also moves the crewmate's live DM
slot, in the same handler and under the same config lock, through
`apply_approval_mode` (the mutation half of `POST /api/chat/mode`); the browser
never writes a thread's permission. The profile locks its pickers while the
thread is opening or refused.

The DM's composer is `CrewComposer`, handed to `ChatPane` as `composerInput`.
It is the shared `ChatInput` (text, attachments, Stop while work runs and the
composer is empty, and an ordinary Send icon -- never the steer/queue split --
once the person types), in responsive mode without the session toolbar line: no agent, model or effort chip, no context
meter, no approval picker, and no prompt optimizer (as in the pop-out crew
window). With that line gone the toolbar holds only attach, mic and send, so
the box is ONE row -- a capsule: attach, the text, mic, send on a single line
-- while the draft fits on that line (`ChatInput`'s `inlineToolbarWhileFits`).
The text wrapping, a newline, or a staged file, quote or session ref moves the
toolbar under the text (the ordinary two-row shape, 16px corners); cutting the
draft back to one line brings it back up. Fit is measured on a hidden probe at
the single row's column width whatever shape the box is in, so the boundary
does not flutter. Only that change of shape animates (the box's height and the
text column slide together, ~200ms spring, none under reduced motion); a line
wrapping inside the two-row shape lands at once, and the buttons never move --
they sit at the box's bottom edge in both shapes. The glass rim is sized from
the two-row height in both shapes (`rimSize`), so the lit edge keeps one width
as the box changes height. The ordinary chat composer is unchanged.

A turn that ends on an error (a Bedrock throttle, a dropped connection) or never
answered gets the ordinary chat's Resume in two places: the composer's Resume
pill, which takes the send slot while the draft is empty (the placeholder then
reads "Last turn was interrupted -- press Resume, or just type"), and the Resume
button on the newest error row. The DM is never the store's active slot, so
`ChatPane` asks about its own slot (`useChatPaneContinue`):
`selectContinuable(state, slot)` and `selectTurnInterrupted(state, slot)` read
that slot's rows, its stream state and the slots frame's `running` / `stopping`,
not the active mirror, and neither Resume shows while the pane's own send still
waits for its answer (the typing-bubble wait, below). Either press is
`POST /api/chat/slots/{slot}/continue`, sent by `useContinuePress`, the same
hook ChatPage uses. The gateway re-checks
under the slot lock, and a refusal shows above the composer as "Couldn't
continue" with the gateway's reason; a turn starting retires it. An error card
with a remedy of its own (a context overflow, a repeated session start, a
refused feature request, a refused app agent) withholds Resume from the composer
too, through the one predicate `newestErrorOwnsRecovery` that ChatPage reads. A
split-view pane of an ordinary chat is the same `ChatPane` and offers the same
Resume.

Placement is decided when the card opens, and two things revisit it. A window
that crosses below `md` while the card holds its column re-places it as the
floating card — kept open, not dropped — so no fixed-width aside stands on a
phone-width viewport; the two placements are different subtrees, so the move
remounts the card body, and with a typed Schedules draft at stake the column is
kept instead (the same call the cross-tab SidePanel reveal makes: a misplaced
column beats a draft discarded with no question). And the card is about the open
crewmate: whenever `activeName` changes — a crewmate switch, a team open, the
phone's Back, a bare `/members` — `profile` is cleared, with no guard of its own,
because every one of those exits already asked the Schedules draft question
before changing the crewmate. Without that clear the card popped open by itself
over the next crewmate, on the previous crewmate's tab (the card is keyed on
name + requested tab + accepted-open nonce).

Profile uses the shared `Tablist` component with `labels="active"`: all four tabs
carry Lucide icons and accessible labels, while only the selected tab paints its
word. The tabs are, in strip order, **Sessions**, **Schedules**, **Goals**, and
**Profile** (the work first, the identity card last). The card opens on Sessions
unless the opener names a tab (`openProfile(tab)`; today only the quiet-chat link
calls it, and it asks for Sessions too). No
tab carries a count, and there is no placeholder tab for a view that does not
exist yet.

**Profile** shows only the face and name in its head. Its body carries the crew
description (two lines with a pushed full-text page); when the registry read fails,
the About section shows the shared actionable `ErrorNotice` above any retained
description, and only omits the no-description claim when no cached description
exists. The read-only Monitor section follows About. Every avatar and Profile surface shares the normalized record and strict `isAutomationActive` visual predicate through `CrewMonitoredAvatar`; that predicate extends `isAutomationRunning`, the broader lifecycle truth that keeps unsupported active records inside chat-sidebar close safety and its running filters. The Members page uses the strict predicate for its roster mark, header label, face flight, and **Patrolling** filter: a non-actionable record is lifecycle-busy but cannot truthfully make a visual on-watch claim. Live Redux frames remain the first source for roster and header visuals, while Profile's existing `session-automation` slot query supplies retained stopped state and full owner-gated details. A failed owner-gated Profile read deliberately hides its local avatar mark and detail rows even when Redux still has a reduced live record; the roster and header keep their live mark, but Profile never turns a failed authorization or refresh into a detailed claim. A failed roster refresh likewise discards its last-good legacy fallback, because stale active state cannot support a current visual claim. Missing cold reads cannot invent an active mark. It shows state, subject type, subject, cadence, current/maximum checks, runtime-clock start, next check, used/maximum runtime, and a retained stop reason; an absent check cap says **No limit**, while unavailable legacy runtime says **Unknown** rather than looking empty. A failed read shows only its State error with no recovery promise; loading says it is checking, never that it failed; a ready slot with no monitor says **Not watching anything** on one line. Structured targets and full state come only from the existing owner-gated `/api/monitors/slot/{slot_key}` read. The ungated compatibility read contributes legacy cadence and counters, but Profile never renders its prompt, banner, sentinel path, or session data. The section contains no control or action handler.
Memory and
Workspace sit side by side, but Memory is a flat readout — a solid `border-border`
hairline, transparent background, no hover and no pointer; never dashed, which is
this card's empty language (Sessions empty) and would make a
set binding read as not set up — while Workspace keeps the filled clickable-card
treatment with its hover. Notes, Permissions, and Model follow.
The pencil, Permissions, and Model entries open `CrewEditorModal`;
the card introduces no configuration writer. Memory is a non-interactive binding
readout — it never opens the unrelated generated Dashboard. The Workspace tile's
label is the crew record's `workspace` field shown as-is: that field is the
configured workspace's NAME (a key into the workspaces list, `default` for most
crewmates), not a directory, so no folder name is taken off it. Workspace opens
SidePanel Files, and Files / Terminal are rooted in `projectDir` = the live DM
slot's `project` — the directory the backend resolved that name to when it opened
the member slot (`default_project_dir`) — never in the name itself, which rooted
them in a folder literally called `default`. When Profile occupies its docked
column, an accepted Workspace navigation closes that card before revealing
Files; otherwise the card and side panel would both remain docked, and the
schedule draft whose discard was just accepted would stay mounted. A floating
Profile remains mounted because Files opens beside it, so Workspace from a
floating card never asks the Schedules draft question: that question belongs
only to an exit that unmounts the card, and asking it there let "keep my draft"
block Files for a draft that was never at risk. The Notes row's sub-line says what
tells it apart from the Memory tile beside it ("Its own working notes ·
read-only"), since "Private to this crewmate" and "What it keeps in mind" read as
one thing.

**Notes** is a pushed page inside Profile and renders the crewmate's self-maintained
briefing (`members/<slug>/briefing.md`) read-only, as markdown, through
`GET /api/members/{slug}/briefing?member=<name>`. It shows no repeated identity
row: the pushed-page bar already names the crewmate and Notes. A line above the
body, rendered in every state, says the crewmate writes these notes for itself
and that they can be read here but not changed. The response carries
`slug`, exact `member`, `supported`, `text`, `updated_ts`, `redacted`, and
`truncated`. Empty text plus a null timestamp is the normal no-notes state.
Unsupported platforms fail closed and say that notes cannot be read on this
computer.

The briefing has no dashboard editor or file pointer. Its bounded reader redacts
exfiltration URLs and credentials before applying `MEMBER_BRIEFING_MAX_CHARS`,
then drops a split tail. The prompt path composes the same helpers. The read opens
the unresolved `members/<slug>` component through the pinned `O_NOFOLLOW` walk,
so replacing a member directory with a symlink cannot redirect the read. The exact
member must derive the slug, exist, and be the only crew deriving it; a collision
returns `409 briefing_slug_ambiguous`. Redaction and truncation are visible lines,
not tooltips. Owner gating, app-token denial, and the `members.briefing.read` SEL
record are unchanged.

**Schedules** first shows the next enabled wake, then one `CrewScheduleList` row
per schedule attributed to this crewmate. Each row states its name, active/paused/
running state, cadence, previous run, result, and next run. A failed list read
shows an actionable `ErrorNotice` above any retained rows instead of replacing
them. A failed previous run remains an inline row status and also produces one
shared actionable notice below the list, outside the row buttons. It carries no
strip badge. A row
opens that job's detail on `/schedule?job=<id>` (edit, pause, run live there);
the explicit `Open Schedule` footer opens `/schedule`. `New
schedule` pushes the existing
`CrewWakeSection` create pane into Profile with its form already open, so the
single New schedule action lands directly on the requested task. The pane is
mounted `chromeless` there: the pushed page's bar already says "New schedule" and
holds the one Back control, so the section withholds its own heading row (the
"What wakes this crewmate" title, the New / Cancel toggle and Open Schedule) —
two titles and two close controls stacked on one short page read as two things to
dismiss. Back is the one exit and still runs the draft guard. With no toggle to
return focus to, the collapse after a save lands focus on the section itself
(`tabIndex={-1}`, script-focusable only) rather than letting it fall to the body.
The form's
pinned-crew hint is the host's sentence here too (`pinnedHint` →
`JobForm.lockedAgentHint`, `schedules_pinned_hint`: "Created from this crewmate's
profile, so the job runs as this crewmate"), like the section's `heading` /
`blurb` / `emptyLine`: the editor's own copy still names the crew editor, and only
the editor shows it. Attribution,
validation, and the write path are shared with the crew editor rather than copied.

A private schedule's `member_id` is the crewmate's immutable slug; `agent` and
`agent_sequence` remain display-name fields. `wakesCrew` therefore receives both
slug and name. A schedule with neither a `member_id` nor a bound agent/sequence is
owned by no crewmate and stays only on `/schedule`, including for the default
crew. `ownedOnly` suppresses only that fallback; legacy name-attributed schedules
still list. The Profile surface never needs to read which crew is default.

The pushed create pane reports dirty and saving state through `CrewWakeSection`.
Profile close, pushed-page Back, a local SidePanel reveal that folds Profile,
the quiet-chat link to Profile Sessions, switching crewmates, opening a team,
session navigation, route navigation, browser Back, and unload all use the
page's existing schedule guard. Cancel keeps Profile and its draft mounted. A
SidePanel hide (including the panel chord and overlay scrim), a transcript file
open, and a Side Chat switch do not ask: all three leave the separate Profile
card mounted, so no draft is at risk. A SidePanel reveal received from another
browser tab cannot be declined, so it leaves a dirty Profile column mounted
instead of dropping the draft; without a draft it keeps the ordinary
fold-to-pill behavior. A save in flight refuses an exit that would unmount
Profile. A typed draft asks once; accepted discard releases only that exit. Side
Chat remains a dynamic tab, and **Ask about this** keeps its synchronous
contract: it focuses Side Chat and returns true while the Profile draft remains
mounted.

**Sessions** lists only live dashboard sessions whose `created_by` is the
crewmate's DM slot. Rows use the shared status precedence (approval, question,
running, idle), link to that session through the page's guarded session exit,
preserve stable ordering while the set is unchanged, and fold after five rows.
Child subagent runs remain presence for the roster but are not dashboard
sessions. Every page-level request to open Profile — including a second use of
the quiet-chat Sessions link after the reader changed card tabs or pushed a
page — advances the Profile open nonce and remounts the card at the requested
initial tab. The nonce advances only inside the action admitted by the schedule
draft guard, so a veto neither remounts the card nor loses its pushed form.

**Goals** (`CrewGoals.tsx`, `CrewGoalsPane`) is the crewmate's goal record
(`member_goals.py`), shared by the user and the crewmate, never written to
`member-rules/`:

- the **goals**, a list with no cap on its length (`goals: [{id, text,
  created_at, updated_at, updated_by}]`, each id `g` and 8 hex digits, each
  text up to `GOAL_TEXT_MAX_CHARS`). The user adds one (`PUT
  /api/members/{slug}/goals/long-term` without `goal_id`; `expected: ""` means
  "no goal yet" and is refused once there is one), replaces one (`goal_id` plus
  a compare-and-set on `expected`), and removes one (an empty text with its
  `goal_id`, or `DELETE .../goals/items/{goal_id}`, which keeps it and which
  milestones served it as `removed_goal` for one inline **Undo**, `POST
  .../goals/items/{goal_id}/restore`). Two goals with the same text are refused
  (`duplicate_goal`). A goal is numbered by its place in the list, and that
  number is what the tab, the card and the crewmate's context all show. The
  crewmate only proposes one (`set_goal` -> a `crewmate.goal` card: add, or a
  new text for one goal by id). A record written before goals were a list holds
  one `long_term` string: it reads as a one-goal list under
  `member_goals.LEGACY_GOAL_ID` and is written in the list shape on its next
  write;
- **key results**, one shared set with no cap on its length, `{id, text,
  target, current, updated_at, readings[], goal_ids[]}`, labelled
  **Milestones** on the tab (the code, API and tools keep `key_results`).
  `goal_ids` names the goals a milestone serves, none, one or several, set by
  the crewmate when it proposes the set (`set_key_results` items carry optional
  `goal_ids`); an id that is not one of the record's goals is refused
  (`unknown_goal`), an item without `goal_ids` keeps the links of the key result
  it matches, and removing a goal strips its id from every milestone. Each row
  shows the text and the numbered circles of its goals, the latest reading in
  the crewmate's
  own words, the target and when it was last read; a reading is free text of up
  to `member_goals.KR_READING_MAX_CHARS` (120) characters, a target of up to
  `KR_VALUE_MAX_CHARS` (40). Opening a row (a button with `aria-expanded`) shows
  its earlier readings and its edit and remove actions.
  The crewmate proposes the whole set (`set_key_results` -> a
  `crewmate.key_results` card) and records readings directly
  (`key_result_progress`); readings are kept 90 days (at most 200). The user
  edits one by replacing the set (`PUT .../goals/key-results`) and removes one
  with `DELETE .../goals/key-results/{kr_id}`, which keeps it, readings and
  place, as `removed_key_result` for one inline **Undo**
  (`POST .../goals/key-results/{kr_id}/restore`); any later write to the set
  ends that Undo. An Undo that can no longer apply (no goal, or the same key
  result back) is refused `stale_undo` and cleared on disk in the
  same write, so the next click answers `undo_unavailable`. A key result is
  known by its text and target, so a set
  holding two with the same pair is refused (`duplicate_key_result`, and the
  card's params too). An edit keeps its draft on the key result's id and saves
  every item with its `id`, so a reworded key result keeps its id, reading and
  readings; an `id` outside the stored set is refused (`unknown_key_result`).
  A card's set carries no ids and matches by text and target. The edit saves
  against the set as it read when editing began, as a goal's edit
  saves against the text it started from, so a change made meanwhile is refused
  rather than overwritten. A row never read shows only its title and its
  **Aiming for** line, with no placeholder. Neither list has a count cap: the
  crewmate is guided toward a small set instead (the `set_goal` and
  `set_key_results` descriptions, the goal-setup nudge and its own context say
  that a few goals and milestones are easier to track, and to suggest merging
  as the list grows), and the whole record stays inside its 64 KiB byte bound;
- the tab has **two pages**. The first only shows state, one card each with its
  icon after its label: the goals (the card itself is not a button: one row per
  goal, its number on a coloured circle then its text, each row a door to that
  goal's page; a **+** icon button, labelled "Add a goal", at the label's right
  opens that page with an empty editor and is always shown), the milestones
  (each row a door to its own page: the latest reading large, every earlier
  update, Edit milestone and Remove milestone), the check-ins as three tiles
  saying on or off and when they run (the card is a door to the switches and
  pickers), a full-width **Add something {name} should track** row, and the reports. The
  goal colours are five theme tile hues (`--tile-blue`, `--tile-purple`,
  `--tile-pink`, `--tile-teal`, `--tile-indigo`), never the ok/warn green and
  yellow, picked by list position and cycling past five; a goal's number always
  goes with its colour. A goal's page edits, saves and removes it and lists the
  milestones that serve it, read-only. Every edit happens on a page pushed over the profile card
  (`ProfilePagePushContext` in `CrewProfilePanel`, the same slide and back
  control as About and Notes); outside the card the page replaces the first
  page with its own back control. A removed milestone or goal returns to the
  first page, where its Undo row is. Error notices offer the existing agent
  hand-off for draft-free check-in, Undo and milestone-removal failures, but
  keep it off while a goal, milestone or tracking form holds unsaved text.
  Goal, milestone and tracking drafts publish dirty/busy state into Profile's
  existing leave and automatic-placement guards. Close, Back, Cancel, leaving
  Goals, route/browser navigation and member switches ask before dropping a
  dirty draft; an in-flight save or tracking send refuses those exits. Automatic
  narrow-viewport relocation and a cross-tab panel reveal keep the same Profile
  mounted while either state is at stake. If the goal or milestone a dirty draft
  edits is removed elsewhere, the page stays open, says the item is gone and
  shows the unsaved text read-only; only its Discard button clears it. A
  successful save clears its draft;
  failed saves preserve it. Writes are serialized while one is pending;
- **Add something {name} should track** opens a page with a text box; Send
  posts what the user typed as their own message (`"Please track this for me:
  <text>"`, localized) into the crewmate's thread through the
  ordinary send path (`chat-core/transport/sendTurn`), so it shows in the
  transcript as anything they typed would; no hidden nudge. Only a `dispatched`
  or `queued` receipt clears the input and closes a floating profile card so
  the reply is in view; a docked one stays. A refusal retains the input with
  the failure notice. `unknown`, `response-late` and `transport-error` retain
  it with the shared unconfirmed-delivery notice: check the conversation before
  resending. No outcome automatically resends. What the crewmate
  then tracks is an item in its work ledger (the Crew board's items): the
  crewmate's own context teaches it to record what it is asked to track there
  and to call it tracking, never a ledger, when it talks to the user. The Goals
  tab shows no tracked item;
- three **check-ins**, each its own switch, off by default and disabled without
  a goal, sharing one time of day: **Daily Goal Reminder**, **Weekly
  report** (a weekday) and **Monthly review** (a day of the month, 1-28)
  (`PUT .../goals/schedules`); each row is its label and its toggle, and the
  weekday and day-of-month pickers show under their switch only while it is on
  (disabled with no goal). Each switch keeps exactly one gateway schedule of
  its own (`managed_by` `goal-<cadence>:<slug>`, `dashboard/goal_reminder.py`),
  removed when it turns off or the last goal is removed. A save leaves a cadence
  whose job already matches (enabled, same cron expression, timezone and name)
  untouched, so its id and run history survive another switch's save; only a
  changed cadence is removed and added again. A save and a goal clear hold
  one per-crewmate lock (`handlers.member_goals._schedule_lock`) across the
  record write and the reconciliation, which reads the record again inside it,
  so concurrent saves never remove each other's schedule. When one fires the gateway's
  cron callback first runs the same fire-time governance gate as a message job
  (`_await_cron_fire_time_gate`, audited as `cron_goal_checkin`), so a tightened
  `capabilities.cron` ceiling denies it with no turn started; otherwise it
  runs it as one hidden gateway-authored turn in the crewmate's
  thread built from the current record, which lists every goal by number and
  each milestone with the goals it serves: the daily turn takes fresh readings and
  then continues with the crewmate's next task; the weekly turn writes a report
  from the readings' movement and the tasks the crewmate finished in the week;
  the monthly turn reviews the same, and
  whether the goal and key results still fit and, when they no longer do,
  proposes the replacement in that turn as a `crewmate.goal` or
  `crewmate.key_results` card (nothing changes until the user presses it). Any
  turn in the crewmate's own pinned thread may raise its `goal`, `key_results`
  and `check_ins` cards through `member.propose_self`, whether or not the user
  sent it (`handlers.guide.REQ_OWN_GOAL_TURN`; a channel turn is refused);
  every other card, and `propose_change`, stays user-turn only.
- the **goal-setup nudge** (`dashboard/goal_setup.py`): adding a goal or really
  changing one's text (the Goals tab or an applied `crewmate.goal` card; not an
  Undo, not an edit at or above 0.9 similarity) sends the
  crewmate one hidden turn once its thread is idle (waiting at most ten
  minutes; a newer save, of that goal or another, replaces a waiting nudge).
  It names the goal that changed by number and id, lists every goal, and asks
  the crewmate to say back that goal in a sentence, research it in ONE
  background `spawn_run` sub-agent and end the turn so the chat stays free,
  then propose milestones with `set_key_results` (each linked with `goal_ids`)
  when the result comes back, keeping the set small and suggesting merges as it
  grows. A goal removed before the turn would run sends nothing; removing a
  goal on the Goals tab instead replaces that waiting nudge with a
  `[Goal removed]` turn (`schedule_removed_nudge`, same wait): it quotes the
  removed goal, lists what is left (or says no goal is left and every check-in
  was switched off), names milestones that served only that goal, and asks the
  crewmate to acknowledge in a sentence, stop that work and propose dropping or
  relinking those milestones with `set_key_results`. An Undo of the removal
  before the turn runs sends nothing. Applying that
  `crewmate.key_results` card (its Apply step sets
  `REQ_CARD_KEY_RESULTS_APPLY`; a Goals-tab edit and an Undo do not) always
  sends a second hidden turn, `[Milestones confirmed]`, the same way, listing
  each milestone with the goals it serves: with no
  check-in on it offers them with `set_check_ins`, otherwise it says what the
  crewmate works on first.
- the **`crewmate.check_ins` card** (`set_check_ins`): turns the Daily Goal
  Reminder, Weekly report and Monthly review on or off and sets `weekday`,
  `month_day` and `time`, writing through the schedules route, which is a
  card-hooked route (`change_card_catalog.HOOKED_ROUTES`) like the goal and
  key-results routes, so the card finishes and offers Undo; Undo puts each
  field it changed back and leaves the other check-in fields as they are.
  Apply carries the preview's values for the fields it writes as `expected`;
  Undo carries the values its Apply wrote, never a post-admission read. Both
  compare those values inside `member_goals.set_schedules`' locked mutation
  and refuse `changed_meanwhile` if another write won. Expectations must be
  valid schedule fields covering every field being written. Same-value saves
  are allowed, and a supplied no-op field is neither written nor undone.
  Ordinary owner requests without `expected` keep their partial-update behavior.
  If schedule synchronization fails, switching the requested cadences back off
  also compares against the complete schedule values that write returned, so
  rollback cannot erase a later schedule edit.
  A switch turned on with no goal is refused (`no_goal`). Weekly and monthly reports are saved with
  `save_goal_report` as Markdown artifacts under `Crewmates/<name>/Reports`, the
  user gets one notification linking it, and the thread carries one short line.
  A check-in that fires with the thread not open or busy does nothing that time;
  the weekly and monthly ones check again after reading the work ledger, so a
  turn that started meanwhile never carries the review mark.
  The check-in jobs are not listed on the Schedule page or in the crewmate's
  Schedules tab (`GET /api/crons` drops them through
  `goal_reminder.is_reminder_job`); the switches are their only control;
- the latest **reports**, each linking its artifact.

What the crewmate is doing now is its work ledger, not the goal record: the tab
holds no short-term list and no link to the ledger. The weekly and monthly
prompts read finished tasks through `goal_reminder.finished_tasks`, which lists
the thread's own ledger through the Crew board's reader
(`work_ledger_board.closed_items` on `session_ledger.ledger_key(slot_key)`; the
goal reminder does not import the store itself), keeps the
items closed (`is_terminal`) inside the window by `closed_at`, at most 30, each
`{title, state, summary}` scrubbed; a thread with no ledger lists none, and the
prompt then asks the crewmate to summarise from its own work log and say so in
the report. A record written while short-term goals existed still carries
`short_term` and `last_crewmate_change`: `_normalize` reads past them and the
next write drops them.

The record lives at `trust/member-goals/<slug>.json` and has the same
protection as `member-rules/`: the tool gate (the sensitive-path floor covers
`trust/`, so the crewmate's file-write and edit tools are refused there), not an
OS seal (`trust/` is a read-write leaf of the crew sandbox, so a sandboxed shell
command can still write it). Because that shell can also plant a symlink there
and the gateway writing the record is not sandboxed, the record is never reached
through a link: `trust`, `member-goals` and the record are each opened relative
to the one before with `O_NOFOLLOW` and written inside that pinned directory
(`member_goals._open_goals_dir`); a link at any of them is refused as
`GoalsUnreadable`, so the gateway cannot be pointed at `profiles/` or another
sealed leaf. The record itself is opened `O_NONBLOCK` and must be a regular
file, so a FIFO planted there is refused instead of hanging the read and the
per-member lock. Without `openat` (Windows) each component is checked with `lstat`
first. It is name-scoped like the rules file. Crewmates created before this
change start with no goal and every check-in off.

The record is held to `member_goals._RECORD_MAX_BYTES` (64 KiB) at write time,
measured on the exact UTF-8 payload written, which is also the read cap: the
oldest readings are dropped until it fits, and a write that still does not fit
is refused `record_too_large`, so a record is never too large to read. Every
goal string leaves the record through the dashboard's exfiltration-URL and
credential scrubbers (`member_goals.scrub_text`): the Goals tab and `goal_read`
view (`public_view`) and the saved report's title and Markdown. The goal
and key-results compare-and-sets accept the scrubbed form the page was shown as
`expected`, and a saved string that is exactly the scrubbed form of the stored one
keeps the stored text (`member_goals.keep_unscrubbed`), so saving one milestone
never replaces another's real text with redaction placeholders. Deleting a crewmate removes its check-in schedules
(`crew_removal._remove_goal_check_ins`) unless another live crew still derives
the slug; the goal record stays, like the rules file.

Since such a crewmate has a goal and no description, the Profile card's About
shows its first goal, marked **From the goal**, while the description is
empty; every other reader takes an empty description as it always has. A goal
record that cannot be read says so there (`ErrorNotice`), instead of reading as
"no goal".

A `crewmate.create` card writes its `goal` here as the new crewmate's first
goal, never as `description` (the `POST /api/agents` step sends none). The goal
is its own apply step after the create (`CREWMATE_CREATE_GOAL_STEP`): a `PUT
.../goals/long-term` with `expected: ""`, its path segment filled from the
`member_id` step 0 returned and its `member` from step 0's `name`. Its optional
`schedule` is the crewmate's work schedule and follows as the last step
(`CREWMATE_CREATE_SCHEDULE_STEP`), unchanged: the same job name and message, never
a goal check-in. A failed step stops the plan, so a goal write that fails leaves
the card `partial` saying the goal (and any schedule) was not saved, and its Undo
removes only the crewmate.
A card persisted before goal records (goal in `description`, schedule at step 1,
`change_card_catalog.legacy_create_apply`) still rebuilds, in either shape a
released build wrote: with step 0's `first_greeting: true`, or from the builds
before it without that key (`first_greeting=False`). Unstarted, it runs
the current plan; started, it keeps its plan and evidence indexes, and its Undo
finds the schedule by the step-1 evidence `id` (`_create_schedule_index`), as
does the after-read its Undo compares (`change_cards.read_state`).

A `crewmate.key_results` card snapshots the full records too
(`key_results_records`; `change_cards.comparable_state` compares the
`{text, target}` shape, the records' ids and the goals each serves, never their
readings). Apply sends that preview's ids and links as `expected_ids` and
`expected_links` alongside its `expected` wording; `set_key_results` validates
these expectations and compares all three inside the same locked mutation that
writes the set. A links-only edit or same-wording recreation after the card's
early snapshot check is therefore refused, while a fresh reading or same-value
save is not a conflict. Its Undo
sends them as `restore`, with the ids its Apply wrote (that step's evidence
`ids`) as `expected_ids` and the goals each served (evidence `links`) as
`expected_links`, which `member_goals.restore_key_results` compares under
the record's lock. A set recreated since with the same wording carries new ids,
and a newer card that changed only which goals a milestone serves changes
`links`, so a stale Undo is refused rather than replacing it and its readings. The plan
is public (`CardStore.public`), so every string in it, the replaced set and the
restored records with their readings, goes through `member_goals.scrub_text`.
What goes back is the raw text: the card's Undo step hands the routes its private
`before` snapshot (`change_cards.REQ_CARD_RAW_RESTORE`), and a value in the body
that is exactly the scrubbed form of the snapshot's is restored raw.
The restore keeps every key result the card kept exactly as it is now, readings
recorded since the card included, and brings back from `restore` only the ones
the card removed, with their ids, readings and current value. A card's goal
text and key results are cleaned as the record stores them
(`member_goals.clean_text`), so its after-read shows the write.

The side panel's **Dashboard** tab shows no goal summary: it is the crewmate's
own page, and the goal, its milestones and the reports are on the Goals tab
only.

The chat SidePanel has two standing entries on this page: the leading
**Dashboard** tab and pinned **Files**. Dashboard renders the crewmate's dynamic
dashboard (`CrewDashboardTab`, read from `GET /api/members/{slug}/dashboard`) for
every reader: the page fills the tab, with no summary card, Contained bar, Expand
control, identity row or Command Center around it. A crewmate with no adopted
page (the read answers `state: "empty"` with the generic default, no bound
package, and no shipped page named for its slug) shows the SUGGESTIONS page
instead of the default template the read still carries (`CrewDashboardEmpty`): the
crewmate's face under a first-person bubble and three prompts that only fill its
chat box (`onAct`); a staged preview and a root session's Overview are not
affected. A refresh opens the Dashboard tab rather than the strip's persisted
focus, once per crewmate and strip bucket per page load (`dashboardOnLoad.ts`),
so a tab picked afterwards or on an in-app return stays. Its "Opening the
conversation" line is withheld while the main column already says it
(`activeSlot` unset), so two live regions never say it at once. This page never
passes the in-chat Command Center dock opener into its `ChatPane`: the permanent
Dashboard tab is the one entrance here, so a second control would point at a
different dashboard concept from the same conversation. With no Command Center
in the tab there is no unsent answer for the page to hold or guard, so exits ask
only about a Schedules draft. The crewmate's `panel_publish` record keeps its MCP
tool and its `GET /api/members/{slug}/panel` read; no surface on this page draws
it. Files is the standard SidePanel browser scoped
to the workspace rule above. Dashboard and Files are the only STANDING tabs.
Side Chat remains available dynamically through selection Ask and the plus
menu; it is not a standing entry — and the plus menu keeps every other dynamic
view the chat page offers (Terminal, Browser, Git, Subagents, Workflows, the
Developer-Mode Logs / Context / Crew log, app-contributed tabs), bound to the
member's slot once the thread is confirmed. Withheld outright are the
transcript-fed views (Changes / Issues / Links / Pins, plus the session
Summary), the chat page's Command Center, and Artifacts — a pinned standing
tab on the chat page that has no place in this page's Dashboard + Files set
(`MEMBERS_WITHHELD_VIEWS`). Withholding that LIST does not withhold an
artifact DOCUMENT: `SidePanel.isWithheld` ties every document tab (file, diff,
folder, artifact) to `'files'`, which this page withholds only while the
thread is unconfirmed, so an artifact opened from a reply link gets its own
tab here while the Artifacts list stays off the strip (#18320; before that
rule an artifact tab was keyed to `'artifacts'` and so appeared and vanished
in one frame).

SidePanel's resting width on this page is 60% of the row after the live navigation
rail, clamped so `CHAT_PANE_MIN_W` remains; a persisted user drag wins. The shared
`SidePanel.defaultWidth` prop changes only the no-preference fallback, leaving the
chat page at 460 px. The Profile hover card uses 34% of the row and never inherits
that width.

The remaining settings — built-from template, memory binding, cloud, routing —
live only in the crew editor. Operator-facing memory diagnostics never render in
Profile or SidePanel.

## One-time prune of sync-generated crewmates (startup migration)

Older dashboards called `POST /api/agents/sync` on every chat mount, and that
sync enrolled discovered user and package specs as crewmates: a `config.agents`
row with no `member_id`, on the shared `default` memory store, bound to the spec
by name and stamped with the spec's discovery source (`builtin`, `package`, or
`aim`, the package source's older name). An existing install therefore carries
one crewmate per synced user or package agent, most never opened.
`crewmate_prune_migration.py` runs once
at dashboard startup (`start_dashboard`: kicked as a tracked background task
right after the listener binds — `_kick_crewmate_prune`, the same shape as the
other post-bind `_kick_*` calls — and awaited by nothing on the startup path,
so the scan never gates readiness. The slot restores do not wait for it
either: a removed row's DM thread held no turn, and a session that ran its
agent elsewhere resolves the same name onto the installed agent on the default
crew's workspace and memory — the binding the removed row carried — so no
restore depends on the row. A session with no execution record gets that from
the installed-agent lookup in `resolve_agent_bindings`; one whose record names
the crewmate as a `member` selection does not, because a member selection never
takes that lookup, so the record decoder re-reads exactly the shape this pass
removes as that agent (see "Records that picked a removed row" below), and every
other missing member stays refused. A recorded `kirocrew-skill-view-*`
name is mapped back to its source agent through the projection's sidecar
(`source_agent_name`) before either lookup. The headless `start_api_server` / `--slack-only`
entrypoint has no dashboard and does not run it) and settles that without any
UI — removal only; nothing is created or rebound:

- **Candidate** = a row that is exactly what the sync wrote: its name is its
  `kiro_agent`; its raw `source` is a string in `builtin`, `package` or `aim`
  and matches the installed spec's source, treating `package` and the legacy
  alias `aim` as one value; and every field the sync did not copy from the spec
  is at its default — no `member_id`, the `default` store, no model, effort,
  triggers, colour, star, avatar or workspace, and no key the record does not
  declare (`_is_fresh_sync_shape`, tested on the raw row as `config.json` holds
  it; a missing declared key reads as its default). Description is copied from
  the spec and exempt from the default comparison, so editing it does not keep
  a never-chatted generated row. The spec must be installed and must itself
  have a source in `builtin`, `package` or `aim`; a missing spec, a row or spec
  stamped `kirocrew`, a non-string or mismatched row source, and a crew's
  private copy (`private_to`) are never candidates. Nor is any row bound to one
  of the runtime's own agents: a spec discovery marks `kirocrew_owned` — the
  conductor, worker, knowledge, research and heartbeat specs, which read as
  `builtin` because `agent_discovery` deliberately keeps that flag apart from
  `source` — is skipped whatever its source, so rows bound to the runtime's
  own agents (kirocrew-owned specs, and rows stamped `kirocrew`) are never
  removed. Both config layers are
  read: a name that `config.local.json` mentions in its own `agents` section —
  a `kirocrew config set --local agents.<name>.…` leaf, the capability writer's
  overlay binding — is never a candidate, whatever the leaf says, because
  deleting the base row would leave the overlay leaf as a crewmate bound to
  nothing. Nor is a crewmate any team lists (`crew_teams.read_teams`, the
  member names as the `teams.json` document in the `crew-teams` data-home directory holds them): placing it on a team is
  the owner's own act, so a teamed crewmate is never removed whatever its
  shape. A created crewmate has a `member_id` and is never removed. No memory
  directory is inspected. A row bound to a skill-view alias
  (exactly the prefix `kirocrew-skill-view-` plus 24 lowercase hex digits, the
  name the projection writes; any other tail is an ordinary row) is
  the one exception to the installed-spec test: the runtime writes those files
  to project a spec's skills, discovery never lists them, and an older sync
  that walked the agents directory enrolled one crewmate per alias file. Such
  a row is judged on its own string stamp (`builtin`, `package` or `aim`) and
  fresh-sync shape; every other rule above -- `member_id`, overlay, team,
  chatted -- still applies.
- **Chatted** = the crewmate's own Crewmates-page DM thread holds a turn, and
  nothing else counts. Its transcript lives at `sessions/dashboard_<slot
  key>.jsonl`, the slot key derived from the slug (`member_slot_key`) and, when
  the DM binding records another, that one too; compaction's archived
  segments (`sessions/archive/<stem>__<stamp>.jsonl`) count the same as the
  live file. A turn is any line past the metadata record — not parsed, since
  even a torn row proves one was being written — or a first line that is a
  message rather than a metadata record (an older build's). A session
  elsewhere that ran the agent — a subagent spawn, a cron job, an app's own
  slot, a plain chat that picked the template — used the AGENT, which stays
  installed, not the crewmate. Opening the thread writes the DM binding and at
  most a metadata line, so a crewmate that was only clicked in the roster is
  not chatted either. The binding is read strictly, never through the
  total-by-contract `read_dm_binding` (which answers "not bound" for a damaged
  file); a binding that names another crew (slugs collide) lends nothing. Names
  that canonicalize to the same Crewmates-page thread are judged by that shared
  transcript, so a turn in it keeps every generated row mapped to the thread.
- **Never chatted → removed** (`remove_never_chatted`): the row is deleted
  through a delta mutate under the base config lock, and only while the base
  row on disk still has the same `kiro_agent`, canonical source identity and
  fresh-sync shape, `config.local.json` still does not name it — read under the
  overlay's own sidecar lock — and the bound spec, when re-read from disk under
  `agents_spec_lock`, still declares the bound `kiro_agent`, remains non-private
  (`private_to` is empty) and not `kirocrew_owned`, and has the same canonical
  discovery source (a skill-view alias row has no spec to re-read, so its row
  identity and shape are the whole test); and no team lists it, the team document re-read under
  `crew_teams.document_lock`. The three
  inner locks are taken inside the base lock, overlay then spec (the order every
  binding writer keeps) then the team document lock innermost — its own
  contract is the registry's lock first, then it, and nothing takes a
  registry, overlay or spec lock while holding it — and all three are
  held until the base write has committed, so neither an overlay leaf for the
  name, a spec replacement nor a team write placing the name can land between
  its check and the delete. The
  fence is identity and shape, not equality with a default-filled snapshot, so
  a row written by a build whose record had fewer keys is still recognised. A
  row whose identity or protected shape changed, a spec that vanished, became
  one of the runtime's own or
  stopped reading as a record, a row that gained an overlay leaf, or a row a
  team came to list is
  **refused**: a refusal is not a commit, so the pass writes no marker, logs the
  names, and the next boot re-judges them. A description edit alone does not
  refuse the delete. Only the base row moves; the overlay and the team document
  are never written and
  the spec is only read.
- **Chatted → untouched.** A kept crewmate stays exactly as it is: on the
  shared `default` store, with no `member_id`. A memory binding is identity and
  is chosen only at creation; an existing member retains its exact V1 binding
  ([memory-skills-hooks](memory-skills-hooks.md#member-memory-experience-and-lifecycle)),
  and no startup pass rewrites it.
- **Doubt keeps; the pass always finishes.** A binding that cannot be
  resolved (a trust-root containment refusal included), read or parsed, or a
  transcript that is there but cannot be judged — the open or read fails, an
  archived segment disappears after it was listed, the file is empty (a
  transcript is born with its metadata line, so an empty one is a torn write),
  the first line is over the 64 KiB budget, not UTF-8 or not JSON — keeps that
  candidate, listed under `doubted` with the reason; the others are judged on
  their own threads. No conversation log at all keeps every candidate, and so
  does a `teams.json` team document that is there but
  cannot be read (`TeamsUnreadable`): no candidate can then be shown to be off
  a team. The pass still completes and writes the marker, so a bad
  file costs at most one boot's judgement and never a prune that re-runs — and
  holds every write — on every later boot; a row kept this way loses nothing
  by staying. One WARNING line names the kept-on-doubt crewmates and why. The
  one startup step that deletes a transcript, the channel transcript migration
  (`migrate_channel_transcripts`), merges but keeps its orphaned copies while
  the pass has not settled, and a tracked follow-up
  (`_kick_deferred_transcript_removal`) removes them once the pass has
  returned, off the readiness path.
- **Agent-writable paths are opened defensively.** Transcripts are opened
  through `open_file_no_reparse` (`O_NOFOLLOW` / reparse-point refusal in the
  same operation as the open, `O_NONBLOCK` so a FIFO cannot hang the pass) and
  read only when `fstat` says regular file; a link or anything else is "no
  record" — neither evidence nor doubt.
- **One process runs the pass.** The whole pass — the marker check, the
  history scan, every delete and the marker write — runs under an exclusive
  cross-process lock on `<config dir>/crewmate_prune.lock`
  (`platform_compat.file_lock`), and the marker is
  created `O_EXCL`. A second gateway on the same data home cannot run its own
  pass beside the first: its pass waits on the lock — and its own request
  barrier holds its writers for as long as it waits — then finds the marker
  and returns. The wait never gives up: every 120 s (`PRUNE_LOCK_WAIT_S`) it
  logs one WARNING and tries again, counted in the report's `lock_waits`. A
  pass that returned with the lock still held would settle its own barrier
  and let a session bind a crewmate the holder had not judged yet, and the
  holder would then delete a row in use. A holder that dies releases the lock
  with its process. Between tries the marker is checked without the lock; it
  is written last, under the lock, so its presence means every delete is
  done. A process-local event alone would let the second process bind a
  session to a candidate between the first's history scan and its delete.
- **Serialized.** `DashboardState.crewmate_prune_settled` is cleared BEFORE
  the listener binds (`_register_crewmate_prune_gate`, installed next to the
  workflow gate) and set after the pass (in `finally`). That function's
  middleware holds every request whose method is not `GET`, `HEAD` or
  `OPTIONS` — on any path, so the route table never has to be mirrored — until
  the event is set (bounded at 60 s, then 503 `prune_in_progress`). Every
  writer that can bind an agent to a session while the gateway is up is such a
  request (chat send, slot create, slot agent switch, member thread, channel
  add, session import under `/api/`, and `POST /v1/chat/completions`), so no
  session can bind an agent and no DM thread can be written to between a
  candidate's check and its delete. One READ is held too, whatever its
  method: every request under `/api/members`
  (`_CREWMATE_PRUNE_GATE_HELD_PREFIXES`), so the roster is read once the pass
  has settled and never lists a row the pass is removing. A held request that outlives the budget is answered 503 and writes
  nothing; it does not stop the pass. The writers that do not come through HTTP
  — the subagent pump, channel agent resume, cron dispatch — start only after
  `await_crewmate_prune_settled` returns (`GatewayOrchestrator.run`, after the
  memory barrier and past `KIROCREW_READY`, so readiness never waits; the
  standalone dashboard before its inline channel resume), and that helper
  returns only once the pass has RETURNED — never beside a pass that can still
  delete. Its 60 s budget bounds how long the pass may keep deleting, not how
  long the writer waits: on timeout the helper sets
  `DashboardState.crewmate_prune_abandon`, which the pass polls before each
  candidate and again inside the config lock right before each delete; the pass
  then keeps whatever it has not judged (listed under `doubted` with
  `ABANDONED_REASON`), writes its marker and returns, and the writer starts.
  The pass always returns: its opens are non-blocking and its lock acquires are
  bounded (`platform_compat.file_lock` raises rather than waits on a stuck
  holder), and either path ends in `finally`. The pass is kicked immediately
  after `_start_site` and runs alongside the rest of startup, so the hold is
  the pass itself (one marker stat on every boot but the first). The fast path
  is one `is_set()` read per request. Each candidate's check runs immediately
  before its own removal, never once for the whole list.
- **Marker.** A completed pass (a no-op included) writes
  `<config dir>/crewmate_prune_v3_migrated.json` with `{migrated_at, removed,
  kept, doubted}` — the same marker-file seam the config loader's one-shot migrations
  use (`connections_ui_migrated.json`); later boots return at once. One INFO
  line records the removal: `removed N unused auto-generated crewmates:
  <names>`. The first build of this pass wrote
  `crewmate_prune_migrated.json`; it judged only user-authored specs and
  counted any session that ran the agent, so on an install full of package
  agents it removed nothing. The next wrote `crewmate_prune_v2_migrated.json`
  and skipped every row bound to a skill-view alias, since no installed spec
  matches one. Neither older marker stops the current pass, which runs once on
  those installs too. All three markers are kept: each one's `removed` list is
  adoption provenance, and `removed_crewmate_names` unions the three, so
  deleting a marker would strand the records its pass left behind.

### Records that picked a removed row

Removing a row must never strand a record that picked it. A chat, a fork, a
subagent run or a cron job that picked a synced crewmate persists an execution
record of exactly the removed shape: `selection_kind == "member"`, no
`member_id`, the `default` store, and `selection_name == template_id`. After
the row is gone that record names a member that no longer exists, and every
reader that checks the kind used to refuse it -- a resumed or forked chat
(`Crew Member '<name>' is unavailable`), a continued subagent (`selected member
is unavailable`), and the prompt builder's member lookup.

So the record DECODER answers once for every reader:
`execution_from_record` passes each decoded record through
`adopt_removed_synced_crewmate`, which re-reads exactly that shape as the
installed template on the same `default` store when a prune marker lists its
name as removed and the name is absent from `config.agents`. A name that is
merely absent, never recorded as removed, keeps refusing. That is the binding the removed row carried, so no other store
becomes reachable. A member with an identity, its own store, or a name that
differs from its template keeps refusing, and so does a record whose config
cannot be read. A template that is no longer installed still fails at the
resolver. The spawn gate applies the same rule to the one input that is not a
record: a slot's inherited `("member", name)` selection whose row is gone keeps
its template on the shared store, and refuses on any other store.

Because the decoder adopts while the stored bytes still say `member`, a
compare-and-set must compare like with like. `bind_session_execution`'s two
CAS predicates (the persistent admission and the restricted privacy
tightening) decode the stored carrier the same way before comparing it with
the decoded record they read (`_carrier_still`). Comparing the decoded
record's `to_record()` with the stored bytes reads the adoption itself as a
concurrent change, and refuses the first send of every such chat with
`session changed during admission`. A carrier another writer changed between
the read and the CAS still decodes to something else and is still refused.

Any future pass that deletes `config.agents` rows must either preserve this
invariant or rewrite the records first. `test_crewmate_prune_migration.py`
pins it (`test_records_bound_to_a_removed_row_stay_executable`: every row the
pass removes decodes as an executable template), and
`test_pruned_crewmate_records.py` pins each reader.

Removed rows do not come back: the dashboard pickers read
`GET /api/agents/catalog` and nothing calls `POST /api/agents/sync` (`useAgents`),
so the rows this pass removes were written by older builds. The sync route's
own contract is unchanged.

There is no enrol-on-mount path from a custom agent to a crewmate; the
Crewmates page's empty state with **New crewmate** is that path. What an existing
user actually has is the opposite problem — the crewmates the old sync already
made — and that is what this migration settles.
## A crewmate's chat

A member-mode slot's transcript is the crewmate's whole working record: the
`[auto-nudge cycle N]` turns of its patrol loop, `[Cron notification …]` and
`[Subagent completion event]` envelopes, every tool call, and the say-nothing
reply a quiet patrol ends on. The Members page's chat pane shows **only what
the crewmate says to the user** and the user's own messages. Everything else stays in the slot's history and is filtered at render time by `filterCrewmateChat`
(`website/src/components/chat/crewmateBubbles.ts`), which `ChatPane` applies
when its host passes `crewmate`. Ordinary chats never pass it and are drawn
unchanged.

What is hidden: rows with role `nudge`, `inject`, `subagent`, `tool`,
`tool_call`, `tool_result`, `thinking`; and an `assistant` row that is not
speech — invisible-only content (the bare U+200B the model is instructed to
answer a quiet patrol with, `isHiddenInvisibleAssistantRow`), a gateway system
notice written under the assistant role (compaction, session reload), or an
injected workflow completion. A turn that ends without addressing the user is
therefore a turn whose final assistant text is empty: the model's own silence
is the signal, and no heuristic over the words decides for it — a row with
visible words is always shown, so a finding or a question can never be
filtered away. No "N quiet patrols" divider is drawn in their place. Rows the
user must see or act on stay: `user`, `error`, `file`, `mcp_oauth`,
`permission` (pending native and background-spawn approvals remain actionable),
question cards, change cards, the stop card (a `system` row of kind
`stop_event`, the record of the person's own Stop press, which breaks a run like
an error row), and the live `streaming` row. One `tool` row is drawn too: the
`spawn_run` launch (`isSpawnRunTool`: a tool row whose persisted output carries
the "Spawned N subagent(s)." header), rendered as the same live
`SubagentRunCard` the chat page draws through `transcriptRenderers`'
`subagent_run_tool` entry, reading the member slot's sub-agent state
(`selectSlotSubagents`) so its counts and per-worker state update in place as
workers run and return. Delegated work outlives the crewmate's own turn: it
says "on it", ends its turn, and the workers run for minutes with no typing
bubble or caption under the last bubble (`CrewmateLiveActivity` mounts only
while the crewmate's own turn is live), so without the card the chat is
perfectly still and reads as frozen. The card is a row the person sees, so it
is a run boundary like an error row (`isRunTransparent` returns false for it):
the reply after it opens a new run. Every other tool row — the launch's own ✅
sibling, `nothing_to_do`, a bare `spawn_run` call with no launch output — stays
hidden, and the `[Subagent completion event]` envelope stays hidden too (the
card's counters already show the return). Runtime `notice` rows and
compaction status cards are hidden rather than removed from history. When the
filter leaves NOTHING to draw although the transcript is not empty (a patroller
that has never spoken), the pane's empty hint is the crewmate's — "<name> hasn't
said anything to you yet." plus a verb-first second line pointing at where its visible work is ("See the sessions it is driving."). That second
line is a link when the host passes `onOpenCrewWorkLog` (the Members page does:
it opens Profile on Sessions), plain text otherwise — words that read as a
destination must be one. Never the fresh-thread "Session ready" line, which
would read as lost history beside a panel counting its wakes; and the drawer's
Recent activity counts the member's *runs* (`activity_chat_count`: one entry per
session the crewmate ran — a patrol wake, a cron, a sub-agent, a chat you
opened), not "chats", so it does not contradict a chat that just said the
crewmate has not spoken. Not "sessions" either: the RFC's vocabulary table keeps
that word out of user copy. The composer of a crewmate's chat
addresses the crewmate by name ("Message <name>…"), not the product. The
composer floats over the transcript like the main chat's (#18279; the dock
layout in
[page-layout](../../../website/docs/page-layout.md#decided-the-transcript-scrolls-under-the-composer-glass)),
so the conversation scrolls under the glass. The
pinned-prompt banner (Settings → Chat → pin last prompt) is never drawn in a
crewmate's chat (#18346): `ChatPane` turns the pin off when handed a `crewmate`,
whatever the setting says. The banner exists for long agent runs in the main
chat, where the question scrolls out of sight while the answer streams; a DM
with a colleague has short turns, the user's bubble is one row back, the
working indicator already reports liveness, and a second floating
card under the floating glass header read as clutter (owner decision,
2026-10-09). Ordinary chats and split panes keep it. The roster row
beside the chat quotes the same thing the chat draws: its `last_message` is
SPEECH only (`last_speech_info` on the cold read, a
`member/message` event without `preview` for a machinery row on the live path —
see [member-event-log](member-event-log.md)), so a never-spoken patroller's row
is blank rather than quoting a shell command; recency still bumps on every row.
What counts as speech is spelled twice — the user's rows plus
`isCrewmateSpeech` in `crewmateBubbles.ts`, and `is_speech_row` in
`dashboard/system_notices.py` — and `test/fixtures/crewmate_speech_rows.json`
pins the two to one verdict per row from both test suites; a new status kind is
added to the fixture first. Status written under the assistant role is not
speech on either side: the compaction and session-reload notices, a workflow
completion envelope whose header parses (under the assistant role only — the
same text pasted by the user is the user speaking), and a sub-agent completion envelope
whose header (or the gateway's `meta` facts) parses — the Slack gateway writes
that last one as `assistant` on its delivery-timeout and orphan paths. The roster's quote itself is spelled once:
`speech_preview` (`preview_text.py`: strip markdown, redact, cap with `…`) builds
it on the cold read and on the live `member/message` event alike, so the fold
and the read agree and the read's correction (below) fires only for a stale
pre-speech-only preview. That correction is a compare-and-append: it refuses
when the roster's quote or recency moved since the read observed it, so a
message the crewmate speaks while a roster read is in flight is never
overwritten by the older answer.

While a turn runs (the slot's stream or its `running` flag), the chat does not
draw the turn's progress rows either; the working indicator is a TYPING
BUBBLE — three bouncing dots in the crewmate's own gray bubble (the same
`.message-bubble.crewmate-bubble` fill, corners and measure as its messages,
so a theme restyling those restyles this), the iMessage sign that a reply is
being typed (#19997) — with ONE status line under it naming what the crewmate
is doing right now, both rendered by `CrewmateLiveActivity` in the footer
column. The chat page's ghost-pose carousel (`ChatFooter`) is NOT mounted in a
crewmate chat: `ChatPane` draws one or the other, never both, so a DM reads as
a conversation and not as a loader, and only one thing moves. The dots are
decorative (`aria-hidden`); the caption is the live region. The dots are the
`TypingDots` glyph; the thread panel's "is replying" row keeps its own smaller
pulsing dots, a status row outside this surface (maintainer ruling on #20032). The
bubble folds by height (it stays mounted) while the reply's text is actively
arriving — that streaming bubble already carries the caret, and dots under it
read as a second reply coming — and while the caption reads `stopping` or
`compacting`, when nothing is being typed (the caption then pulses, with the
footer's hourglass for compacting, so the chat still visibly works); the same quiet rule `ChatFooter`
uses (`useStreamIdle`, `STREAM_IDLE_MS`) brings it back when the model pauses
mid-stream. The slots frame's `stopping` flag is a `useSlotActivity` input
(`resolvePillActivity` folds it with `streamState === 'stopping'`), passed by
both the pill and the pane, so a Stop from another tab reads "Stopping…" on
both surfaces from the one seam — as the footer the caption replaces did. The caption's text is the DM
header identity pill's OWN reading, through the one shared hook
`useSlotActivity` (`pages/members/useSlotActivity.ts` → `resolvePillActivity`
over the slot's live status record `slotStatusDetail`, the stream state and
the tool log's returned flag, labelled by the shared `toolStatusLabel`), so
the header and the chat read one moment from one seam and can never disagree
on WHAT the crewmate is doing — only on when the chat line says it, see the
hold below. It answers
"what is the crewmate doing now", never "what has it done": the open call's
label while the call runs (`data-activity=tool`), the indicator's own
"Thinking…" once the call has returned or the model is reasoning
(`thinking`), the pill's other phase words otherwise (`writing`, `working`,
`delegated`, and — since no footer is mounted to say them — `compacting` and
`stopping`); and the line goes with the turn. The chat line, and only the chat line, HOLDS each reading
(`components/chat/useHeldActivity.ts`; #18527): a tool call often returns in
tens of milliseconds, so painting the raw reading made titles flash past a
line that mostly said "Thinking…". Every step stays at least 600ms
(`HOLD_STEP_MS`); a finished tool's title stays 1.5s
(`HOLD_BEFORE_THINKING_MS`) before the line falls back to "Thinking…", so the
next tool usually replaces it directly; when a hold ends the newest reading
wins, except that a tool which started and finished inside the hold still
gets its turn before "Thinking…" (the line names the newest step, never an
older one); `stopping` and `compacting` — the user's own actions — swap at
once; a pending swap is dropped when a newer reading arrives or the line
unmounts. The hold is display timing only: the slot's status record and the
header pill (which reads `useSlotActivity` directly) are not delayed, so for
up to 1.5s the pill can say "Thinking…" while the chat line still names the
tool that just returned. The held step belongs to one crewmate: the Members
page rebinds ONE `ChatPane` to the newly picked crewmate without remounting
it, so `ChatPane` keys the indicator by slot and a rebind starts from that
crewmate's own reading, never from the previous one's held step. The caption
is words only — the dots above it carry the motion; a second
spinner read as a second "working" sign. The indicator is a status, not a
transcript row, for a reason that is part of the contract: the caption is one
fixed-height row (`h-5`, truncating), mounted for the WHOLE live turn and
never blank, whose text changes — so nothing moves while
the crewmate works. Transcript rows mounting and unmounting above the
indicator hopped it by a row on every step, and the "hasn't said anything yet"
hint stays hidden while the crewmate is live for the same reason. The one step
that never names itself in the chat line, even in flight, is the
`nothing_to_do` call (#16429; the line reads "Thinking…"): matched inside
the hook on the tool log entry's trusted identity (`tool_name` +
`mcp_server`, the `QUIET_END_*` constants `store/chat/selectors.ts` defines,
imported through the `store/chatSlice` facade), never on its title — the
quiet end has to look quiet from its first frame, applied or refused, and
since the header pill reads the same hook it reads "Thinking…" there too: one
reading, no fork. The chat line shows the tool label in full (`fullText`,
CSS-truncated, carried as `title`) where the pill clamps it to 40 chars.

While work runs, the conversation keeps the typing bubble and its status line
above, but draws no subagent progress bars or completion cards. Questions, errors, authentication requests and native/spawn approvals
remain visible and actionable. The header and task detail surfaces keep their
existing state sources; this is a conversation-only presentation rule, not a
change to execution or history. A queued message is drawn as the person's own
bubble WHERE IT WAS SENT, with no queue card and no interrupt control: the
order is strictly chronological, iMessage's rule, so a reply (even one to an
earlier message) that arrives after it draws below it, and it never moves to
the bottom. `withQueuedAsBubbles` draws the slot's queued rows in place from the
store order. The person's own send is one stable bubble from Send to drain: a
queued receipt keeps the optimistic bubble (`ChatPane` does not drop it), the
`queue_push` replaces that bubble in place -- same index, same `ts`, the
bubble's ts kept as `meta.clientTs`, marked `responsiveSend` (`appendCorrelatedQueue`)
-- a refresh that still lists the entry leaves it there (`hydrateQueuedBubbles`),
and the drain rebuilds the user row at that same index with the same `ts` and
key (`removeQueuedMessage`), so the row is never removed and redrawn (the
flash) and never re-keyed. Only these crewmate sends drain in place; any other
drained entry is its turn's opener and still lands at the end. The transcript
keeps the same order: a send that waits behind a live turn
(`responsive_chat.write_row_at_send`) cuts that turn's running text segment
(not marked interrupted, the reply goes on) and writes the person's user row at
send time, and the drain reuses that row (`reusable_row`) instead of writing
one after the whole reply -- so the turn-end reconcile and a reload read the
order the chat drew live. The send also records the row's durable position, so
a row the capped live window evicts during a long reply is still reused (it is
already in the transcript) instead of being written a second time. Its echo takes the queued bubble's place and key
(`reconcileOptimisticEcho`). No row is written for a send that starts its turn
at once, or while a stop is settling the slot. While a send's cancel claim is in
flight its row waits, and so does the row of any send that arrives during it;
both are written in send order once the claim settles, so a burst never lands
above the message that preceded it. A queued send whose row is already written
is part of the conversation, so the queue-edit and queue-cancel routes refuse it
(409 `queue_item_already_sent`) rather than leave the old row and add a second one
at the drain, or leave the row and hand the text back to be sent twice. Sessions
keeps the queue card, whose cancel still works for any send not yet written.

The typing bubble also shows from the moment the person sends, before the
turn that will answer is running (a queued send, or an interrupt-and-requeue,
starts that turn seconds later): `awaitingCrewmateReply` holds while the
pane's newest send still has its row (sent, queued or drained), its delivery
is not in doubt (`deliveryUnconfirmed`), and nothing after it has answered --
no assistant row (speech or the say-nothing row of a quiet end), error, stop
card, pending approval or change card. A refused send's bubble is gone, so it
waits on nothing. A live turn that ends while the send is not parked in the
queue ends the wait too (the turn that answers it wrote no row); a turn that
ends while the send is still queued is the one before it. So does a slots
frame, while the slot is not live, naming a different turn than the one
current at Send (`turn`, or `turn_gen` after a gateway restart): the answering
turn began and ended between two coalesced frames. Until the answering
turn is live the caption reads the pill's `working` phase, never the slot's
stale status record from the last turn.

The composer reads the same wait as busy (`ChatPane`'s `composerBusy`), so
from the Send it shows Stop while empty, not the idle Send, and Resume is
withheld in both places (`useChatPaneContinue`'s `sendPending`). The DM is
never the store's active slot, so the store holds no pending-turn mark for it
the way ChatPage's `startLocalTurn` does; without the wait, the person's own
row, confirmed and unanswered, reads as an interrupted turn until a frame
shows the slot running.

The crewmate `ChatPane` sends `responsive: true` on its normal Send request.
Submitting a native question's answer while its turn runs additionally carries
`steer: true`: the existing answer route, not a visible steer control. Idle
native/stateless answers start a next turn; blocking MCP asks keep their answer
endpoint and approval futures are untouched. The gateway validates the owner and the real member DM before
prioritising it; it uses the same session, queue and cooperative cancel contracts,
not a decision model or a fork. Both that path and a validated explicit human
steer receive reply-first/delegate-heavy-work principles as prompt context, not
as displayed or persisted user text. Pending approvals/questions can defer the
response rather than being silently rejected. An initial idle-admitted work turn
remains interruptible; a send to a running turn is steered in first and cancels
that turn only when the steer is still unconsumed after a 3-second grace, so a
tool call that finishes in time is not thrown away. A harness without steer
support, or a steer the backend refuses, queues the send and cancels at once:
the grace waits on a steer, and there is none. The priority answer admitted behind active work is protected
so a burst never cancels that answer: a later send is steered into it when the
backend accepts a steer, and otherwise waits. Several sends waiting behind one
turn drain together as one turn (`responsive_chat.dequeue_waiting_run`), whatever
`dashboard.merge_queued_messages` says: each keeps the row it was given where it
was sent, the turn anchors on the last of them, and the run stops at an
attachment, a possibly-delivered steer or a send with no written row. The prompt
principles ask for one reply to a burst that adds only what is new. A crewmate chat
draws a requeued steer's queue entry as no second bubble: its send already has
its row. Cancellation is
not proof a tool or external side effect stopped. The full scheduling and recovery contract is in
[session](session.md#responsive-human-sends-in-a-crewmate-dm). Ordinary Sessions
and the reply-thread panel's separate reply endpoint retain their behavior.

How it is drawn: the crewmate's messages form **runs**. A message carries NO
author line — no avatar, no name, no time row — and no avatar gutter: the chat
is a 1:1 thread with one speaker besides the user, and the DM header's identity
chip already names that speaker (#16617 retired the per-run author line #15167
had made redundant); for assistive tech each message row is a `group`
labelled with the crewmate's name, which draws nothing. Every message is its
own FILLED gray bubble (`.crewmate-bubble`, no border, `max-w-[72ch]`) in the full
text column, left-aligned; the user's bubble opposite is filled with the theme
ACCENT (`bg-accent` / `text-accent-fg`, the `tone="accent"` variant of
`UserMessage`), the iMessage pairing #17839 chose, so colour tells the two
speakers apart before alignment does. The `user` entry that carries the tone is
emitted by `createTranscriptRenderers` whenever the host passes `crewmate` (or
asks to hide the steer badge). The crewmate surface hides the Steered chip
and ordinary interruption markings; user messages remain plain accent bubbles.
The transcript retains its delivery metadata for identity and recovery. `--bg-hover` is the gray because
`--bg-elevated` and `--card` equal the page background in kiro-light,
highcontrast-light and everforest-light (the bubble would vanish), while
`--bg-hover` sits above the page in every shipped theme; in forced-colors mode
the gray bubble draws a border instead. The `.crewmate-bubble` rule in
`index.css` also scopes the surface tokens the reply's contents paint with
(`--bg-hover`, `--bg-elevated`, `--card`) one step off the fill for its subtree
— kiro-light's inline-code patch IS `var(--bg-hover)`, and so is every
`hover:bg-bg-hover` control, so on the bare fill they would vanish; kiro-dark's
literal code patch gets the same step by one scoped rule. Inside the accent bubble the
`user-bubble-accent` hook in `index.css` REDEFINES the theme tokens for the
subtree (`--text`, `--muted`, `--accent`, `--border`, the `--bg*` surfaces, …)
as values derived from `--accent-fg` — text and strokes as `--accent-fg` mixes,
surfaces as translucent black so a patch darkens the fill rather than pulling
it toward the text colour — so everything the message renders (inline code,
link pills, the sent quote card, whatever is added later) inherits readable
colours with no per-element rule; the fill itself reads `--bubble-accent`,
snapshotted from the outer accent on the bubble's parent. Links keep a link
signal (an underline) once their colour is the body colour. The single-chat
page's user bubble is untouched.
Consecutive bubbles read as one speaker through their grouped corners alone.
Corner rule on
the run's (left) side:
single = all corners full; first = bottom-left small; middle = top-left and
bottom-left small; last = top-left small; right corners always full. A run is
ONE TURN's bubbles (RFC screen 05): it breaks on a user message, on any row the
user sees between two messages (an error, a pending approval), and at a turn
boundary the unfiltered transcript still carries — a patrol wake, a cron or
sub-agent envelope between two replies — which `crewmateRunPosition` reads off
the unfiltered rows the `crewmateTranscript` getter the pane hands its renderer
returns; there is no time rule. A
turn's own machinery (tool rows, thinking, the wire-only `done`), a resolved
approval or a state-only row between two messages does not split the run, and a
streaming row continues it. A change card (`role: 'card'`) is the crewmate's own
message and a member of the run: it is drawn inside `CrewmateMessage` at the
reply's width, so a card that opens the run carries the author line and a card
after an acknowledgement continues it, and the turn keeps one avatar. The
bubble is the ordinary `AssistantMessage` (markdown, `[OPTIONS:]` chips) with
the bubble surface passed in as `bubbleClassName`; the SDK's footer rule
(`renderAssistantBubble`) is shared, not copied, with two host overrides: the
run's last bubble (`single` / `end`) always carries the footer and its hover
actions — the SDK's own rule would withhold it when the next drawn row is
another reply, but in this chat a run only ends on a boundary the user sees,
so the run end IS the turn end — and the "Steered" chip never draws
(`suppressSteerAck: true`): delivery mechanics stay outside the conversation,
including a `[STEERING …]` acknowledgement from an explicit steer; the marker is still
stripped from the prose, and the main chat keeps its chip with the SDK's own
policy-block rule. The UNFILTERED transcript the pane hands over as the
`crewmateTranscript` getter is read for the run position only; the getter is
called on every row render, so the renderer set keeps its identity while a
reply streams. User messages keep
their existing rendering apart from the accent fill above. The run position is exported
(`crewmateRunPosition`) for a reply-thread footer to reuse; no DOM attribute is
stamped until that reader exists. The reply thread's own panel
(`pages/members/ThreadPanel`, see history.md) is that reader: it draws the
crewmate's replies on the same `crewmateRunPosition` / `crewmateBubbleClass`
rule; the user's replies are always singles.

**Session links resolve.** A crewmate's prose names sessions constantly — the
worker it dispatched, the session it is answering about — as a `/chat?sid=…`
link, a bare slot key, or a short name (`chat-1380`). In this chat those behave
exactly as they do on the single-chat page: a plain click switches to that
session, a key chip is an affordance, and an unresolvable one stays muted text
with its href intact (the #9914 rule). There is no second resolver — the
markdown renderer's own `SessionActionCtx` does the work on both surfaces, and it
gates on (`onSessionOpen` AND `sessions`), so what changed is only who supplies
them. `ChatPane` takes the triple as optional props and hands it to
`createTranscriptRenderers`, which spreads it across every row it draws: the
crewmate bubble (through `renderAssistantBubble`'s host options, with the row's
`ts` so the short form can refuse a slot minted after the text naming it), the
crewmate / steer-only user row (through `renderUserContent`), and the sub-agent / workflow
completion cards. A pane host that passes none of it — a side chat, an embedded
chat — renders those links plainly, as before.

What the Members page supplies is deliberate on all three counts. `sessions` is
built by `utils/sessionRoster.sessionTitleRoster` from the WS `slots` frame the
page already subscribes to, narrowed to the slots the DESTINATION can render
(`isChatPageSurface`) — the same builder `ChatPage` uses for its own roster,
because a chip offered here navigates there, and two copies of "which slots may
chip" is how the affordance and the destination drift apart. It is WITHHELD, not
emptied, until the socket is up and a real slots snapshot has arrived: absent
means "this surface does not know which sessions exist", which leaves the link
plain rather than live-looking and dead. `activeSession` is the DM's own slot
key, so a link naming the thread you are reading is inert. And `onSessionOpen`
navigates to `/chat?sid=…` — the same primitive the drawer's Driving-sessions
rows use — because a foreign slot belongs to the chat page with its sidebar,
history paging and composer; the DM never hosts one in its own pane.

**File and artifact links open in the member's side panel.** A crewmate's
reply names what it made — a backticked path, a `/artifacts/<slug>` link — and
both open the way they do on the single-chat page: in the side panel docked to
this page, as a Files document tab or an Artifacts document tab against the DM
slot (`usePanelDocumentActions`, the chat page's own implementation, bound to
`confirmedSlot`; in overlay mode the panel reveals itself after the open). The
pane never resolves either itself: `ChatPane` takes `onFileOpen` (#9487,
#14458) and `onArtifactOpen` (#18320) as optional props and hands them to
`ChatMessageList`, whose per-row `MessageRenderContext` carries them to every
assistant row — the SDK default and the crewmate bubble both go through
`renderAssistantBubble`, which passes them to `AssistantMessage` and so to the
markdown renderer. Capability by omission on both: a host that passes nothing
gets the renderer's own fallback — a path chip asks the OS to reveal the file,
an artifact link stays a plain `target="_blank"` anchor to the standalone view
— which is right for a side chat or an embedded chat and was wrong for the DM,
where the artifact a crewmate just made opened in another browser tab, or
nowhere at all when the shell swallows the popup. The split-view pane still passes neither,
for the reason #3300 records (its dock is `activeSlot`-keyed while pane focus
is not).

**A document in the member's side panel takes inline comments.** The side
panel owner (`useMembersPageSidePanel`) hands the panel `onSubmitComments` and
`connected`, as the chat page does, so a markdown file tab offers the selection
comment composer and the comment list, and an artifact tab its submit-to-chat
bar (#20081). The send lives in `useMemberDocumentComments`
(`website/src/hooks/`): Submit sends the batch the panel formats into the DM
through the chat-core transport (`sendTurn`), carrying the composer's busy-send
decision for that slot (`slotBusySteer`).
The target is `confirmedSlot`, read at the press: the slot the document
actions stamp every tab with, on a strip bucketed per member, so a batch
reaches only the crewmate the panel belongs to. While the thread is
unconfirmed the panel withholds its document tabs with the other slot-bound
views, so Submit is not on screen, and `submitComments` sends nothing without
a confirmed slot. The page draws no optimistic row for the batch. Every
outcome shows feedback outside the DM transcript -- the crewmate's DM stays
messages-only per `docs/decisions/2026-10-09-crewmate-chat-draws-messages-only.md`.
A failure goes through `showActionError` (an ErrorNotice banner above the
thread; in overlay placement it also closes the overlay, which would otherwise
cover the notice) and leaves the comments pending. `refused` names the
crewmate, says the comments are still in the document and to send them again
(the server's reason is logged, never surfaced). `response-late` and
`transport-error` both show a delivery-unconfirmed notice that gives the rule
for the retry: send the comments again if they are not in the conversation
within a minute, because sending again sooner repeats comments that did arrive
(a minute is the bound the chat page gives for an unconfirmed message). Each
notice has two variants, picked by where the page placed the panel. Beside the
thread they say to send the comments again from the document, which is already
on screen. In overlay placement the failure has just closed the panel, so they
say to reopen the side panel and send them again. No variant names a button,
since the retry control is Submit All on a file tab and Submit on an artifact
tab. A delivered outcome sets the hook's status line (neutral text, drawn by
the thread column with `role=status`, auto-dismisses after 5 s), clears the
comments and clears any earlier failure notice. `dispatched`, `queued` and `unknown` (a 2xx whose body
could not be read, so the server took the request) all show "N comments sent".

## Selection: the `select_crew` contract

Discovery importing a provider template as a configured member does not rebind
an existing dashboard conversation that selected the template. Resolved bindings
carry a positive `selection_kind`; the canonical session execution record preserves
that namespace across callbacks and restore. New member conversations still
capture the member ID and store without opening the learned database. An explicit owner agent choice
may replace selection provenance, but cannot migrate an existing V1 native
conversation into member memory. The persistence and legacy-session rules are
owned by [session](session.md#agent-selection-provenance).

`select_crew` has two modes, both answered as JSON by `_do_select_crew`.

`route_crew` takes a non-empty `task` (an empty or blank one answers
`{"error": "task must be a non-empty string"}`) and ranks the crews whose
triggers match it, best first. It answers `task` (cut to 200 characters),
`default_agent`, `matches` (`{crew, score, description, memory_store}` plus
`display_name` when it differs from the key), `unavailable` (`{crew, reason}`,
same label rule) and `guidance`.

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
- `default_agent` (the roster's default crewmate) is omitted, triggers or not,
  and `route_crew` skips it the same way: RFC rule 5 keeps it as the one
  `select_crew` holds for itself, the standing fallback the guidance names. This
  is a routing rule, not an identity -- the calling session is a template
  session (the default custom agent on Global memory), never that crewmate.
- The response carries `default_agent` and `guidance` so the model has an
  explicit fallback and a high-confidence bar rather than inferring one.

`agents.<crew>.display_name` is a presentation label only. The crew's name
stays its immutable identity: it keys the record, addresses
`/api/agents/{name}`, and is what dispatch, crons and spawn resolve, so
renaming the label never breaks a binding. Empty means the name is shown, and a
non-string value in `config.json` reads as empty. Create and update refuse a
non-string label with `400 invalid_display_name`. Rosters mask it like every
other externally controlled string (`_roster_mask`), and the member and config
projections carry it.

An entry carries `display_name` when the crew has a label that differs from
its key, and so does each `route_crew` match and `unavailable` entry. The user names a crew by that
label while `spawn_run(crew=...)` takes the key, so without it a renamed crew
cannot be matched to the user's words.

**Bind** (`crew` names a roster entry):

```json
{"crew": "oncall",
 "bound": {"kiro_agent": "oncall-agent", "workspace": "/…/oncall",
           "memory_store": "oncall-mem", "model": ""}}
```

An unknown name answers `{"error": "unknown crew '…'", "available": "…"}`, where
`available` lists every key, each followed by `(display_name)` when that label
differs from it, so a label passed as `crew=` can be mapped to its key. The
membership test against `cfg.agents` is the deny-by-default gate;
`SELECT_CREW_SCHEMA` deliberately does not impose a name grammar, because crew
creation only strips the name, so a stricter schema would list a crew in the
roster and then refuse to bind it.

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
resolves its provider template and member memory together. The separate
`agent=` argument identifies a provider template, not a durable member identity;
it must not be used to infer access to a member's memory.
The model-facing `spawn_run` schema advertises `crew` separately from `agent`,
so a caller can select a member through tool discovery. A batch's `crew` applies
to every task; delegating to different members requires separate calls.

An ordinary member sub-task inherits its captured member/store. An explicit
existing target member selects that member's store under ordinary spawn,
owner/app and governance permissions. Memory ownership itself adds no separate
cross-member ACL. Continuations retain the original run's member/store even if
a different member now requests the continuation. The gateway still requires
ordinary authenticated session identity before accepting a parent session.

A named-but-unknown agent is **refused**, never silently answered by the default
agent, with the machine-readable code `agent_not_found`. That refusal is a
privilege boundary: the default agent frequently runs at broader approval, so a
typo'd or injected name falling back to it would be an escalation at the manager
primitive. An empty `agent` still means "use the default".

Crew Mode resolves the mate server-side instead of relying on the
coincidence: `spawn_run(crew=<member>)` forwards `crew` in the request body
(`mcp_tools/spawn.py`) to `dashboard/messaging_api/spawn.py`. That handler
refuses a name that is not a key of `config.agents` with `404 unknown_member`,
and otherwise derives the member's execution (its store and template) through
`derive_execution` → `resolve_member_execution` before the spawn reaches
`subagent._validate_agent`. A crew name therefore never quietly runs the
default agent under a stale name.

## Crew capability drafts

The Crew editor has an independent Capabilities rail pane with MCP, Tools,
Auto-approved and Skills categories. It stays mounted while hidden so both its
local draft and its signed server preview survive rail changes. Its footer owns
Discard draft and Review/save; the generic crew save cannot discard a capability
draft. Closing or opening chat asks before losing that draft. A capability
request in progress holds dismissal. Dirty and busy state reach the parent in
layout effects, before paint, so an immediate Escape after pasting cannot close
against an older clean state. Browser unload also warns about the draft.
Opening the embedded editor writes an explicit `tab=crews` route, so a resize
cannot replace its ancestry with the mobile root list. On narrow screens the
member identity owns a full header row. The capability form scrolls independently
above a non-overlapping footer. The horizontally scrollable category strip does
not flex-shrink when an expanded transport form exceeds the pane height; all
category labels retain their full height. Review shows values from the server's sanitized
projected rows, never from secret-bearing local drafts. Source validation errors
are distinct from provider loading failures.

The editor reads and writes through `api/crewCapabilities.ts`, using the shared
transport. Preview and save send the same explicit inheritance operations; save
adds only the server-issued preview token. A stale version preserves the draft
and requires reloading and reviewing against the new version. Save success never
stands in for runtime application: runtime status comes from the server and
active sessions are not promised a hot reload.

The legacy template pane keeps its instant-save behavior for independent and
shared definitions. Enrolled definitions direct model and skill edits to
Capabilities instead. Reset and publish remain in the template pane but lock
while a capability draft exists. A mask is never a literal replacement value.
The form can keep unchanged secrets, select a configured connection, or replace the
whole transport using a blank form. MCP set operations carry a complete transport
plus RFC6901 `retain_paths` for unchanged `[REDACTED]` leaves. Each pointer keeps
its original member/revision binding. Editing a hidden value removes that pointer;
a typed mask without a retained pointer blocks preview. Hidden argument positions
and hidden map keys cannot move until their values are replaced explicitly.
Environment and HTTP header values remain password inputs. Managed transport
fields use the row's authoritative `managed` flag, independently of the connection
catalog; their supported enable switch sends only `disabled`. Absent prompt/model
rows can be set, and model choices use the shared advertised-model query. Version
hashes live in a collapsed details section rather than in the main status banner.
Parent-change and impact previews use the server's redacted projection.

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
| `test/test_pruned_crewmate_records.py` | Records bound to a pruned synced crewmate decode as their template for every reader (chat resume, subagent continuation and inheritance, prompt builder); owned members still refuse; the first send of a resumed pruned chat is admitted by the session CAS while a real concurrent change is still refused |
| `test/test_dashboard_agents_composition_contract.py` | The agents handlers keep their surface across the `agent_admin` split: every base name still resolves on `handlers/agents.py`, routes and the handlers package reach the same objects, each owner runs on the facade's globals so a test's patch reaches it, and what repository guards read in `agents.py` stays there or is re-keyed to its owner |
| `test/test_agent_execution_catalog.py` | Read-only catalog, same-name member/template choices, requesting-project isolation, private-template exclusion and explicit discovery failure |
| `test/test_agent_templates_endpoint.py` | Templates roster marks editability (a row with no spec file beneath the agents directory — empty, foreign or absent `filename` — is read-only for the runtime's reason) and references (crews, default, schedules by what they dispatch — sequence over dormant `agent_id`, the captured execution's template over a stale or empty `agent_id`, script jobs over neither — chat-folder pins, webhook pins, private copies) and masks package-controlled strings like the sibling rosters (the delete refusal's references too); a row whose filename is absolute, traversing or nested names nothing to delete (404, file intact); create writes a minimal runnable spec or a lineage-free copy (re-read inside the spec lock, where the source name is re-resolved and must reach exactly the probed file — a second claimant or a replacement refuses, nothing written) and refuses taken, bound (in the base or only in the overlay), reserved, ambiguous and malformed names; delete refuses read-only and referenced templates (listing the references), a name two files reach — a crossover or a same-name twin the roster would collapse (neither unlinked), a row whose file does not answer to the requested name, a second claimant that lands after the probe (ambiguity re-checked under the lock) and a row that calls a package file plain (the file re-read and classified under the lock), checks and unlinks inside one folder-store hold rather than from a snapshot, counts a binding that lives only in `config.local.json`, does not count a template that merely shares the default crew's alias, holds the schedule store's own lock from the reference walk through the rename (probed on both sides) and answers 503 `schedule_store_busy` with the file intact when another holder keeps it past the bounded wait, names a schedule written past the lock (warning + SEL row), fails closed on an unreadable cron store before the unlink (503, file intact) and only warns after it, retires the file as a one-deep tombstone (renamed before the older grave goes, so a refused rename keeps both; same-second graves stay distinct; the sweep spares a live template whose name looks like a grave), runs both mutations through the drained seam, and removes an unreferenced one; create re-scans by declared name under the lock; a successful create and delete emit operation-labelled SEL events; a create publishes its name to the dispatch snapshot before scheduling the rescan and a delete awaits the rescan before answering (a refusal touches neither); the detail PATCH writes the definition keys on an owned template, refuses them on a package one, refuses every key on an ambiguous name (neither file touched) and a claimant landing after the scan (re-checked under the write lock), classifies the targeted file rather than its name, and validates their shape |
| `website/src/test/AgentTemplatesTab.test.tsx` | Grouping by origin, the two-control action row with its overflow menu (enroll hint, Delete vs Duplicate-to-edit by editability), the definition save through the detail PATCH (changed keys only — a prompt-only save never resends the model), the dirty-draft guard on row switch, on a background refetch, on Discard (asks; declined keeps the draft) and on New custom agent (a create never inherits the previous draft), a saved skill list written into the detail cache before the refetch lands, the saved confirmation in the bar's slot and the visible Add tool label, a delete naming the deleted template over the next row and, on a narrow viewport, returning to the list, every in-app link routed through the shell's leave gate with its target, `beforeunload` armed only while dirty, a rejected detail read rendering its error rather than Loading, a refused save reported inside the save bar beside Save and cleared by Discard, resources as plain rows, string-only MCP fields from a hand-edited spec, one Skills heading, the referenced-delete dialog (opened directly from the row's own holders with no confirm or request, and from the server's refusal when a holder landed later; including a chat-folder row and a private-copy row that links to its crew), a private copy's Open crewmate, the usage line naming folder and webhook holders with each holder linked to where it is held, blank vs `from` create with the created row selected after the roster refetch, an enroll refetching the crewmate roster, and chat-with in the template namespace (enabled while dirty, behind the discard confirm) |
| `website/src/components/RestartButton.cov80.test.tsx` | Apply & Restart (mounted in the Connections header) asks first, naming what stays (chats and history) and what stops (a reply in progress); declined does nothing, and the confirmed paths (success, failure, in-flight, MCP reconcile) run with the ask answered yes |
| `test/test_chat_agent_kind.py` | `agent_kind` on local slot create and switch, and a relay-archive switch refused with `409 relay_archive_read_only`: template picks skip the member store pin, name and kind persist atomically, an unresolvable stated kind is `409 agent_choice_unavailable` refused before any slot is minted, an unknown kind is `400 invalid_agent_kind`, a member thread refuses the same-name template kind, the slot projection carries the committed kind |
| `test/test_open_slots_persistence.py` (`test_restore_carries_the_agent_selection_namespace`) | A template-picked slot restores as a template pick; an unknown persisted kind reads as name-only |
| `test/test_select_crew.py` | Roster excludes the default crew (triggers or not) and every triggerless crew, carries `default_agent` plus guidance; an entry and a `route_crew` match or `unavailable` entry carry `display_name` only when it differs from the key; a named crew returns its bindings; an unknown name returns `error` plus `available`, each key followed by its differing label; the schema accepts spaces and dots in a crew name |
| `test/test_crew_reasoning_effort.py` | Per-crew effort reaches a crew dispatch |
| `test/test_members.py`, `test/test_members_dm_thread.py`, `test/test_members_dm_thread_activity_and_briefing.py` | Slug validation and containment, activity recording and dedupe, DM-binding canonicality, rules and briefing reads, briefing endpoint |
| `test/test_chat_send_agent_model_default.py` | The crew model default a new session starts on |
| `website/src/components/chat/crewmateBubbles.test.ts` | What a crewmate's chat draws (machinery dropped, speech and user-facing rows kept, same array back when nothing is dropped) and the run rule (first/middle/last, single, breaks on a user row and a pending approval but not on a long silence, reads through a resolved approval and an untimestamped streaming row, corners on the left side only); a queued message drawn where it was sent, the requeued twin, undrawn queued rows and injected (cron / app) queued rows dropped; when a send is still awaiting a reply |
| `website/src/test/ChatPane.crewSteerLandsNow.test.tsx` | A crewmate's responsive Send end to end, including one stable bubble through a queued receipt in either order with its push, a later reply drawn below a queued message and the drain keeping order and node, and the typing bubble from Send until a reply, a refusal or the answering turn's silent end |
| `website/src/store/chat/queue.responsive.test.ts` | Exact queued-send identity, and a crewmate send keeping its index, `ts` and key through push, drain, a missed push and a refresh, while a Sessions send still lands at the end |

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
