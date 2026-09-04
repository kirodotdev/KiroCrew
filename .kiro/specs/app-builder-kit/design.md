# Design — Kiro Crew App Builder Kit

## Overview

The App Builder Kit is a new frontend library at `website/src/kit/` that layers on the
existing `website/src/app-sdk/`. `app-sdk` owns the **app contract** (identity, api, events,
theme, chat); `kit` owns **reusable UI + patterns**. The two do not overlap: the kit imports
from `app-sdk`, never the reverse.

The kit is delivered as five modules — `ui`, `app`, `tool-views`, `data`, `gallery` — each
independently importable and tree-shakeable. Heavy visualization dependencies (Recharts,
MapLibre GL) are optional peer dependencies imported only by the component that needs them.

## Architecture

```
website/src/
├── app-sdk/                 # EXISTING (unchanged): AppApi, useAppApi, useTheme,
│                            #   useNavigate, useNotify, useAppEvents, useChatLauncher,
│                            #   ChatPanel, ChatEmbed, messageRenderers, protocol/
└── kit/                     # NEW
    ├── ui/                  # DataTable, Card, EmptyState, LoadingSkeleton, ErrorNotice,
    │                        #   Toolbar, SplitLayout, Drawer, StatusBadge, ConfirmDialog
    ├── app/                 # defineApp(); ListDetail / Settings / Dashboard templates
    ├── tool-views/          # ToolViewFrame, ToolPreviewFrame, defineToolView, ToolRenderer
    │   ├── chart/           #   ChartToolView + chartToolSchema        (peer: recharts)
    │   ├── table/           #   TableToolView + tableToolSchema        (reuses ui/DataTable)
    │   ├── map/             #   MapToolView + mapToolSchema            (peer: maplibre-gl)
    │   ├── image/           #   ImageToolView + imageToolSchema
    │   └── diff/            #   DiffToolView + diffToolSchema          (reuses DiffBlock)
    ├── data/                # useAppResource/useAppMutation/useAppLiveResource
    │                        #   (thin wrappers over @tanstack/react-query — already a dep)
    └── gallery/             # dev-only route: every primitive + tool view × themes
```

### Boundary with existing components (verified paths)

| Existing (`website/src/`) | Kit relationship |
|---|---|
| `app-sdk/index.ts` (`useTheme`, `useAppEvents`, `AppApi`) | Kit consumes; source of theme + events |
| `apps/builtinRegistry.ts` (`registerBuiltinComponents`) | `kit/app/defineApp` wraps registration |
| `components/ApprovalCard.tsx`, `components/ToolInputPreview.tsx` | Enhanced to host `ToolPreviewFrame` |
| `components/ChatInput.tsx` (`api.approveChatSlot`) | Unchanged approval path the preview reuses |
| `components/WidgetFrame.tsx`, `components/ArtifactBody.tsx` | Inline tool views render inside these |
| `components/DiffBlock.tsx` | `DiffToolView` reuses it |
| `components/ContentRenderer.tsx` | `ToolRenderer` dispatch integrates here |
| `components/ErrorNotice.tsx` | Consolidated into `kit/ui/ErrorNotice` |
| `api/client.ts` | `kit/data` wraps the existing client |

## Components and Interfaces

### kit/ui
Presentational, theme-variable-driven, i18n via `i18nT()`. `DataTable<T>` takes
`{ rows: T[]; columns: Column<T>[]; pageSize?; sortable? }` and owns sort/paginate state.
State primitives (`EmptyState`, `LoadingSkeleton`, `ErrorNotice`) are the canonical
loading/empty/error surfaces every app and tool view reuses.

### kit/app
`defineApp(config)` returns a registration object and calls `registerBuiltinComponents`
with the app's lazy routes, honoring the existing seam-collision report (no silent
overwrite). Screen templates (`ListDetailTemplate`, `SettingsTemplate`, `DashboardTemplate`)
are composition shells mirroring the meetings app (`MeetingsPage`/`MeetingView`/`SettingsView`).

### kit/tool-views
- **Lifecycle contract:** a discriminated union over `state` — `input-streaming` |
  `input-available` | `output-available` | `output-error` — mirroring AI-SDK
  `UIToolInvocation<T>`. `ToolViewFrame` renders the state scaffold (skeleton / running /
  result / error) and delegates the `output-available` body to the specific component.
- **Schema contract:** each component publishes a **Zod** schema for the tool output it renders.
  **Dependency note:** `zod` is NOT currently a `website/package.json` dependency — this kit
  adds it (a new runtime dep, ~12KB gzipped min, tree-shakeable), alongside the acknowledged
  `recharts` and `maplibre-gl` optional peers. `defineToolView(schema, InlineComponent,
  PreviewComponent?)` co-locates schema + renderers and yields a typed entry. `z.infer<schema>`
  is the component's output prop type — a drift between the tool's output and the renderer is a
  compile error.
- **Encoding + producer (see Requirement 8 — the load-bearing prerequisite).** This codebase
  has **no typed `tool-<name>` part stream**: the chat transport is markdown + `<mcwidget>`
  (`useBlockAssembler.ts`) and tool I/O is opaque strings (`input?/output?: string`,
  `tool_input: string` in `types/index.ts`). So the kit does NOT borrow AI-SDK's wire model
  literally. Instead:
  - **Encoding:** a fenced `tool-view` JSON block carried in the agent's message text —
    `{"tool":"<name>","schemaVersion":1,"data":{...}}` — parsed by `ContentRenderer` exactly
    like it already parses `<mcwidget>` HTML. **No backend wire-format change.**
  - **Producer:** an **agent prompt/skill convention** — the agent emits the `tool-view`
    block just as it emits `<mcwidget>` today. This keeps the frontend-only scope intact;
    there is no MCP or backend protocol change.
  - The AI-SDK `UIToolInvocation` lifecycle union (`input-streaming` → … → `output-error`) is
    reused only as the **component-internal state model** for `ToolViewFrame`, not as a claim
    about the transport.
- **Dispatch:** `ToolRenderer` maps a parsed `tool-view` block's `tool` field to a registered
  `ToolViewDef` and validates `data` against its schema; on match it renders the typed
  component, otherwise it falls through to the existing `<mcwidget>` render. Integrated at
  `ContentRenderer`.
- **Preview:** `ToolPreviewFrame` renders inside `ApprovalCard`. When a schema matches the
  pending call's (partial) input it shows a rich preview; otherwise it renders the current
  `ToolInputPreview` `<pre>`. Approve/deny is passed straight to the existing
  `onApprove(decision, pattern?)` callback — no new API.

### kit/data
Thin wrappers over the repo's existing **`@tanstack/react-query`** (already in
`website/package.json` at 5.96.0) — the kit does NOT ship a bespoke async cache.
`useAppResource<T>(fetcher)` wraps `useQuery` (keyed off the `api/client.ts` call) →
`{ data, loading, error, refetch }`. `useAppMutation` wraps `useMutation`, wiring optimistic
apply + rollback through react-query's `onMutate`/`onError`/`onSettled`. `useAppLiveResource`
composes `useAppResource` with `useAppEvents` (gated by `checkSubscribeAllowed`) and
invalidates the relevant query keys on an event.

## Data Models

```ts
// kit/tool-views/core/types.ts
type ToolViewState<TIn, TOut> =
  | { state: 'input-streaming';  input: Partial<TIn> }
  | { state: 'input-available';  input: TIn }
  | { state: 'output-available'; input: TIn; output: TOut }
  | { state: 'output-error';     input: TIn; error: string }

interface ToolViewDef<TIn, TOut> {
  schema: ZodType<TOut>          // output contract
  Inline: FC<{ invocation: ToolViewState<TIn, TOut> }>
  Preview?: FC<{ input: Partial<TIn>; onApprove: (d: string, p?: string) => void }>
}
```

Example schema (chart): `{ kind: 'line'|'bar'|'area'|'pie', series: {name,data:{x,y}[]}[],
xLabel?, yLabel?, extra?: Record<string,unknown> }`. The `extra` passthrough keeps schemas
strict without blocking app-specific fields.

## Error Handling

- Tool views never throw to the tree: `output-error` renders `kit/ui/ErrorNotice`; a schema
  parse failure downgrades to the `<mcwidget>` fallback and logs via the existing logger.
- `useAppResource` surfaces errors as state, not exceptions; `useAppMutation` rolls back
  optimistic writes on rejection.
- `defineApp` route collisions use the existing `reportSeamCollision` path, not a throw.

## Testing Strategy

- **Unit (vitest):** each primitive and tool view — state coverage (all four lifecycle
  states), theme-variable usage (no literal colors), keyboard/ARIA. Reuse existing test
  patterns (e.g. `WidgetFrame.test.tsx`).
- **Type tests:** a fixture asserting a mismatched tool output fails to compile (`tsc`).
- **Integration:** `ToolRenderer` dispatch + `<mcwidget>` fallback; `ApprovalCard` preview
  path routing through a mocked `onApprove`/`approveChatSlot`.
- **Visual/Screenshot Evidence:** capture the gallery route via dev-server + Playwright
  (light path; dist build needs 6 GB) for the CI gate.
- **Dogfood:** refactor one screen of an existing app (meetings) onto the kit and assert no
  UX regression.

## CI / constraints (repo-specific)

- Screenshot Evidence gate — gallery route makes capture cheap; SHA-pinned screenshots in PR.
- jscpd 0% — kit consolidates clones; kit components must not copy from each other.
- catalogParity — all new `i18nT()` keys land in every locale file.
- PR Hygiene — single squashed commit per PR.
