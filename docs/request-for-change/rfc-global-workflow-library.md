---
title: Global Workflow System — reusable definitions and TaskRunner composition
status: in-progress
revision: v2
author: Kyle Seaman, with Codex
created: 2026-08-25
last-audited: 2026-08-26
audited-at: 3407351dd
doc-pr: 5951
implementation-prs: [5951]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Global Workflow System — reusable definitions and TaskRunner composition

- Status: in-progress — nothing from this RFC is on `main`. RFC and
  implementation are proposed together in PR #5951.
- Author: Kyle Seaman, with Codex
- Created: 2026-08-25
- Audited against: `3407351dd`
- Related: `docs/system-specs/modules/workflows.md`,
  `docs/system-specs/modules/taskrunner.md`,
  `docs/system-specs/modules/harness-parity.md`, and
  `docs/system-specs/modules/memory-skills-hooks.md`

## 1. Summary

Add a global, user-managed library of reusable workflows to Kiro Crew.
The library is the durable second layer above the existing automatic run
snapshots:

1. Every workflow invocation persists its source, events, result, and
   format-specific recovery state in its run snapshot or linked project state.
2. A workflow becomes reusable by name only when the user explicitly chooses
   **Save to library** in a dashboard confirmation flow.

Saved definitions live in the protected global Kiro Crew data-home directory,
normally `~/.kiro/crew/workflow_library/`. They are not scoped to one repository,
harness, or configured run-snapshot directory. Each definition has a stable id,
a slash-command slug, immutable revision history, and optional lineage to the
saved revision from which it was adapted.

When Kiro Crew authors a new workflow from an intent, it searches the local
library for relevant examples. The model may adapt a match into a new draft, but
it never mutates the matched definition. If the user explicitly invokes a saved
workflow by id or slug, Kiro Crew executes the exact saved source instead of
asking the harness to reinterpret the request.

The Agent Capabilities page gains a Workflows surface for listing, searching,
authoring, reviewing, saving, editing, and running definitions. Chat gains one
namespace command:

```text
/workflow
/workflow <name> [input]
```

The first form lists the library. The second runs the current saved revision and
exposes the remaining free-form text as `ctx.args["input"]`.

The workflow system is also the execution substrate for TaskRunner. It owns the
common run identity, event stream, persistence, cancellation, and reusable
definition contract. TaskRunner remains the higher-level project-delivery layer:
it adds planning, approvals, retries, tests, git/worktree coordination, and
replanning around a workflow run. Dynamic Python and TaskRunner YAML are source
formats for the same workflow system, not separately selectable engines.

## 2. Motivation and current state

Verified at `749468d42` on 2026-08-25.

### 2.1 Run snapshots already provide automatic, durable iteration

`WorkflowRunStore` persists one JSON record per invocation under
`<workflows.dir>/runs/`. The record includes the authored script, event stream,
result, and resume cache, and is rehydrated after a gateway restart
(`src/kiro_crew/workflows/store.py:1-17`, `81-170`). This is the right automatic
behavior: a user can inspect a run, edit its source, rerun it, or resume a prefix
without first deciding whether the script deserves a permanent name.

That run store is not a reusable workflow catalog. Its identity is a generated
run id; it has no stable human-facing name, revision lineage, or management
surface. Treating every run snapshot as a slash command would turn transient
experiments into permanent capabilities without consent.

### 2.2 There is no named definition layer at the audited base

At `749468d42`, the workflow package contains run persistence and execution but
no `WorkflowDefinitionLibrary`. The MCP registry has run-oriented tools, but no
library list, save, or update tools. The dashboard recognizes `/workflows` for
the existing run surface, but not `/workflow <name>` for exact saved execution.
These absences were checked with definitions and callers, not inferred from the
UI alone.

The missing layer creates four product problems:

1. A useful workflow discovered during normal work cannot be promoted into a
   stable reusable capability without manual file management.
2. New authoring starts without examples from workflows the user has already
   refined locally.
3. Copying an existing workflow loses the relationship to the source revision,
   so later readers cannot tell whether two definitions are related.
4. A named request may be reinterpreted by whichever harness receives the chat
   turn instead of executing the definition the user selected.

### 2.3 Harness support needs one Kiro Crew-owned contract

Kiro is the first-class harness and KAS is an adapted harness. A workflow library
must not become a per-harness feature directory or a second provider system.
Definitions, lookup, validation, and invocation belong to Kiro Crew so every
supported harness sees the same names and exact source.

The command therefore resolves before harness session acquisition. No harness
gets authority to map a saved name to different source, and adding KAS support
does not add a conditional to the Kiro workflow engine.

### 2.4 TaskRunner is an opinionated workflow layer, not a peer engine

TaskRunner and dynamic workflows currently duplicate run-level concepts while
solving different parts of the problem. Both assign ids, schedule background
work, persist status, expose cancellation, and report progress. TaskRunner then
adds project-delivery semantics that do not belong in a general workflow kernel:
plan decomposition, per-task approval gates, retry and replan policy, tests,
self-review, git commits, and worktree coordination.

Treating them as selectable engines would freeze that duplication into the
public definition model and force users to understand an implementation detail.
Instead, the workflow system owns the shared run substrate. TaskRunner wraps a
host-side workflow run and retains only its project-specific extension state.
The Python DSL remains one authoring format for the workflow kernel; a TaskRunner
plan is another format whose control loop runs in trusted host code.

## 3. Goals

1. Preserve automatic per-run script persistence and resume behavior.
2. Make durable reuse an explicit user choice rather than an automatic side
   effect of authoring.
3. Store reusable definitions globally under the Kiro Crew data home.
4. Let new authoring search and adapt relevant saved local workflows.
5. Preserve adaptation lineage without modifying or aliasing the parent.
6. Keep every saved edit as a new revision with optimistic conflict detection.
7. Provide one UI surface for viewing, creating, editing, and running saved
   workflows.
8. Provide one slash namespace whose behavior is identical across Kiro and
   adapted harnesses.
9. Expose discovery and exact invocation through stateless MCP tools while
   keeping durable mutations behind explicit dashboard actions.
10. Keep the existing Python validator, runner, governance, event journal,
    completion injection, and run persistence as the dynamic-source path.
11. Make every new gateway TaskRunner execution one workflow run with one shared
    lifecycle and event identity.
12. Keep TaskRunner's project-specific guarantees as an extension around the
    workflow run rather than duplicating them in the workflow kernel.
13. Let a TaskRunner plan be explicitly saved and invoked from the same global
    workflow library without exposing an engine selector.

## 4. Non-goals

- Automatically promoting every authored or successful workflow.
- Replacing run snapshots with saved definitions.
- Adding repository-scoped or team-shared workflow libraries in this version.
- Creating one top-level slash command per saved definition.
- Allowing a harness to reinterpret an explicitly named saved workflow.
- Generating sandboxed Python from a TaskRunner plan.
- Moving TaskRunner planning, approvals, retries, tests, git/worktree handling,
  or replanning into the workflow kernel in this version.
- Removing the Tasks UI, TaskRunner APIs, or `runs.json` compatibility mirror in
  this version.
- Adding deletion, archival, import/export, or remote synchronization.
- Invoking an arbitrary historical definition revision by slash syntax. The
  current saved revision is invoked; completed runs retain their own source
  snapshot.
- Using embeddings or an external service for local adaptation search.
- Changing `agent.provider` or weakening harness-parity invariants.

## 5. Decisions and invariants

The implementation and tests use these design invariants:

| ID | Invariant |
|---|---|
| WFL1 | Every invocation persists through the existing run store whether or not the workflow is saved by name |
| WFL2 | Promotion into the named library requires an explicit user action |
| WFL3 | Definitions are global in a dedicated agent-protected Kiro Crew data-home directory |
| WFL4 | Adapting a definition creates a separate identity and records the exact parent id and revision |
| WFL5 | Updating a definition appends a revision and rejects a stale expected revision |
| WFL6 | Intent authoring receives only a bounded set of locally matched definitions |
| WFL7 | An explicit id or slug executes exact validated saved source without harness reinterpretation |
| WFL8 | `/workflow` is Kiro Crew-owned and has the same behavior for Kiro, KAS, and future adapted harnesses |
| WFL9 | Persisted and returned LLM-derived strings pass through credential and exfiltration-url redaction |
| WFL10 | A new gateway TaskRunner project is attached to exactly one workflow run; the workflow registry owns its common lifecycle projection |
| WFL11 | TaskRunner uses the host-side workflow run API and never generates or executes sandboxed Python for a task plan |
| WFL12 | Definition source format selects validation and authoring syntax only; users never select an execution engine |
| WFL13 | Existing TaskRunner callers and stored projects remain readable and keep their current task-specific behavior during migration |

## 6. Design

### 6.1 Two persistence layers

The layers have different lifetimes and different promotion rules:

| Layer | Location | Creation | Identity | Purpose |
|---|---|---|---|---|
| Run snapshot | `<workflows dir>/runs/<run_id>.json` | Automatic for every invocation | Generated run id | Resume, inspect, rerun, and preserve execution evidence |
| Saved definition | `<KIROCREW_HOME>/workflow_library/<workflow_id>.json` | Explicit user choice | Stable id plus unique slug | Reusable named capability with revisions and lineage |

Run snapshots resolve `<workflows dir>` through the existing `workflows.dir`
setting, falling back to `<KIROCREW_HOME>/workflows`. Saved definitions instead
use the fixed `<KIROCREW_HOME>/workflow_library` trust root. That directory is
listed in `security._CREW_SECRET_LEAVES`, so agent file tools and shell commands
cannot plant or rewrite executable definitions while the dashboard and service
can still perform the user's explicit management actions directly. “Global”
means Kiro Crew-user scope rather than repository scope.

Run snapshots remain best-effort because an I/O failure must not terminate an
active run. Definition writes are user-requested management operations and fail
loudly through a machine-readable API error if persistence fails.

### 6.2 Definition record

One owner-only JSON file stores one definition and its revision history:

```json
{
  "schema_version": 2,
  "id": "wfd_0123456789abcdef",
  "slug": "debug-login-flow",
  "name": "Debug login flow",
  "description": "Investigate a failing authentication path",
  "format": "python",
  "created_at": "2026-08-25T12:00:00Z",
  "updated_at": "2026-08-25T12:30:00Z",
  "revision": 2,
  "source": "META = {...}\nasync def workflow(ctx): ...\n",
  "content_hash": "<sha256>",
  "derived_from": {
    "workflow_id": "wfd_fedcba9876543210",
    "revision": 4
  },
  "revisions": [
    {"revision": 1, "source": "...", "created_at": "..."},
    {"revision": 2, "source": "...", "created_at": "..."}
  ]
}
```

The stable id is the durable reference. The slug is a unique, human-facing
alias restricted to 64 lowercase alphanumeric/hyphen characters after
normalization. A collision gains a numeric suffix. An omitted or blank slug on
update preserves the existing alias.

`format` is immutable for the lifetime of a definition. Version 2 accepts
`python` and `task-plan`; a version 1 record with no format is read as `python`.
The marker chooses source validation and presentation syntax, not an execution
engine. Python definitions use the existing restricted workflow validator.
Task-plan definitions use TaskRunner's existing safe YAML parser and `agents:`
DAG schema. A revision cannot change format because that would change the
definition's meaning under the same stable identity.

The task-plan schema extends the existing `agents:` records with optional
`requires_approval` and `force_approval` booleans. The parser rejects non-boolean
values. The canonical serializer round-trips both fields so saving a TaskRunner
project cannot silently discard an approval gate. Existing YAML without these
keys retains its current behavior.

`content_hash` identifies byte-identical source but does not deduplicate records.
This is deliberate: an adapted workflow must remain a separate definition even
when the first saved draft happens to be identical to its parent. Identity and
lineage express user intent; content equality does not.

An update supplies `expected_revision`. If it differs from the current revision,
the write returns a conflict instead of overwriting a newer edit. A successful
update appends a new immutable source entry and replaces the current projection.

### 6.3 Local matching and adaptation

`WorkflowService.author(intent)` performs deterministic local lexical matching
before opening its isolated authoring session. It considers only `python`
definitions because the authoring prompt requests Python; a task plan is not
silently translated into another language. It ranks tokens from the saved
slug/name most strongly, description next, and source last. It supplies at most
three matches, with each source bounded before prompt insertion.

The authoring prompt distinguishes two cases:

- If no useful local workflow exists, author from scratch and omit lineage.
- If a match is useful, adapt it into a new draft and set
  `META["adapted_from"] = "<workflow-id>@<revision>"`.

Kiro Crew accepts reported lineage only when the id and current revision exactly
match one of the candidates supplied to that authoring call. Historical revisions
present in a candidate's stored history are not prompt references. A fabricated or
unsupplied reference is discarded. The result remains an unsaved draft until the
user chooses promotion.

This is example-based adaptation, not inheritance. A child does not dynamically
read its parent at execution time, and editing a parent never changes existing
children.

### 6.4 Exact saved invocation

`WorkflowService.start_definition(ref, ...)` resolves the current definition by
stable id or unique slug and validates it according to its source format.
Python source passes through the existing `start()` path. A task-plan definition
is handed to TaskRunner through the registered host-workflow start port;
TaskRunner attaches the resulting project to one workflow run before planning or
execution begins. Neither path asks a harness to reinterpret the saved source.

Non-empty free-form command input is copied into the workflow run's `input`
argument. Python exposes it as `ctx.args["input"]`. TaskRunner records it as the
project's original run context, so its existing task-prompt builder supplies it
to each planned step without modifying the saved YAML bytes. Structured MCP and
HTTP callers may also supply an `args` object. If both contain a non-empty
`input`, the explicit free-form field is authoritative.

The started run response carries `workflow_id`, `slug`, and `revision`. The run
snapshot stores the executed source, so a later definition update cannot alter
the meaning of an already completed or resumable run.

### 6.5 Slash-command contract

Kiro Crew registers `/workflow` as a static slash command:

```text
/workflow
```

Lists saved definitions and their current revisions.

```text
/workflow debug-login-flow reproduce with user 123
```

Resolves `debug-login-flow`, starts its exact current source, and supplies
`"reproduce with user 123"` as `ctx.args["input"]`.

The dashboard dispatches the command locally before acquiring or forwarding to
a harness (`src/kiro_crew/dashboard/chat_runner.py:4965-5003` in the implementation
worktree). This is the critical harness-neutrality boundary: Kiro and KAS cannot
assign different meanings to the same saved name.

One namespace command is preferred over registering each definition as its own
top-level command. It avoids collisions with harness and Kiro Crew commands,
keeps command discovery bounded, and makes rename behavior explicit.

### 6.6 HTTP API

The dashboard owns the management API:

| Method and route | Contract |
|---|---|
| `GET /api/workflows/definitions?q=` | List all definitions or return locally ranked matches |
| `POST /api/workflows/definitions` | Validate and explicitly save `{source, format?, name?, description?, slug?, derived_from?}`; omitted format is `python` |
| `POST /api/workflows/runs/{run_id}/promote` | Save eligible server-side source with `{name?, description?, slug?}`; Python uses the original completed source, while a linked TaskRunner project uses its canonical current plan |
| `GET /api/workflows/definitions/{id-or-slug}` | Return one definition including revisions and lineage |
| `PATCH /api/workflows/definitions/{id-or-slug}` | Validate and append `{source, expected_revision, name?, description?, slug?}` without changing format |
| `POST /api/workflows/definitions/{id-or-slug}/run` | Run exact source with `{input?, args?, budget_total?, timeout_secs?}` |

New non-2xx JSON bodies carry stable machine-readable `code` values. A stale
update is `409`; an unknown definition is `404`; unavailable workflow service is
`503`; malformed input is `400`; durable I/O failure is `500`.

### 6.7 MCP tools

Agents receive the read and execution subset through stateless tools:

| Tool | Purpose |
|---|---|
| `workflow_library_list` | List or locally search saved definitions |
| `workflow_run` with `workflow` | Invoke an exact saved id or slug |

The existing source- and intent-based `workflow_run` forms remain compatible.
If `workflow` is present, exact saved execution takes precedence. The tools keep
no module-global caller state; they forward through the dashboard API and resolve
session identity per call through the existing MCP transport.

Create and update are not model-facing tools. The core MCP server auto-approves
its own handlers, so a tool description or model-supplied confirmation flag could
not prove that a human chose durable retention. The Agent Capabilities editor and
the completed-session save dialog call the HTTP mutation routes only after a user
clicks the corresponding confirmation action.

### 6.8 Agent Capabilities UI

Agent Capabilities gains a Workflows tab with two views:

- **Workflow library** contains a searchable definition list and a
  detail/editor pane.
- **Runs** contains the common run history for dynamic Python workflows and
  TaskRunner projects. Selecting a run shows its shared lifecycle projection,
  source format and driver provenance, plus capability-driven actions such as
  save and cancel.

The library view contains:

- A searchable definition list displaying the workflow name and
  `/workflow <slug>` invocation.
- A detail/editor surface showing metadata, current revision, lineage, editable
  source with format-appropriate Python or YAML highlighting, line numbers, and
  horizontal scrolling,
  save-revision action, run input, and the started run id. Presentation never
  reformats the source bytes that revision checks and exact invocation use.

The create path starts with a natural-language intent. Kiro Crew authors and
validates an unsaved draft, displays any verified lineage, and presents a
separate **Save to library** action. Closing or abandoning the draft does not
promote it.

A successful ad-hoc workflow card in chat also presents **Save workflow**. The
compact dialog loads the completed run snapshot, pre-fills a name and
slash-command slug plus the source metadata description, and shows the
response-redacted source in a compact read-only highlighted preview. After the
user confirms **Save to library**, the client submits the run id and metadata;
the service promotes the original source from its server-side run handle rather
than round-tripping display-safe text. The run store records whether source bytes
survived redaction unchanged: exact restored source remains promotable, while
redaction-changed and legacy source fails closed. An unedited rerun preserves that
provenance.
Running, failed, cancelled, and exact saved
invocations do not show the promotion action. After saving, the card displays
the resulting `/workflow <slug>` command and links to the Workflows tab.

Session promotion does not accept client-supplied source or lineage. It parses
`META["adapted_from"]` from the validated original run source and records it only
if the referenced id and revision resolve in the global library. The general
management create route may explicitly submit lineage and applies the same exact
revision check.

Paused plans and completed TaskRunner projects in the unified Runs view gain
**Save workflow**. That action uses the current plan from the common run handle,
which TaskRunner updates through its existing canonical YAML serializer, and
opens the same explicit save confirmation. The browser does not submit
executable YAML copied from its display. Saved task plans then appear in the
same Workflows library and use the same `/workflow <slug>` command. The UI
labels their source as a task plan; it does not ask the user to choose an engine.

The surface uses the existing API client and query cache, localizes all visible
strings through the 12-catalog i18n system, formats dates through locale-aware
helpers, and uses Lucide icons rather than emoji.

### 6.9 TaskRunner composition and the host-side run API

The workflow package exposes a trusted host-side run boundary alongside the
sandboxed Python entrypoint. It registers one ordinary workflow `RunHandle`,
emits through the existing typed event stream, checkpoints through the existing
run store, and binds cancellation to the host coroutine driving the work. Its
operations are intentionally small:

```python
run = workflow_service.begin_host_run(
    name=project.name,
    source=known_plan_yaml_or_empty,
    source_format="task-plan",
    session_key=session_key,
)
run.bind_task(driver_task)
run.phase(title)
run.set_source(canonical_plan_yaml)
await run.step(index=index, label=title, execute=execute_task)
run.log(message)
run.pause()
run.rebind(resume_task)
run.finish(result)
run.fail(error)
```

This is trusted Python called by Kiro Crew itself. It does not enter the
restricted script namespace and does not broaden the DSL available to saved
Python. `step()` wraps TaskRunner's existing task operation: the workflow layer
emits start/finish events, records the settled result, and preserves cancellation
semantics; TaskRunner still performs approvals, retries, tests, self-review,
commits, and replanning inside that operation.

`set_source()` is needed because a direct YAML launch knows its canonical source
at registration time while an LLM-decomposed TaskRunner project does not. The
host run starts in a Planning phase with empty source, then stores the canonical
`plan_to_yaml()` output as soon as decomposition succeeds and after any replan
changes the DAG. Definition provenance separately pins the original saved id and
revision, so a revised effective plan cannot masquerade as the exact saved
revision. Promotion is disabled until canonical source has been set and survived
persistence redaction.

Gateway wiring attaches the shared `WorkflowService` to TaskRunner after both
are constructed. The workflow package does not import TaskRunner. Exact
task-plan invocation calls a narrow registered start port, and promotion calls a
server-side canonical-plan resolver; every ordinary TaskRunner launch opens a
host run through the same service. Non-gateway callers without that optional
integration retain their current behavior in v1.

`Project` persists `workflow_run_id` and optional definition id, slug, and
revision. Its current status remains as a compatibility projection for the Tasks
API and existing `runs.json`; the workflow registry is authoritative for the
common Workflows view. A legacy project without `workflow_run_id` loads exactly
as it does today and is not silently synthesized into historical workflow data.

TaskRunner and workflow terminal transitions are coordinated once. The host run
is finished only after TaskRunner completes its cleanup and durable project
write plus git/worktree finalization. Cancelling from either surface cancels the same bound asyncio task;
TaskRunner maps the cancellation into its project status before the workflow
registry records the terminal event. The common registry adds `paused` as a
non-terminal status. Pausing unbinds the completed driver task without evicting
the run; resume rebinds the new driver to the same handle. On restart, TaskRunner
reconciles a linked project recovered as paused back onto the persisted workflow
handle. The Tasks UI retains its existing resume controls.

Common run snapshots advertise actions as capabilities rather than exposing a
runtime name: Python runs support subtree rerun; linked TaskRunner runs support
pause/resume and retry-from-task. Both may support cancellation while active.
The Workflows UI uses those flags to avoid offering an invalid action.

TaskRunner already owns completion notifications and chat routing. Host runs set
`completion_injection=false`, so the workflow completion callback does not add a
second chat message or auto-turn for the same project. They still broadcast live
events and appear in the Workflows run list. Deleting a finished TaskRunner
project also asks the common registry to delete its linked workflow run. Shared
history publication remains best-effort: a registry failure is logged and never
turns a successful existing TaskRunner deletion into a new product-layer
failure.

## 7. Failure handling

### Corrupt definition file

One unreadable JSON record is logged at debug level and skipped. It does not hide
the rest of the library. The file remains on disk for operator diagnosis.

### Failed durable write

The library writes a temporary file, restricts it to the owner, and atomically
replaces the target. A failed save or update removes the temporary file where
possible and returns a coded error. The prior definition remains intact.

### Stale editor

The API returns `workflow_definition_conflict` with status `409`. The client
refetches the library and disables another save until the user reloads the editor;
it does not silently overwrite the newer revision.

### Invalid source

Create, update, and exact invocation use the validator for the definition's
source format. Invalid Python or an invalid/cyclic TaskRunner YAML DAG never
enters the library or a run.

### Missing definition

HTTP and slash callers receive a clear not-found result. Kiro Crew does not fall
back to intent authoring because that would violate exact-reference semantics.

### Authoring model omits or fabricates lineage

The draft remains valid, but lineage is recorded only when the reported parent
matches a candidate supplied by Kiro Crew. The user may still save the draft as
an independent definition.

### TaskRunner integration is unavailable

A task-plan definition cannot start until the gateway has registered TaskRunner's
host start port. The API returns `workflow_taskrunner_unavailable` with status
`503`; it does not reinterpret the YAML as Python or fall back to model planning.
Ordinary TaskRunner launches keep their existing path if the optional workflow
integration was not wired by a non-gateway host.

### Workflow run persistence fails during a TaskRunner project

Workflow run persistence remains a best-effort mirror and never terminates the
TaskRunner project. TaskRunner's existing fsync-backed project persistence stays
the recovery authority for task-specific state in v1. The common run may lose
recent events on a hard process failure, but project checkpoints, approvals, and
resume behavior retain their existing guarantees.

## 8. Security considerations

### 8.1 Storage and redaction

The library directory is created owner-only and each temporary definition file
is restricted before atomic replacement. File names derive from sanitized
Kiro Crew-generated ids, with a hash suffix if sanitization changes an input, so
a malformed reference cannot escape the library directory.

Credential and exfiltration-url redaction runs recursively before normalization
and persistence, including before a name or explicit slug is lowercased into the
slash-command slug. Redaction runs again at HTTP, MCP read/run, and chat egress boundaries.
A save is rejected when that
redaction would change executable source; silently rewriting a validated Python
module could corrupt it or change its behavior. This is defense in depth; it does
not make workflow source a suitable secret store. Completed-run promotion reads
the original source on the server and therefore rejects a sensitive source rather
than persisting the response-redacted preview. Gateway, authoring, and saved-run
paths offload library disk I/O from the shared event loop and serialize operations
inside the service.

### 8.2 Explicit promotion is a security boundary

Automatically saving successful scripts would retain more user and model text
than necessary and would make transient tool behavior permanently invokable.
WFL2 therefore serves both product clarity and data minimization. Mutation tools
are absent from the model-facing MCP registry; durable promotion and revision
updates require a positively authenticated dashboard-user action. App tokens are
refused even when their manifest allowlists `/api/workflows`. They are also refused
before a saved run accepts `X-Session-Key`, preventing cross-session result injection.
Authorization denials are SEL-audited.

### 8.3 Saved source is not trusted code

A saved definition does not bypass workflow validation, tool governance,
approval, budgets, timeouts, or the workflow runner's restricted namespace.
Exact means “do not reinterpret the orchestration source,” not “skip execution
policy.”

A task-plan definition is parsed as data with `yaml.safe_load`, the existing
size, key, task-count, dependency, and acyclic-graph checks, and TaskRunner's
normal approval and governance path. It never reaches `exec` and never receives
the Python workflow namespace. The host-side run API is callable only by trusted
Kiro Crew code and is not exposed as an MCP or dashboard mutation surface.

### 8.4 Local examples are prompt input

Saved workflow source is user-owned local content, but it can still contain
adversarial or obsolete instructions. Matching is bounded to three records and
bounded source length; the system prompt identifies them as examples rather than
authority. Lineage is verified structurally after generation. No match can edit
itself or another definition during authoring.

### 8.5 Harness parity

The feature does not add a new `agent.provider` value or infer Kiro identity from
the absence of another harness. Library and command behavior are Kiro Crew-owned.
Any future harness opts into existing workflow execution seams; it does not gain
a special storage directory, sandbox waiver, or reinterpretation path.

## 9. Backward compatibility

- Existing version 1 definition files load as `format="python"`; new writes use
  schema version 2.
- Existing dynamic run JSON files and rehydration remain unchanged.
- Existing `workflow_run` calls using `source` or `intent` remain valid.
- Existing `/workflows` behavior remains separate; it manages run history, while
  `/workflow` manages and invokes reusable definitions.
- An empty library changes no authoring output except the bounded prompt note
  that no saved local workflow matched.
- No migration of run snapshots into definitions occurs. Promotion is opt-in.
- Existing TaskRunner `runs.json` projects load without a workflow run link.
  New linked projects continue writing the legacy status projection so the
  Tasks API, CLI, Slack, and restart recovery remain compatible.
- Existing TaskRunner start, cancel, pause, execute, retry, and delete routes
  retain their request and response contracts. When workflow integration is
  present, they operate on the linked common run as part of the same action.
- A configured `workflows.dir` continues to own run snapshots only; saved
  definitions use the fixed `<KIROCREW_HOME>/workflow_library` trust root.
- Kiro remains the default first-class harness. Adapted harnesses consume the
  same service without changing Kiro's path.

## 10. Migration plan

The phases are independently testable. They may land together, but no later
phase changes the authority established by an earlier one.

### Phase 1 — definition storage and service boundary

- Add `WorkflowDefinitionLibrary` under the workflow package.
- Add stable ids, unique slugs, revisions, lineage, local search, atomic writes,
  owner-only permissions, and redaction.
- Add list/get/save/update/start-definition methods to `WorkflowService`.
- Add bounded local matches to intent authoring.

**Exit criteria:** library round trips survive a new object instance; identical
source may create separate lineaged identities; stale revision writes fail;
debugging intent ranks a debugging definition first; exact Python start passes
saved source and input through the existing runner.

### Phase 2 — transport and slash contracts

- Add definition CRUD/run HTTP routes with coded errors.
- Add stateless MCP list and exact-run operations and schemas; keep save and
  update dashboard-only.
- Register and locally dispatch `/workflow` before harness acquisition.

**Exit criteria:** API create/list/get/update/run tests pass; all new non-2xx
responses carry codes; MCP schema coverage includes every new handler; bare
slash lists definitions; named slash passes exact source and free-form input;
harness-parity gate remains green.

### Phase 3 — Agent Capabilities management surface

- Add the Workflows tab and localized catalog entries.
- Support search, intent authoring, unsaved draft review, explicit promotion,
  source/metadata editing, revision save, lineage display, and exact run.
- Add `/workflow` to slash-command discovery and fallback menus.

**Exit criteria:** component tests cover list, draft promotion, edit, and run
behavior; all 12 catalogs pass parity and changed-value checks; TypeScript,
ESLint, and the production build pass.

### Phase 4 — contract hardening and documentation

- Update the workflow system specification and architecture layering contract.
- Register new redaction sinks and persistence allowlist entries.
- Add this RFC and index it.
- Run focused backend, security-posture, error-code, docs, i18n, harness, and
  frontend verification.

**Exit criteria:** documentation lint, diff checks, formatting, flake8, mypy,
security-posture tests, error-code tests, focused backend tests, frontend tests,
and production build pass. Any unrelated environment failure is named rather
than hidden by rerunning.

### Phase 5 — TaskRunner composition v1

- Add definition schema version 2 with immutable `python` and `task-plan` source
  formats; read version 1 as Python.
- Add the trusted host-side workflow run API over the existing registry, events,
  store, and cancellation task binding.
- Attach new gateway TaskRunner projects to host workflow runs and persist their
  common run link plus optional saved-definition provenance.
- Wrap TaskRunner task execution in workflow phases and step events without
  moving its project policies into the workflow kernel.
- Validate, save, edit, and invoke TaskRunner YAML definitions through the global
  library and `/workflow` namespace.
- Add explicit **Save workflow** promotion to TaskRunner plan/completion UI and
  render linked TaskRunner progress in the existing Workflows run surface.

**Exit criteria:** one TaskRunner launch produces one linked workflow run; both
surfaces show the same live task transitions; cancellation from either surface
cancels the same task; pause/resume preserves the run link; a saved task plan
runs by slash name through TaskRunner; legacy projects and Python definitions
remain readable; focused backend and frontend tests plus all repository gates
pass.

## 11. Testing strategy

| Area | Required evidence |
|---|---|
| Storage | Create/read, unique slug, separate identity for identical source, immutable revisions, stale conflict, corrupt-file isolation |
| Matching | Relevant deterministic ranking, bounded candidate count, no false lineage acceptance |
| Service | Validation before save/update, exact saved source, free-form input mapping, existing run persistence unchanged |
| HTTP | All routes, malformed bodies, unavailable service, not found, conflict, durable I/O failure, machine-readable codes |
| MCP | Handler registry/schema parity, mutation tools absent, transport paths, exact workflow precedence |
| Slash | Parsing, bare list, exact named execution, no harness forwarding |
| UI | List/search, draft authoring, explicit management or completed-session save, edit as revision, lineage display, run feedback |
| Definition formats | Version 1 defaults to Python, task-plan YAML validation, immutable format, format-preserving revisions |
| Host run API | Event order, step result checkpoint, one terminal transition, bound-task cancellation, pause/rebind |
| TaskRunner composition | One linked run, planning/task projection, canonical-source update, persisted provenance, shared cancellation, pause/resume/restart reconciliation, deletion, legacy project load |
| Task-plan invocation | Exact saved YAML, input as run context, unavailable-port failure, slash/API/MCP parity |
| TaskRunner UI | Explicit plan promotion, common library appearance, task-plan editing, capability-driven actions, linked run navigation, no duplicate completion card |
| Cross-cutting | Security posture, redaction sink coverage, workflow architecture, i18n, harness parity, formatting, typing, build |

## 12. Alternatives considered

### 12.1 Automatically promote every authored workflow

Rejected. It erases the distinction between iteration evidence and a reusable
capability, creates a noisy command library, and persists content without an
explicit retention decision. Automatic run snapshots already preserve the work
needed for same-session and post-restart iteration.

### 12.2 Store definitions as repository `.py` files

Rejected for this version. Repository files are attractive for review and
version control, but the settled scope is global to the Kiro Crew user and shared
across harnesses. A JSON envelope keeps source, revision history, lineage, and
metadata atomic. Exporting a definition into a repository can be added later
without changing the library identity.

### 12.3 Support both repository and user-global libraries immediately

Rejected as premature. Two scopes require precedence, shadowing, rename,
write-target, and UI-origin rules. The user selected global scope. One authority
keeps lookup and exact invocation unambiguous.

### 12.4 Adapt by editing the matched definition

Rejected. A debugging workflow used as an example must not change because a new
login-specific workflow was requested. Copy-on-adapt with parent revision
lineage preserves both stability and provenance.

### 12.5 Ask the harness to interpret `/workflow <name>`

Rejected. Different harnesses could resolve or rewrite the request differently,
and a harness without workflow awareness could treat it as ordinary prose. Local
resolution is the only contract that guarantees exact execution.

### 12.6 Register every saved workflow as a top-level slash command

Rejected. It creates collision and discovery problems and makes a rename alter
the global command vocabulary. `/workflow <slug>` provides one stable namespace.

### 12.7 Use embeddings for adaptation lookup

Deferred. Embeddings may improve recall in a large library, but deterministic
lexical matching is local, explainable, inexpensive, and adequate for the first
version. The service boundary permits replacing the ranker later without
changing definition identity or execution.

### 12.8 Model TaskRunner and dynamic workflows as peer engines

Rejected. An `engine` selector leaks the current implementation split into every
definition, transport, and UI. It also leaves two lifecycle authorities and
makes later convergence a breaking migration. TaskRunner is instead a workflow
application with additional project guarantees.

### 12.9 Compile TaskRunner plans into sandboxed Python

Rejected. Generated Python would have to reproduce approval gates, retry and
replan policy, tests, git transactions, and checkpoint recovery inside a
restricted language that deliberately cannot access those facilities. The
host-side run API shares lifecycle and events without weakening the sandbox or
duplicating TaskRunner policy.

## 13. Open questions and future work

None of these block the current design:

1. Should a future export action materialize a saved definition as a
   repository-owned file suitable for review and commit?
2. Should exact invocation eventually accept an explicit historical revision,
   such as `/workflow debug-login-flow@2`?
3. Should deletion be permanent, soft-delete, or archive-only once external
   references can point at stable workflow ids?
4. At what library size does lexical search need an indexed or embedding-backed
   implementation?
5. Should lineage become navigable as a graph rather than the current direct
   parent reference?
6. When all callers can consume the shared run projection, which TaskRunner
   fields can stop being mirrored in `runs.json` without weakening recovery?
7. Should non-gateway TaskRunner hosts construct a local workflow service by
   default after the gateway integration has proved stable?

Any addition of repository scope must specify precedence and write-target rules
in an RFC update before implementation. Any deletion design must preserve the
meaning of lineage and completed run snapshots.

## 14. Decision

Adopt the two-layer model:

- Run snapshots are automatic and execution-scoped.
- Reusable definitions are global and explicitly promoted.
- New intent authoring may adapt bounded local matches into separate lineaged
  drafts.
- Explicit saved references execute exact source through Kiro Crew.
- `/workflow <name> [input]` is the cross-harness invocation contract.
- Agent Capabilities → Workflows is the human management surface.
- The workflow system owns the common run substrate and definition identity.
- TaskRunner composes planning, approvals, quality gates, and project delivery
  around a host-side workflow run.
- Python and TaskRunner YAML are source formats, not selectable engines.
- New gateway TaskRunner projects link to one authoritative workflow run and
  share its cancellation path while legacy TaskRunner storage remains a
  compatibility mirror.

This keeps iteration cheap, durable reuse intentional, provenance visible, and
harness behavior consistent while making TaskRunner a strict product-layer
superset of the workflow system.
