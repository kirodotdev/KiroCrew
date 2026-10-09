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
| `src/kiro_crew/dashboard/handlers/agents.py` | The agents handlers' import and patch surface: `GET /api/agents`, the model picker reads and the crew model-pin checks, composed with the `agent_admin` owners below |
| `src/kiro_crew/dashboard/agent_admin/` | Crew CRUD on `/api/agents` (`crew_records.py`, `crew_update.py`, `crew_removal.py`), the roster row serializer (`roster.py`), the Custom agents tab's detail, fork, publish and reset (`agent_detail.py`, `fork_publish.py`, `template_lineage.py`) and the default-agent write (`default_agent.py`) |
| `src/kiro_crew/dashboard/handlers/agent_catalog.py` | Read-only `/api/agents/catalog` execution choices, with separate member and template namespaces |
| `src/kiro_crew/dashboard/handlers/agent_templates.py` | The Custom agents tab's roster (`/api/agents/templates`), create, delete with reference guard, and the read-only rule the detail PATCH applies to definition edits |
| `website/src/pages/overview/AgentTemplatesTab.tsx` | The **Custom agents** tab of `CapabilitiesPage` (Customize): list by origin, edit the shared definition, create, delete, chat-with / enroll |
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, thread get-or-create, rules, activity |
| `src/kiro_crew/crew_teams.py` + `src/kiro_crew/dashboard/handlers/teams.py` | Crewmate teams (`teams.json` in the masked gateway-only data-home directory `crew-teams`; `/api/teams`): the Crewmates page's roster grouping and team view — specified under [learn-cron-dashboard](learn-cron-dashboard.md) with the members handlers |
| `website/src/pages/KiroCrewAgentsPage.tsx` | The crewmate roster, mounted as the **Crewmates** tab of `CapabilitiesPage` (Customize) |
| `website/src/components/crew/crewEditorSections.ts` | The crew editor's pane registry, including the Routing pane that edits `triggers` |
| `website/src/components/CrewWakeSection.tsx` | "What wakes this agent" — schedules, deliberately distinct from `triggers` |
| `website/src/components/chat/crewmateBubbles.ts` | A crewmate's chat: what the transcript draws (`filterCrewmateChat`) and the run / corner rule for its bubbles (`crewmateRunPosition`, `crewmateBubbleClass`) |
| `website/src/pages/chat/CrewmateMessage.tsx` | One crewmate message in its chat: the bubble alone, placed by its run position (no author line, no avatar gutter) |

Crew creation reports `409 agent_exists` for both an existing name and a
concurrent name collision. The member-titled form uses its translated duplicate
message only for that status and code together. Other conflicts, including
memory and template-ownership failures, retain the API error message; missing
or malformed codes are not guessed to mean a duplicate. Failed creation leaves
the form open with its entered name and selected template intact.

## Execution-choice catalog

`GET /api/agents/catalog` lists configured members and discovered shared templates
without enrolling, pruning or allocating a member. Each row carries an explicit
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

Private copies and the runtime's background-only `kirocrew-lite` spec (matched on
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
and memory unchanged. The member-management API (`/api/agents`) and the
synchronization route (`POST /api/agents/sync`) retain their contracts, but the
dashboard pickers no longer call sync: `useAgents` reads the catalog, so opening a
chat, the schedule form or the channel page enrols nothing. The hook returns the
typed list as `choices` (the chat agent pop-up renders it grouped under
**Crewmates** / **Custom agents**, each member row wearing the same avatar the
roster draws for it, the origin badge dropped because the header already says what
a row is, and the custom-agents group carrying a one-line hint that a custom-agent pick
runs on the default crewmate's workspace and memory and creates nothing new) and
the same list folded to one row per name, member first, as `agents` for the
name-only consumers (cron `agent_id`, channel and project bindings, the cycle
shortcuts). The pop-up draws the group headers and the custom-agents hint only when it
lists more than one kind: a header that separates nothing is chrome, and the hint
contrasts a template against a crewmate the list must then be showing. The folded
rows keep their `selection_kind`, and `AgentSelector` (the shared roster picker)
groups by it under the same two headers and hint when a caller passes
`groupByKind`; the schedule job form does, so a cron's agent field offers
**Crewmates** then **Agent templates** in one dropdown, and a template pick stores
the bare template name -- the backend's name-first resolution runs an unaliased
template on the default crew's workspace and memory, so no cron contract changes.
The chrome follows the same one-kind rule, decided on the unfiltered roster so a
filter that narrows to one group keeps its header; the `role="group"` label stays
for assistive technology either way. Callers that do not opt in, and any name-only
roster, render flat as before. Unless the config key
`dashboard.crewmates_in_agent_picker` is on (off by default, read from the shared
`/api/config/kirocrew` query), `useAgents.ts` withholds from `choices` every member
row a listed template already COVERS (`withoutCoveredCrewmates`): a template of the
same name is listed, or the member has no memory of its own (`memory_store` is
`default`) and runs a listed template, which is the identical binding (the built-in
`default` crew is this case). A crewmate no template covers stays pickable: one made
by hand with its own memory, and one running its own private copy, which the catalog
never lists as a template. Withholding those left the agent unreachable from any
chat, since neither group offered it. Turning the key on lists every member and
restores the two groups; the folded `agents` list and the request contract below
are unaffected either way.
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
engine), and the KAS projection
(`acp/kas_agents.to_client_custom_agent`) refuses the id before the wire with
the remedy in the dashboard's own labels -- the crewmate's Built from pane;
on the crewmate's own copy 'Save as new template…' under another name, which
moves the crewmate to the copy and keeps its edits, or 'Reset my changes' to the
shared template; on a shared template bound under a reserved id, where neither
control renders, the template picker at the top of the tab -- raised without the generic
`cannot project agent ... onto KAS` prefix (`KasReservedAgentIdError`), instead of letting the activation guard report the spec as
missing or a built-in run in the crewmate's place. The dashboard renders the
refusal in the user's language inside the dialog that sent the name
(`components.agentTemplateDetail.publish_name_reserved`,
`pages.overview.agentTemplatesTab.err_name_reserved`), naming the typed name.

## Custom agents tab

The Template pane inside a crew editor edits that crew's PRIVATE copy of a
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
(`_materialized_kiro_agent`, what "Chat with this template" and every
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

The tab groups rows as Mine / Crewmate overrides / From packages / Built-in
(the overrides group carries a one-line gloss under its heading, and an
override row is described in the tab's own words — "Crewmate X’s override of
Y" — not the fork-written "private copy" sentence, so one object has one name
on one screen; a crewmate's private copy is a "crewmate override" everywhere
the tab speaks — and, so the term survives the jump to the crew's Template pane, in
the shared `lib.templateSource` badge that pane shows for the same file — and
Duplicate yields "a template of your own"; the word "copy" is not used for
either, so the two are never confused)
(`lib/templateSource.ts`), lets an owned template's description, model,
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
ceiling may speak to. A read-only template's banner leads with a bold two-word reason
(**From a package** / **Built in** / **Markdown file** / **Crewmate
override**) so the four states read apart at a glance, carries the reason
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
holds two controls — **Chat with this template** and an overflow menu (enroll,
duplicate, delete); **Chat with this template** creates a slot with
`agent_kind: "template"` (its title says it is a one-off chat that creates
nothing; while the draft is dirty it stays enabled and asks the same discard
confirm a row switch does, rather than greying out with the reason in a
title); **Enroll as
crewmate** is the ordinary `POST /api/agents` with the template as
`kiro_agent`, and its menu row says what it starts (a crewmate with its own
memory, nothing running). The unsaved-changes bar names how many crewmates a
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
override" — the same word as the group heading and the banner, so one fact is
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
and offers a datalist of kiro-cli's native tool names plus every name the
template already grants (offered, not enforced); the enroll row says where
the result lands (under Crewmates); the save bar's two buttons never wrap or
shrink, and its instruction names the button by its label ("Save custom agent"). A private copy is never a dead end: its
banner offers **Open crewmate** (the crew's Template pane, where the copy is
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
existing V1 members keep their declared V1 binding.
The built-in Captain member (config key `kirocrew-captain`,
`agent_files.ASSISTANT_MEMBER_NAME`) is a separate crew member created once, bound to
the managed `kirocrew-captain` template, with its own private V2 memory store
(`member_id` `kirocrew-captain`), exactly as an explicitly created crewmate resolves.
It speaks as its scrubbed display name, falling back to Captain, only while it is
on that template and not running as an explicitly selected template; its
identity, rules and member key stay `kirocrew-captain`. What Captain's memory
holds and the read-only path it has into Global memory are specified in
[memory-skills-hooks](memory-skills-hooks.md#captains-memory). The reserved `default` member is
never rebound and keeps the ordinary member identity whatever template it names.
The personal-assistant persona lives in the managed `kirocrew-captain` template,
not in the generic member identity block. That template reads the existing
onboarding profile to calibrate communication: role informs examples, technical
comfort informs depth, and explicit user requests take precedence. Skipped or
withheld profiles are not inferred.
`kirocrew-captain` is a singleton template: only the `kirocrew-captain` member binds
to it, and a capability edit naming it for any other member is refused
(`assistant_template_reserved`). The installer rewrites the file on every
gateway start, so the member's capability edits are never a private fork; they
are stored as member overrides in the agent-state sidecar and re-applied on
every regeneration, ahead of the governance ceiling, the auto-approve strip and
the derived `permissions`. The capability view shows each override as a local
row on top of the installer's own output.
The template's `model` follows Settings -> Chat "Default model" through the default
agent's own propagation: the installer writes the effective `agent.model`
(`KiroCrewConfig.load()`, i.e. `config.json` with the `config.local.json` overlay
merged over it, read through `normalize_agent_model`) into the spec, or the `auto`
sentinel when it is unset or `auto`. The `agent.model` config applier reruns
`rebuild_agent_config`, which re-installs this template, so a change reaches
Captain's next session without a gateway restart, as it does the default agent's.
The watcher diffs the merged document, so an overlay-only change fires the same
applier. A rebuild that leaves Captain's managed spec stale (an install that
raised, an unreadable spec or unreadable member overrides) is recorded
(`assistant_agent.take_refresh_failure`) and takes the applier's failure path:
one "Default model could not be applied" notification, no refresh broadcast, and
a re-raise so the watcher retries. A foreign file at the path and a kept
dashboard pin are not failures. The resolvers are unchanged: the spec's value is
an ordinary per-agent pin. An explicit
pick recorded on the template (`model_managed` False in the agent-state sidecar) is
kept across every rewrite and outranks the global; an unreadable sidecar keeps the
file's model. A crew-level `model` on the Captain row outranks both, as for any crew.
The role section's reply rules: no text before the first tool call (at most a
bare "Checking.", and none when the reply frames a proposal or guide), then the
outcome once in the single final reply, never said before a card and again after
it, without re-asking a question already asked; no internal ids (setting ids,
tool names, field names) or internal words (cron, MCP, slot, spec, template,
writable, index, build, location id) in replies, a refusal worded as "I can't
switch that one for you" or "I couldn't find that here"; no bold UI labels inside
a Chinese, Japanese or Korean sentence (quoted with 「」 or plain, since dropped
markers leave stray spaces); no claim that a guide started
or a page opened before the user presses Start, and no mention of Start unless
`guide_start` offered a guide that turn; no status-report line in answer to a plain
question; dashboard locations and setup steps only after a lookup, never from
memory; after saving the user's name, one plain question about repeated work the
user would hand over, talked through before anything is proposed; a crewmate
explained and agreed to before `crewmate.create` is proposed, unless the user
asked for one by name; no "Done"/"Saved" while a proposal waits, and no reply
option that repeats or presses the card's button (Apply change, Start, Create
schedule); a where-is answer that offers the
matching guide in the same reply as the path; a how-to answered in at most three
plain first steps from its docs page, with the card that turns an off feature on
proposed in the same reply; every proposal and guide offer made before the one
reply that refers to them (the `propose_change` and `guide_start` descriptions
say so, and each offer's result carries a `next` line that follows its real
state: queued when no tab received it, and the acknowledgement box named for a
widening change; a shown card's `next` also says the reply is the only text,
repeating nothing said before the card and offering no option that presses its
button); a setting that governs a request
proposed in place of a saved preference; no terminal command handed to the user
to change a setting, turn a feature on or restart, even when a docs page shows
one: the change is proposed, or reached through its Settings path and the
dashboard's restart control; and a reminder's time zone taken from the `[CURRENT DATE]` line
(the configured Kiro Crew time zone, as an abbreviation), asked in plain words only
when unknown, the IANA id going into the card alone.
The template file's `prompt` holds only the installer mark (`# Kiro Crew
Captain`, its first line and the provenance check the installer reads before
replacing the file) and the role section. A `kirocrew-assistant.json` left by a
build from before the template took Captain's name is not Captain's: it is not
an owned file, its name and `# Kiro Crew Assistant` mark fail the provenance
check, and the installer leaves it untouched (never rewritten, never deleted),
so it lists as an ordinary template the owner may keep or delete. A session on that template gets an
`[AGENT SYSTEM PROMPT]` block made of the contract an ordinary `kirocrew` chat
gets for the slot's mode (the user's `prompt.md` when present, placeholders
filled) followed by the role section, so the contract reaches the model once. A
file at that path without the installer mark keeps its own prompt.
`rebuild_agent_config` creates the `kirocrew-captain` row once, after the template's
install outcome is known, under the config lock, and in the same call moves it onto
a newly provisioned private store (`memory_stores.give_assistant_private_memory`:
`provision_member_memory` then `persist_member_config`, the steps every explicit
member creation takes), so a fresh install's first Captain turn already runs on it.
A Captain row an earlier build created on Global memory is moved by the same
function at the next process start (`repair_legacy_member_stores`, in the
gateway's memory worker after readiness and in the CLI prologue); Global data is
never moved, copied or deleted, so what Captain learned there stays in Global. The
member id is allocated as the pre-identity upgrade allocates one, so Captain's own
retained DM binding does not push it off the `kirocrew-captain` slug. Because a V2
member's pinned thread is keyed by its store generation, the move opens a fresh
Captain thread; the earlier thread's transcript stays in History. It is skipped when the key
already exists in `config.json` or the `config.local.json` overlay, when the
template did not install (a foreign or unreadable file owns the path), or when
the overlay supplies the whole roster. An empty base roster is seeded with the
same `default` row the loader's migration writes, so the implicit default member
survives. The `captain_member_created.json` sidecar records the attempt, so the
creation is one-time. Captain is identified positively by key AND binding
(`agent_files.is_assistant_member`); a member with any other key, including one
a user named `assistant`, is an ordinary crewmate the installer neither reads nor
changes.

Captain's identity is locked:

- **Not deletable.** `DELETE /api/agents/{name}` (pre-check and inside the config
  lock) and `kirocrew agent delete` refuse it with 409 / exit 1, code
  `assistant_member_protected`; the agent-sync and crewmate-prune passes skip it;
  the change-card catalog refuses any plan or undo plan that deletes
  `/api/agents/kirocrew-captain` (`deletes_assistant_member`). The crew editor
  shows Captain's Danger zone row disabled with the reason.
- **Key reserved for the installer.** `members.key_new_crew` -- shared by
  `POST /api/agents` (which the New crewmate card and the `crewmate.create`
  card both call) and `kirocrew agent create` -- refuses the key with 409
  `assistant_member_reserved` whether or not Captain exists, and never derives it
  from a free-form name. The `crewmate.create` card refuses it at preview, and
  agent sync never registers a discovered template under it.
- **Template fixed.** Rebinding Captain off `kirocrew-captain` (`PUT
  /api/agents/{name}` fast and generic paths, `kirocrew agent update
  --kiro-agent`) is refused with `assistant_member_reserved`. Keys are never
  renamed, so renaming Captain writes `display_name` only.
- **One Captain name.** Another member may not take Captain's current label
  (`display_name`, else `Captain`), compared trimmed and case-insensitively
  (`agent_files.collides_with_assistant_name`): create, `PUT /api/agents/{name}`
  and the `crewmate.create` / `crewmate.update` cards refuse it with 409
  `assistant_name_taken`, and the create and rename forms show the refusal. The ordinary chat
template and `default_agent` selection are not changed.
User-authored role text, permanent rules and briefing remain intact.
Recommendations use only the memory and history already available to the session
(Captain's own memory, its read-only `global_memory_recall`, and the
workspace-scoped `search_chat_history` / `get_chat_session` / `list_sessions`)
and explain their evidence. Changes to Kiro Crew itself (settings, schedules,
crewmates and their capabilities, templates, MCP servers, connections, secrets,
app trust, denied-command rules) go through change cards: the template proposes
one registered kind with `propose_change`, after `list_change_kinds` when the
kind is unclear, `find_setting` for a setting, and `get_member_capabilities` for
a crewmate's tools and approvals; `get_change_status` reads an outcome. All five
are pre-approved, since a proposal changes nothing. A reminder that runs once is
a `schedule.create` card with `at` (a local ISO-8601 date-time, no offset)
instead of `cron_expr`; the prompt says "Ready to schedule for ..." before the
owner approves and calls it scheduled only after the card result reports it
applied. How-to questions are answered from the packaged user docs through
`search_docs` on `kirocrew-guide`, and where-is questions first through
`find_ui` on the same server (both `agent._ASSISTANT_DOCS_GRANTS`, pre-approved):
`search_docs` searches and pages through `kiro_crew/docs/*.md` only, by the
directory's own listing, and `find_ui` searches the packaged dashboard location
index ([mcp](../../architecture/mcp.md#find_ui-and-the-ui-location-index)); its
labels follow the language the user's dashboard renders (the tab's `X-UI-Lang`,
read back through `GET /api/guide/agent/language`), not the conversation's, and
`find_setting` rows carry the on-screen label the same way. The prompt forbids reading
documentation or a tool's spilled output through the shell or a file read, and
keeps dashboard locations out of the prompt: the rule is procedural (quote the
path labels `find_ui` returns, in order, as plain text and untranslated, with a route only ever
as a markdown link on a label and never bare or in code formatting; state its
prerequisites, a reveal step carrying `only_if` as a conditional step such as
"If you don't see the list, click ... first" rather than a fact about where the
target is shown; treat no_match as "not indexed"; on a miss or an ambiguous
result, call `find_ui` again with `area` to list the likeliest area whole and pick
the entry that means the question, quoting only returned entries and relaying a
`tier: auto` entry hedged; if none fits, say so and offer `search_docs`), so no
location is ever stated from memory. A result's `guide_ref` (`settings.show`,
`mcp.open_add` or the generic `ui.show`) is offered as is with `guide_start` in
the same turn, so the guide arrives with the first reply and Captain never asks
whether to show it; a new offer replaces the chat's unfinished guide by itself,
so Captain never checks or cancels one first.
When offering a guide, the prompt has Captain pass one action for the one thing
asked about (more only when the user asked for several things, never a second
action just to reach a page, since each action walks there itself), with a short
`intro` and a `note` per action in the language of the user's latest message, saying why each step matters for what they
asked, without restating the card's title or the button name the panel already
shows and without telling the user to do anything the guide does not point at. The reply, and
every quick-reply option, follow the language of the user's latest message;
nothing is written before or between tool calls (the prompt pins this with a
bad/good pair: an "I'll find where…" bubble before the lookups versus the
lookups and the guide offer first, then one reply), tool results, blockers and
guide state are never narrated, and the reply never names a card's buttons
(they leave with the card while the reply stays). To turn a setting on or off,
Captain reads its current value from `find_setting` and offers only the change
it does not already have.
When `guide_status` or `find_ui` reports `gate_off`, the prompt has Captain
propose turning that `setting_id` on with `propose_change` (`setting.change`),
or offer `settings.show` for it when the setting is not writable; the guide
itself never flips a setting and goes on once the gate is on. The never-say-"card" rule names two bad shapes from real
runs, the second being "I'll set up a card where you type the value in
directly", with the replacement "I'll put a secure field here for you to type
the value into". When a loose request gets a default plan, the
plan keeps everything the user stated and fills only the unstated details. For "why" questions the
template also has two read-only troubleshooting sources, both pre-approved:
`diagnose_settings` on `kirocrew-guide` (`findings` first: read-only symptom
probes from `diagnose_probes.py`, one per reported failure -- a hidden model, an
agent spec installed after the gateway started, a model pin the account does not
offer, a deprecated agent spec, failing crons, dead MCP paths in agent specs,
kiro-cli sign-in, embedding coverage and dimension, an unreachable Remote Crew,
a turn running over 20 minutes, the last day's tool failures and refused turns,
and the usage limit -- each `{id, status: ok|warn|problem|unknown, summary,
evidence, fix?}` where `fix` is a change card to propose or steps to tell the
user; then every config and dashboard-config key
whose value differs from its dataclass default, joined to its Settings entry;
credential-like keys report only whether they are set; plus the newest 50
"Dashboard: ..." lines from Global memory history; an optional `topic` is split
into meaningful words and keeps a row, history line or `ok` finding when any word
matches its key, setting id, label or Settings description, most hits first,
returning the unfiltered result with `topic_matched: false` when nothing matches) and the `kirocrew-crew-log` server's `crew_log_list`, `crew_log_read` and
`crew_log_projection` (`agent._ASSISTANT_CREW_LOG_GRANTS`), mounted only on this
template and read under the crew-log routes' own scope. The prompt orders them:
settings diagnosis, then the crew log for a particular run, then the packaged
docs, and only then anything else; the answer names its source in plain words
and a misconfiguration found that way becomes a change card. The card renders in the
proposing chat as a row of the conversation at the point it was proposed, and only the owner's click
applies it through the existing settings route, verified by the card hook (see
[MCP architecture](../../architecture/mcp.md)). A secret's value is typed into the
card and never passes through the agent. The template does not apply settings
with `kirocrew config set`, and uses guides only when the user asks where a
control is or wants to make the change themselves. Card outcomes reach its next
turn as a `[CHANGE CARD RESULTS]` block that never starts a turn, and every owner
change through those routes, card or manual, is a names-only "Dashboard: ..." line
in Global memory history it recalls when recommending configuration. Crewmate
proposals are `crewmate.create` cards; the card builds the crewmate from
`kirocrew` (`change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`, the
Crewmates page's own default) and sends `first_greeting`, so the crewmate opens
its chat by asking about the proposed goal exactly as one created on the
Crewmates page does (see the first greeting below); the
`/members?create=1&name=<encoded-name>&goal=<encoded-goal>` link to an editable UI
draft remains for a user who wants to fill it in; opening a draft never creates a
member or starts a schedule.
Explicitly created members own unique V2 stores identified by an immutable persisted `member_id`, independent of their editable label.
Automatically discovered agents start on Global V1 without member allocation.
Missing, unreadable, shared or mismatched member identity makes memory operations
unavailable without choosing Global. Rules and briefing remain usable without
the learned database. Member isolation is routing for built-in tools, not secrecy
against arbitrary code running as the same OS user.
Selecting a member as `default_agent` preserves that member's memory version and
binding. With no agents configured, the resolver returns the existing defaults.

Member creation automatically provisions empty member memory. Members cannot
choose a shared store or rebind their member store. Legacy members may continue
using V1; member updates never initialize a V2 database. Global and named V1
contents remain untouched. Config fields, exclusive database creation, immutable database identity and
recovery semantics are owned by [config](config.md#named-memory-stores-memory_storespy).

A new member DM inherits the member's configured workspace, falling back to
`default_workspace` when that name is undeclared. Its project directory uses the
shared `default_project_dir` validation, so provider cwd and project essentials
refer to the same workspace. Resolution finishes before publishing the slot;
the first slot broadcast includes its project directory. A concurrent opener's
existing slot is preserved. Reopening a live or restored
thread keeps its saved workspace and project, including an explicitly empty
project, rather than resetting a session choice to the member default.

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

The Crewmates page (`/members`, titled "Crewmates") opens the built-in
`kirocrew-captain` member when there is no previously used conversation.
An explicitly configured display name takes precedence; its key, thread and
private V2 memory store are its own. Captain also reads the user's Global
`preferences.md` (injected read-only) and has a Captain-only read-only Global
recall, specified in
[memory-skills-hooks](memory-skills-hooks.md#captains-memory). A roster
containing only this member is not an
empty state. Returning users restore their remembered member, otherwise the most
recently used conversation, before falling back to Captain. Explicit member
and team links take precedence. On narrow screens, Back opens the roster through
`view=roster`; that explicit roster view does not auto-open a conversation.
Loading, a failed roster read and a genuinely empty roster remain distinct.

A fresh install's first run ends on Captain's chat (`CAPTAIN_ROUTE`):
`useFinishFirstRun` (`shell/boot/captainLanding.ts`) wraps `markOnboarded` and
navigates there only on the first completion, when Captain is registered, in the
main window, and when the dashboard was OPENED on the default landing (`/` or
bare `/chat`, no `sid`/`slot`). The entry address is captured on the first
render, so the chat page's own empty-session rewrite and the tour's page visits
do not change the answer, and a deep link the user opened is never overridden.
An onboarded user replaying the tour stays where they are. Every exit of first
run lands the same way: Done, Skip all (from Import setup or the tour) and
Privacy's Continue all end in that one callback. "Registered" is read from the
crew list (`/api/agents`), which can still be loading when an exit is pressed on
a cold start; that is not read as "no Captain". The exit is held
(`captainKnown: false`) and lands once the list answers with Captain, unless the
user has moved to another page meanwhile.

Captain's chat has no static welcome card. Captain speaks first instead: once
the thread endpoint has confirmed Captain's pinned thread, the page calls
`POST /api/members/{slug}/greet` (owner-only; app tokens get 404), once per
thread per tab (`pages/members/useMemberFirstGreeting.ts`). The server
(`dashboard/captain_greeting.py`) starts a greeting only when the slug's DM
binding names `kirocrew-captain`, the live slot is that member's pinned
(`mode="member"`, agent `kirocrew-captain`) local thread, it holds no rows or
queued entries, and no turn is running. It then claims the once-only marker
`members/<slug>/captain_greeting.json` with `O_CREAT|O_EXCL` before dispatch, so
two tabs, a reload or a retry after a failed turn never greet twice; a declined
request leaves the marker unclaimed. The greeting is one real Captain turn:
`_run_chat` with the hidden kickoff `CAPTAIN_GREETING_KICKOFF`
(`_synthetic_payload=True`, actor `gateway`) and NO transcript row for it, so the
user sees only Captain's own reply, which stays in Captain's session context. The
Captain role prompt's "First greeting and names" section tells it to reply in at
most three short sentences: introduce itself by its name, cover in one sentence
finding things, pointing with an arrow on the page, making confirmed changes and
troubleshooting, then ask what to call the user, with no feature tour, suggestion
or tool call. When the user answers, it saves the name once with
`global_preference_add` (a standing preference on Global memory). It uses a name
only when the User Preferences block or a memory result states one explicitly,
and then greets the user by it instead of asking; it never infers one from a
username, home path, email or host name, and a first greeting never says
"welcome back". The kickoff restates both. The route answers
`{"outcome": ...}` (`started`, `already_greeted`, `not_empty`, `busy`,
`not_captain`, `no_thread`). A greeting turn that fails surfaces through the
ordinary turn error path once; the marker is already claimed, so it never loops,
and the composer stays usable.

A newly created crewmate speaks first through the same route and the same
dispatch (`maybe_start_member_greeting`), and only when its create asked for it:
`POST /api/agents` with `first_greeting: true` (exactly `true`; any other value
owes nothing) writes `members/<member_id>/first_greeting_owed.json` after the
record is published. The Crewmates page's create form sends it, and so does the `crewmate.create` card; the crew manager's door and the
CLI do not. The page asks only for Captain and for
dashboard-created rows (`dashboard_created`); the server answers `not_owed` for
a crewmate without the record, so an existing crewmate is never greeted. An
owed crewmate greets on its own pinned local thread (binding member == slot
agent), while that thread is empty and idle, at most once: its own
`members/<slug>/first_greeting.json` claim, with `O_CREAT|O_EXCL` before
dispatch, beside Captain's untouched marker. The turn runs the hidden kickoff
`CREWMATE_GOAL_KICKOFF` with no transcript row, which asks the crewmate to
introduce itself by name and ask, in plain words, what the user wants it to do,
without starting work. The user chose a name and a look, not a template, so the
kickoff also tells it not to name its template, role or agent type and not to
describe how it works; when the
record carries a `description`, `crewmate_goal_kickoff` appends it as a first
draft of the goal for the crewmate to confirm. A failed record write is logged
and leaves the create successful, with a chat that simply opens empty.

The primary New crewmate action (the header **+** menu's row, the empty roster's
hero and the switcher's New crewmate) opens the embedded `NewCrewmateDialog`:
one card headed **New crewmate** with a **Name** field, an **Avatar** preview
with **Try another look**, an **Advanced settings** row, a one-line hint that the
crewmate will ask what it should do, and **Cancel** / **Create** (stacked
full-width below `sm`, Create on top). Create builds the crewmate from
`kirocrew`, and the folded card does not name that template. The look is a
name-seeded ghost (`seededTraits`), re-rolled by appending `#<n>` to the seed,
and is pinned on the record as `avatar: {kind: 'ghost', traits}`, so the face
shown is the face kept. The **Advanced settings** row (the whole row is the
button; its chevron turns as it opens) unfolds inline below the avatar, in the
same card: Built from, What it looks after, and the crew editor fields
(workspace, model, triggers, session colour). It is folded by default;
`startExpanded` opens it unfolded, which is what the hero's Advanced link and
the crew manager's door pass; the "+" menu holds only New crewmate and New team. Folding unmounts
those fields but keeps their values, and every create sends them.

The card is the page's one creation surface. A drafted proposal (a Captain
create link `/members?create=1&name=…&goal=…`, clicked in Captain's chat or
reached by address, or the `crewmate.create` guide, whose enter step is that
link) opens the same card with the proposal filled in: the name in **Name**,
the goal in **What it looks after**, with Advanced settings unfolded when a
goal is proposed so it is in view (`initialDraft`). The page strips the link's
parameters once read. A proposal left as proposed is not the user's draft: it
leaves without asking, and a later proposal replaces it at once; a card the
user changed is replaced only once its own leave guard agrees, or when the
guide that brought the proposal already asked. No modal, viewport scrim or
focus trap is added. The current chat stays mounted while hidden, preserving
its draft and reading position, and closing the card returns to it. The guided
door (`guided`) carries the guide's anchor on its Create button, and its create
request carries the guide headers so the gateway credits the guide's one
commit step from the crewmate it actually created; a save the guide had not
reached closes the guide as saved without it, and a refused save stops it
waiting. When the crewmate runs is not asked on the card: the crewmate's first
greeting asks for its goal and, once the goal is clear, may offer a schedule
for the user to confirm (see `captain_greeting.py`).

The explicit Advanced entry opens the same card with its workspace, model,
routing and colour controls already unfolded, also embedded in the page. That
card is the ONE "New crewmate" dialog (`website/src/pages/members/NewCrewmateDialog.tsx`): it is
also mounted by the crew manager's "Add crew member" tile / header button and its
dashed roster card (`website/src/pages/KiroCrewAgentsPage.tsx`, the Crews tab of
Agent Capabilities), which has no separate create sheet. Both forms use the
existing owner-gated `POST /api/agents`; editing an existing member still uses
the crew manager. The crew manager's door opens with Advanced settings
unfolded and `kirocrew` as the default **Built from** (labelled as the
default), and shows no first-greeting hint, since it asks for none. What happens after a create depends on
the door. On the Crewmates page the dialog sends `first_greeting`, the page
re-reads the roster and opens the new crewmate's verified thread (a failed
re-read keeps its retry), and that open's greeting request starts the hidden
goal question; nothing is sent in the user's name. From
the crew manager the door is CONFIG mode: the dialog closes and the crew manager
re-reads its roster so the new card appears in place; it does NOT navigate to the
crewmate's chat and asks for no greeting. The dialog's own success path (cache
invalidation, the post-failure reconcile, "what it looks after" stored as
`description`) is identical on both doors; only the caller's `onCreated`
differs. `NewCrewmateDialog` imports its field frame and field components from
`KiroCrewAgentsPage`, so the two pages cannot drift; the resulting call-time
import cycle is resolved because both reference the other's bindings only inside
component bodies.

The roster lists a row unasked when EITHER its Crewmates-page DM thread already
holds a message (any origin) OR it was created on the dashboard (`source` is
`kirocrew` AND the record carries a `member_id`, which covers a greeting that
never landed) OR the user starred it; the default crew (whichever crew the top-level `default_agent` names) is
always listed. Every other row (an app's own source stamp, with or without a
member id, a sync-generated row, a legacy `kirocrew` row without a member id,
none of them chatted with) is hidden and appears when the search text matches
it. The star, origin and status filters narrow the rows the roster shows, so
choosing an origin does not reach a hidden row; the search is the one door
within the roster list. A team's view (`?team=`) and the team dialog are built
from the whole roster, so they still list every crewmate the user put on that
team -- placing a crewmate on a team is itself a choice to use it -- and a team
whose crewmates are all hidden keeps its (empty) roster header. The crewmate
open in the thread stays listed while open, and a remembered crewmate is
restored even when the rule hides it; with nothing but the default crew
listed while hidden crewmates exist, the landing opens the most recently used
hidden one (listed while open); where nothing auto-opens (below md) and every
row is hidden, the roster says so and names the search. If
the default-crew lookup fails, every row is listed and an error notice says why. `GET /api/members` carries the two facts as booleans, `dashboard_created`
and `has_dm_message` (`ConversationLog.has_messages` on the bound thread, which
stops at the first non-metadata row, or rows held by the live slot; an
unreadable transcript counts as a message); the member id itself is not on the
wire. The client also treats a non-empty live `last_message` as a message, so a
row the user just chatted with stays listed before the next roster read. A row
from an older gateway carrying neither field is listed. The header count and
the filter tallies count the listed rows plus any hidden row the search
reaches. The landing fallback opens a remembered crewmate first (even a
hidden one, which is then listed while open), then the most recently used listed
crewmate, then the most recently used hidden one when only the default crew is
listed.

Deleting a crewmate is not on this page: it lives in the crewmate's settings
on the Customize page's Crewmates tab (the editor's Danger pane), which also
reaches rows this page hides.

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
outranks all four and is not considered there. A bound template whose `model` is
`auto` is NOT treated alike everywhere. The chip (`resolve_effective_model`)
collapses it through `normalize_agent_model` and shows the global. The session
layer (`session._session_model` -> `_model_fallback`) defers to the provider for
ANY nonempty template value, `auto` included, so it returns `None`. The factory
(`acp_effective_model`) then reads the template pin, collapses `auto` to `""`, and
sends no model, which leaves kiro-cli's own resolution in charge rather than the
global. That precedence is unchanged for ordinary named templates. No template
follows the global default through these resolvers. Captain's template follows it
through materialization instead: its installer writes the effective `agent.model`
(base `config.json` with the `config.local.json` overlay merged, normalized) into
the spec, or `auto` when that is unset. The `agent.model` applier's
`rebuild_agent_config` then rewrites the spec on each change, while a dashboard
pin (`model_managed` False) is kept.

The loader is defensive about hand-edited config: a non-string `model` or
`triggers` collapses to `""`, an unknown `reasoning_effort` collapses to inherit,
and a junk watchdog override collapses to `0`.

### Crewmate conversation layout: switcher, Profile, Dashboard, Files

When a crewmate conversation is open, the standing roster is hidden at every
desktop width and the thread receives that space. `CrewmateSwitcher` replaces it
in the thread header: the closed Glass chip stacks up to three faces plus the
roster count; its popover searches every crewmate, marks the current one, switches
the thread through the existing verified `openMember` path, and opens the same
`NewCrewmateDialog` as the roster's create entry. The stacked faces are the OTHER
crewmates' in roster order — the open crewmate's face is already the identity
pill beside the chip, and a second copy there read as a duplicate rather than as
the rest of the crew; it joins the stack only when it is the whole roster, so the
chip never draws empty. Below `md`, the roster remains the first level and the
thread's Back control returns to it.

The switcher lists crewmates only. Team headers (and the team view they open),
**New team**, the per-crew star, the filters and the sort live on the roster
column, and on desktop nothing else reaches them once a DM is open (the Back
control is `md:hidden` and a bare `/members` reopens a crewmate). So the
popover's footer carries a second action beside **New crewmate**: **Show the
full roster** (`roster_show`) pins the roster column back beside the thread —
page state (`rosterPinned`), not persisted, since the folded roster is the
desktop norm and a pin is a detour from it. While pinned the aside is `hidden
md:flex` (still folded below `md`, where Back is the way), it reserves its width
again for `panelSitsBeside`, its resize handle returns, and the same footer
action reads **Hide the roster** (`roster_hide`). Picking a row from the pinned
roster switches the thread and keeps the pin, and the roster header carries its
own close control (`member-roster-hide`, `md+` only, shown only while pinned
beside a thread) that lifts it. A TEAM view is not folded: `TeamView` draws no
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

The centred identity pill opens `CrewProfilePanel`, not the editor. It ends in a
small `ChevronRight` (decorative, `aria-hidden`): the one visible sign that this
Glass chip is a door, since the switcher chip beside it is another Glass chip
with faces in it. The pill is a toggle, as its `aria-expanded` says: with Profile
already open a press CLOSES it through the same schedule-draft guard as the
card's own close control (`requestCloseProfile`). It must not re-open on the
Profile tab: `profile.tab` is part of the card's React key, so a card opened on
another tab (the quiet-chat Sessions link) would be remounted and a New schedule
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
pill unmounts in the non-phone column state and its `CrewStateAvatar` shares a Framer
Motion `layoutId` with the card's head avatar: one face moves from the pill to the
card rather than two faces cross-fading. Opening SidePanel collapses that Profile
column and restores the pill. If SidePanel is already open, or the viewport is a
phone, the pill opens Profile as a full-height, rounded hover card centred
horizontally in the chat width the open SidePanel leaves; both surfaces remain
visible. The card matches SidePanel's vertical bounds but is not a SidePanel tab
or a second panel. Escape is handled on the card subtree, not the window, so it
cannot close Profile from the roster switcher or another composer. While a
pushed page is open, the covered tab subtree is both `aria-hidden` and `inert`,
so keyboard focus cannot reach controls behind the pushed page. Pushing About or
Notes moves focus to the back control (New schedule focuses its own form), so
Escape still pops the page, and popping returns focus to the control that opened it.

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
word. The tabs are **Profile**, **Schedules**, **Sessions**, and **Goals**. No
tab carries a count, and there is no placeholder tab for a view that does not
exist yet.

**Profile** shows only the face and name in its head. Its body carries the crew
description (two lines with a pushed full-text page); when the registry read fails,
the About section shows the shared actionable `ErrorNotice` above any retained
description, and only omits the no-description claim when no cached description
exists. Memory and
Workspace sit side by side, but Memory is a flat readout — a solid `border-border`
hairline, transparent background, no hover and no pointer; never dashed, which is
this card's empty / coming-soon language (Sessions empty, Goals) and would make a
set binding read as not set up — while Workspace keeps the filled clickable-card
treatment with its hover. Notes, Permissions, and Model follow.
The pencil, Permissions, and Model entries open the existing `CrewEditorDialog`;
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

**Goals** is a coming-soon placeholder with no input. Goals need a record the
crewmate can read and act on; a browser-local list it never sees would promise
the opposite, so none is offered until that record exists.

The chat SidePanel has two standing entries on this page: the leading
**Dashboard** tab and pinned **Files**. Dashboard is the crewmate's generated HTML
report (`GET /api/members/{slug}/panel?member=<exact-name>`) rendered by
`CrewDashboardFrame`: the full document fills the tab, minted on mount, with no
summary card, Contained bar, Expand control, identity row or Command Center
around it. It shares the drawer's read (`member-panel` query keyed on slug and
exact member), its `CREW_WEBVIEW_SANDBOX` (`allow-scripts` only), and its
single-use mint rule; the `CrewWebview` drawer itself is unchanged. Its
"Opening the conversation" line is withheld while the main column already says
it (`activeSlot` unset), so two live regions never say it at once. The tab is a
standing entry whatever `PREVIEW_DASHBOARD` says: that preview gates the Dynamic
Dashboard (the Command Center and its in-chat dock), but the crewmate's own
published page is not that surface. This page never passes the in-chat Command
Center dock opener into its `ChatPane`, regardless of the preview flag: the
permanent Dashboard tab is the one entrance here, so a second control would point
at a different dashboard concept from the same conversation. With no Command
Center in the tab there is no unsent answer for the page
to hold or guard, so exits ask only about a Schedules draft. The frame's own
read-error and mint-failure notices carry a retry and NO agent hand-off: the
hand-off is a raw soft navigate to `/chat` that unmounts this page without asking
its leave guard, and `ErrorNotice` has no gate to hand it, so with a Schedules
draft or an editor pane open it was an unguarded exit. With nothing
published it says so and points at the chat: a crewmate publishes through
`panel_publish` (on by default via `agent.crew_panel`) when asked or on its own
cycle, and nothing in the crew editor makes it publish, so the state has no
set-up control. That sentence names the crewmate by its display name
(`displayName`, the page's `crewDisplayName`), falling back to the exact
`member`; the read itself stays keyed on the raw `member`, so a crewmate shown as
"Atlas" is not told "atlas has not published". Files is the standard SidePanel browser scoped
to the workspace rule above. Dashboard and Files are the only STANDING tabs.
Side Chat remains available dynamically through selection Ask and the plus
menu; it is not a standing entry — and the plus menu keeps every other dynamic
view the chat page offers (Terminal, Browser, Git, Subagents, Workflows, the
Developer-Mode Logs / Context / Crew log, app-contributed tabs), bound to the
member's slot once the thread is confirmed. Withheld outright are the
transcript-fed views (Changes / Issues / Links / Pins, plus the session
Summary), the chat page's Command Center, and Artifacts — a pinned standing
tab on the chat page that has no place in this page's Dashboard + Files set
(`MEMBERS_WITHHELD_VIEWS`).

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
  matches one. Both markers are left in place and do not stop the current
  pass, which runs once on those installs too.

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
installed template on the same `default` store when its name is absent from
`config.agents`. That is the binding the removed row carried, so no other store
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

Removed rows do not come back: since #12224 the dashboard pickers read
`GET /api/agents/catalog` and nothing calls `POST /api/agents/sync`, so the rows
this pass removes were written by builds before that change. The sync route's
own contract is unchanged.

The RFC's original screen 03 ("a user with custom agents but no crewmates is
offered to add them") is superseded: #12224 ended the enrol-on-mount behaviour,
and the Crewmates page's empty state with **New crewmate** (#12924) is the path
from a custom agent to a crewmate. What an existing user actually has is the
opposite problem — the crewmates that sync already made — and that is what this
migration settles.
## A crewmate's chat

A member-mode slot's transcript is the crewmate's whole working record: the
`[auto-nudge cycle N]` turns of its patrol loop, `[Cron notification …]` and
`[Subagent completion event]` envelopes, every tool call, and the say-nothing
reply a quiet patrol ends on. The Members page's chat pane shows **only what
the crewmate says to the user** and the user's own messages. Everything else stays in the slot's history and is filtered at render time by `filterCrewmateChat`
(`website/src/components/chat/crewmateBubbles.ts`), which `ChatPane` applies
when its host passes `crewmate`. Ordinary chats never pass it and are drawn
unchanged.

Captain's chat (the identity carries `captain`, set from `isAssistantMember`)
also folds the narration the model writes before a tool call ("I'll look up
where chat history lives…"), structurally, because prompt rules did not stop
it. `captainNarration` reads the unfiltered transcript one turn at a time
(turn boundaries as the run rule reads them): a speech row followed by work (a
tool row or a change card / guide offer) and, after that work, by more speech
is hidden; while the turn is live, a speech row with no work before it draws
as a muted one-line status (`captain-status-line`) instead of a bubble, since
it may yet be narration or the whole answer; every other speech row is a
bubble, including the final text after the last tool call and a settled
turn's only text. Other crewmates' chats skip the read.

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
user must see or act on stay: `user`, `error`, `notice`, `file`, `mcp_oauth`,
`permission` (a pending approval is still the approval surface), the stop
card, and the live `streaming` row. A stop card (a `system` row of kind
`stop_event`) is drawn too, and like an error row it breaks a run. When the
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
addresses the crewmate by name ("Message <name>…"), not the product. The roster row
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

How it is drawn: the crewmate's messages form **runs**. A message carries NO
author line — no avatar, no name, no time row — and no avatar gutter: the chat
is a 1:1 thread with one speaker besides the user, and the DM header's identity
chip already names that speaker (#16617 retired the per-run author line #15167
had made redundant). Every message is its own bubble (`bg-card`,
`border-border`, `max-w-[72ch]`) in the full text column, and consecutive
bubbles read as one speaker through their grouped corners alone. Corner rule on
the run's (left) side:
single = all corners full; first = bottom-left small; middle = top-left and
bottom-left small; last = top-left small; right corners always full. A run is
ONE TURN's bubbles (RFC screen 05): it breaks on a user message, on any row the
user sees between two messages (an error, a pending approval), and at a turn
boundary the unfiltered transcript still carries — a patrol wake, a cron or
sub-agent envelope between two replies — which `crewmateRunPosition` reads off
the `crewmateTranscript` the pane hands its renderer; there is no time rule. A
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
another reply, but in this chat a run only ends on a boundary the user sees or
on the silence gap, so the run end IS the turn end — and the steer-chip
suppression (`turnHadPolicyBlock`) reads the UNFILTERED transcript the pane
passes as `crewmateTranscript`, because the policy-block marker lives on an
`inject` row the filter drops, and reading the filtered list would credit a
system-forced continuation to the user. User messages keep their existing
rendering. The run position is exported (`crewmateRunPosition`) for a
reply-thread footer to reuse; no DOM attribute is stamped until that reader
exists. The reply thread's own panel
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
steer-only user row (through `renderUserContent`), and the sub-agent / workflow
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
| `test/test_pruned_crewmate_records.py` | Records bound to a pruned synced crewmate decode as their template for every reader (chat resume, subagent continuation and inheritance, prompt builder); owned members still refuse; the first send of a resumed pruned chat is admitted by the session CAS while a real concurrent change is still refused |
| `test/test_dashboard_agents_composition_contract.py` | The agents handlers keep their surface across the `agent_admin` split: every base name still resolves on `handlers/agents.py`, routes and the handlers package reach the same objects, each owner runs on the facade's globals so a test's patch reaches it, and what repository guards read in `agents.py` stays there or is re-keyed to its owner |
| `test/test_agent_execution_catalog.py` | Read-only catalog, same-name member/template choices, requesting-project isolation, private-template exclusion and explicit discovery failure |
| `test/test_agent_templates_endpoint.py` | Templates roster marks editability (a row with no spec file beneath the agents directory — empty, foreign or absent `filename` — is read-only for the runtime's reason) and references (crews, default, schedules by what they dispatch — sequence over dormant `agent_id`, the captured execution's template over a stale or empty `agent_id`, script jobs over neither — chat-folder pins, webhook pins, private copies) and masks package-controlled strings like the sibling rosters (the delete refusal's references too); a row whose filename is absolute, traversing or nested names nothing to delete (404, file intact); create writes a minimal runnable spec or a lineage-free copy (re-read inside the spec lock, where the source name is re-resolved and must reach exactly the probed file — a second claimant or a replacement refuses, nothing written) and refuses taken, bound (in the base or only in the overlay), reserved, ambiguous and malformed names; delete refuses read-only and referenced templates (listing the references), a name two files reach — a crossover or a same-name twin the roster would collapse (neither unlinked), a row whose file does not answer to the requested name, a second claimant that lands after the probe (ambiguity re-checked under the lock) and a row that calls a package file plain (the file re-read and classified under the lock), checks and unlinks inside one folder-store hold rather than from a snapshot, counts a binding that lives only in `config.local.json`, does not count a template that merely shares the default crew's alias, holds the schedule store's own lock from the reference walk through the rename (probed on both sides) and answers 503 `schedule_store_busy` with the file intact when another holder keeps it past the bounded wait, names a schedule written past the lock (warning + SEL row), fails closed on an unreadable cron store before the unlink (503, file intact) and only warns after it, retires the file as a one-deep tombstone (renamed before the older grave goes, so a refused rename keeps both; same-second graves stay distinct; the sweep spares a live template whose name looks like a grave), runs both mutations through the drained seam, and removes an unreferenced one; create re-scans by declared name under the lock; a successful create and delete emit operation-labelled SEL events; a create publishes its name to the dispatch snapshot before scheduling the rescan and a delete awaits the rescan before answering (a refusal touches neither); the detail PATCH writes the definition keys on an owned template, refuses them on a package one, refuses every key on an ambiguous name (neither file touched) and a claimant landing after the scan (re-checked under the write lock), classifies the targeted file rather than its name, and validates their shape |
| `website/src/test/AgentTemplatesTab.test.tsx` | Grouping by origin, the two-control action row with its overflow menu (enroll hint, Delete vs Duplicate-to-edit by editability), the definition save through the detail PATCH (changed keys only — a prompt-only save never resends the model), the dirty-draft guard on row switch, on a background refetch, on Discard (asks; declined keeps the draft) and on New custom agent (a create never inherits the previous draft), a saved skill list written into the detail cache before the refetch lands, the saved confirmation in the bar's slot and the visible Add tool label, a delete naming the deleted template over the next row and, on a narrow viewport, returning to the list, every in-app link routed through the shell's leave gate with its target, `beforeunload` armed only while dirty, a rejected detail read rendering its error rather than Loading, a refused save reported inside the save bar beside Save and cleared by Discard, resources as plain rows, string-only MCP fields from a hand-edited spec, one Skills heading, the referenced-delete dialog (opened directly from the row's own holders with no confirm or request, and from the server's refusal when a holder landed later; including a chat-folder row and a private-copy row that links to its crew), a private copy's Open crewmate, the usage line naming folder and webhook holders with each holder linked to where it is held, blank vs `from` create with the created row selected after the roster refetch, and chat-with in the template namespace (enabled while dirty, behind the discard confirm) |
| `website/src/components/RestartButton.cov80.test.tsx` | Apply & Restart (mounted in the Connections header) asks first, naming what stays (chats and history) and what stops (a reply in progress); declined does nothing, and the confirmed paths (success, failure, in-flight, MCP reconcile) run with the ask answered yes |
| `test/test_chat_agent_kind.py` | `agent_kind` on local and remote slot create and switch: template picks skip the member store pin, name and kind persist atomically, an unresolvable stated kind is `409 agent_choice_unavailable` refused before any slot is minted, an unknown kind is `400 invalid_agent_kind`, a member thread refuses the same-name template kind, the slot projection carries the committed kind |
| `test/test_remote_crew_execution.py`, `test/test_remote_crew_adopt.py` | Remote create, switch and adopt preserve the selected agent namespace across the gateway boundary and in mirrored history |
| `test/test_open_slots_persistence.py` (`test_restore_carries_the_agent_selection_namespace`) | A template-picked slot restores as a template pick; an unknown persisted kind reads as name-only |
| `test/test_select_crew.py` | Roster excludes the default crew and every triggerless crew, carries `default_agent` plus guidance; an entry and a `route_crew` match or `unavailable` entry carry `display_name` only when it differs from the key; a named crew returns its bindings; an unknown name returns `error` plus `available`, each key followed by its differing label; the schema accepts spaces and dots in a crew name |
| `test/test_crew_reasoning_effort.py` | Per-crew effort reaches a crew dispatch |
| `test/test_members.py`, `test/test_members_dm_thread.py` | Slug validation and containment, activity recording and dedupe, DM-binding canonicality, rules and briefing reads, briefing endpoint |
| `test/test_chat_send_agent_model_default.py` | The crew model default a new session starts on |
| `website/src/components/chat/crewmateBubbles.test.ts` | What a crewmate's chat draws (machinery dropped, speech and user-facing rows kept, same array back when nothing is dropped) and the run rule (first/middle/last, single, breaks on a user row, a pending approval and the 5-minute gap, reads through a resolved approval and an untimestamped streaming row, corners on the left side only) |

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
a "Crew Members" door that opens `/members`.
