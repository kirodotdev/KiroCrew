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
| `src/kiro_crew/config/prompt-orchestrator.md` | The orchestrator prompt that names `select_crew` and the delegation rule |
| `src/kiro_crew/dashboard/handlers/agents.py` | Crew CRUD on `/api/agents`, and the roster row serializer |
| `src/kiro_crew/dashboard/handlers/agent_catalog.py` | Read-only `/api/agents/catalog` execution choices, with separate member and template namespaces |
| `src/kiro_crew/dashboard/handlers/agent_templates.py` | The Agent templates tab's roster (`/api/agents/templates`), create, delete with reference guard, and the read-only rule the detail PATCH applies to definition edits |
| `website/src/pages/overview/AgentTemplatesTab.tsx` | The **Agent templates** tab of `CapabilitiesPage`: list by origin, edit the shared definition, create, delete, chat-with / enroll |
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, thread get-or-create, rules, activity |
| `website/src/pages/KiroCrewAgentsPage.tsx` | The Crews UI, mounted as the **Crews** tab of `CapabilitiesPage` (Agent Capabilities) |
| `website/src/components/crew/crewEditorSections.ts` | The crew editor's pane registry, including the Routing pane that edits `triggers` |
| `website/src/components/CrewWakeSection.tsx` | "What wakes this agent" — schedules, deliberately distinct from `triggers` |

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
**Crewmates** / **Agent templates**, each member row wearing the same avatar the
roster draws for it, the origin badge dropped because the header already says what
a row is, and the templates group carrying a one-line hint that a template pick
runs the shared template on the shared default memory and enrols nothing) and
the same list folded to one row per name, member first, as `agents` for the
name-only consumers (cron `agent_id`, channel and project bindings, the cycle
shortcuts). The pop-up draws the group headers and the templates hint only when it
lists more than one kind: a header that separates nothing is chrome, and the hint
contrasts a template against a crewmate the list must then be showing. Temporarily,
`HIDE_CREWMATE_CHOICES` in `useAgents.ts` withholds the member rows from `choices`,
so the pop-up offers templates only -- a plain list, no header -- and a crewmate is
reached from its DM thread instead; the folded `agents` list and the request
contract below are unaffected, and turning the flag off restores the two groups.
A pick sends `agent_kind` with the name on slot create and on
`/api/chat/slots/{slot}/agent`; the slot stores the committed kind, persists it with
the other slot-owned metadata (`SLOT_OWNED_META_KEYS`, so a restart restores a
template pick as a template pick and a later name-only pick retracts it) and the list
projection exposes it, so a same-name member and template are distinct sessions. A
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
the remedy in the dashboard's own labels -- the crewmate's Agent Template tab;
on the crewmate's own copy 'Save as new template…' under another name, which
moves the crewmate to the copy and keeps its edits, or 'Reset my changes' to the
shared template; on a shared template bound under a reserved id, where neither
control renders, the template picker at the top of the tab -- raised without the generic
`cannot project agent ... onto KAS` prefix (`KasReservedAgentIdError`), instead of letting the activation guard report the spec as
missing or a built-in run in the crewmate's place. The dashboard renders the
refusal in the user's language inside the dialog that sent the name
(`components.agentTemplateDetail.publish_name_reserved`,
`pages.overview.agentTemplatesTab.err_name_reserved`), naming the typed name.

## Agent templates tab

The Template pane inside a crew editor edits that crew's PRIVATE copy of a
template (blueprint semantics, below). The **Agent templates** tab under Agent
Capabilities is the other half: it manages the shared templates themselves,
the files under `~/.kiro/agents/` a chat or a crewmate runs.

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
`_unlink_copy_unless_referenced` in `handlers/agents.py` established. The
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
is guarded like one: **New template** asks before discarding a dirty draft,
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
save affects and carries both halves of the save-vs-restart model in one line
(save; new chats use it at once; chats already running pick it up after
**Apply & Restart**, top right), so the reader never has to reconcile a "no
restart needed" bar with a Restart button; the page header's **Apply &
Restart** carries its own tooltip saying what it is for (relaunching sessions
already running); the tab's glossary also says, in visible text, that saving
writes the file at once and Apply & Restart only relaunches running sessions
while chats and their history stay, for the reader who never hovers. The save
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
template runs — duplicate it to write your own") rather than mechanism; the Apply & Restart
tooltip says what a relaunch does to work in progress (chats and history stay,
a reply in progress stops, the next message starts fresh), and the button asks
the same in a confirm at the moment of the click (`RestartButton`), since a
reader who reads "Restart" as breaking something never presses it from a
hover title alone. The MCP servers heading carries a plain-word gloss beside
the acronym ("external tool connections"). The Add tool
input keeps the words "Add tool" visible as its label while open (one control
in two states, not two controls), is wide enough for its example placeholder (`fs_write or @github/…`)
and offers a datalist of kiro-cli's native tool names plus every name the
template already grants (offered, not enforced); the enroll row says where
the result lands (under Agents); the save bar's two buttons never wrap or
shrink, and its instruction names the button by its label ("Save template"). A private copy is never a dead end: its
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
reason as a title; the per-template box carries the shared-edit warning, the
tab's header is a term-led glossary — **Template**, **Chat** (a one-off
session that runs a template; nothing is created or enrolled — said so it does
not collide with the sidebar's Chat), **Crewmate**,
one line each, the override defined under its own group instead — plus one
sentence on saving vs Apply & Restart that says in visible text when a
relaunch is needed (a chat already running picks up a saved change only after
it) and what it keeps (chats and their history); not a second copy of that warning.
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

Reopening a running Member DM, including a turn awaiting tool approval,
reuses its captured execution record. The canonical session key, selected
member, live slot store and execution record must agree. This read does
not pin or repair memory while work is active; missing, mismatched or unreadable
identity still refuses. The handler rechecks slot identity after the off-loop
store read, and a link to another session remains a conflict.

The member's presence indicator includes active child runs even while its own
turn is idle. Completion of the member's planning turn does not imply its
delegated work has finished. When only child runs are active, the Work log
tab's status line says "Delegated work running". Driving sessions still lists dashboard
sessions created by the member; child runs do not become dashboard sessions.

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
does not migrate it or grant access to that member's member store.

`resolve_effective_model` is the single source of truth for what model a new
session on a crew starts with, highest tier first: the crew's own `model`, the
bound kiro agent's pinned model (skipped for the built-in `kirocrew` agent), the
global `agent.model`, then the installed agent file's model. A per-session pick
outranks all four and is not considered there.

The loader is defensive about hand-edited config: a non-string `model` or
`triggers` collapses to `""`, an unknown `reasoning_effort` collapses to inherit,
and a junk watchdog override collapses to `0`.

### Crewmate panel: Notes, Work log, Dashboard

The Crewmates page's right panel has exactly three host tabs, in this order:
**Notes**, **Work log**, **Dashboard**.

**Notes** renders the crewmate's self-maintained briefing
(`members/<slug>/briefing.md`) read-only, as markdown, through
`GET /api/members/{slug}/briefing?member=<name>`. The response carries `slug`,
`member` (the exact name echoed back — the slug is lossy, so the frontend keys
its cache by name), `supported`, `text`, `updated_ts` (the file's mtime in epoch
seconds, `null` when there is no file), `redacted` and `truncated`. `text` is
`""` and `updated_ts` is `null` for a crewmate that has not written notes yet;
that is the normal state, never a 404. `supported` is
`member_briefing_supported()`: on a platform without `O_NOFOLLOW` plus the
pinned ancestor walk the read fails closed to `""` and the panel says the notes
cannot be read on this computer instead of showing an empty briefing.

The panel offers NO editor for the file, and the response carries no file
pointer. The file is agent-written; the dashboard's file viewer reads through
`/api/file-read`, which redacts, and its Save writes the buffer back, so any
in-dashboard edit could replace a secret the crewmate wrote in the meantime
with its placeholder — and a read-time "safe to edit" verdict cannot close that
window, because the viewer re-reads on open and on its live watch. The notes
are changed where the crewmate keeps them, outside the dashboard.

The text is the bounded buffer of `read_member_briefing_bounded`, redacted
through the same chain as the activity endpoint (`redact_exfiltration_urls`,
then `redact_credentials`) because the file is agent-written, and only THEN cut
at `MEMBER_BRIEFING_MAX_CHARS` with the visible marker (`cap_member_briefing`,
`drop_split_tail=True`): a redaction over already-capped text cannot match a
token the cap split in two, so the plaintext half would cross the wire
unmatched, and the cut also drops a trailing split word so the shown text never
ends in the first half of a token (the bounded read has an edge of its own).
The cut is judged on the REDACTED length, so a briefing that only overflowed
before its placeholders shrank it is shown whole, with no marker. The prompt
path (`read_member_briefing`) composes the same two functions with the plain
cut. The read treats `members/<slug>` LEXICALLY: `read_member_briefing_bounded`
resolves the members root once and appends the slug unresolved, so the pinned
walk opens that component with `O_NOFOLLOW` and a `members/<slug>` swapped for
a symlink to a peer's directory is refused (it reads as no notes) instead of
being followed by `member_dir`'s `resolve()` before the walk begins — this is
the reader the prompt path uses too, so the crewmate's own context is built
from the same refusal. The mtime comes from the same pinned open as the text,
never a separate `lstat`, so `updated_ts` is `null` exactly when the text reads
as none (unsupported platform included).

App-token callers are denied exactly as on every other member surface, and the
read is owner-gated like the rules read (`require_owner_dashboard_request`,
before any validation or file IO): the briefing is the owner's private notes,
and a `!dashboard` session minted by another allowed user must not see them.
The exact `member` must derive the slug, exist, and be the ONLY crew that
derives it (the rules endpoint's posture): the briefing is one file per slug,
so for a colliding slug the notes belong to neither crewmate and the read
answers 409 `briefing_slug_ambiguous`, which the panel renders as a plain
sentence naming the fix (rename one of the two); 400 `member_slug_mismatch` and
404 `member_not_found` cover the other two mismatches. `redacted` says a
placeholder replaced a secret in the text; `truncated` (the second value of
`cap_member_briefing`, not a marker-text check) says the marker stands in for
the tail. The panel says either in a visible line above the notes — a
placeholder with no reason reads as the crewmate's own words, and a tooltip
reaches neither keyboard nor touch. A successful read leaves a SEL row
(`members.briefing.read`, outcome `allowed`, `slug=<slug>`), the rules read's
posture: who read a crewmate's private notes is as much a fact of record as
who was refused.

**Work log** is the live status line, the today / 7-day counters and recent
activity from `/activity`, the sessions the crewmate is driving, the
auto-patrol status, and the DM thread's own Crew Log record under the heading
"This conversation" — named for the thread, so it is not read as one of the
driven sessions listed above it.

**Dashboard** is the crewmate's published webview
(`GET /api/members/{slug}/panel`).

Settings content — the built-from template, wake sources and schedules, the
memory binding, cloud — lives only on the crew editor / detail page.
Operator-facing memory diagnostics never render in the panel.

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

**Bind** (`crew` names a roster entry):

```json
{"crew": "oncall",
 "bound": {"kiro_agent": "oncall-agent", "workspace": "/…/oncall",
           "memory_store": "oncall-mem", "model": ""}}
```

An unknown name answers `{"error": "unknown crew '…'", "available": "…"}`. The
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
| `test/test_agent_execution_catalog.py` | Read-only catalog, same-name member/template choices, requesting-project isolation, private-template exclusion and explicit discovery failure |
| `test/test_agent_templates_endpoint.py` | Templates roster marks editability (a row with no spec file beneath the agents directory — empty, foreign or absent `filename` — is read-only for the runtime's reason) and references (crews, default, schedules by what they dispatch — sequence over dormant `agent_id`, the captured execution's template over a stale or empty `agent_id`, script jobs over neither — chat-folder pins, webhook pins, private copies) and masks package-controlled strings like the sibling rosters (the delete refusal's references too); a row whose filename is absolute, traversing or nested names nothing to delete (404, file intact); create writes a minimal runnable spec or a lineage-free copy (re-read inside the spec lock, where the source name is re-resolved and must reach exactly the probed file — a second claimant or a replacement refuses, nothing written) and refuses taken, bound (in the base or only in the overlay), reserved, ambiguous and malformed names; delete refuses read-only and referenced templates (listing the references), a name two files reach — a crossover or a same-name twin the roster would collapse (neither unlinked), a row whose file does not answer to the requested name, a second claimant that lands after the probe (ambiguity re-checked under the lock) and a row that calls a package file plain (the file re-read and classified under the lock), checks and unlinks inside one folder-store hold rather than from a snapshot, counts a binding that lives only in `config.local.json`, does not count a template that merely shares the default crew's alias, holds the schedule store's own lock from the reference walk through the rename (probed on both sides) and answers 503 `schedule_store_busy` with the file intact when another holder keeps it past the bounded wait, names a schedule written past the lock (warning + SEL row), fails closed on an unreadable cron store before the unlink (503, file intact) and only warns after it, retires the file as a one-deep tombstone (renamed before the older grave goes, so a refused rename keeps both; same-second graves stay distinct; the sweep spares a live template whose name looks like a grave), runs both mutations through the drained seam, and removes an unreferenced one; create re-scans by declared name under the lock; a successful create and delete emit operation-labelled SEL events; a create publishes its name to the dispatch snapshot before scheduling the rescan and a delete awaits the rescan before answering (a refusal touches neither); the detail PATCH writes the definition keys on an owned template, refuses them on a package one, refuses every key on an ambiguous name (neither file touched) and a claimant landing after the scan (re-checked under the write lock), classifies the targeted file rather than its name, and validates their shape |
| `website/src/test/AgentTemplatesTab.test.tsx` | Grouping by origin, the two-control action row with its overflow menu (enroll hint, Delete vs Duplicate-to-edit by editability), the definition save through the detail PATCH (changed keys only — a prompt-only save never resends the model), the dirty-draft guard on row switch, on a background refetch, on Discard (asks; declined keeps the draft) and on New template (a create never inherits the previous draft), a saved skill list written into the detail cache before the refetch lands, the saved confirmation in the bar's slot and the visible Add tool label, a delete naming the deleted template over the next row and, on a narrow viewport, returning to the list, every in-app link routed through the shell's leave gate with its target, `beforeunload` armed only while dirty, a rejected detail read rendering its error rather than Loading, a refused save reported inside the save bar beside Save and cleared by Discard, resources as plain rows, string-only MCP fields from a hand-edited spec, one Skills heading, the referenced-delete dialog (opened directly from the row's own holders with no confirm or request, and from the server's refusal when a holder landed later; including a chat-folder row and a private-copy row that links to its crew), a private copy's Open crewmate, the usage line naming folder and webhook holders with each holder linked to where it is held, blank vs `from` create with the created row selected after the roster refetch, and chat-with in the template namespace (enabled while dirty, behind the discard confirm) |
| `website/src/components/RestartButton.cov80.test.tsx` | Apply & Restart asks first, naming what stays (chats and history) and what stops (a reply in progress); declined does nothing, and the confirmed paths (success, failure, in-flight, MCP reconcile) run with the ask answered yes |
| `test/test_chat_agent_kind.py` | `agent_kind` on slot create and switch: template picks skip the member store pin, an unresolvable stated kind is `409 agent_choice_unavailable` refused before any slot is minted, an unknown kind is `400 invalid_agent_kind`, a member thread refuses the same-name template kind, the slot projection carries the committed kind |
| `test/test_open_slots_persistence.py` (`test_restore_carries_the_agent_selection_namespace`) | A template-picked slot restores as a template pick; an unknown persisted kind reads as name-only |
| `test/test_select_crew.py` | Roster excludes the default crew and every triggerless crew, carries `default_agent` plus guidance; a named crew returns its bindings; an unknown name returns `error` plus `available`; the schema accepts spaces and dots in a crew name |
| `test/test_crew_reasoning_effort.py` | Per-crew effort reaches a crew dispatch |
| `test/test_members.py`, `test/test_members_dm_thread.py` | Slug validation and containment, activity recording and dedupe, DM-binding canonicality, rules and briefing reads, briefing endpoint |
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
