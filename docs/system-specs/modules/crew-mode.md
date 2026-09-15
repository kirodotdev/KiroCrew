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
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, `POST /api/members`, thread get-or-create, rules, activity, briefing, the hire gallery's catalog |
| `src/kiro_crew/member_gallery.py` | The hire gallery's catalog: every template a member can be hired from, from an app's cards, the shipped built-ins and the user's own agent files, in one shape |
| `src/kiro_crew/agent_discovery.py` | `list_agents` and the provenance of each agent file (`source`: `kirocrew` / `builtin` / `app` / `package` / `local`) |
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

`source.kind` must be a string in `_HIRE_SOURCE_KINDS` -- `local` or `store` (an
unhashable value is a 400 `unsupported_source_kind`, never a `TypeError`);
`source.agent` must be in the template-name grammar (`invalid_source_agent`) -- the
source is a FILE the installed listing offers, dots included (`reviewer.v2`), and the
row is bound to the copy, whose stem the copy writer mints from the member id, so the
binding stays inside the agent-name grammar; the optional text fields (`workspace`,
`description`, `triggers`, `session_color`) must be strings when present (400
`invalid_<field>`, before anything is written -- the create core stores them as given,
and a list there is a member that persists and then raises when its thread opens or
`route_crew` reads it).
A display name whose minted id would share another member's SLUG is 409
`slug_collision` before anything is written (`_slug_collision_refusal`, the create's
`admit` hook: it runs INSIDE the config-lock hold against the snapshot the row is
published from, with the id the create minted, and AGAIN inside
`persist_member_config`'s cross-process locked mutation against the document's
`agents` map as it is on disk at the write (`MemberAdmissionRefused` carries the
hook's own code back; nothing is written) -- the in-process lock does not hold a
second gateway or the CLI, and two processes' pre-lock checks could both pass for
`Triage` and `triage` and then serialize into two rows on one slug): the slug keys
`members/<slug>/`, the rules file and the DM binding, and it is lossy (`Foo` and
`foo` share one), so two members on it would inherit each other's briefing and be
refused their thread and rules as a collision -- the hire is where that is still
sayable.
Source kind **`store`** hires from a template an installed app offers in its
manifest's `crew.templates` (`app-kit-platform.md` 3.1): `source: {kind: "store",
app, agent}` where `agent` is the manifest `agents` path the card names. The whole
store hire runs under the app's lifecycle lock (`apps.manager.app_lifecycle_lock`,
the one install/update/uninstall take): an update of the app between resolving the
listing and copying its materialized agent would copy the new bytes while recording
the old version and spec as the member's pristine BASE. Inside it,
`member_templates.resolve_store_template` resolves the listing BEFORE anything is
written: the app must be installed (404 `app_not_installed`) and enabled (409
`app_disabled` -- only an enabled app has its agents materialized), the app must
pass **admission again** against the manifest on disk now (409
`app_admission_denied`: install, update and enable each ran it, but the card's
role, triggers and briefing become prompt-adjacent text on the member and the
manifest they come from is the app's to rewrite after enable -- a signed manifest
edited afterwards no longer carries a valid signature; shipped builtins are exempt
exactly as enable exempts them, decided by the IMMUTABLE package tree
(`bridges.shipped_builtin_app_root`, the same provenance the resource registration
reads), never by `installed.json`'s `origin` -- that file is the app's own to
rewrite, so a forged `origin: builtin` buys no exemption, and a builtin's manifest
and card files are read from its package root, not the data-home copy (pinned:
`test_admission_is_re_run_against_the_manifest_on_disk_at_hire`); the policy is read ONCE per hire and that snapshot decides both the deny and whether the card must pin its files (`require_signature`), so a policy edited between two reads cannot admit the hire under one policy and verify its content under another -- pinned: `test_the_admission_policy_is_read_once_per_hire`), the manifest
must carry a card for that agent (404 `template_not_offered`), the `crew` section
must still validate against the tree as it is NOW -- the same checks install ran,
against the same root, plus canonical containment of the card's `agent` and
`initial_briefing` paths (409 `template_invalid`; the manifest install validated is
not the manifest on disk after the app rewrote it), the card's **content digests**
must hold (`_verify_card_content`: a declared `digests.agent` / `digests.initial_briefing`
is checked against the bytes on disk NOW through `pinned_fs.read_bytes_pinned` -- a
mismatch is 409 `template_tampered`, nothing written -- and a fleet whose admission
policy `require_signature`s refuses a card that does not pin EVERY file it points at
(the agent, and the briefing when one is named), 409 `template_unverified`: the
signature covers the card, and only the card's digests carry it to the agent file and
the briefing, so for that fleet a signed posting with an unpinned file is not the
verification it asks for; an open fleet hires an undigested card as before), the
shipped spec must read (409 `template_spec_unreadable`), the materialized
`<app>--<agent name>` file must exist (409 `template_not_materialized`) and, for a
pinned card or a signature fleet, must be EXACTLY the bridge's rendering of the
verified shipped definition now (`_verify_materialized_matches` against
`bridges.render_app_agent_spec(keep_user_edits=False)` -- own servers, managed refs,
the per-app MCP policy and prompt, compared as parsed JSON; 409 `template_tampered`
otherwise): the digests speak for the shipped bytes, but the hire COPIES the
materialized file, which lives in the agent-writable agents directory, so a hand
edit to a pinned app's shared template or an agent's rewrite of it is refused
rather than copied as the publisher's (customize the member's own copy instead).
For that same pinned card or signature fleet a **file-backed system prompt**
(`prompt: file://...`, the bridge's per-app prompt seam) is authenticated like the
agent file and the briefing (`_authenticate_runtime_prompt`): one that resolves into
the app's mutable data dir or outside its packaged root is 409
`template_prompt_unverified` (the render matches on both sides there while the
shipped digest says nothing about the instructions the member would run); one inside
the root is not authenticated by containment either (the tree is the app's to rewrite
after signing), so a signature fleet refuses a card that pins no `digests.prompt` for
it (409 `template_unverified`), a declared `digests.prompt` is checked against the
file's bytes read through the pinned root descriptor (409 `template_tampered`), and
the VERIFIED text is INLINED into the dict the hire copies and the pristine copy
records -- from the hire on the member's instructions are its own bytes, and a later
rewrite of the app's prompt file reaches nothing the member runs (pinned:
`test_a_pinned_hire_refuses_a_prompt_the_app_renders_at_runtime`). An open fleet
hires an undigested in-root prompt as before, URI and all.
**Shared template files are read-only to members** and carry a **recorded
fingerprint**: the materialized `<app>--<agent>` file (a builtin app's included)
sits in the kiro agents tree, which the agent file-edit gate refuses to write and
the OS sandbox seals read-only as a directory (`sandbox._resolved_kiro_agents_targets`),
so a member's tools cannot rewrite the definition its siblings are still hired
from; and when the bridge materializes the file it records `sha256:<hex>` of the
bytes it wrote in the agent-state sidecar (`agent_state.set_shared_template_digest`,
itself sealed) -- the on-disk read the render preserves user edits from, the write
and the record held as ONE critical section under `agents_spec_lock`, the lock every
other template-spec writer takes, so an editor rewrite cannot land between the
bridge's write and its record and leave the edited bytes under the bridge's digest
(pinned: `test_the_write_and_its_record_happen_under_the_template_spec_lock`) --,
every trusted rewrite of that file re-records it
(`agent._atomic_json_write`, the funnel the editor's PATCH, a model reset and the
spec migration all use -- file and record as ONE transaction: the record is read
strictly before a byte lands (an unreadable sidecar refuses the write), and when the
record cannot be written after the replace the previous bytes are put back, so what
is on disk still matches the recorded digest and the template stays spawnable while
the caller sees its edit fail; only a rollback that itself fails leaves the two apart,
and that is logged -- pinned: `test_a_rewrite_whose_record_cannot_follow_is_rolled_back`),
deregistration forgets it, and the KAS projection
(`acp/kas_agents.load_agent_spec`) refuses to inject a fingerprinted file whose
bytes no longer match -- log + security event `shared_template_fingerprint_mismatch`,
fail closed with the repair named (re-enable the app or restart the gateway to
re-materialize), and an unreadable record is a refusal too -- for the ids the record
can fingerprint, the bridge's `<app>--<agent>` shape (`_recorded_fingerprint`, read once
per projection): "cannot verify" is not "not fingerprinted" there, while for every
other id the record holds nothing, so a corrupt sidecar is logged and the spec loads as
the plain file it is rather than stopping every session on the host; a file no bridge
materialized (a hand-written spec, a member's own copy) has no record and loads as
before. The declared-name scan is checked the same way: an app's agent is
materialized as `<app>--<agent>.json` and DISPATCHED by its declared bare name, so
the normal path to it is the scan, and the spec the scan finds is verified against
the fingerprint recorded for the FILE it came from -- the bytes the guarded read
parsed (`agent_discovery.spec_by_declared_name_with_source`), never a reopen --
so a rewrite refused under `pack--triage` is refused under `triage` too (pinned:
`test_a_tampered_file_found_by_its_declared_name_is_refused`). The boot reconcile's
prune of an agent the manifest no longer declares clears its fingerprint with the
file, as deregistration does, so a stem freed by the manifest is not refused when
it is legitimately re-occupied (pinned: `test_stale_app_agent_and_server_are_pruned`).
Pinned in `test/test_app_bridges.py::TestSharedTemplateFingerprint`,
`test/test_kas_agents.py::TestRecordedFingerprint` and the store hire's
`test_shared_template_files_are_read_only_to_member_agents`.
Every app-tree read is anchored to ONE descriptor of the app root, pinned right after validation (`pinned_fs.open_dir_pinned`; `read_bytes_at` walks the card's relative paths component by component with `O_NOFOLLOW` from it): validation resolved the tree by path, and a by-path read afterwards re-resolves it, so an `agents` directory swapped for a link to a credential directory between the two would be pinned AS the parent and its files hired -- and copied into the pristine copy -- as the template's (pinned: `test_a_tree_swapped_between_validation_and_the_read_is_not_followed`). There is NO by-path fallback: a platform that cannot open relative to a directory descriptor with `O_NOFOLLOW` (Windows; `pinned_fs.supports_pinned_walk()` false) has no read that closes that window, so `resolve_store_template` REFUSES the hire there -- 409 `template_read_unpinned`, nothing minted, copied or enrolled, and the gallery lists the card as unhireable with that code -- the same fail-closed rule the member briefing applies (`members.member_briefing_supported`; pinned: `test_without_a_pinned_walk_the_store_hire_fails_closed`). Everything works from ONE snapshot: `_verify_card_content` returns the verified bytes
and the shipped spec and the briefing are parsed from them (never a second read of
the path); the render check starts from those same bytes (`render_app_agent_spec(
shipped_text=)`, a pure `_render_shipped_text` shared with registration); the
materialized file is read once into `StoreTemplate.materialized_spec` and the create
copies THAT dict (`_create_crew(copy_spec=)` → `_write_private_copy(source_spec=)`)
rather than re-reading the source -- a file swapped between the checks and the copy is
not what gets hired. The shipped spec and the initial briefing
are read through `pinned_fs.read_file_pinned` (ancestors pinned, a non-regular
final component refused): the app's tree is the app's to change after install, and
a by-name read there is a disclosure primitive pointed at whatever the name
resolves to. The materialized name becomes the `agent` the local steps below run
against, and the card's `role` and `triggers` fill the create body where the caller
sent NO such key (the card is the default; the caller's word wins, and an explicit
empty string is a word -- "no triggers" -- not an absence). The route
composes the create core, owner-gated once at the top, and is **atomic** -- the
member either exists with its own copy of the source or does not exist -- and **at
no moment is a row bound to the SHARED source readable**: the copy is made inside
the create's config-lock hold, before the row exists, and the row is published
already bound to it. The other order (publish, then fork and rebind) left a
source-bound row on disk between the two; a concurrent thread open in that gap
resolves the source binding and runs a session that keeps using the shared template
after the hire completed, which is the exact hazard the copy exists to remove.
Steps 2 onward run as ONE transaction under `chat_utils.drained` (the coroutine
twin of `drained_to_thread`): a cancellation of the request mid-way -- a gateway
shutdown, a client that closed the connection -- is absorbed until the transaction
reaches its own end (success or roll-back) and re-raised afterwards, so a copy is
never left without its row and a linked member never without its link:

| Step | Core | On failure |
|---|---|---|
| 1. resolve the source | `_load_template_specs` | 404 `template_not_found` / 409 `ambiguous_template_name`; nothing written. The create path tolerates a missing template (a crew may be bound ahead of an install); a hire may not, because its promise is a copy of that file |
| 2. copy-on-hire | inside `_create_crew(copy_source=…)`, after the id is minted, the slug admitted and the source has passed the foreign-private-copy check, under the config FILE lock (`update_config_locked` with a read-only mutate, the cross-process one the fork and publish paths hold for the same reason: a writer in another process cannot bind the destination between the bindings read and the file's first byte) and, inside it, the spec lock: `_write_private_copy` (the one writer of a private copy, shared with the editor's first-edit fork) copies the source into a member-owned file whose stem derives from the member id, re-reading the source in-lock, reserving every current binding and the boot-rebuilt stems, suffixing past collisions and reserved basenames, and records lineage in the `agent_state` sidecar in the same hold | 404 `template_not_found` (the source vanished between 1 and 2), 409 `ambiguous_template_name`, 500 `bookkeeping_failed` / `fork_failed`; nothing published |
| 3. publish the wrapper row | the rest of `_create_crew` (the body of `POST /api/agents`: private memory provisioned, the row persisted) with `kiro_agent` = the copy. The publication's `admit` hook (`_admit_doc`, every create -- plain and hire) re-runs the LINEAGE check inside `persist_member_config`'s cross-process locked mutation, against the sidecar and the document as they are at the write: the pre-lock check holds only the in-process lock, so a hire in another gateway can copy a template to exactly the name a plain create validated as "missing, tolerated" and record its member as the owner in between, and a row published anyway would bind a second member to one private definition; a foreign owner is 409 `foreign_private_copy`, an unreadable sidecar 409 `lineage_unverifiable`, and a row another process already bound to the private copy this member is about to bind is 409 `foreign_private_copy` as well (the sidecar names one owner; a row is the other half of the same fact). Pinned: `test_a_copy_recorded_between_the_lineage_check_and_the_write_is_refused`, `test_a_row_another_process_bound_to_the_copy_refuses_the_hire`. Then, outside the lock, the fork governance refresh (`_refresh_forked_templates`) re-runs exactly as the fork endpoint runs it after its rebind, so a pass that interleaved between the copy's lineage record and the row's persist -- and recorded the copy as an uncorroborated fork -- cannot leave the new member blocked at the spawn gate | its own 4xx/409, verbatim -- and the copy is **unwound** (`_unwind_private_copy`: file and lineage, unless a row already took the name -- reference check and unlink one critical section under the config lock, so a binder in another process cannot land between them), so a retry does not find a stranded file claiming the id |

A store hire has a **step 4**, inside the same atom and under the config lock the
create just released -- and the CROSS-PROCESS config file lock is held across the
guarded row update AND both file publications (`update_config_locked(after_write=)`:
the files are written after the row's rename landed, inside the same hold), so
every locked writer in any process (a delete, a rebind, a same-id recreate by a
second gateway) waits and the row the guard checked is the row the files are
published for -- the files are slug-keyed and the slug is lossy, so published
outside the hold a replacement member would receive this hire's base and briefing
as its own; a rename that fails writes neither file and the hire rolls back:
`_link_member_to_template`
records `template` (`<app>/<agent name>`) and `template_version` (the app's
manifest version) on the row in a locked read-modify-write that requires the row
to still carry this hire's generation and copy, writes the **pristine copy**
`<data home>/member-templates/<member id>.json` (`member_templates.write_pristine_copy`:
the agent definition as MATERIALIZED -- the `<app>--<agent>` file the hire copies,
the app bridge's own servers and managed refs included, never the raw shipped spec,
so BASE and copy are one and the same at the hire even when an unsigned app's
shipped file was rewritten without its materialized file being re-rendered, and a
later merge does not read the bridge's plumbing as the member's customization --
plus the card's `role`/`triggers` at that version, stamped with the member id and
the private-store generation -- the BASE a later role update three-way merges
against; written through `update_config_locked(after_write=)`, i.e. AFTER the row's
rename landed and INSIDE the same lock hold, so a rename that fails leaves no base
claiming a provenance no row got and a same-id fire + rehire cannot slip between the
row and its base; keyed by the immutable id in a gateway-only TOP-LEVEL leaf of the
data home -- on `sandbox._CREW_HIDDEN_LEAVES` (bind-masked from every sandboxed
process; `trust/`, where it once lived, is sandbox-VISIBLE because the SEL key and
log have in-sandbox readers, so a spawned interpreter's `open()` could rewrite a base
there past the file-tool gate), on `_CREW_PRECREATE_HIDDEN_DIR_LEAVES` (materialized
owner-only before each spawn, since the root is otherwise built by the first store
hire and the mask loop is guarded on `isdir`) and on `security._CREW_SECRET_LEAVES`
(the agent file tools refuse it on every OS); NOT in `members/<slug>/`: that
directory is agent-writable and its slug is lossy, so a BASE kept there could be
forged by an agent or shared by two members, and a forged or shared BASE makes the
merge skip a template change or overwrite a customization silently; published
through `pinned_fs.write_file_pinned`), and writes `members/<slug>/briefing.md` from the card's
`initial_briefing` (`seed_briefing`: the member did not exist until this locked
publication and no row held its slug -- the slug admission refuses a collision -- so
nothing at that name is the member's lived state: the member directory is
agent-writable and the slug a deterministic derivation of the display name, and a
file or link planted there ahead of the hire would otherwise become the new member's
`[CURRENT ASSIGNMENT]`; a pre-existing regular file or link is removed by its literal
name relative to the pinned directory (a link as a link, its target untouched),
recorded in the security event log (`member_briefing_preexisting_replaced`), and
the template's briefing is created in its place through an exclusive
`O_CREAT|O_EXCL|O_NOFOLLOW` create -- never a follow, never a truncation of a
target; any other kind of entry, or a name taken again after the removal, fails the
hire closed; every byte is written and a write that fails midway removes the file;
a platform where briefings are not read gets none; from the hire on the file is the
member's lived state and no template operation touches it -- pinned:
`test_a_briefing_planted_before_the_hire_is_replaced_not_adopted`,
`test_a_store_hire_replaces_a_briefing_planted_at_its_slug`), and LAST -- in the
same hold, after both files landed -- **enrolls** the member (`enroll(id,
generation)` -> `agent_state.set_crewmate_record`). A store hire's row is therefore
NOT a crewmate between the create and the end of step 4: no enrollment record, so
`GET /api/members` does not list it and the member routes (thread, rules) answer
404 for it -- nobody can open the thread of a row this step may still roll back.
A failure anywhere in the step first removes the files the step had written so
far -- the pristine copy and the seeded briefing, through the pinned member
directory (`remove_step_four_files`), still inside the hold while the row is
provably this hire's -- since left behind, the next member to take the slug (a
local hire of the same name) would inherit the failed hire's base and briefing as
its own; then the caller rolls the hire back (`_roll_back_hire`: the delete
route's own mutation through `_delete_crew_record` -- row removed, a private V2
store archived under the retirement marker, cached handles released -- but ONLY
while the row is still the one this hire made, bound to its copy and carrying the
private store name the create minted -- checked on the parsed config under the
in-process lock and AGAIN inside the delete's cross-process locked read-modify-write
(`_delete_crew_record(expect=)`, against the document on disk at the write), since a
writer in another process is not held by the in-process lock and a row it replaced
in the gap is not this hire's to delete; then the copy itself through
`_remove_private_copy`, inside one cross-process locked read-only mutation of the
config -- the "no row is bound to the copy" check reads the document under that lock
and the unlink happens in the same hold, so a same-id recreate in another process
(which publishes under the same lock) is either seen or waits, and its agent file is
never deleted under it -- only while the sidecar still names this member as the
copy's owner) and answers 500 `template_link_failed` with
`rolled_back: true`; a roll-back that is refused or fails answers 500
`hire_incomplete` naming the member `id`. A roll-back also REFUSES a row that is
enrolled with this hire's generation: enrollment is step 4's last act, so an
enrolled row is a completed hire -- a crewmate someone may already be talking to
-- and no roll-back removes a crewmate (pinned:
`test_a_failed_template_link_rolls_the_hire_back` observes the un-enrolled row at
the start of step 4; `test_a_failed_enrollment_takes_step_fours_files_with_the_row`;
`test_a_roll_back_never_removes_an_enrolled_crewmate`).

The create body is this package's own contract (pinned by its tests); the hire
reads it strictly -- a missing `name`, `kiro_agent`, `memory_store` or
`display_name` is a 500 `hire_incomplete`, never a guessed default. The answer
carries `kiro_agent` (the copy's name) beside the id for exactly this reader.
Why a server verb rather than the two client-reachable calls: a client that dies
between create and fork leaves a member bound to the SHARED source it was told
it owns -- the exact hazard copy-on-hire removes -- and only the server can roll
the first half back. Two members hired from one file therefore coexist, each with
its own copy and row; a second hire whose display name mints a taken id is a 409
`agent_exists` (the message names the typed name and the id).

Success: `{"ok": true, "id"}` -- the minted id (what `/members?member=` resolves);
the copy the member is bound to and what the caller sent (label, role, source) are
read back from the roster row, not echoed. The `GET /api/members` row carries `template` and
`template_version` (store provenance, `""` for a hand-made or locally adopted
member; the drawer's Source row reads `Template <app>/<agent> (v<version>)`) and
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
and the enrollment record stay. The UI's hire entry points in this step: the Crew
page's **Add a Crewmate** (the empty roster's call to action and the header `+`)
opens the crew form in the roster's words ("Add a Crewmate", "What this
crewmate uses", **Hire crewmate**), and that form -- template picked, name typed,
Hire pressed -- posts to `POST /api/members` (`KiroCrewAgentsPage` `createMut`,
`fromMembers`), never the plain create, so the roster it returns to lists what was
just added; and the **hire gallery** under the Crew page (Your Crewmates) (step 6,
`/members/hire`) -- it lists every enabled installed app's cards
(`crew.templates`, with the card's duty, tags, category, starter prompts and ghost
face) beside the built-in and local agent files; selecting a card opens the name
entry, and only a confirmed name hires (a store card's hire sends `source: {kind:
"store", app, agent}` with the typed `display_name`). The crew manager's own
button and sheet are **Add an agent** -- its page's vocabulary, never "Add a
Crewmate", which would promise a teammate that never reaches the roster -- and
stay a plain create (bind to a shared template) that enrolls nobody
(pinned: `CrewRoster.test.tsx`, a create via `?new=1&from=members` calls
`hireMember` with the picked template and the typed name and never
`createKirocrewAgent`; the crew manager's own button still creates).
The drawer's Source row for a store-hired member reads `Hired from Oncall pack --
"triage" (v1.2.0)` -- it opens with the verb, not with "Template", because the
Agent template row beneath it already says "template" for the crewmate's own file
-- the app by its display name, from the installed-apps listing read only while a
template-hired member is open; the app id is the fallback when the app is gone,
and a FAILED read is said under the row (`ErrorNotice` with the agent hand-off;
the row still shows the id) rather than passed off as the app having no display
name -- and ONLY there while the crewmate summary is the side panel's shown tab:
the panel-level app-list notice (`SidePanel` `AppPanelTabsErrorNotice`, raw
message, overlaying the identity row) is withheld for the host's leading tab, so
one failure is said once (pinned: `panelTabSessionIdentity.test.ts`). Pinned in
`test/test_member_hire.py` (the gates: two named members from one file coexist,
both enrolled, `default` off the roster; one store template hired twice under two
names coexist, each with the card's defaults, provenance, pristine copy and
seeded briefing; the card's ghost face is the member's face and the hire body
cannot pass one; a hire without a name -- with or without
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
an unhireable listing -- uninstalled, disabled, admission-denied, no card,
unmaterialized, unreadable spec, a card path rewritten to leave the app root after
install -- is refused before anything is written; an explicit empty role or
triggers is kept, not replaced by the card's; a link planted at the member
directory fails the hire with nothing written through it; a briefing seed that
fails midway leaves no file behind; a failed template link rolls the hire back,
copy included, and a failed roll-back names the member; unhashable kinds and
non-string text fields are 400; lineage on the roster),
`test/test_agents_roster_contract.py` (`template` and `template_version` withheld
from the crew manager's roster; `crewmate` is the one handler-added bool) and
`MembersPage.identity.test.tsx` (the Source row by display name; the id with no
notice when the app is gone; an `ErrorNotice` when the apps read fails).

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
- **When it is written.** A file hire: inside the create's publication
  (`_create_crew(enroll=)`): after the private copy and the store are
  provisioned, `set_crewmate_record(id, generation=<the row's store>,
  template=<source>, hired_at=<UTC>)`, then the row persists; a row that fails to
  land clears the record it just wrote, so the two never disagree. A STORE hire:
  as the last act of step 4 (`_link_member_to_template(enroll=)`), after the
  template link, the pristine copy and the briefing landed -- until then the row
  is a session agent, not a crewmate, so a step-4 failure rolls back a row nobody
  could have opened.
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

## Role update and detach (design step 4)

A member hired from a template can take the template's newer version without
losing what it has become, and can leave the template for good. Both verbs live
in `handlers/members.py` and `member_templates.py`; neither touches lived state
(briefing, rules, activity, the DM thread) or the member's id.

**The plan: `GET /api/members/{member}/role-update`.** Owner-gated; nothing is
written. The three sides are the **pristine copy**
`<data home>/member-templates/<member id>.json` the hire recorded (BASE --
`read_pristine_copy`, pinned read, shape-validated, and it must name THIS member id,
THIS row's private-store generation and the row's own `template` -- a copy written
for a same-id member that was deleted and re-hired, or a same-name file on a
case-insensitive filesystem, is not this member's base -- else 409
`pristine_copy_missing`; read under the copy's own cap, `PRISTINE_COPY_MAX_BYTES`,
eight times the spec cap, since the copy wraps the materialized spec in an envelope
and is written pretty-printed with `ensure_ascii`, so a spec near
`TEMPLATE_SPEC_MAX_BYTES` comes back several times larger and a read cap equal to
the spec cap would refuse the very file the hire wrote --
`test_a_pristine_copy_of_a_spec_near_the_cap_still_reads`), the
member's OWN agent file plus its `role` and `triggers` (MINE -- the file is read
through the editor's no-symlink / in-directory fence and must be the copy the
sidecar names as `private_to` this member, else 409 `not_private_copy` -- a binding that is not a string or is outside the agent-name grammar (`kiro_agent: []` in a hand-edited config) is the same 409 from the plan and the apply (detach never reads the binding), never a sidecar lookup on an unhashable key (pinned: `test_a_malformed_binding_is_not_private_copy_not_a_500`): a row
hand-pointed at the app's shared materialized file would otherwise have the update
rewrite a file every member of that template shares), and the template as
MATERIALIZED now -- the `<app>--<agent>` file the bridge writes, the same form the
hire copied and the pristine copy recorded, so the bridge's own servers and managed
refs read as `unchanged`, never as the member's edit (THEIRS -- `resolve_template_ref`
walks the app's cards to the one whose agent declares the row's recorded name and
resolves it with every check the hire runs, so a disabled, uninstalled, tampered or
unmaterialized template answers with the hire's own codes). A member with no `template` is 409 `not_linked`.

`plan_role_update` compares field by field. The fields are the union of the agent
definition's keys on the three sides -- except `name`, which is the member's id on
one side and the template's on the other and nobody's customization -- plus the
card's `role` and `triggers`; a side that lacks a key is `MISSING`, distinct from a
key set to JSON null. Equality is whole-value JSON equality (`member_templates.json_equal`:
same type and same value through every level -- never Python's `==`, under which
`true == 1` and `1 == 1.0`, so a member's `strict: true` -> `1` would read as
unchanged and a template flipping the flag would be applied over the edit with no
conflict; iterative on an explicit work stack, not recursive, since `json.loads`
accepts nesting far past the recursion limit and a recursive compare would turn a
legal file into a 500; pinned:
`test_a_type_change_is_a_change_json_equality_not_pythons`,
`test_deeply_nested_values_compare_without_recursing`): a tool list that gained
one entry is one changed field, because a definition is what its author reviewed as
a whole.
Each field is `unchanged`, `apply` (only THEIRS changed), `keep` (only MINE
changed), `agree` (both changed to the same value) or `conflict` (both changed
apart -- the user picks). The response carries every field with its three values --
each run, string leaf by string leaf and key by key with shapes preserved, through
the credential / exfiltration-URL redactors before it leaves (`_redact_plan_leaves`:
agent-file fields -- an `mcpServers` env, a URL with a secret in its query, a
prompt -- are hand-editable, agent-writable or a third party's text, and the plan
would otherwise hand a token to the browser verbatim; the fingerprints below are
over the REAL values, so a redacted echo never changes what an apply is checked
against -- pinned: `test_the_plan_echoes_no_credential_from_either_side`; the row's
`template` ref and an unavailable template's refusal text, both template-author-typed,
ship through the same redactor -- `test_the_plan_redacts_a_credential_shaped_template_ref`) -- and
`update_available` -- true when any field is `apply` or `conflict`, when the
installed version differs from the row's `template_version` (a version-only move
still advances the record), or when the pristine copy's version is behind the
row's (a pristine write that failed after the row advanced: applying is what
rewrites the base, and a stale base would turn the next template change into false
conflicts). It also carries `member_fingerprint`, a digest of MINE as the plan saw
it (the definition plus the row's `role` and `triggers`, canonical JSON, sha256
truncated) and `template_fingerprint`, the same digest of THEIRS (the materialized
definition, the card's role and triggers, the version). Update is offered, never
automatic: nothing in the gateway applies a plan by itself. A stored ref that two
cards' agents both answer to is 409 `template_ambiguous`, never resolved by card
order (the manifest validator refuses such an app at install and re-validation; the
ref is a stored string, so the resolver checks too), and an app's own verdicts --
disabled, admission-denied, an invalid crew section -- surface as themselves from
whichever card raised them, never as `template_not_offered`.

**Applying: `POST /api/members/{member}/role-update`** with
`{"resolutions": {"<field>": "mine" | "theirs"}, "expected_version": "<v>",
"member_fingerprint": "<from the plan>", "template_fingerprint": "<from the plan>"}`.
The plan is re-derived under the app's lifecycle lock (the template cannot move
mid-merge) and the config lock, and drained like a hire. All three anchors are
REQUIRED: an apply is a decision about a plan the user reviewed, and the plan names
the version it was made against, the template body it read and the member it was
made about. `expected_version`
(400 `invalid_expected_version` when absent; without it the route would apply a
THEIRS nobody looked at) must equal the
installed version (409 `template_changed`, naming the version now installed);
`template_fingerprint` (400 `invalid_template_fingerprint` when absent) must equal
the re-derived plan's digest of THEIRS (409 `template_changed` too: the version alone
does not pin the bytes an app materialized under it -- a shipped file rewritten or
re-plumbed under the same version would otherwise merge a body nobody reviewed);
`member_fingerprint` (400 `invalid_member_fingerprint` when absent) must equal the
re-derived plan's digest of MINE (409 `member_changed_since_plan`: a prompt rewritten
in the crew editor or a role renamed between review and apply would otherwise let a
stale `theirs` choice overwrite the newer customization; the panel re-reads the plan
and the user reviews again); every
`conflict` needs a resolution, keyed by the field's opaque **`id`** (`field_id`:
`f` + 12 hex of the field name's SHA-256, set on every plan field after the
redaction pass) -- never by the field NAME, which is member- or template-authored
text (a hand-edited agent file can carry a credential-shaped top-level key) and so
ships as a redacted label the apply could not match back; the refusal (409
`unresolved_conflicts`) names the ids and the redacted labels, never the raw names
-- a partial resolution never half-applies; a resolution for a field not in
conflict is ignored, the plan decides what is in conflict (pinned:
`test_the_plan_redacts_a_credential_shaped_version_and_resolves_conflicts_by_id`,
which also pins `member_version` -- the row's `template_version` -- through the
redactor). `merge_role_update`
takes THEIRS for `apply` and for a conflict resolved `theirs`, keeps MINE
otherwise, and removes a key the template removed when that applies; the member's
declared `name` is written back unchanged. Everything below happens INSIDE one
`update_config_locked` mutation -- config file lock outer, spec lock inner, the
nesting every other writer of both uses -- and NOTHING is written until every
re-check has passed: (1) the row's binding and private-store generation must be
unchanged, and so must its lineage -- `template` AND `template_version` (409
`member_changed`: a detach that committed after the plan strips the ref; an apply of
this same plan that committed in another process, keeping MINE on a conflict,
advances the version alone, and a re-check on the ref alone would let a second apply
write THEIRS over the choice the first one committed --
`test_a_second_apply_of_one_plan_does_not_overwrite_the_first_ones_choice`) AND its
`role` / `triggers` still the card half of
MINE the plan digested (409 `member_changed_since_plan`: a card edit that landed in
another process after the plan is a customization this merge never saw, and the
binding alone would not see it); (2) inside the spec lock, the copy's lineage is
re-checked (409 `not_private_copy`) and the FILE is re-read and re-digested and
must still match `member_fingerprint` (409 `member_changed_since_plan`: the plan
read the file before any lock, and the fork refresh or the agent editor -- every
other writer of it takes this lock -- may have landed in between; a whole-file
write of the older merge would revert their write); only then is the member's
agent file written -- through **`agent.write_agent_definition`**, the ONE writer
of a whole agent definition a merge or a patch produced, shared with the agent
editor's PATCH overwrite (the merged definition passes
`sanitize_agent_config_governance`, the same whole-config funnel every spec writer
runs before persisting, so a grant governance withholds cannot arrive through a
template; then an atomic replace whose shared-template fingerprint follows the
bytes); the caller owns the locks and the identity checks, the function owns
governance and atomicity (pinned:
`test_the_members_definition_is_written_through_the_one_member_writer`,
`test_patch_write_is_governance_sanitized`) -- and the row's `role`, `triggers`
and `template_version` updated in the same document. The file is written AFTER the card check on purpose:
written before it, a stale card's 409 would leave the template's spec on disk over
the member's customization with the row and base still behind it -- the
customization lost and the next plan reporting nothing to re-apply (pinned:
`test_a_card_edit_landing_inside_the_apply_is_not_overwritten`, the member's own
prompt intact after the refusal); (3) the pristine copy advances to the new version (`write_pristine_copy`) AFTER the
row's rename landed and INSIDE the same lock hold (`update_config_locked(after_write=)`):
after, because the base is the row's provenance made concrete -- a rename that fails
must leave the base where the row still is, or the next plan would merge against a
BASE one version ahead of the row and read the template's own edits as the member's;
inside, so a same-id member replaced afterwards (fire + rehire, which take the same
lock) can never receive a late pristine write stamped with the old generation over
its own base -- and detach removes the pristine copy the same way, after its own
commit and inside its hold. The publish is atomic, so a base write that fails after
the commit keeps the PREVIOUS base: the row is one version ahead of it, the next plan
sees MINE equal to THEIRS on every field the template changed and its apply rewrites
the base (logged, `ok` still answered -- the state self-corrects rather than
refusing). The order is what makes a crash recoverable by the next plan: an applied
file beside an un-advanced pristine copy reads as `agree` and re-applies as a no-op,
whereas the reverse order would read the update as the member's own customization
and keep it forever. The response is `{ok}`, like detach: the panel refetches the roster (the new
`template_version`) and the plan, so the response carries nothing the refetch does
not; the plan already told the user what applied and what was kept.

**Detach: `POST /api/members/{member}/detach`.** Clears `template` and
`template_version` on the row (409 `not_linked` when there is nothing to
clear, so a second detach is a refusal, not a second severing) and removes the
pristine copy (`remove_pristine_copy`: an `unlink` on the id-keyed name in
`member-templates/`, after the row's commit landed and inside the same hold, so a
commit that fails keeps a still-linked row's base). The severing runs under the config lock as a read-modify-write that
requires the row to still carry the store generation and the template the request
validated against (409 `member_changed`): a same-id member replaced in between --
deleted and re-hired, or linked to another template -- is somebody else's, and its
provenance and pristine base are not this request's to remove. The agent file, `role`,
`triggers`, the copy's lineage and every piece of lived state stay as they are;
the roster row then reads as a locally created member with a customized copy.
One-way: re-linking is a hire.

Frontend (`pages/members/RoleUpdatePanel.tsx`, under the drawer's Source row for
a member with a `template`): reads the plan; says *Template up to date (vX)*, or
*<App> vY is available (this member is on vX)* with a **Review update** button,
or why the template is unavailable (no base, not its own copy, app gone or
disabled), and always offers **Detach from <App>** as a two-step inline confirm
that says what stays -- with the cost said BEFORE the click too, in one line under
the button (*Keeps everything; stops following <App>. Cannot be undone -- you'll be
asked to confirm first.*), so the reader need not press it to learn whether it can
be undone nor fear that the first click is the act; the button and the review
dialog's title (*Update role from <App>*) name the app, so "template" in this panel
is only ever the Agent template row's file. While the confirm is open, Review update
stays in place disabled and its title says why (*Finish or cancel the detach
first.*). A detach that succeeds is not silent: the roster refetch clears
`template` and unmounts this panel, so the drawer renders the closure where the
panel stood (*Detached from <App>. This crewmate keeps everything it has and no
longer follows that template.*, `role="status"`) and, for that crewmate, reads the
Agent template row as *<template> -- customized copy (detached from <App>)* so it
does not contradict the Source row's *Created here*; both are the drawer's memory
of the detach it performed (kept per member name until the page is left -- nothing
durable marks a detached row, whose lineage honestly still names the template it
was copied from). The "no base" reason reads in the user's words (*The original
copy of its template is missing, so changes can't be compared. Detach it, or hire
again.*). Pinned: `RoleUpdatePanel.test.tsx`, `MembersPage.identity.test.tsx`
("a detach is not silent"). The review dialog lists
only fields that change or need a decision (`apply`, `keep`, `conflict`;
`unchanged`/`agree` say nothing), shows each side's value, and keeps **Apply vY**
disabled until every conflict has a side chosen (the disabled button's title says
which fields still need one); a `template_changed` or `member_changed_since_plan`
refusal is said in the dialog (an `ErrorNotice` with NO agent hand-off: the dialog
holds the user's unsaved conflict picks, and *Ask the agent* would navigate away and
lose them -- the retry is the same button) and the plan re-read, and the picks made
against the old plan are dropped whenever either fingerprint moves -- a `theirs`
chosen for the old prompt is not carried onto the new one. Both mutations refetch the roster and the plan before their pending
state ends. Pinned in `test/test_member_role_update.py` (**the step-4 gate**:
update a template one member customized -- the member's prompt edit and added
tool survive, the template's new hook and triggers apply, the prompt conflict is
resolved the member's way, version and pristine copy advance, briefing untouched,
a second apply is a no-op; a moved template, a template body rewritten under the
same version, two cards answering one ref (`template_ambiguous`; a banned app
surfaces its own verdict), a member edited or renamed since the plan, bad bodies, an unlinked member, an
unavailable template, a missing or foreign pristine copy (another template, another
member, an earlier generation) and a shared binding are refused before anything is
written; a pristine copy behind the row keeps the update offered until an apply
advances it; the pristine path is id-keyed, validated, and distinct for two ids that
share a slug; the bridge's plumbing reads as unchanged and a
re-plumbed materialized file as the template's change; the governance funnel runs on the merged
definition; a conflict resolved the template's way takes theirs; a renamed role is
kept when the card did not move; detach clears provenance and leaves everything
else, and refuses a member replaced under it) and `RoleUpdatePanel.test.tsx`
(current vs available, Apply gated on conflicts and sent with the plan's version
and fingerprint, `template_changed` and `member_changed_since_plan` said in place,
unavailable reasons, two-step detach).

## Fire (design step 5): `POST /api/members/{member}/fire`

Fire is the reverse of hire, and the design's decision 3 is the archive policy:
**archive the DM thread and activity like a closed session; destroy only on
explicit request.** Body `{"purge": false}` (optional; a non-boolean is 400
`invalid_purge`). Owner-gated; the default member cannot be fired (409
`cannot_fire_default` -- make another member the default first); **only an
enrolled crewmate can be fired**: an unknown id, and a row that is not enrolled
(a built-in, an app's materialized agent, a synced file, a plain crew -- session
agents the crew manager owns), are 404 `member_not_found` and nothing is touched
(an unreadable enrollment record is 503 `members_unavailable`, never "not a
member"), and the delete's own locked mutation re-checks that the record still
names this row's generation -- a member un-enrolled in between by a fire or a
delete elsewhere is left alone (409 `member_changed`; pinned:
`test_only_an_enrolled_crewmate_can_be_fired`,
`test_a_member_un_enrolled_inside_the_window_is_not_fired`) -- unless a fire of
it was interrupted (below). A member that shares its
slug with another LIVE member is refused (409 `slug_collision`, in plain words -- "Nothing was fired. Another member, 'X', has the same short name ('slug') as 'Y', so they share files. Rename or fire that member first: find it under Your Crewmates if it is a crewmate, otherwise under Agent Capabilities › Agents." -- the sentence says where the colliding member lives by the labels the navigation SHOWS (the Crew page's title, the sidebar entry and its tab), since a plain crew is not on the roster the reader is looking at -- never the internal "member space" / "shorten to" vocabulary; the body also carries the fact structured, `collision: {member, slug}`, and `FirePanel` renders it translated (`fire_slug_collision`, 13 catalogs; pinned in `FirePanel.test.tsx`)): `members/<slug>/`,
the rules and the binding are keyed by the lossy slug, so retiring them would take
the colleague's along; that member is renamed or fired first. The whole fire runs
under `_dm_thread_lock`, the lock the thread open (`POST /api/members/{slug}/thread`)
holds from its binding read to its binding write, so an open cannot re-bind the slug
between the fire's binding read and its retirement.

Order, and why: (1) the **thread** is closed first through the tab's own close
path (`chat_handlers.close_slot` on the slot the DM binding names -- only when the
binding names THIS member exactly AND points at this member's canonical slot -- the
key the thread open derives from the slug and the row's CURRENT private-store
generation (`_member_thread_slot`), with the binding's recorded store matching it:
one left by a deleted same-slug member attributes nothing to fire, and so does one a
same-ID predecessor left (delete, hire the same name again, fire): it carries the OLD
generation, and a purge that accepted it would delete the predecessor's transcript as
this member's (pinned: `test_a_replacement_member_cannot_purge_its_predecessors_thread`);
either closes no slot and reports no thread -- the same
sequence the tab's close runs: tombstone, retire the nudge loop, notify the owning
app, persist as closed, tear down the session), so a live session never keeps
running against a row that is about to vanish; its transcript stays in the History
tab under `dashboard:<slot key>`. A close that fails (500 `thread_close_failed`)
leaves the member whole. (2) **Row + store**, under the config lock and only while
the row is still the one the request saw (binding and store generation unchanged,
else 409 `member_changed`): first the fire's **intent marker** is written to
`<data home>/member-fires/<member id>.json` (slug, copy, purge, the `fired.json` record;
a gateway-only TOP-LEVEL leaf -- on `sandbox._CREW_HIDDEN_LEAVES`,
`_CREW_PRECREATE_HIDDEN_DIR_LEAVES` and `security._CREW_SECRET_LEAVES` -- and NOT under
`trust/`, which is sandbox-VISIBLE: the resume trusts the marker's `purge`, `slug` and
history key, and a spawned shell that could rewrite one by `open()` would have the
next fire delete unrelated lived state or another thread's transcript;
500 `fire_not_recorded` when it cannot be, nothing removed), then
`_delete_crew_record`, the delete route's own mutation
-- the row removed, a private V2 store archived under the ordinary retirement
marker (never erased; the memory admin surface restores it), cached handles
released. Steps 3 and 4 then run in the SAME config-lock hold (a hire of the same id
takes that lock to publish, so it cannot interleave and lose the copy, pristine copy
or space it just received to this fire's cleanup); the row removal itself re-checks the
binding and the store generation INSIDE the delete's cross-process locked mutation
(`_delete_crew_record(expect=)`), so a row another process replaced after the locked
re-read is not retired as the fired member, and re-checks the OTHER rows in the same
mutation (`expect_others=`, the document's `agents` map as on disk): the slug check
above ran against a pre-lock read, and a plain create in another process -- whose
admit refuses only a PENDING fire, so it may have run before this fire's marker
landed -- can publish a same-slug row in between, which makes the member space this
fire archives or purges SHARED and not this fire's to remove (409 `slug_collision`,
marker cleared, row kept, the newcomer's lived state untouched); and the hire's `admit` hook -- and the plain
`POST /api/agents` create, through the same `_pending_fire_refusal` -- refuses to publish
under an id whose fire is still pending OR whose SLUG a pending fire is mid-way on (the
lived state is slug-keyed, so a same-slug/different-id replacement would inherit it;
409 `fire_pending`) --
and fails CLOSED on a marker that exists but cannot be read (`FireMarkerUnreadable`:
an unreadable file, junk JSON, no `slug`): only a MISSING marker is "no fire pending",
since an unreadable one still means lived state under that id may be waiting, so the
hire is refused with the same code and a fire of the vanished member answers 500
`fire_marker_unreadable` (fix or remove the record under `member-fires/`) rather
than 404.
The marker itself is written through `pinned_fs.write_file_pinned` and read through
`read_file_pinned` at an UNRESOLVED path under `member-fires/` (the id grammar
admits no traversal): a link planted at the marker's name is refused, never written
through or followed -- a resolved path would have handed the link's target back as the
file to write. From here the fire is **resumable**: steps 3 and 4 are idempotent and run
from the marker (`_finish_fire`, under the config lock; a row under the id at that
point -- a hand-edited config -- is somebody else's member and is refused, 409
`member_changed`, marker kept); a step that fails after the row is gone answers
500 `fire_incomplete` and leaves the marker, never a success
that left a same-name re-hire to inherit stale state, and the next fire of that id
-- which has no row -- resumes the cleanup instead of answering 404; the marker is
cleared only once everything is done -- and DURABLE: the marker is the fire's only
resume anchor, so its unlink must never reach disk ahead of the work it says is done.
Renames and unlinks are directory entries, on disk only once their directory is
synced, and a file's bytes only once the file is; so before the marker goes,
`fired.json` is fsync'd and every directory the retirement touched is synced through
the descriptor the step used (`atomic_write.fsync_dir_fd`: the members root and its
`.retired`, the archive entry, `trust/member-rules` and its `.retired`,
`trust/member-bindings`; `fsync_dir` for the agents directory the private copy left
and the pristine copies' directory) -- a sync the device refuses is an OSError, so the
fire answers `fire_incomplete` with the marker kept rather than clearing it over
unsynced work; the marker directory is synced after the unlink, best-effort (a marker
that survives a crash only re-runs idempotent steps). Pinned:
`test_the_archive_is_durable_before_the_marker_is_cleared`. (3) The member's **own agent file** goes (`_remove_private_copy`, only
while the sidecar names this member as the copy's owner and no row is bound to it
-- the shared template a hire copied from is never touched) and so does its
**pristine copy**. (4) **Lived state** (`members.retire_member_space`): archive
moves `members/<slug>/` (activity, briefing) to `members/.retired/<slug>--<stamp>/`
in one rename and writes `fired.json` there (member id, display name, the thread's
slot and history keys, template and version, when), so the thread History still
lists can be tied back to the colleague it belonged to; the member's rules file
moves to `trust/member-rules/.retired/<slug>--<stamp>.json` -- the same protected
subtree it lived in, never the agent-writable archive; the DM binding is removed
only when it names this member (`remove_dm_binding_of`; one naming another member
of the slug is that member's, an unreadable one attributes nothing and goes) -- a
same-name hire later starts from nothing. `.retired` is outside the slug grammar,
so no member can be named it; a symlink planted at the member directory's name is
removed as a link, never moved or followed -- and on POSIX every step of the move is
relative to **pinned descriptors** (`_retire_dir_pinned` / `_move_member_dir_pinned`:
the members root opened as the LITERAL `members` child of the data home with
`O_NOFOLLOW` -- a link planted at the root itself is refused with `MemberSlugError`,
never resolved, since a resolved root would let a purge delete, and an archive land
in, whatever tree the link names -- the archive root as the literal `.retired` child
of that descriptor, the entry `lstat`-ed, unlinked or renamed relative to them, the
record written through a descriptor of the archive entry), so a directory swapped
for a link between a check and the rename is unlinked, never renamed or written
through; the path flow (Windows) keeps the `lstat` refusals a planted link needs no
race to defeat, and the rules archive root (`retired_rules_root`) is refused the same
way when a link or a plain file sits at `.retired`, re-checked after the `mkdir`. The
`trust/` children the fire touches -- `member-rules` and `member-bindings` -- are
taken LITERALLY (`_trust_child`: a link at `trust` or at the child refuses instead
of resolving; `trust/` is sandbox-writable, so an agent can plant a directory link
there and a rules file for a slug named `config` would otherwise resolve to
`config.json` and be unlinked or replaced). Every literal-path check in the
retirement flow -- the trust children, the members root, `.retired` and its
entries, the member's own name -- asks `platform_compat.is_link_or_junction`, never
`Path.is_symlink` alone: on Windows an agent can plant a directory JUNCTION where it
cannot plant a symlink, `is_symlink` is False for one, and a purge would `rmtree`
through it into the data home; a junction at the member's name is unlinked as a
link (`unlink_link_or_junction`), its target untouched (pinned, the platform probe
made to report the planted names:
`test_a_windows_junction_at_a_trust_child_or_the_members_root_is_refused_too`). On
POSIX the rules step and the
binding removal run pinned (`_retire_rules_pinned`: `trust` and `member-rules`
opened as literal children with `O_NOFOLLOW`, the file `lstat`-ed relative to them,
unlinked or renamed into the pinned `.retired` child with `src_dir_fd`/`dst_dir_fd`;
a LINK at the rules name is unlinked as a link in both flows -- left in place it would
survive the fire and the next member hired on the slug would inherit whatever rules
it resolves to, and the leaf both flows act on is the literal `<slug>.json`, never the
resolved path, which after a planted `<fired>.json -> <victim>.json` would be another
member's rules -- while anything else that is not a regular file (a directory, a
device) is refused so the fire keeps its marker over the obstacle (pinned:
`test_a_link_planted_at_the_rules_name_is_removed_as_a_link`); and when nothing sits at the live name, the entry an interrupted attempt of this fire already archived under the marker's fixed name (`.retired/<name>.json`, opened relative to the pinned directory) counts as retired -- a resume that purges removes it, one that archives keeps it -- instead of being read as "no rules" while the rules sit in the trust subtree (pinned: `test_a_resume_after_the_rules_moved_sees_the_archived_rules`);
`remove_dm_binding_of` `lstat`s and unlinks the LITERAL `<slug>.json` relative to the
pinned `member-bindings` -- never the resolved path's name, which after a planted
`<fired>.json -> <victim>.json` would be the victim's binding; a link at the name is
removed as the link it is, its target untouched, and the file is read `O_NOFOLLOW`
to decide whose binding it is; only ABSENCE is a quiet `False` -- a link or a plain
file at `trust` / `member-bindings`, or a directory or device at the binding's name,
raises `MemberSlugError` in both flows, so the fire keeps its marker and reports
`fire_incomplete` instead of clearing it over an obstacle the next member on the slug
could never bind its thread past (`write_dm_binding` renames onto it and fails on
every attempt) -- pinned: `test_an_obstacle_at_the_binding_name_refuses_the_fire`), so nothing
between a check and the move resolves a name again (pinned:
`test_retire_never_follows_a_link_planted_at_a_trust_child`, both flows, the linked
`config.json` untouched;
`test_a_link_planted_at_the_binding_name_never_removes_another_members_binding`).
The archive entry's NAME (`new_archive_name`: `<slug>--<stamp>-<4 hex>`) is chosen once
and written into the fire's intent marker (`archive_name`) BEFORE anything moves, so
an attempt that dies between the rename and `fired.json` (ENOSPC) resumes into that
same entry -- the resume writes the missing record there rather than minting a
second entry beside an unrecorded first, and a resume asked to purge removes that
exact entry (the request's own `purge`, or the marker's recorded one -- read as a
purge only when it is exactly `true`: the marker is a file, and a hand-edited
`"false"` is not a purge the user asked for); a member space that exists AGAIN beside its archive entry (recreated after
the rename) refuses the retire with `MemberSlugError`, keeping the marker, since
skipping the source would clear it with live state on disk. `fired.json` is written
through an open WITHOUT `O_TRUNC`: the descriptor is `fstat`-ed and must be a regular
file with exactly one link before it is truncated and written (`_write_record_fd`), so a
hard link planted at the record's name -- the linked file itself, which `O_NOFOLLOW`
does not see -- is never truncated. With **`purge: true`** the directory and the rules
are removed instead of archived, and the thread's transcript is deleted through
the same sequence `DELETE /api/sessions/{key}` runs (`_purge_thread_history`:
claim the slot/transcript route, bind cron ownership, unlink under the transcript
lock, remove the slot) -- and when that path refuses the thread is reported
**kept**, never claimed gone, WITH the reason (`thread.kept_reason`:
`cron_claim_unreadable` -- a scheduled job's claim on the transcript could not be
read, so deleting it would strand that job; `store_unreadable` -- the cron store
could not be read; `refused` -- no conversation log, the transcript lock timed out
or the delete itself declined), so the notice names the repair instead of guessing
at one. A thread that was bound but never wrote a transcript (opened, nothing said;
probed with the log's `has_log`) has nothing to archive or purge and reads `none`,
the same as a member that never opened one. A plain file (or anything that is not
a directory or a link) at `members/<slug>` is not a space to archive or purge and
is refused with `MemberSlugError` in both filesystem flows -- left there, the next
member on the slug could not create its directory and its activity writes would
fail silently -- so the fire keeps its marker and answers `fire_incomplete`. Right
before the marker goes, the fire looks ONCE more at the LITERAL entry
`members/<slug>` (`members.member_space_exists`: an `lstat` of the unresolved name
relative to the pinned members root on POSIX, of the literal path on Windows -- never
through the resolving `member_dir`, which read a dangling link planted there as
"gone"): any entry, a link included, is live state the fire has not retired, so the
marker stays and the answer is `fire_incomplete`; the resumed fire removes a link as a
link, its target untouched, and a recreated directory is reported beside its archive
(pinned: `test_activity_landing_after_the_archive_cannot_survive_a_completed_fire`,
`test_a_dangling_link_planted_at_the_space_after_the_archive_keeps_the_marker`). The
marker's own removal is the last step and is part of the fire: an unlink that fails
is answered as resumable `fire_incomplete` (500), never 200, because a marker left
behind reads as a PENDING fire and refuses every creation on the slug with
`fire_pending` until the next fire clears it (its only remaining work). The answer
says what happened: `{ok, thread: {state: archived|purged|kept|none,
kept_reason?}, lived_state: archived|purged|none}` -- what happened to the
conversation, not where it lives (the notice links the History pane, which finds it
by name; no reader takes a key, so none is answered), and no `resumable` flag: the
`fire_incomplete` code and its sentence ("fire it again to finish") are the signal.
The purge of the conversation is the SAME sequence `DELETE /api/sessions/{key}`
runs -- `sessions.delete_history_transcript`, one helper returning a
`HistoryDeleteOutcome` (claim the slot/transcript route, strict cron-owner sweep,
locked unlink, post-unlink sweep, slot removal) -- so the route and the fire cannot
diverge; the route keeps its own refusal bodies, the fire maps the outcome to its
`kept_reason`. **Deferred, recorded here:** the crew editor's `DELETE /api/agents/{name}`
stays the crew-centric removal -- it clears the enrollment record but is not a fire, so
deleting an ENROLLED crewmate there leaves its agent file, pristine copy, binding and
`members/<slug>/` where they are -- and a same-name hire later INHERITS the
slug-keyed lived state: `members/<slug>/` (activity, briefing, rules) is keyed by
slug alone and the deleted row no longer collides, so `slug_collision` does not
fire, and the DM binding is checked by member NAME, which a same-name hire shares.
Only the pristine copy reads as missing (it is stamped with the store generation).
That is exactly the inheritance Fire exists to prevent, reachable through the
other removal verb. Whether that delete should refuse an enrolled crewmate and
point at Fire (the smaller close: it removes the bypass without a second copy of
Fire's cleanup), or clean the way Fire cleans, is a product decision the maintainer
has not taken; until it is, fire is the member's verb and the only one that
archives lived state, and the crew editor's delete is the documented gap.

**Two side effects of the fire on existing routes, declared.** (1) `PUT
/api/members/{slug}/rules` now waits on the config lock: the rules write re-reads
the roster (enrollment, strict) INSIDE the lock the fire holds from its row
delete through its cleanup, so a rules write cannot land between the fire's
decision and its archive of the rules file (it waits, then sees the member gone
and answers 404) -- a timing change for a route that used to take no lock; it
is a single short hold. (2) `members.record_activity` DROPS a record (returns
`False`, logged at debug) while a fire marker names the member's slug: the space
is being archived or purged, so an entry written then would land in a directory
about to move -- or recreate the directory after the move and strand it -- and
the fire is the member's last act; the session that produced the record is being
closed by the same fire. Both are the reason the fire can promise that nothing of
the member's is written after it. **Residual, recorded:** `_trust_child` takes
`trust/member-rules` and `trust/member-bindings` LITERALLY (a planted link refuses);
five other production readers still resolve a `trust/` child by name
(`autonudge_selfarm.py`, `subagent_persistence.py`, `platform/update_stepup.py`,
`platform/wheel_engine.py`, `apps/builtins/spec_builder/backend/decisions.py`) --
the same root cause with a fix wider than this change; they are not touched here.

Frontend (`pages/members/FirePanel.tsx`, at the foot of the drawer, withheld for
the default member -- decided by the shared `['default-agent']` read the drawer's wake block already runs (`defaultAgentQuery`), not a per-row field; nothing is offered until it has answered, and a failed read offers the verb and lets the server's 409 `cannot_fire_default` answer):
**Fire <name>** opens a two-step confirm that says what goes and what stays. Step 0
(`fire_explain`) says a confirmation follows -- "Nothing is removed until you
confirm." -- and uses the confirm step's own word, **archived**, for where the
thread, activity and notes go (one vocabulary across both steps; "History" is not
on this surface, so step 0 does not lean on it), with
a **purge** tick ("Also delete the thread, activity, briefing and rules. This
cannot be undone.") that relabels the confirm to *Yes, fire and delete* AND swaps the
confirm sentence for the purge's own (`fire_confirm_explain_purge`: "… are DELETED, not
archived. This cannot be undone."), so the destructive path never reads "archived,
not deleted" beside a box that says the opposite; the panel's copy is at body tone,
not the muted weight. The fire
hands the outcome up (`onFired(result, fired)`) for the member the confirm was
pressed FOR -- name, label and handler are fixed in the mutation's variables at
that press, never read off the panel's current props, since the user can select
another member while the request is in flight (that member's panel shows neither
"Firing…" nor the first member's refusal, and is never evicted or announced) --
and the page navigates to the roster the moment it completes when the fired
member is still the one in view -- the drawer unmounts with the member, so the
outcome is said THERE,
in a dismissible notice (`member-fired-notice`, Lucide `X` to dismiss): thread
archived / deleted / KEPT -- naming the server's `kept_reason` when there is one
(`fired_thread_kept_cron_claim`, `fired_thread_kept_store`) rather than guessing --
/ none; where the sentence says **History** the word is a `Link` to
`/chat?history=1`, the chat page's History pane (opened on arrival by that
param), since the pane is not on this surface (`firedOutcomeKey` picks the
sentence, `firedOutcomeText` is its plain-text twin for the accessible name and
the tests). Before navigating to the roster the page EVICTS the fired row from the
cached roster (`MEMBERS_ROSTER_QUERY_KEY`) and then invalidates: the roster route
renders against the cache, and a row still there for the refetch's duration would
reopen the member the user just fired for a beat. Pinned in
`test/test_member_fire.py` (**the step-5 gate**: row, copy, lineage and
pristine copy gone, the shared template untouched, the store archived under the
retirement marker, activity moved to `.retired` with a `fired.json` naming the
member, its thread's history key and its template, the rules archived under the
protected `trust/` subtree, the binding gone;
the open thread is closed through the tab's path before the row goes and a close
that fails leaves the member whole; purge removes the lived state and reports a
thread the history path could not delete as kept, and as purged when it could;
a member that never lived leaves nothing to archive; the default is refused and
bad bodies are 400; a member that changed under the request is not fired; the
same name can be hired again afterwards and starts from nothing; a link planted
at the member directory is removed, never followed; a link or a file planted at
`.retired` refuses the archive with nothing moved; only a binding naming the member
is removed; a member sharing its slug with a live colleague is refused before
anything is closed; a binding naming a ghost of the slug closes no slot and
reports no thread; a fire interrupted after the row went answers `fire_incomplete`,
leaves the marker and is finished by the next fire, while an unknown member with
nothing pending is still 404; a hire under a pending id is refused until the fire
finishes; the cleanup runs with the config lock held; a resume with a row under
the id refuses and keeps the marker; a directory swapped for a link mid-retire is
unlinked, never followed; the fire holds the thread lock; a plain file at the
member directory is refused in both flows; a marker that cannot be cleared is
answered `fire_incomplete` with the slug still pending until the retry clears it;
a kept thread carries the server's reason) and
`FirePanel.test.tsx` (two-step, step 0 says a confirmation follows and says
"archived", archive by default, the outcome handed up, purge as an explicit tick
with a kept thread said and its reason named, a refused fire kept in view, a fire
completing after another member was selected attributed to the member it was for)
and
`MembersPage.identity.test.tsx` (the notice's History link, the reason a kept
thread stayed, the fired row gone from the roster with the drawer).

## Hire gallery (design step 6): `/members/hire`, `GET /api/members/templates`

The one place a crew member is hired from, whatever the template's origin --
the rail stays on Crew Members; the gallery is a view under the page (its own
route so a hire can be linked to; `App.tsx` mounts it lazily like the page).
The roster's **Add a Crewmate** (header `+` and the empty roster's call to action)
opens it. Three sources, one card shape (`member_gallery.build_catalog`, served
by `GET /api/members/templates`, owner-gated like the hire it leads to; free
text on a card passes the roster's identity redactor):

- **App cards** -- every card an ENABLED installed app offers in
  `crew.templates` (`app-kit-platform.md` §3.1), resolved through
  `member_templates.resolve_store_template` so the card carries the
  materialized agent's name and capabilities; a card whose hire would be
  refused right now (not materialized, unreadable spec, invalid crew section)
  still lists with `hireable: false` and the hire's own code as
  `unavailable_code`, so the gallery says why Hire is off in the words the
  hire would use. `source` is `{kind: "store", app, agent}`.
- **Built-ins** -- the agent files this package ships (`OWNED_KIRO_AGENT_FILES`,
  `source == "builtin"` in discovery): role humanized from the name
  (`pipeline-conductor` -> *Pipeline Conductor*), duty from the spec's
  description, publisher *Kiro Crew*, category `other`. `source` is
  `{kind: "local", agent}`.
- **Local files** -- the user's own agent files (`source == "local"`), the same
  derivation, publisher empty. The assistant (`kirocrew`) is never a card (the
  `default` member IS it); a member's **private copy** is never a card (it is
  one colleague's definition, not a posting); an app's materialized file is
  offered only through its card; a package's agent is the package's to offer.

Each card: `id` (`app:<app>/<agent path>`, `builtin:<name>`, `local:<name>`),
`origin`, `source`, `role`, `duty` (a card without one falls back to its
description's first sentence), `description`, `tags`, `category` (the closed
`CREW_CATEGORIES` vocabulary; unset or unknown is `other`), `starter_prompts`,
`avatar` (the card's ghost, or null), `publisher`, `version`, `agent`,
`capabilities` (the definition's skills and MCP servers, each once), and
`hired_as` -- the crewmates hired from this card and still enrolled, each
`{id, display_name}` in roster order (`member_gallery.enrolled_records`: the
same `enrolled_member_ids` predicate `GET /api/members` applies, so a row that
merely binds the card's file, a session that used the template or a record
whose generation is not the row's store is not a hire; a fired crewmate has no
record and drops out). The `id` opens the DM, the `display_name` is what the
gallery's *Chat with* and picker say (two crewmates may carry one label; the id
tells them apart). A card that cannot be hired from today still names them: the
crewmates are the owner's whatever the template's state, so *Chat with* / the
picker stay while Hire is off. They are attributed by the template's canonical
ref -- the refusal's own (`TemplateUnavailable.ref`, set once the shipped spec
was read and the declared name is known: not materialized, tampered, runtime
prompt) or, when the spec itself could not be read, the card's file stem, which
is the declared name in every case but a spec that renames itself.
Every free-text field on a card -- `role`, `duty`, `description`, `publisher`,
`version` (a semver build suffix is app-authored text like the publisher),
`unavailable_reason`, the `tags`, the `starter_prompts`, each capability's
`name` (an MCP server or skill name is app- or user-authored config too) and each
hired crewmate's `display_name` -- passes the roster's identity redactor
(`_identity_text`) before it ships, so a credential-shaped string in a manifest
or an agent file never renders in the gallery. The ROUTING identifiers -- `id`,
`agent`, `source.agent` / `source.app`, and a hired crewmate's `id` -- are what the
client posts or navigates back, so they cannot be redacted and still work: a
card (or a `hired_as` entry) whose routing identifier the redactor would alter
(a hand-authored agent file or a hand-edited row named like a credential; the
agent-name grammar admits one) is OMITTED
from the listing rather than shipped raw (pinned:
`test_credential_shaped_card_text_is_redacted`,
`test_a_card_whose_routing_identifier_reads_as_a_credential_is_omitted`).

**Provenance is real, not guessed from the filename.** `agent_discovery`
classifies a user-level agent file as `kirocrew` (the assistant and its lite
twin, which the sync never auto-creates a crew for), `builtin` (another file
this package ships), `app` (`<app>--<agent>.json` where `<app>` is an INSTALLED
app -- known from the apps root, `installed_app_names`, resolved LAZILY on the first
app-shaped stem so a listing without one does no apps-directory I/O, memoized on the
root's stat signature so a warm listing costs one stat, and answering `None` when the
root exists but cannot be read -- a failure kept apart from the empty set, during which
an app-shaped file is classified as an app's, the retention-safe answer; the double
dash is the bridge's separator and never a package's, so a leftover of an uninstalled
app is `local`, not a package named `<app>-`), `package` (`{package}-{name}.json`) or
`local` (everything else -- a hand-written file is the user's, not built in). A
project checkout's own `.kiro/agents` file is `local`. The roster's *mine*
filter reads `kirocrew` and `local` alike (`normalize_member_source`). The sync
(`POST /api/agents/sync`) registers an app's agents with `source: "app"`, and its
prune step treats that spelling as it treats `package` (and the legacy `aim`):
disabling or removing the app deletes the materialized `<app>--<agent>.json`, and
a row left behind would dispatch to a definition that is gone, so it is pruned --
and an `app` row is KEPT only by ITS OWN app's file of that name: the sync
records the registering app on the row (`source_app`, the app's name -- the row's
`kiro_agent` is the bare DECLARED name, which two installed apps may both use),
and the row survives only while a discovery that is that app's file (`source ==
"app"`, `package == source_app`) -- or one whose dedup DROPPED that app's file of
the name (`AgentInfo.shadowed_apps` beside `shadowed_sources`: the apps and the
sources the listing gave up in favour of the kept entry; a name an app and an
earlier-sorting package both ship is ambiguous, not proof the app stopped shipping
it, so the app row and its memory stay -- pinned:
`test_a_name_an_app_and_a_package_both_ship_keeps_the_app_row`) -- still answers to
it. Another app's same-named file keeps nothing: with the originating app disabled
the row goes, so its private memory never follows a bare name to a different app
(pinned: `test_an_app_row_is_kept_only_by_its_own_apps_file`). A row from before
`source_app` existed is attributed once, in the locked write, when exactly one
installed app ships the name; two or more is ambiguous -- the row is kept
unattributed and the gap logged, never guessed (pinned:
`test_a_legacy_app_row_is_attributed_once_when_one_app_ships_the_name`). A row an
older sync spelled `package` (or `aim`) for what discovery now classifies as an
app's `<app>--<agent>.json` is RE-STAMPED the same way -- `source: app` plus the
app -- when exactly one installed app ships the name and no package file declares
it, in the locked write and as an app row for the rest of that pass, so an install
that already had members gets the app-row protections too (pinned:
`test_a_pre_existing_package_row_for_an_apps_file_is_restamped_as_the_apps`). "Ships
the name" is answered by the materialized file first and, when no app file is
present right now, by the installed MANIFESTS (`_apps_declaring_agents`, the same
manifest + root the resource registration reads): an app update removes and
rewrites its files under the lifecycle lock, and a legacy row caught in that window
would otherwise stay `package`, miss the lifecycle hold and prune with its memory
archived -- the one moment the app-row protections matter (pinned:
`test_a_legacy_package_row_whose_app_file_is_in_transit_is_recognised_from_the_manifest`).
Neither the re-stamp nor the CREATION of an app row happens while the apps
directory cannot be read: discovery's unreadable-time reading ("every `--` file is
an app's") is retention-safe for rows that exist and no evidence for a new one -- a
user's own `foo--bar.json` would be registered with a private store and pruned, memory
archived, by the next readable sync; such a file waits for a sync that can read the
provenance (pinned: `test_an_unreadable_apps_directory_creates_and_restamps_no_app_row`). The
predicate behind the roster, `enrolled_member_ids`, lives in `agent_state` beside
the record it reads; the roster route wraps it and the gallery reads it there. A local
or package agent that happens to share the name
must not keep the row alive after the app is disabled, or the row would dispatch
that unrelated definition in the app agent's name; when the apps directory could
not be read (`installed_app_names() is None`, asked off the loop in the same hop as
the listing -- and ONE app entry that cannot be decided makes the whole answer
`None`, never "the other apps": an entry that cannot be inspected, or an
`installed.json` present but not a regular file (a directory, a dangling link --
nothing Kiro Crew's own writer produces); only a PROVEN absence of the marker excludes
an app, because an app omitted from the set reads as uninstalled and its member would
be pruned; the failure is not memoized) NO `app` row is pruned -- an
unreadable directory or entry says nothing about which apps are gone, and pruning on
it would archive an app member's memory --
while package rows are decided as usual; and while ANY app's lifecycle lock is
held (`app_lifecycle_in_progress`: an install, update, enable, disable or
uninstall running on the loop) NO `app` row is pruned either -- an update
deregisters an app's materialized agents and registers them again under one
hold, so a file a scan alongside it found missing is in transit, not gone; the
sync cannot take that lock (it holds the config lock, which the lifecycle routes
take inside theirs), so it leaves every app row for the next sync. The locked
write re-checks an app row's file on disk (`_app_agent_file_present`: for a row
that records its app, only `<source_app>--*.json` declaring the row's binding; for
a legacy row, either name it can wear -- the bare `<name>.json` or any
`<app>--<name>.json` declaring the name -- an app row's `kiro_agent` is the
DECLARED name, not the file stem, so a bare-name check alone would call every app
agent missing) and keeps a row whose file is back (a lifecycle operation that
began after the check finished registering it), and the answer's `pruned` names
only the rows that went. `local` rows -- the user's
own files -- never are pruned (pinned in `test/test_agent_sync_prune.py`,
including `test_an_app_lifecycle_in_progress_prunes_no_app_row` and
`test_an_app_file_back_on_disk_at_write_time_keeps_the_row`).

**Frontend** (`pages/members/HireGalleryPage.tsx`): scenario chips (All plus
every category present, in the design's order), a search over role, duty, tags
and publisher, cards (ghost avatar seeded by the card id or wearing the card's
ghost; role; duty; the first three tags; the actions below -- the card wraps, the
text keeps a minimum basis and the action cluster drops to its own row on a narrow
viewport instead of crushing the role and duty or overflowing), and a detail Dialog
(full description, every tag, an `ErrorNotice` when unhireable, **Try asking**
starters -- previews, not controls: a row that reads like a question must not
commit a hire; the same prompts become the Ask cards in the new member's thread
-- a collapsible **Built-in capabilities (N)**, a quiet `publisher · vX · origin ·
hired as <display name>` / `hired N times` line, and the same actions in its
footer). No publisher, version or verified mark on the card face.

**Hire is named, then confirmed.** **Hire** is the PRIMARY action on every card
and in the detail, whatever the card's count, and it always opens the naming
step (`HireNameDialog`, composed through the dialog's standard slots --
`DialogHeader` / `DialogBody` / `DialogFooter` supply the inset and the header's
clearance from the close button, since `DialogContent` carries no padding of its
own; the picker below is composed the same way): the role is the input's
placeholder, never its value;
a blank or whitespace-only name cannot be confirmed (the button is disabled and
the hint says a name is required); Cancel or Escape closes it with NO request
made, and the next card's dialog starts with an empty field. Only the confirm
sends `POST /api/members {source, display_name}` -- the name whitespace-collapsed
(`hireName`) -- after which the roster, catalog and installed-agent queries are
invalidated and the page navigates to `/members?member=<id>`. A refusal is said
inside the naming dialog with the typed name kept for the retry (no hand-off:
the dialog is the unsaved state).

**One secondary action follows the count** (`hired_as.length`, the server's
enrolled-active count): **0** -- none; **1** -- **Chat with <display name>**,
which opens that crewmate's DM by its immutable id; **2+** -- **Your crewmates
(N)**, which opens an accessible picker (`CrewmatePicker`, a Dialog listing each
crewmate by name with its id beside, in roster order) where nothing navigates
until a row is chosen and Escape leaves the gallery where it was. Opening a chat
never hires. Before navigating, the page re-reads the roster: a crewmate fired
since the catalog was read is SAID (`hire-chat-error`, "<name> isn't one of your
crewmates anymore", and the catalog is refetched), never opened as an empty
thread or swapped for another crewmate from the same card; a roster that cannot
be read is said the same way with the hand-off, and nothing navigates. Either
outcome first closes the layer the action came from -- the detail Dialog or the
picker -- so the notice, which lives on the page under them, is not covered by
the dialog that led to it. The rail
row stays active under `/members/hire` (`navRowActive` matches the prefix).
Pinned in `test/test_member_gallery.py` (**the gate**: cards from all three
sources with the exact source bodies, the app card's every field, a member's
copy never listed; **the gate**: `hired_as` is the enrolled active roster from
the card -- two named hires listed by id and name, a rename followed, a
hand-bound row and a hand-templated row not counted, a fired crewmate dropped; a
record of another generation not a hire; an unhireable card listed with the
hire's code and still naming the crewmates hired from it, a disabled app's cards
absent; redaction, including a
credential-shaped enrolled id omitted from `hired_as`; owner gate;
`humanize_agent_name`), `test/test_agent_discovery.py::TestProvenance` and
`HireGalleryPage.test.tsx` (filter and chips, the face and its actions per
count, **the gates**: Hire asks for a name and posts nothing until it is
confirmed, then sends source + name and lands on the thread; both dialogs use the
header / body / footer slots; cancel writes
nothing and the next card's field is fresh; Chat with opens by id and hires
nothing; the picker navigates only on an explicit choice and tells two same-label
crewmates apart by id; a second hire from a hired card goes through the same
naming step, whose description then says it ADDS one -- "you already have N
crewmate(s) hired from this template" (`name_description_another`, plural) -- so
a reader cannot mistake it for a duplicate; Hire from the detail layer CLOSES that
layer as the naming step opens (one Hire on screen at a time); a fired target is
said; the detail layer (its capability kind tags in sentence case: "Skill", "Tool
server", "Knowledge"); a refusal said in the dialog; catalog and roster errors, a
failed chat from the detail layer or the picker closing that layer; the empty
state).

**Post-hire thread** (`pages/members/MemberThreadExtras.tsx`): the DM header's
title row is the **rename affordance** -- a crewmate arrives already named by
its owner (the gallery's naming step is required), so the header shows that
name with the role as a subtitle (withheld while it equals the name, so it never
reads "X · X") and no hint about how it was named; the pencil opens an input
over the label, Enter saves `display_name` ONLY through `PUT /api/agents/{id}`
(Escape cancels, an unchanged name writes nothing). **This reverses a recorded
decision, on purpose and for one field**: the Crew page was pinned as "never a
second writer" (#9103; its avatar entry was likewise kept a navigation, #9425) --
the pencil used to open the crew editor. With the gallery, the display name is
what the owner typed at the naming step moments before, and sending them to
another page to fix a typo in it made the hire's own words a second-class edit;
`display_name` is a label with zero blast radius (the id never moves), so the
header writes THAT field and nothing else -- every other change still leaves
the page (`learn-cron-dashboard.md` says the same). Naming the reversal here is
the record; a maintainer who wants the old pin back reverts `MemberNameEditor`
to a navigation. **Same-label twins wear their
id**: when two roster rows carry one label (a role hired twice and named alike, or
a rename landing on a sibling's name -- `rosterFilter.twinLabelIds`, exact after
whitespace normalization), the roster row and the thread header show the mono id
beside the label with the word **id** on its face (*id: Night-pager*), on those
rows only, so the destination says which of the two it is -- and which of the two
strings is the name -- the way the picker did; distinct labels stay a list of
names (pinned: `MembersPage.identity.test.tsx`). The editor is
mounted with `key={active.name}`, so switching members mid-rename remounts it: a
failed request's draft, its error and its retry cannot outlive the member they
were typed for and land on the next one (pinned in `MembersPage.test.tsx`). No
navigation to the crew editor for a name; the drawer's Configuration keeps the "Edit in crew
manager" exit for everything else. The **empty thread** is ChatPane's
`emptyState` render prop (handed the pane's own `doSend`, so a starter goes
through the composer path -- optimistic bubble, queueing rules -- and renders in
the message column, never as an overlay): a greeting, the member's one-line duty
(from the catalog card that attributes the member, else its role) and up to
three starter prompts whose **Ask** sends the prompt (`cardForMember` finds the
card whose `hired_as` names the member); a member with no card
falls back to the plain "Session ready" hint, and a catalog that cannot be read is
said there as an `ErrorNotice` (never a "Session ready" that reads like a member
with no card). The roster says the same about a failed apps read (the badge then
falls back to the app id) in an `ErrorNotice` above the rows.

**Drawer as data** (`pages/members/drawerSections.tsx`): the Crewmate summary is an
ordered `DrawerSection[]` rendered by one loop (`DRAWER_SECTION_ORDER`):
**Briefing** (`GET /api/members/{slug}/briefing?member=`, owner-gated, read-only,
the same pinned fail-closed read the prompt builder uses, the text through the
identity redactors -- an absent file is empty text, a platform that cannot read
one race-free says so) -> **Capabilities**
(the skills and MCP servers of the file the member is bound to, from
`/api/agents/installed`; the hire invalidates that list so the new copy's
capabilities read right away) -> **Role template** (a store-hired member only:
the role-update offer / up-to-date line and detach, in the open part of the
drawer because an offer the owner cannot see is not one) -> **Activity** (the
Today / 7-day tiles and the recent log) -> **Sessions it's driving** -> **Auto
patrol** -> **Wake sources** -> **Configuration** (Member id, Role, Source,
Agent template, Model, Workspace, Memory store, the memory notice and the
crew-manager exit) folded behind a disclosure, closed by default. Fire stays at
the foot, outside the sections. **Roster rows** wear the member's face (the
card's ghost, copied at hire) and a compact source badge on a third line --
the pack's display name on the face, the version in the title; *Built-in* for a
shipped member; none for a member created here. Pinned in
`MembersPage.test.tsx` (rename in place, no just-hired hint, the empty state
sending through the pane, the section order against the rendered document and
the folded Configuration) and `MembersPage.identity.test.tsx` (badges).

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
