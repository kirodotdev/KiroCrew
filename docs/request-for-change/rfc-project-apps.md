---
title: Project Apps — a React app built to order for each project, driving its crew through a gated Crew API
status: in-progress
author: Zedmor, with Claude Code
created: 2026-11-05
last-audited: 2026-11-05
audited-at: d4c35f448f
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Project Apps

**Status:** in-progress (2026-11-05), on branch `feat/project-apps` (base `d4c35f448f`) and the external host-app repository; nothing is on main. Phase 0's verdict deferred the generated-bundle frame point (§6.3): v1 shows a project app through `/api/sandbox-doc`, and the pane brokers every Crew API call. Phase 1 (`contributes.sessionKinds`, with the bound slot's approvals drawn natively over the app pane) is built in core. Phases 3–6 are built in the host app: the pane, the Crew API backend, the conductor's tools and playbook, the SDK and starter, and the custom, tracker and wizard blueprints. All of it is verified end to end in an isolated pod: create, intake, spec, approve, build, a confirmed write, pick-and-ask, rebuild. Claims about main are measured at `d4c35f448f`; claims about the non-linear session surfaces at the `feat/docwork` head `9d5ac353be`, which is not on main.

## 1. Summary

When a person starts a piece of work ("let's work on the open issues in my
repository", "help me do my taxes", "I want to work on my app's UI"), the
conductor builds a **project app**: a real React application shaped for that one
project: an issue tracker, a TurboTax-like guided return, a canvas of the app's
screens. The person then works in that app: they edit the project's data, start
and steer workers on parts of it, and pick any element on it to talk to the
conductor about it.

A project app is made from two inputs:

1. **A project theme**, resolved to a **blueprint**: what kind of work this is,
   and so which entities, screens, workflows and default goals the app starts from.
2. **The Crew API**: a small, typed, versioned set of operations the generated app
   may call (read and patch the project's data, start, read, steer and stop
   workers, ask the conductor). It is the only way the app can affect anything.

The generated code is treated as untrusted. It runs in an opaque-origin frame
with no network beyond its own capability-checked calls. Every write it asks for
needs a confirmation that the person gives outside the frame.

Following tenet 8, the product ships as an App Kit app from its own repository.
Core gains only two generic seams it does not have today: an app-drawn session
pane, and a frame point that serves app-generated bundles.

## 2. Motivation

### 2.1 Current state

- **Non-linear sessions** (`docs/architecture/non-linear-sessions.md` on
  `feat/docwork`) drive one session from a surface instead of a chat: a doc, a
  board, a codebase model or a dashboard. The **dashboard** surface is the
  closest thing to a project app, and it shows the limits:
  - The page is one HTML string that the conductor rewrites with `page-html` /
    `page-replace` blocks, which the browser applies
    (`website/src/pages/chat/page/dashboard/dashboardModel.ts`, `pageChangesIn`).
  - It has no network and no way back into Crew. Its CSP sets
    `connect-src 'none'` (`dashboard/pageFrame.ts`, `CSP`), and the conductor is
    told to "put the data in the page itself"
    (`dashboard/dashboardTurn.ts`).
  - The page talks to its pane only to pick elements: `hover`, `pick`, `rects`
    and `key` messages (`pageFrame.ts`, `FrameMessage`). It can show work but
    cannot start any.
  - A conductor may open only doc and board children. "A codebase or dashboard
    page needs inputs a starting text cannot give"
    (`src/kiro_crew/dashboard/doc_child.py`, `DOC_CHILD_KINDS`).
- **App Kit** renders an app's pages in process. `ui.entry` and `ui.pages` are
  ESM modules loaded with `import()` (`importAppBundle` in
  `website/src/components/appBundleImport.ts`).
  The trust model says the frontend API scoping "is a guardrail, not a boundary",
  and that an embedded app "could call `fetch('/api/anything')` directly and
  succeed" (`docs/architecture/app-platform-trust-model.md`, "The frontend
  `useAppApi` / `useAppEvents` scoping is a guardrail, not a boundary").
- **App Kit frame points** (§18a/§18b of `docs/system-specs/modules/app-kit-platform.md`,
  owned by `src/kiro_crew/apps/app_frame.py`) are the other half of App Kit, and
  where untrusted UI already runs:
  - The frame is an opaque origin, served from the gateway's own port with
    `Content-Security-Policy: sandbox allow-scripts` (`frame_csp`).
  - Its only fetch is `/app-frame-fetch/{name}/{path}`. That route answers only
    a request carrying a per-frame `X-KiroCrew-Capability`, and only a person's
    dashboard session may mint one. It is bound to app, client, frame id and
    `(point, id)` for 15 minutes (`_CAPABILITY_TTL_SECS`). It reaches only the
    app's own registered routes or its process backend.
  - **The entry is always an ESM module under the app's shipped `ui/`
    directory**: `FrameEntry` is `(entry relative to ui/, process_backed, app name)`,
    and `_frame_html` serves it from `/apps/{name}/ui/{entry}`.
- **MCP Apps** (`docs/system-specs/modules/mcp-apps.md`) render a tool result's UI
  in a null-origin `srcdoc` frame with `sandbox="allow-scripts allow-forms"`
  (`website/src/components/McpAppFrame.tsx`). They gate every `tools/call` through
  `POST /api/mcp-apps/call` (`_gate_app_capability_request` in
  `src/kiro_crew/dashboard/handlers/mcp_apps.py`, then `handle_app_call` in
  `src/kiro_crew/mcp_gateway/app_call.py`):
  - the caller must be the owner, and the call is bound to the session that
    produced the app;
  - the capability is compared in constant time (`hmac.compare_digest`);
  - `_meta.ui.visibility` is enforced (`_tool_visible_to_app`);
  - arguments are validated against the schema (`validate_mcp_tool_arguments`);
  - the governance `mcp` scope applies (`_governance_denial`), the call runs as
    `CallerContext(session_type="mcp-app")`, and every decision is SEL-audited
    (`_audit`).

  The spool record a rendered app lives on expires after 24 hours
  (`SPOOL_TTL_SECS` in `src/kiro_crew/mcp_apps_render.py`).
- **Session control is internal-only.** "Every route registered under
  /api/session-control MUST appear" in the strict internal set
  (`_STRICT_INTERNAL_API_PATHS` in `src/kiro_crew/dashboard/server_internal_paths.py`).
  The conductor's work-ledger board is readable only by the dashboard owner
  (`api_work_ledger_board` and `api_work_ledger_board_action` in
  `src/kiro_crew/dashboard/handlers/work_ledger_board.py`).
- **Connectors are descriptors only.** The GitHub vendor module "builds no
  request, holds no credential, and performs no I/O"
  (`docs/system-specs/modules/connector-github.md`). There is no
  browser-callable `github.issues.list`.
- **No declarative or generative UI layer exists.**
  `rfc-composable-layout-mechanism.md` (`in-progress`) reviews json-render, A2UI
  and MCP-UI in its §9.2 as inspiration, not adoption.

### 2.2 Problems

1. **The surface is generic; the work is not.** A doc or a board can hold any
   plan, so it fits no particular one. Taxes want a guided interview with forms
   and a document inbox. A repository's issues want a sortable list with a
   status and a worker per row. Today the person draws that shape by hand, or
   the conductor writes a static HTML page that cannot act.
2. **A page cannot act.** The dashboard page cannot start a worker on the row
   the person is looking at, or save an edit to the project's data. Every action
   goes back through a typed ask to the conductor.
3. **Code and data are one string.** Refreshing one number rewrites the page,
   which is capped at 80,000 characters per turn (`dashboardTurn.ts`,
   `PAGE_CAP`), and every refresh is a full page diff.
4. **No host is right for agent-written code with authority.** App Kit's
   in-process UI carries the owner's cookie, and its frame points serve only
   shipped `ui/` modules. MCP Apps are tied to one tool result's 24-hour spool,
   not to a long-lived project.

## 3. Goals

- G1. From a request in plain words, the conductor proposes a project app (a
  blueprint, an app spec, the Crew API operations it needs), and after the person
  approves it, builds it without hand edits.
- G2. The app is a real React + TypeScript application, built per project and
  versioned, so any build can be undone.
- G3. The app reads and writes the project's data, and starts, reads, steers and
  stops workers tied to an element (an issue row, a tax form, a screen region),
  only through the Crew API.
- G4. The person can pick any element of the app and talk to the conductor about
  it, with the thread and goal conventions of non-linear sessions.
- G5. Generated code never gains authority beyond its project's approved Crew API
  operations, and never performs a write the person did not confirm outside the
  frame.
- G6. Core changes are generic App Kit seams that any app could use, with no
  project-app-specific code in core.

## 4. Non-goals

- Not a general app store for generated apps. Turning a project app into a
  reviewed App Kit app is a later, human-driven step (§9.1).
- No live connector calls from the frame in v1. External data (GitHub, a tax
  form, a web page) is fetched by the conductor and its workers with their own
  tools and written into the project's data. A typed connector layer is deferred
  to `connector-capability-manifest.md`.
- No running a target application's dev server inside a frame. The screen canvas
  works from screenshots (§6.8).
- No change to any existing iframe host's sandbox, to App Kit's in-process trust
  model, or to the strict internal paths.
- No tax, legal or financial advice product. The tax example is a shape, not a
  promise about correctness.

## 5. Terms

| Term | Meaning |
|---|---|
| **Project** | One piece of work: one conductor session of kind `projectApp`, its data, its app and its workers. |
| **Blueprint** | The project theme made concrete: a starter source tree plus `blueprint.json` (entities, screens, operations, default goals, a `when` description). |
| **App spec** | What the conductor proposes for one project: chosen blueprint, data schema, screens, operation list. Approved by the person before a build. |
| **Project app** | The generated React app for one project, built to one HTML document per version. |
| **Host app** | The App Kit app this RFC ships ("Project Apps"): the session pane, the Crew API backend, the builder skill and the blueprints. |
| **Crew API** | The typed operations a project app may call, served by the host app's backend. |

## 6. Design

### 6.1 Where each part lives

| Part | Lives in | Trust |
|---|---|---|
| Session pane seam (§6.2), generated-bundle frame point (§6.3) | core | trust boundary, generic |
| Host app: pane chrome, Crew API backend, builder skill, blueprints | own repository, listed in the official registry | first-party app, App Kit trust model |
| Project apps (generated code, per-project data) | the host app's data directory | **untrusted** |

This follows tenet 8 ("anything that renders or interprets is an app", `TENETS.md`)
and the `no-new-builtin-apps` rule: core keeps the frame boundary and the
capability check, and everything that renders ships as an app.

### 6.2 Core seam 1: an app-drawn session pane (`contributes.sessionKinds`)

An app declares a kind of session whose main pane it draws:

```json
"contributes": {
  "sessionKinds": [{
    "id": "project",
    "title": "Project app",
    "menuLabel": "New project app",
    "icon": "AppWindow",
    "entry": "dist/project-pane.mjs",
    "defaultAgent": "kirocrew-conductor"
  }]
}
```

- The sidebar's create menu lists each enabled kind next to New doc and New board.
- The slot is bound to `{ app, kind, object }` and persisted in the session's
  metadata line, as the `doc` binding is on `feat/docwork` (`_ChatSlot._doc`).
  An idle session comes back as its kind after a restart.
- The pane mounts `entry` in process, the same way `contributes.panelTabs`
  already mounts a body-owning tab (`docs/app-kit/manifest-reference.md`, "`contributes.panelTabs` — Chat side-panel tabs").
  The pane is the host app's own reviewed code, so it carries the same authority
  as any app page.
- When the app is uninstalled or disabled, the session falls back to its
  transcript, read-only, with a notice. Nothing is deleted.

This is also the seam the four `feat/docwork` surfaces would need to leave core,
which tenet 8 asks of them (Open question Q6).

### 6.3 Core seam 2: a frame point for generated bundles

> **Phase 0 verdict (2026-11-05): deferred; v1 needs no core frame point.** The
> dashboard already has a generic way to show bytes the authenticated dashboard
> holds: `POST /api/sandbox-doc` (`api_stash_sandbox_doc` in
> `src/kiro_crew/dashboard/handlers/sandbox_doc.py`) stashes a document of up to
> 8 MiB, and `serve_sandbox_doc` serves it once as an opaque origin under
> `Content-Security-Policy: sandbox allow-scripts; connect-src 'none'; …`. The
> host app's pane fetches a version's HTML from its own backend and wraps it
> before stashing. The wrapper's first bytes are a meta CSP the pane writes
> (`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
> img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'`),
> then the theme variables and the pick helper. CSP policies only intersect, so
> generated bytes after it cannot loosen it. The frame therefore has **no network
> at all**, which is tighter than the point below. The point is kept as the
> design for when a project app needs to fetch from its own routes.

The host app shows a project app in a §18b frame. The point resolves its entry
from the app's **data** directory, not its shipped `ui/` directory:

- `register_frame_point` gains a data-backed resolver form. It returns a path
  inside `<app data>/projects/<project id>/versions/<n>/` with strict containment
  (no `..`, no symlink out, the version directory frozen once published), served
  under a new `/app-frame-data/{name}/{project}/{version}/…` prefix.
- The point id is the project id. A frame minted for project A cannot load
  project B's bundle, because the frame token already binds `(point, id)`.
- **A tighter policy than the shared `frame_csp`.** The shared policy allows
  `img-src 'self' https: data: blob:` (`frame_csp`) so link-preview cards
  can show avatars. A generated app holding tax data could use that to leak it
  in an image URL. This point uses `img-src 'self' data: blob:` and
  `connect-src 'self'`, with every other directive as in `frame_csp`. This is a
  new host with its own row in `docs/architecture/dashboard-iframe-hosts.md`;
  no existing host is widened.

### 6.4 The Crew API

The host app's backend serves the Crew API as its own routes. In v1 the project
app cannot reach them at all (§6.3 verdict). It posts each call to the pane over
the `pa/1` postMessage protocol, and the pane, which is the host app's own
reviewed code, brokers it: it checks the allowlist, draws the confirmation for a
write, then calls the backend itself. (With the deferred frame point, the call
would instead go through the frame's capability fetch, `ctx.fetch` →
`/app-frame-fetch/{name}/api/crew/…` (§18a), and would need the tickets below.)
The protocol is fixed in the host app's `CONTRACT.md`. Each operation has an id, an
`effect` (`read` or `write`), versioned input and output JSON schemas, and the
core call it makes with the host app's deny-by-default token (`permissions.*`).

| Operation | Effect | Does |
|---|---|---|
| `data.get(path)` / `data.subscribe(path)` | read | The project's data store: JSON documents in the host app's data directory, each with a version stamp (the SHA-256 scheme of `dashboard/doc_files.py`). |
| `data.patch(ops, version)` | write | JSON Patch against a version; a stale version is refused with the current document. |
| `files.list(dir)` / `files.read(path)` | read | Inside the project's folder only. |
| `files.write(path, bytes)` | write | Same containment. |
| `workers.list()` / `workers.status(id)` / `workers.read(id, since)` | read | Workers this project's conductor owns. |
| `workers.spawn(anchor, brief)` | write | Starts a worker tied to an element id (`issue-142`), records it on the conductor's ledger, binds it to the anchor. |
| `workers.steer(id, text)` / `workers.stop(id)` | write | Only for workers this project spawned. |
| `conductor.ask(text, anchor?)` | write | Starts a turn in the project's conductor session, injected as app-authored text, never as the person's words, the rule MCP Apps' `ui/message` already follows (`MCP_APP_MESSAGE_KIND` in `src/kiro_crew/dashboard/chat_utils_injected_rows.py`), rate-limited. |
| `ui.open(target)` | read | Asks the pane to open a session, file or link; the pane follows the link-bridge rule (a real click in that frame). |

Rules:

- **Per-project allowlist.** The app spec names the operations the project uses.
  The backend refuses any other operation for that project id. Adding one later
  is a widening that the person approves again, following the staged-grant pattern
  App Kit uses for link-preview hosts (§18a "Grant").
- **Confirmation for writes.** In v1 the pane confirms before it forwards, and
  the frame has no route to the backend to bypass it, so no ticket is needed. The
  ticket rule below applies once the frame point lands.
- **Confirmation tickets for writes (with the frame point).** The frame cannot complete a `write` alone.
  It posts the request to the pane, and the pane draws the confirmation outside
  the frame, showing the exact operation and arguments (a spawn shows the full
  brief). Only after the person confirms does the pane ask the backend for a
  ticket, single-use and bound to `(project, operation, SHA-256 of the
  arguments)`, valid for 60 seconds. The frame's call must carry it. A
  `data.patch` the person makes by typing into a form can be confirmed for the
  rest of the session, per operation, with a toggle the pane draws. A spawn or
  steer is confirmed every time.
- **Governance unchanged.** A spawned worker is an ordinary session under the
  `POLICY ∩ PROFILE` ceiling, with the project's approval mode. Its approvals are
  answered in native host controls, never by the frame.

### 6.5 Blueprints: the project theme

`blueprint.json` plus a starter tree:

```json
{
  "id": "tracker",
  "version": 1,
  "when": "Work on a list of items that each move through states: issues, tickets, leads, bugs.",
  "entities": { "$ref": "schema/entities.json" },
  "screens": ["list", "board", "detail"],
  "ops": ["data.get", "data.patch", "workers.list",
                 "workers.read", "workers.spawn", "workers.steer", "workers.stop", "conductor.ask", "ui.open"],
  "goals": [{ "section": "items", "text": "Keep every item's status current", "every": 10 }]
}
```

- The conductor matches the request against each blueprint's `when`. If none
  fits, it starts from `custom`: the generic starter and an empty schema.
- Blueprints ship in the host app's repository; others can come from the
  registry or the person's own folder.
- The starter tree is a Vite + React + TypeScript project with a committed
  lockfile, the project-app SDK (§6.7) and the base kit.

### 6.6 The build loop

1. **Intake.** The project's conductor (Ultra by default) loads the
   `project-app-builder` skill and produces an app spec: blueprint, data schema,
   screens, operations, first data source. It shows the spec in the main thread
   and waits for approval.
2. **Seed data.** The conductor or its workers gather the first data with their
   own tools (`gh` for a repository, documents the person drops in for taxes) and
   write it with `data.patch` through the host app's MCP tools (below).
3. **Build.** A builder worker works in `<app data>/projects/<id>/app/`, a git
   repository:
   - `npm ci --offline --ignore-scripts` from the blueprint's lockfile;
   - generate the code;
   - `npm run build`, producing one HTML document with the bundle inlined;
   - `tsc --noEmit`;
   - a headless smoke render: it mounts, logs no console error, and calls no
     operation outside the spec;
   - publish as `versions/<n>/`. The pane reloads the frame on the new version.
4. **Iterate.** The person picks an element and asks. The thread follows the
   non-linear session conventions (`[Doc ask <id>]` tags, `deriveThreads`). The
   builder edits the source, rebuilds and publishes the next version. Undo
   republishes an earlier version, and the git history keeps the source.
5. **Goals refresh data, not code.** A goal on a screen or entity is an
   auto-nudge loop that patches data. The app re-renders from `data.subscribe`
   without a build.

The conductor's capabilities (spec, build, publish, data patch) ship as MCP
tools from the host app (App Kit `resources`), stateless, with all state in the
host app's data directory, as `docs/architecture/mcp.md` requires.

**Dependencies.** A package outside the blueprint's lockfile and the host app's
approved list is a widening. The builder stops and asks; it never installs from
the network on its own.

### 6.7 The project-app SDK

`@kirocrew/project-app-sdk` is bundled into each project app at build time, not
loaded at runtime:

- `CrewProvider` runs the frame protocol: the pick, hover and rect reporting
  ported from `pageFrame.ts`'s helper, plus Crew API calls with their tickets.
- Hooks: `useData(path)`, `usePatch(path)`, `useWorkers({ anchor })`, `useAsk()`.
- Components: `<Pickable id>`, which gives an element the stable `data-id` that
  picking, threads and anchors use, and `<WorkerBadge anchor>`, which draws a
  worker's live state from `workers.status`. This replaces the HTML status
  overlay with structured status, the option the features doc names under
  "Status overlay".
- A base kit (Table, Kanban, Wizard, Form, DocumentInbox, Timeline) styled from
  the host theme's CSS variables. The model writes the project's logic, not the
  plumbing, which bounds both token cost and visual drift.

### 6.8 Worked examples

- **"Let's work on the open issues in my repository" → `tracker`.** List, board
  and detail screens over `items[]`. Each row has a Start worker button that
  calls `workers.spawn(anchor="issue-142", brief)`. Its `<WorkerBadge>` follows
  the worker, and the detail view shows `workers.read` and the PR link the worker
  reports. The goal "keep statuses current" patches `items[]` every 10 minutes.
  The built-in Issue Radar app (`docs/system-specs/modules/issue-radar.md`) is the
  domain reference.
- **"Help me do my taxes" → `wizard`.** An interview of steps over `return`,
  `incomes[]`, `deductions[]` and `documents[]`, a document inbox in the project
  folder, and a summary. Workers do bounded jobs ("extract the fields of this
  W-2", "check whether this deduction applies") and patch the data, each confirmed
  as a spawn. The data stays in the data home, and the frame has no network beyond
  the gateway.
- **"I want to work on my app's UI" → `screens`.** A canvas of the target app's
  routes as frames. Each frame is a screenshot taken by a pod with Playwright
  (the `pod-e2e` harness). Picking a region spawns a worker with the route, the
  crop and the component files, mapped through the `/api/codebase/scan` model on
  `feat/docwork`. The worker changes the target repository in its own worktree.
  Its report triggers a re-capture, shown as a before/after pair.

## 7. Migration plan

Each phase is PR-sized and can ship or be abandoned on its own.

### Phase 0 — Probe (no product code)

Answer Q1 and Q2 with a throwaway host app in a pod, and write each verdict into
§10 with `file:line`.

- Exit: both verdicts recorded here. If either is "needs a new core route", that
  route is named and added to Phase 1 or Phase 2.

### Phase 1 — Core: `contributes.sessionKinds`

- The manifest key, its validation, and the create-menu rows.
- The slot binding `{app, kind, object}` in the metadata codec.
- The in-process pane mount and the fallback when the app is uninstalled.
- An example under `docs/app-kit/examples/`.
- Exit:
  - the example app creates a session of its kind from the menu;
  - the session comes back as that kind after a gateway restart and a reload;
  - disabling the app shows the transcript read-only and deletes nothing;
  - the binding round-trips in `test_slot_metadata_codec.py`.

### Phase 2 — Core: the generated-bundle frame point

- The data-backed resolver, the `/app-frame-data/` prefix, and the point's
  tighter CSP.
- Its row in `dashboard-iframe-hosts.md`.
- Exit:
  - a frame minted for project A gets 403 for project B's version path and for
    any path outside `versions/`, symlinks included;
  - the response policy has no `https:` source in `img-src`;
  - the frame's fetch reaches only the app's own routes.

### Phase 3 — Host app: read-only

In the host app's repository:

- the `projectApp` session kind and its pane: frame, pick and ask, threads, main
  thread;
- the read operations of the Crew API;
- the `tracker` blueprint with a hand-written app.
- Exit:
  - a person creates a project app and sees the tracker;
  - picking a row and asking produces an answer in that row's thread;
  - every `write` operation returns 403 because the backend has no route for it.

### Phase 4 — Host app: writes with tickets

- `data.patch`, `files.write`, `workers.spawn`, `workers.steer`, `workers.stop`
  and `conductor.ask`, with the pane's confirmations and the tickets.
- Exit:
  - a write without a ticket is refused;
  - a ticket is refused when reused, after 60 seconds, or with different
    arguments;
  - a spawned worker appears on the conductor's ledger bound to its anchor, and
    its badge shows its state;
  - an operation outside the project's allowlist is refused even with a ticket.

### Phase 5 — Host app: the builder

- The `project-app-builder` skill, spec approval, the build loop of §6.6,
  versions and undo.
- Exit, in a pod with a fixture repository holding three open issues:
  - "let's work on the open issues" yields an approved spec and a tracker that
    lists all three, with no hand edits;
  - a second ask that changes one component leaves every other source file
    byte-identical;
  - undo republishes the previous version;
  - a build that needs an unapproved package stops and asks.

### Phase 6 — Blueprints

- `wizard` and `screens`, each with a pod scenario.
- Exit, per blueprint: its scenario passes with `KIROCREW_E2E_REQUIRE=1`.

## 8. Backward compatibility

Additive throughout:

- No existing frame host, App Kit point, permission or route changes behaviour.
- `contributes.sessionKinds` and the new frame point are optional manifest
  keys; a manifest without them installs as today.
- Sessions of a removed kind remain readable as transcripts.
- The `feat/docwork` surfaces are untouched by this RFC; moving them onto
  `sessionKinds` is a separate decision (Q6).

## 9. Security considerations

**Threat model.** Project data is attacker-reachable: an issue body, a PDF, a
web page the conductor read. Text in it can steer the conductor or the builder
into writing hostile app code. The design therefore assumes **every project app
is hostile**, and puts every check outside it:

| Concern | Control |
|---|---|
| The app reads the dashboard, its cookie or its storage | Opaque origin (`sandbox allow-scripts`, no `allow-same-origin`). The app is never mounted in process. |
| The app sends data off the machine | v1: the sandbox document's `connect-src 'none'` plus the pane's meta CSP (`default-src 'none'`, `img-src data: blob:`, `frame-src 'none'`, `form-action 'none'`), so fetch, XHR, images, fonts, frames and forms reach nothing. Two channels remain that CSP does not govern; see **Residual risks** below. |
| The app calls an operation it was not given | A per-project allowlist enforced by the host backend, on top of App Kit's deny-by-default app token. |
| The app performs a write the person did not intend | A single-use ticket bound to the operation and its arguments, minted only after a confirmation drawn outside the frame. |
| The app spawns a worker with an injected brief | Every spawn is confirmed with the full brief shown. The worker runs under the governance ceiling and the project's approval mode, and its approvals are answered natively. |
| The app answers an approval | Never possible: approvals stay native, as `docs/system-specs/modules/artifacts.md` already requires of sandboxed pages. |
| Dependency supply chain | Offline install from the blueprint's lockfile with `--ignore-scripts`. Any new package is a widening that needs approval. |
| A build step does something harmful | The build is an ordinary worker's commands, under `sandbox.py` and the tool gate like any other. |
| A frame loads another project's bundle | The frame token binds `(point, id)`, with id = project id, plus path containment (§6.3). |
| Session control reached from a browser | Not reached: the host backend uses App Kit's app-token routes, and the strict internal paths are not touched. |

**Residual risks (found in Phase 0; not closed in v1).**

- **Self-navigation.** A sandboxed frame may navigate itself (`location = …`).
  The navigation is checked against the *embedding* document's `frame-src`, and
  the dashboard's allows `https://*.cloudfront.net` (`_BASE_CSP` in
  `src/kiro_crew/dashboard/server.py`), which anyone can host. A hostile app
  could put data in that URL before the pane notices. v1 mitigation: the pane
  treats any second `load` of the frame as a navigation away and removes the
  frame (the rule `loadMintedFrame` already applies to App Kit frames), which
  limits the leak to one URL. Closing it needs an intermediate same-origin shell
  document whose own `frame-src` admits only the project app.
- **WebRTC.** `connect-src` does not govern `RTCPeerConnection`, so ICE/STUN
  traffic is an outbound channel in current browsers. No CSP directive closes it.
  Closing it needs a browser-level control (a `webrtc 'block'` CSP directive
  where supported, or a Permissions-Policy on the frame) and is tracked as an
  open question.

Until both are closed, a project app must be treated as able to leak what it
can read, by those two channels. A project holding sensitive data (the tax
example) should be told so in the pane.

The host app's own in-process pane carries the trust any installed app carries
(`app-platform-trust-model.md`). It is first-party code reviewed in its own
repository; generated code never runs there.

## 10. Alternatives considered

1. **Install each project app as a real App Kit app.** Rejected. Its UI would
   run in process with the owner's authority, and the trust model calls the
   frontend scoping "a guardrail, not a boundary". Kept as a later, manual
   *graduation* path: a person reviews the source, gives it a manifest and
   publishes it.
2. **The MCP Apps call path as the transport.** It has the best per-call gating
   in the repository. But it is owner-only and bound to the session and tool
   result that produced the app, and its spool record expires after 24 hours
   (`SPOOL_TTL_SECS`). A long-lived project would need a durable binding
   and a second producer. It is worth revisiting if Q1 finds the app-token route
   cannot reach the ledger.
3. **Extend the `feat/docwork` dashboard surface** with a bridge. Rejected: the
   requirement is a full React app, and the dashboard mixes code and data in one
   string with no build, no versions beyond undo, and no typed calls.
4. **A declarative UI schema** (json-render, A2UI) drawn by host components.
   Safer and cheaper, but it cannot express a bespoke flow such as a guided
   return, and `rfc-composable-layout-mechanism.md` §9.2 already chose
   "inspiration, not adoption". It may still become the base kit's layout layer.
5. **Run the target app's dev server live in a frame** for the screen canvas.
   Rejected for v1: a dev-server listener reached from the dashboard is a new
   exposure, and its origin is mixed. Screenshots from a pod need neither.

## 11. Open questions

- **Q1 (blocks Phase 4).** Can an app token read the work ledger of a conductor
  session the app created? `work_ledger_board.py` admits only the dashboard owner
  today (`api_work_ledger_board`, `api_work_ledger_board_action`). If not, either a narrow app-token read scoped to slots
  the app owns, or the host backend keeps its own record of the workers it
  spawned.
- **Q2 (blocks Phase 2).** Is a data-backed resolver an extension of
  `FrameEntry` in `app_frame.py` or a sibling type? Either way the
  `script-src 'self'` rule must keep holding for the inlined bundle; a per-frame
  nonce on the one bundle script is the likely answer.
- **Q3.** Should the project conductor default to Ultra (proposed), unlike a new
  doc or board, which starts Plain?
- **Q4.** What is the budget for a build per edit? An incremental Vite build is
  seconds, but a full `npm ci` per project is not; a shared offline cache per
  blueprint is assumed.
- **Q5.** How does a project relate to `rfc-crew-projects.md` (`draft`), whose
  Project is a portable context bundle? Proposed: a project app attaches to a
  Crew Project when one exists, and to its session and folder until then.
- **Q6.** Should the four `feat/docwork` surfaces move onto
  `contributes.sessionKinds`, which tenet 8 asks of anything that renders?
  That is a separate RFC.
- **Q7.** Does showing worker state inside a project app conflict with
  `docs/decisions/2026-10-10-conductor-board-tab-replaces-the-in-chat-board.md`?
  Proposed: no. The conductor's Dashboard tab stays its task board, and the
  project app draws the same ledger in place.
