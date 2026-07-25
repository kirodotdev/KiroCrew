# KiroCrewWebsite — Agent Guidelines

This is the **single source of truth** for frontend work in `website/` (the
backend has its own `AGENTS.md` at the repo root). KiroCrew dashboard frontend —
React + TypeScript SPA. Production builds output to `website/dist/`, staged into
`KiroCrew/src/kiro_crew/static/dist/` and served by the Python backend.

## Stack

React 18, Redux Toolkit (`@reduxjs/toolkit`), React Query (`@tanstack/react-query`), React Router v7 (`react-router-dom`), Framer Motion (`framer-motion`), Tailwind CSS 3, Lucide React (`lucide-react`), DOMPurify, highlight.js, Monaco, TypeScript, Vite 5.

## Build / dev / test

```bash
npm install
npm run build        # tsc -b + vite build → website/dist   (this is the real typecheck)
npm run dev          # Vite dev server on :3000, proxies /api to backend :5476
npm run check        # typecheck + lint + tests
npm run test         # vitest (website + electron)
npm run lint         # eslint src
```

After building, stage the bundle so the backend serves it:
`cp -R website/dist ../src/kiro_crew/static/dist`.

**Gotcha — `npm run typecheck` is a FALSE PASS.** It runs `tsc --noEmit`, but the
root `tsconfig.json` has `files: []` + project references, so `--noEmit` checks
**zero files** and always passes. **Always use `tsc -b`** (which `npm run build`
runs) to actually type-check. Don't trust a green `npm run typecheck` alone.

**Gotcha — localStorage polyfill in tests.** `website/integration/setup.ts`
installs an in-memory `localStorage`/`sessionStorage` polyfill. Required: Node
25's native `--localstorage-file` storage shadows jsdom's spec-complete `Storage`
and lacks `.clear()`. The polyfill puts methods on `Storage.prototype` so
quota-error spies still work — don't remove it or move methods off the prototype.

## This is a public OSS fork — don't reintroduce internal couplings

When changing the frontend, **do not reintroduce**:
- Build/infra: `npm-pretty-much`, Brazil, AIM, CodeArtifact registries,
  Coverlay/jscpd-as-a-build-gate. The public build is plain **npm + Vite**;
  `website/.npmrc` pins the **public** registry (`registry.npmjs.org`).
- Identity/telemetry: live Cognito pools or RUM app ids (`src/rum.ts` is a no-op
  telemetry stub — keep it inert), `aws-rum-web`. The backend is KiroACP
  (`kiro-cli`) only; the frontend never needs an ACP adapter as a web dependency.
- Removed product surfaces: internal feature-app pages/tabs/API-client methods and
  the credential-TTL card on the Overview page. They were deleted with their
  backend; don't re-add the UI (a downstream edition re-adds them additively via
  the extension seams below — never by editing core).

> Stale references: `website/Config` and `website/AUTOSDE.yaml` are leftover
> internal files not used by the public build — ignore them, and treat any
> "brazil-build"/"Coverlay" mentions as historical.

## Browser support

Chrome, Firefox, Safari, Edge. Use standard Web APIs only; guard browser-specific
ones (e.g. `typeof Notification !== 'undefined'`).

## Icons: Lucide Only, No Emoji

Use `lucide-react` for all icons with `className="lucide-inline"` for inline placement. The `lucide-inline` CSS class handles sizing and vertical alignment so icons stay on the same line as adjacent text.

```tsx
// Good
import { Search, AlertTriangle, Inbox } from 'lucide-react'
<button><Search className="lucide-inline" /> Search</button>

// Bad
<button>🔍 Search</button>
<button><Search size={13} /> Search</button>  // use lucide-inline, not size={}
```

Do NOT use emojis, `size={N}` props, `inline-flex` wrappers, inline SVG icon components, or hand-rolled SVG paths. Emojis in rendered UI are a bug — replace with the nearest Lucide equivalent.

**Exceptions** (emoji allowed):
- `src/components/EmojiPicker.tsx` — emoji catalog component
- `src/pages/scenes/` — decorative scene elements
- `src/hooks/useTheme.ts` and `src/components/themeEditor.tsx` — theme display names
- `src/pages/ChatSidebar.tsx` folder icons — a folder's icon is a single emoji
  the backend auto-generates (and the user may override via `FolderIconPicker`).
  It is folder *data* rendered by `FolderGlyph`, not a status/UI icon, so
  `FOLDER_EMOJIS` (the curated picker grid) and the free-emoji input are
  intentional.

See `AUTOSDE.yaml` rules `use-lucide-icons` and `no-emoji-as-icons` for enforcement.

## Data Fetching

Always use React Query (`useQuery`/`useMutation`) for server state. Do NOT use manual `useState` + `useEffect` + `useCallback` patterns for API calls. Use optimistic updates via `queryClient.setQueryData` where possible. Query keys follow `['resource-name']` convention (e.g. `['mcp-servers']`, `['mcp-registry']`, `['skills']`).

## Animations

Use Framer Motion for orchestrated component transitions (enter/exit, layout animations, gesture-driven). Use Tailwind `transition-*` for simple state changes (hover, toggle, color). Use Tailwind `animate-*` only for simple indicators (spin, pulse). Do NOT add new CSS `@keyframes` — use Framer Motion instead.

## Styling

Tailwind CSS with custom theme in `tailwind.config.js` — `darkMode: ['selector', '[data-theme="dark"]']`. 11-theme color system (dark/light variants) with CSS custom properties (design tokens) defined in `index.css`, including semantic colors (`--aim`, `--clarify`, `--diff-*`). Color Theme picker in Overview > Display tab with cross-instance sync. CSS utilities: `.top-bar-pill`, `.topbar-glass`, `.scroll-shadow`, `.table-striped`, `.skeleton`, `.focus-ring`. Theme toggle crossfades via `transition` on `body`.

## Shared Components

`src/components/ui.tsx`: `Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`, `Badge`, `AimBadge`, `StatCard`, `Skeleton`, `ContentSkeleton`, `EmptyState`, `PageHeader`, `SearchInput`, `Toggle`

Other shared: `SegmentedControl.tsx` (iOS-style sliding tab selector with Framer Motion), `DetailPanel.tsx` (resizable side panel with animated open/close), `SidePanelLayout.tsx` (shared side-panel page layout), `AgentSelector.tsx` (portal dropdown with ARIA), `layout.ts` (`LAYOUT` constants), `InfoTip.tsx`, `MarkdownRenderer.tsx` (with highlight.js syntax highlighting), `TypewriterText.tsx`

## Typography Scale

body 14px, descriptions/details `text-sm` (14px), labels/buttons/sidebar `text-[13px]`, badges/captions `text-[12px]`, decorative icons `text-[10px]`–`text-[11px]`. Minimum readable text: 11px. Code blocks: 13px mono. **No text below 10px.** Do not use `text-xs` (use `text-[13px]`), do not use `text-[9px]` or smaller.

## Security

All `dangerouslySetInnerHTML` content sanitized via DOMPurify (`src/api/helpers.ts`). `md()` renders markdown-like formatting + sanitizes. `sanitize()` for pre-escaped HTML. `esc()` for plain text escaping.

## Accessibility (a11y)

All interactive elements MUST be keyboard accessible. Use `<Clickable>` from `src/components/Clickable.tsx` instead of `<div onClick>`.

```tsx
// Good
import Clickable from '../components/Clickable'
<Clickable onClick={handler} className="...">Click me</Clickable>

// Bad — not keyboard accessible, fails jsx-a11y lint
<div onClick={handler} className="...">Click me</div>
```

For animated interactive elements, wrap `Clickable` with Framer Motion — it forwards refs and spreads props, so animation and a11y compose cleanly:

```tsx
import { motion } from 'framer-motion'
import Clickable from '../components/Clickable'
const MotionClickable = motion.create(Clickable)  // motion(Clickable) on older versions
// <MotionClickable onClick={handler} whileHover={{ scale: 1.02 }}>…</MotionClickable>
```

Alternatively add `role="button"` + `tabIndex` + `onKeyDown` directly on the `motion.div`.

Rules:
- Never use `<div onClick>` or `<span onClick>` without `role="button"` + `tabIndex` + `onKeyDown`. Prefer `<Clickable>` which handles all three.
- All icon-only buttons MUST have `aria-label` describing the action.
- Modals MUST have `role="dialog"`, `aria-modal="true"`, `aria-label`, Escape key dismissal, and focus trap.
- Dynamic content updates (streaming messages, notifications) should use `aria-live="polite"`.
- Do NOT use `<button>` elements directly — use `<Btn>` or `<SendBtn>` from `ui.tsx` (which handle styling), or `<Clickable>` for div-based interactive elements.

Tooling: `eslint-plugin-jsx-a11y` (warns on violations), `@axe-core/react` (runtime DOM scanning in dev mode — check browser console).

## Page Layout Guide

All dashboard pages MUST follow this consistent layout pattern. Do NOT invent custom layouts.

**Page skeleton** (every page):
```tsx
<>
  <PageHeader title="PageName" subtitle="Short description" />
  <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    {/* StatCard row → Cards with tables/forms */}
  </div>
</>
```

**Stat cards** — summary metrics at the top of every page:
```tsx
<div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
  <StatCard label="Total" value={count} accent />
  <StatCard label="Active" value={active} />
</div>
```

**Data sections** — use `Card` + `CardTitle` + `InfoTip`:
```tsx
<Card>
  <CardTitle>Section Name <InfoTip text="Explanation." /></CardTitle>
  <SearchInput placeholder="Filter…" value={filter} onChange={…} />
  {items.length === 0 ? <EmptyState icon={<Anchor className="lucide-inline" />} title="None yet" /> : (
    <table className="w-full border-collapse table-striped">…</table>
  )}
</Card>
```

**Tables** — striped with standard header style:
```tsx
<th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
```

**Forms** — inline within `Card`, using shared components:
- `Input` for text fields
- `SendBtn` for primary actions (accent color)
- `Btn` for secondary actions, `Btn danger` for destructive
- Styled `select`: `bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring`
- `AgentSelector` for agent dropdowns (portal-based, ARIA)

**Status indicators**:
- `Badge variant="ok"` (green), `variant="err"` (red), `variant="warn"` (amber), `variant="aim"` (purple)
- `AimBadge source="kirocrew"` (orange), `source="aim"` (purple), `source="builtin"` (gray)
- Toggle switches: `w-9 h-5 rounded-full` with `bg-accent` (on) / `bg-border` (off)

**Errors** — dismissible banner:
```tsx
<div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
```

**Animations**: `animate-rise` on cards/banners, `animate-scale-in` on inline reveals.

**Do NOT**:
- Wrap pages in `<div className="p-6 max-w-[960px] mx-auto">` — use `PageHeader` + `px-6 pb-8` container
- Use raw `<input>` / `<select>` / `<button>` — use `Input`, `Btn`, `SendBtn`, `SearchInput`
- Use raw status text — use `Badge` component
- Use `text-xs` — use `text-[13px]`

## Architecture

- **Entry**: `src/main.tsx` — wraps `<App>` in `<Provider>` (Redux) and `<BrowserRouter>` (React Router)
- **Routing**: `App.tsx` uses `<Routes>` / `<Route>` with client-side navigation; SPA fallback middleware in `server.py` serves `index.html` for non-API GET requests
- **State management**: Redux store (`src/store/index.ts`) with three slices:
  - `dashboardSlice` — SSE/WS connection state, chat slots, approval mode, optimistic slot add/remove reducers, async thunks for slot fetch / approval mode change
  - `chatSlice` — active slot, messages, history (sessions list with pagination), WS chunk/done handling, optimistic slot mutations, async thunks for slot CRUD / history fetch / resume / delete
  - `notificationsSlice` — notification list with add/delete/clear, async thunks for fetch/delete/clear
- **Real-time**: Single WebSocket at `/api/ws` multiplexes all real-time events (dashboard status, slots, slot_title, notification, refresh, chat_chunk, chat_done, chat_message, log, refine). `useWebSocket` hook with exponential backoff reconnect (1s→2s→4s→max 10s); on reconnect re-fetches state via Redux (no page reload) unless server version changes
- **API client**: `src/api/client.ts` — typed wrapper around all `/api/*` endpoints
- **Helpers**: `src/api/helpers.ts` — `esc()` (HTML escape), `md()` (markdown + DOMPurify), `sanitize()` (DOMPurify wrapper), `fmtSpeed()` (network speed formatting)
- **Types**: `src/types/index.ts` — shared interfaces (`ChatSlot`, `SessionInfo`, `ChatMessage`, `StatusData`, `Notification`, etc.)
- **Diff rendering**: `MarkdownRenderer` auto-detects diff code blocks (standard `+`/`-` format and kiro-cli `+N:`/`-N:` format) and renders with colored lines (green additions, red deletions, blue hunks)
- **Build output**: `vite.config.ts` outputs to `KiroCrew/src/kiro_crew/static/dist/`; `build-frontend.sh` runs the production build
- **Dev mode**: `./dev-frontend.sh` runs Vite dev server on port 3000 with API proxy to backend on 5476

## New Features

### Widget Event Bridge

Widgets (`<mcwidget>`) now support bidirectional communication via `data-action` events. Widget JS can emit structured events back to the agent session:

```js
window.parent.postMessage({type: 'kirocrew:action', action: 'submit', payload: {value: 42}}, '*')
```

### Chat Embedding (App SDK)

Apps can embed a full chat interface via the `ChatEmbed` component from `@kirocrew/sdk`:

```tsx
import { ChatEmbed } from '@kirocrew/sdk'
<ChatEmbed agent="my-agent" height={400} />
```

### Testing

- **jscpd duplication gate** — the build fails if copy-paste duplication is detected. Extract shared logic into utilities.
- **Vitest cobertura** — coverage emitted as cobertura XML for CI coverage integration.
- **Coverage integration** — coverage reports visible on pull requests via CI badges.

### URL Sanitization

`react-markdown` URL sanitizer now allows `vscode://` protocol URLs. Add new protocols to the allowlist in `urlTransform.ts`.

### Builtin App Auto-Discovery

Builtin apps no longer need manual `NAV_ITEMS` entries. The `builtinRegistry.ts` auto-discovers routes from the `src/pages/apps/` directory structure. To add a new builtin app:

1. Create `src/pages/apps/MyApp/index.tsx`
2. Export default component
3. Add route config to `builtinRegistry.ts`

### Frontend extension seams

Additive registries let a **downstream edition** (a separate build that composes
this SPA — e.g. an internal fork) contribute UI without copy-and-shadowing core
files. The core registers nothing new into them, so every seam is inert in the
stock build. There are **eight** registry seams:

| Seam | Module | Registrar → reader |
|------|--------|--------------------|
| Builtin page routes | `apps/builtinRegistry.ts` | `registerBuiltinComponents()` → `getBuiltinComponent()` |
| Nav icons | `apps/builtinIcons.tsx` | `registerBuiltinIcons()` → `getBuiltinIcon()` |
| Theme branding | `themeBranding.tsx` | `registerThemeBranding()` → `getThemeBranding()` |
| Theme picker options | `hooks/useTheme.tsx` | `registerTheme()` → `getRegisteredThemes()` |
| Top-bar widgets | `apps/topBarWidgets.tsx` | `registerTopBarWidgets()` → `getTopBarWidgets()` |
| Readout-capsule segments | `apps/capsuleSegments.tsx` | `registerCapsuleSegment()` → `getCapsuleSegments()` |
| Overview status cards | `pages/overviewStatCards.tsx` | `registerOverviewStatCards()` → `getOverviewStatCards()` |
| Panel nav + migration | `hooks/useKeyboardShortcuts.ts`, `components/MigrationCheck.tsx` | `registerPanelShortcut()`, `registerNonAppPrefix()` |

Plus one **exported-transport** seam for edition-owned API methods (not a
registry — see "API methods" below): `api/apiTransport.ts` exports `apiTransport`,
and the edition builds its own typed API module on it.

**Composition root.** `src/extensions.ts` is **core-owned** and imported first in
`main.tsx` (before the store/providers/`App`), so all registration runs before
render. It imports the `virtual:kirocrew-edition` module, which the
`editionExtensionPlugin` in `vite.config.ts` resolves to:
- an **inert empty module** in the stock OSS build (`KIROCREW_EDITION_DIR`
  unset) — the stock build registers nothing, byte-identical to no seam;
- the **downstream edition's own** `$KIROCREW_EDITION_DIR/extensions.tsx` when
  that env var points at an edition repo — so the edition injects its
  `register*()` calls + component imports **by build config**, compiled through
  the same vite/rollup pass, **without shadowing/overlaying any core file**
  (the copy-and-shadow erosion the seams exist to eliminate). A misconfigured
  `KIROCREW_EDITION_DIR` (set but missing `extensions.tsx`/`.ts`) **fails the
  build loudly** rather than silently degrading to the stock SPA.

**Edition-build safety (fail-closed opt-in):** edition composition is **opt-in
and fail-closed** — `KIROCREW_EDITION_DIR` alone is NOT enough; the plugin also
requires **`KIROCREW_ALLOW_EDITION=1`** or it THROWS. So every pipeline
(including release/publish, and the backend `setup.py` → `build-frontend.sh`
path) is protected by default: a stray/inherited `KIROCREW_EDITION_DIR` can
never silently compile proprietary edition sources into `website/dist` (the dist
staged into the public OSS wheel — a contaminated public release cannot be
unpublished). Only the edition's own build sets `KIROCREW_ALLOW_EDITION=1`.
Forgetting the opt-in fails safe (stock); there is no guard var a release job
must "remember to set." An edition-mode build additionally prints a loud
`[kirocrew-edition] ⚠ BUILDING WITH EDITION COMPOSITION ROOT` line so the mode
is unmissable in local + CI logs.

**Edition peer-dependency rule:** an edition dir resolves bare imports from its
OWN `node_modules`, so any **context-carrying singleton** the core's provider
tree owns must be de-duplicated or its hooks bind to a second instance
(`Invalid hook call`, `No QueryClient set`, null router context, silently empty
data — only at runtime, only in the edition build). `vite.config.ts`
`resolve.dedupe` covers `react`, `react-dom`, `react-redux`, `react-router`,
`react-router-dom`, `@tanstack/react-query`, `framer-motion`. **When the core
adds a new global-context provider, add its package here** (and the edition
should declare these as peer deps).

The core must register **nothing of its own** in `extensions.ts` —
`extensionSeams.test.tsx` asserts its stock body is the edition import + `export
{}` (plus comments). Put core registrations in the seed maps, never here.
Registries are read at module-load / first-render and are **not reactive** —
the edition registers via this import path, not after mount.

**Builtin routes.** `registerBuiltinComponents()` accepts only a single, plain
top-level path segment (`/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/`) — `BuiltinAppRoute`
resolves the catch-all `/:builtinApp` from one path parameter and matches only
`location.pathname`, never the query/hash. So a multi-segment (`/reports/daily`),
query (`/reports?daily`), hash (`/reports#x`), whitespace, or `.`/`..` route
would register but never resolve (navigation redirects to chat). A non-conforming
route routes through `reportSeamCollision`.

**Panel shortcuts.** `registerPanelShortcut({ code, path, label })` identifies the
chord solely by `KeyboardEvent.code`; the displayed key is derived from it, so the
advertised chord can never diverge from the handled one. Beyond core panel chords
and prior extensions, it also rejects any code in `RESERVED_PANEL_CODES` — the Alt
chords the handler consumes before panel routing (shortcuts modal, settings,
focus-input, MRU, chat-jump digits, arrows) — since a panel on one of those would
be advertised but unreachable. All rejections route through `reportSeamCollision`.

**Theme picker options.** `registerTheme([{ value, label }])` adds a built-in
theme to the picker; `useTheme` reads it via `allThemes = [...THEMES,
...registered, ...customThemes]`. The theme's CSS block ships in the edition's
overlay — this seam only contributes the picker entry. A `value` already in
`THEMES` or previously registered is rejected via `reportSeamCollision` (core
wins).

**Readout-capsule segments.** `registerCapsuleSegment([{ id, order?, component,
hideOnMobile? }])` mounts a status segment INSIDE the header's readout capsule
(sharing its border, `|` dividers, and offline tint), not as a standalone sibling
pill — use this over `registerTopBarWidgets` when the readout must join that
grouping (e.g. a credential-TTL or spend segment). App.tsx splices registered
segments after the core segments in `order`; each renders with an `offline` prop
and is isolated in its own `ErrorBoundary`.

**Overview status cards.** `registerOverviewStatCards([{ id, order?, component }])`
adds a self-contained `StatCard` (owning its own query/state, like the core
`TunnelStatus`) to the Settings → Overview grid, after the core cards. Each
receives a `delay` prop for the grid's stagger animation and is `ErrorBoundary`-
isolated.

**Theme branding reaches two consumers.** `getThemeBranding(colorTheme)` drives
both the App.tsx shell chrome AND `WelcomeView.tsx` (the new-session/welcome
screen brand mark) — a registered theme's `logo` shows in both, falling back to
the stock `KiroGhost` when the theme registers none.

**API methods.** There is no registrar. An edition imports `apiTransport` from
`api/apiTransport.ts` and writes its own fully-typed API module on it — see the
"API methods (exported transport, not a registry)" note below for why.

**Collision policy (`apps/seamCollision.ts`).** A registration whose key collides
with a core (or already-registered) entry is resolved core-wins. It is
**fail-loud in dev/test** (`reportSeamCollision` throws under
`import.meta.env.DEV`) so a colliding upstream sync is caught at build/test time,
and **degrades safe in production** (warn + ignore) so a shipped app never
white-screens over a duplicate.

**API methods (exported transport, not a registry).** Unlike the six registry
seams, the core never *consumes* edition API methods — they are written and read
only by the edition. A registry the core never reads would add public,
stringly-typed (`unknown`-cast) seam surface for zero composition benefit. So
instead of a registrar, `api/apiTransport.ts` **exports** the blessed
`apiTransport` — the same `get`/`post`/`put`/`del`/`patch` + `j`/`jNullable` the
core methods use (`client.ts` installs them via `installApiTransport` at module
load). An edition builds its OWN fully-typed API module on `apiTransport`:

```ts
import { apiTransport as t } from '../api/apiTransport'
export const editionApi = {
  midwayTtl: () => t.get('/api/midway-ttl').then(t.j) as Promise<MidwayTtl>,
}
```

This gives the edition the one thing it needs — the `X-Session-Key` header (the
fail-open ephemeral-gate guard) and the auth-recovery/`ApiError` pipeline by
construction — with full static types on the edition side and **no new *registry*
contract**. It never forks `client.ts` and never writes raw `fetch` (which would
silently drop the session key).

`ApiTransport` (the seven helper signatures + the `j`/`jNullable` semantics) **is**
a small, **intentionally-frozen** downstream contract — a separately-built edition
compiles against it. There is no `CONTRACT_VERSION`-style guard on this frontend
seam, so treat it like the backend's "CONTRACT_VERSION pinned at 1 pre-launch":
changing a request helper's shape or `j`'s error behavior is **edition-breaking**
(the stock build stays green — the seam is inert — so breakage surfaces only at
runtime in the out-of-repo edition), not a free refactor. Evolve additively. Each
`apiTransport` method is a stable wrapper that resolves the installed helper at
call time, so an edition may import/destructure it at module-init without an
ordering hazard vs. `extensions.ts`. Trust boundary: the transport carries the
session key — it is for the edition composition root, **never**
app/plugin-contributed frontend code.

**`onActivate` timing.** A theme branding's `onActivate` side-effect fires on the
first render for the initially-active theme (not only on a later switch); keep it
idempotent and cheap. Inert in the stock build (no theme registers one).
