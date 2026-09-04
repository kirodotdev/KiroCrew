# Implementation Plan — Kiro Crew App Builder Kit

- [ ] 0. Define the tool-part encoding contract + producer (PREREQUISITE — do this first)
  - Specify the `tool-view` fenced JSON block carried in agent message text: `{"tool":"<name>","schemaVersion":1,"data":{...}}`, riding the existing markdown/`<mcwidget>` transport with NO backend wire-format change
  - Parse it in `components/ContentRenderer.tsx`; on a registered-tool + schema match, hand off to the typed component, else fall through to the existing `<mcwidget>` / `ToolInputPreview` render
  - Document the producer as an agent prompt/skill convention (agent emits the block like it emits `<mcwidget>`); write the convention doc + one example the ImageToolView spike (task 3.1) consumes end-to-end
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [ ] 1. Scaffold the kit module and lifecycle core
  - Create `website/src/kit/` with `ui/`, `app/`, `tool-views/`, `data/`, `gallery/` and barrel exports
  - Implement `kit/tool-views/core/types.ts` (`ToolViewState`, `ToolViewDef`) and `defineToolView`; add `zod` as a `website/package.json` dependency (name it in the PR — not previously present)
  - Implement `ToolViewFrame` rendering the four lifecycle states (skeleton / running / result / error)
  - _Requirements: 2.2, 2.3_

- [ ] 2. Build the UI primitives (kit/ui)
- [ ] 2.1 Implement state primitives and consolidate ErrorNotice
  - Add `EmptyState`, `LoadingSkeleton`; move `components/ErrorNotice.tsx` behavior into `kit/ui/ErrorNotice` and re-export for back-compat
  - Drive all styling from theme CSS variables; route text through `i18nT()`
  - Unit tests for theme-variable usage (no literal colors) and ARIA
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 6.3_
- [ ] 2.2 Implement DataTable and layout primitives
  - `DataTable<T>` with sorting + pagination owned internally; `Card`, `Toolbar`, `SplitLayout`, `Drawer`, `StatusBadge`, `ConfirmDialog`
  - Keyboard navigation + ARIA on DataTable and Drawer/ConfirmDialog
  - Unit tests covering sort, paginate, keyboard
  - _Requirements: 1.1, 1.4, 1.5_

- [ ] 3. Implement inline tool views
- [ ] 3.1 ImageToolView + schema (spike component)
  - Zod `imageToolSchema` (base64|url, partialImages); render all four states; download affordance
  - Prove the `defineToolView` contract end-to-end inside `ToolViewFrame`
  - _Requirements: 2.1, 2.2, 2.3, 2.6_
- [ ] 3.2 DiffToolView reusing DiffBlock
  - `diffToolSchema`; delegate render to `components/DiffBlock.tsx`; preserve Open-file diff-header affordance
  - _Requirements: 2.1, 2.4_
- [ ] 3.3 TableToolView reusing kit/ui DataTable
  - `tableToolSchema`; map schema rows/columns onto `DataTable`
  - _Requirements: 2.1, 1.4_
- [ ] 3.4 ChartToolView (Recharts optional peer)
  - `chartToolSchema` (line/bar/area/pie + `extra` passthrough); Recharts as optional peer, per-component import
  - _Requirements: 2.1, 2.3_
- [ ] 3.5 Type-safety test
  - Add a `tsc` fixture asserting a mismatched tool output fails to compile
  - _Requirements: 2.3_

- [ ] 4. Dispatch and mcwidget fallback (builds on the task-0 encoding)
  - Implement `ToolRenderer` mapping a parsed `tool-view` block's `tool` field (task 0 encoding) to registered `ToolViewDef`s, validating `data` against the def's schema
  - Integrate at `components/ContentRenderer.tsx`; blocks with no registered tool or a failing schema parse fall through to the existing `<mcwidget>` render with no regression
  - Integration test: matched → component, unmatched/parse-fail → `<mcwidget>`
  - _Requirements: 2.1, 2.5, 8.3_

- [ ] 5. Artifact persistence for inline tool views
  - Ensure a rendered tool view body persists via `artifact_save(kind="widget")` and re-renders from `ArtifactBody.tsx`
  - _Requirements: 2.6_

- [ ] 6. Rich tool-request preview and approval
  - Implement `ToolPreviewFrame`; render inside `components/ApprovalCard.tsx` when a schema matches the pending input, else fall back to `ToolInputPreview` `<pre>`
  - Route approve/reject through the existing `onApprove(decision, pattern?)` → verify it reaches slot-scoped `api.approveChatSlot` (no new API)
  - Keyboard-operable controls; integration test with mocked `onApprove`
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 7. App scaffolding (kit/app)
- [ ] 7.1 defineApp + registration
  - `defineApp({ id, icon, routes, permissions })` wrapping `registerBuiltinComponents` and populating `AppInfo`/`AppPermissions`; honor `reportSeamCollision` on duplicate routes
  - _Requirements: 4.1, 4.3_
- [ ] 7.2 Screen templates
  - `ListDetailTemplate`, `SettingsTemplate`, `DashboardTemplate` modeled on the meetings app
  - _Requirements: 4.2_

- [ ] 8. Data and async helpers (kit/data) — thin wrappers over @tanstack/react-query (already a dep; do NOT reimplement it)
  - `useAppResource<T>` wraps `useQuery` over `api/client.ts` (data/loading/error/refetch); `useAppMutation` wraps `useMutation` with optimistic apply + rollback via `onMutate`/`onError`/`onSettled`; `useAppLiveResource` composes `useAppResource` with `useAppEvents` (gated by `checkSubscribeAllowed`) and invalidates query keys on events
  - Unit tests for loading/error transitions and rollback
  - _Requirements: 5.1, 5.2, 5.3_

- [ ] 9. Gallery route and screenshot evidence
  - Dev-only gallery rendering every primitive + tool view across light/dark/custom themes
  - Capture screenshots via dev-server + Playwright (light path) for the Screenshot Evidence gate
  - _Requirements: 6.1, 7.2_

- [ ] 10. Dogfood refactor + CI compliance
  - Refactor one screen of an existing app (meetings or ops-mission-control) onto kit primitives + a tool view; verify no UX regression
  - Confirm jscpd 0% (kit reduces clones), catalogParity keys in all locales, single-commit PR hygiene
  - _Requirements: 6.2, 6.3, 6.4, 7.1_

- [ ] 11. (Deferred) scaffold-app codegen
  - `scaffold-app <name>` emitting the app directory + registry entry
  - _Requirements: 4.4_
