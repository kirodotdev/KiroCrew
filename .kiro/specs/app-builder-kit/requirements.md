# Requirements — Kiro Crew App Builder Kit

## Introduction

The Kiro Crew dashboard frontend (`website/src/`) already exposes an `app-sdk/` that defines
the app contract (`AppApi`, `useAppApi`, `useTheme`, `useNavigate`, `useNotify`,
`useAppEvents`, `useChatLauncher`, `ChatPanel`, `messageRenderers`, `protocol/`), with apps
registered through `apps/builtinRegistry.ts`. However, UI building blocks are not
consolidated: the ~15 apps under `apps/` each re-implement their own tables, cards, and
loading/error/empty states, and agent tool-output rendering is hand-rolled (raw
`<mcwidget>` HTML via `WidgetFrame.tsx`; the tool-request preview in `ApprovalCard.tsx` /
`ToolInputPreview.tsx` renders input as a raw `<pre>` args dump).

The **App Builder Kit** is a frontend build-velocity library that layers on the existing
`app-sdk` to let a developer stand up a new app or feature fast, with production-quality
theming, accessibility, i18n, and agent-output rendering by default. It absorbs the earlier
"Generative-UI Tool Component Library" idea as one module (Tool Views).

Scope is the React frontend only. This is not a backend/MCP toolkit, not an `app-sdk`
rewrite, and not a design-system overhaul.

## Requirements

### Requirement 1 — Reusable UI primitives

**User Story:** As an app author, I want themed, accessible UI primitives, so that I stop
re-implementing tables, cards, and state views in every app.

#### Acceptance Criteria
1. WHEN a developer imports a kit primitive (DataTable, Card, EmptyState, LoadingSkeleton, ErrorNotice, Toolbar, SplitLayout, Drawer, StatusBadge, ConfirmDialog) THEN the kit SHALL render it using the dashboard theme CSS variables with no hard-coded colors.
2. WHEN the active theme changes (light, dark, or custom) THEN each primitive SHALL reflect the new theme without additional code.
3. WHEN a primitive renders user-facing text THEN it SHALL use `i18nT()` and the kit SHALL provide catalog keys in all shipped locale files.
4. WHEN a `DataTable` receives rows and columns THEN it SHALL support sorting and pagination without app-level implementation.
5. IF a primitive is interactive THEN it SHALL be keyboard operable and expose appropriate ARIA roles/labels (WCAG AA).

### Requirement 2 — Typed tool-view components (inline)

**User Story:** As an agent (and as an app author), I want typed components that render common tool outputs, so that I don't hand-write `<mcwidget>` HTML per response.

#### Acceptance Criteria
1. WHERE a tool output matches a kit-published schema THE kit SHALL provide a typed component that renders it inline (ChartToolView, TableToolView, MapToolView, ImageToolView, DiffToolView). NOTE: `MapToolView` (and its `maplibre-gl` peer dependency) is the one v1 component with no named consumer in the design boundary table; it SHALL be deferred out of the v1 shipping set until a shipped tool actually emits map output, and is retained here only as a planned module.
2. WHEN a tool invocation is in `input-streaming`, `input-available`, `output-available`, or `output-error` state THEN the component SHALL render a defined view for that state (skeleton, running affordance, result, error surface respectively).
3. WHEN a tool's output shape does not match the component's declared schema at author time THEN the mismatch between renderer and schema SHALL surface as a TypeScript compile-time error. Because tool output arrives at runtime from backend/MCP tools, the compile check binds the renderer to its schema only — the runtime guarantee is the parse-failure fallback in Requirement 2.5 (a payload that fails schema validation downgrades to the `<mcwidget>` fallback rather than mis-rendering).
4. WHEN a `DiffToolView` renders THEN it SHALL reuse the existing `DiffBlock.tsx` and preserve the dashboard's Open-file diff-header affordance.
5. IF no typed component matches a tool part THEN the kit SHALL fall back to the existing `<mcwidget>` rendering with no regression.
6. WHEN a tool view is rendered inline THEN it SHALL be persistable as an artifact (`kind="widget"`) consistent with the artifacts system.

### Requirement 3 — Rich tool-request preview and approval

**User Story:** As an operator, I want a rich preview of a pending tool call before I approve it, so that I can intervene with full context instead of reading a raw args dump.

#### Acceptance Criteria
1. WHEN a pending tool call has a matching tool-view schema THEN the kit SHALL render a rich preview inside `ApprovalCard` / `ToolInputPreview` in place of the raw `<pre>` dump as the default view.
2. WHERE a tool-view schema does not match THE approval surface SHALL fall back to the current `ToolInputPreview` `<pre>` behavior.
3. WHEN the operator approves or rejects THEN the decision SHALL flow through the existing `onApprove(decision, pattern?)` callback and the kit SHALL NOT introduce a new approval API path.
4. WHEN an approval decision is submitted from a chat slot THEN it SHALL resolve via the existing slot-scoped `api.approveChatSlot(slot, action, extra)` path in `ChatInput.tsx`.
5. IF the approval controls are rendered THEN they SHALL be keyboard operable (operators batch-approve).
6. WHERE a rich preview is shown for a pending tool call THE exact, unmodified raw tool input SHALL remain available to the operator one interaction away (e.g. an expandable "show raw input" control). A rich renderer is lossy by design (`extra` passthrough fields, truncated series, fields the view does not plot) and tool input is attacker-influenceable; the operator MUST be able to inspect the verbatim args before approving so a consequential argument is never hidden by the summary.

### Requirement 4 — App scaffolding

**User Story:** As an app author, I want a standard way to define and register an app with proven screen layouts, so that I start from a working shape instead of copying another app.

#### Acceptance Criteria
1. WHEN a developer calls `defineApp({ id, icon, routes, permissions })` THEN the kit SHALL register the app with `builtinRegistry` and populate `AppInfo`/`AppPermissions` in a single call.
2. WHERE a new app needs a common layout THE kit SHALL provide ListDetail, Settings, and Dashboard screen templates modeled on the existing meetings app structure.
3. WHEN two apps register the same route THEN the kit SHALL surface the existing seam-collision report rather than silently overwriting.
4. WHEN the scaffold codegen (`scaffold-app <name>`) runs THEN it SHALL emit the app directory and its registry entry (this criterion MAY be deferred to a later milestone).

### Requirement 5 — Data and async helpers

**User Story:** As a feature dev, I want typed fetch/loading/error and live-update helpers, so that I stop re-writing the same async triad.

#### Acceptance Criteria
1. WHEN a developer uses `useAppResource` THEN it SHALL return typed loading, error, and data states as a **thin wrapper over the repo's existing `@tanstack/react-query`** (already a `website/package.json` dependency) — the kit SHALL NOT introduce a bespoke async cache that reimplements react-query.
2. WHEN a mutation is performed with the kit's mutation helper THEN it SHALL provide optimistic update and rollback by configuring react-query's mutation cache (`onMutate`/`onError`/`onSettled`), not a hand-rolled rollback mechanism.
3. WHERE an app subscribes to server events THE kit SHALL provide a live-update hook built on `useAppEvents` that respects `checkSubscribeAllowed` and invalidates the relevant react-query keys.

### Requirement 6 — Non-regression and CI compliance

**User Story:** As a maintainer, I want the kit to satisfy the repo's CI gates, so that adopting it does not create review or build friction.

#### Acceptance Criteria
1. WHEN a kit component introduces a user-visible surface change THEN the change SHALL ship with committed, SHA-pinned screenshots satisfying the Screenshot Evidence gate.
2. WHEN kit code is added THEN it SHALL NOT introduce copy/paste clones that fail the jscpd 0% threshold.
3. WHEN kit components add user-facing strings THEN the corresponding keys SHALL exist in all locale files (catalogParity).
4. WHEN an existing app is refactored onto the kit THEN it SHALL show no UX regression versus its pre-refactor behavior.

### Requirement 7 — Dogfood and discoverability

**User Story:** As a maintainer, I want the kit proven on a real app and browsable, so that adoption is de-risked and discoverable.

#### Acceptance Criteria
1. WHEN v1 is complete THEN at least one existing app (e.g. meetings or ops-mission-control) SHALL be refactored onto the kit as a dogfood proof.
2. WHERE a developer wants to see available primitives and tool views THE kit SHALL provide a dev-only gallery route rendering each in light, dark, and custom themes.

### Requirement 8 — Tool-part encoding contract and producer (PREREQUISITE)

**User Story:** As an implementer, I need a defined wire encoding for typed tool output AND a named producer that emits it, so that the tool-view renderers (Req 2/3) have something real to dispatch on instead of assuming an AI-SDK-style part stream this codebase does not produce.

**Context (verified against the codebase):** The dashboard chat transport is markdown + `<mcwidget>` blocks (`website/src/hooks/useBlockAssembler.ts`), and tool activity reaches the frontend as **opaque strings** — `input?: string` / `output?: string` and `tool_input: string` in `website/src/types/index.ts`. There is no typed `tool-<name>` part stream today. Requirement 2/3's `ToolRenderer` therefore has no producer within a frontend-only scope, and the design must not silently smuggle in a backend wire-format change.

#### Acceptance Criteria
1. WHEN the kit defines how a typed tool view is addressed THEN the spec SHALL define the **encoding** carried inside the existing opaque `output`/`tool_input` string — the chosen mechanism SHALL be a **fenced `<mcwidget>`-adjacent JSON convention** (a `tool-view` block: `{"tool":"<name>","schemaVersion":1,"data":{...}}`) parsed by `ContentRenderer`, so it rides the existing markdown/`<mcwidget>` transport with NO backend wire-format change.
2. WHERE the encoding must be produced THE spec SHALL name the **producer** explicitly as an **agent prompt/skill convention** (the agent emits the `tool-view` JSON block in its message text, exactly as it already emits `<mcwidget>` HTML) — NOT a backend or MCP protocol change, keeping the frontend-only scope intact.
3. WHEN a message contains a `tool-view` block whose `tool` matches a registered `ToolViewDef` and whose `data` parses against that def's schema THEN `ContentRenderer` SHALL render the typed component; otherwise it SHALL fall back to the existing `<mcwidget>` / `ToolInputPreview` rendering (Req 2.5) with no regression.
4. WHEN the encoding is defined THEN it SHALL be the FIRST implementation task (task 0), because the ImageToolView spike cannot prove the contract end-to-end without a producer emitting the block.
