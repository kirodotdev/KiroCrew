# KiroCrewWebsite — Agent Guidelines

KiroCrew dashboard frontend — React + TypeScript SPA. Built assets are bundled into `KiroCrew/src/kiro_crew/static/dist/`.

## Stack

React 18, Redux Toolkit (`@reduxjs/toolkit`), React Query (`@tanstack/react-query`), React Router v7 (`react-router-dom`), Framer Motion (`framer-motion`), Tailwind CSS 3, Lucide React (`lucide-react`), DOMPurify, highlight.js, TypeScript, Vite 5.

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
