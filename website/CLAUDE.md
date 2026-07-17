# CLAUDE.md — KiroCrew dashboard (frontend)

Guidance for Claude Code working in `website/`. This is the high-signal quick
reference; **`website/AGENTS.md` holds the exhaustive design system** (icon /
component / layout / styling / a11y / page-layout conventions). Read it before
UI work. The backend has its own `CLAUDE.md` at the repo root.

## What this is

The KiroCrew web dashboard: a React 18 + TypeScript + Vite 5 SPA. Production
builds output to `website/dist/`, which is staged into
`src/kiro_crew/static/dist/` and served by the Python backend.

**Stack:** React 18, Redux Toolkit, React Query (`@tanstack/react-query`),
React Router v7, Framer Motion, Tailwind CSS 3, Lucide React, DOMPurify,
highlight.js, Monaco, Vite 5.

## Build / dev / test

```bash
npm install
npm run build        # tsc -b + vite build → website/dist   (this is the real typecheck)
npm run dev          # Vite dev server on :3000, proxies /api to backend :5476
npm run check        # typecheck + lint + tests
npm run test         # vitest (website + electron)
npm run lint         # eslint src
```

After building, stage the bundle into the package so the backend serves it:

```bash
cp -R website/dist ../src/kiro_crew/static/dist
```

### Gotcha — `npm run typecheck` is a FALSE PASS

`npm run typecheck` runs `tsc --noEmit`, but the root `tsconfig.json` has
`files: []` + project references, so `--noEmit` checks **zero files** and always
passes. **Always use `tsc -b`** (which `npm run build` runs) to actually
type-check the project. Don't trust a green `npm run typecheck` alone.

### Gotcha — localStorage polyfill in tests

`website/integration/setup.ts` installs an in-memory `localStorage` /
`sessionStorage` polyfill. This is **required**: Node 25's native
`--localstorage-file` storage shadows jsdom's spec-complete `Storage` and lacks
`.clear()`, which breaks the test suite. The polyfill puts methods on
`Storage.prototype` so quota-error spies still work — don't remove it or move
the methods off the prototype.

## This is a public OSS fork — don't reintroduce internal couplings

This is the de-Amazoned public fork. When changing the frontend, **do not
reintroduce**:

- Build/infra: `npm-pretty-much`, Brazil, AIM, CodeArtifact registries,
  Coverlay/jscpd-as-brazil-gate. The public build is plain **npm + Vite**, and
  `website/.npmrc` pins the **public** registry (`registry.npmjs.org`).
- Identity/telemetry: live Cognito pools or RUM app ids (`src/rum.ts` is a
  no-op telemetry stub — keep it inert), `aws-rum-web`. The backend is
  KiroACP (`kiro-cli`) only; the frontend never needs an ACP adapter as a web
  dependency.
- Removed product surfaces: TaskKeeper, secretary, mimir, code-reviewer pages /
  tabs / API client methods, and the Midway card on the Overview page. They were
  deleted with their backend; don't re-add the UI.

> Stale references: `website/Config` (Brazil / `npm-pretty-much`) and
> `website/AUTOSDE.yaml` are leftover internal files not used by the public
> build — ignore them, and treat any "brazil-build" / "Coverlay" /
> "`@kirocrew/sdk`" mentions in `website/AGENTS.md` as historical, not current.

## Core conventions (full details in `website/AGENTS.md`)

- **Icons: Lucide only, never emojis.** `import { X } from 'lucide-react'` with
  `className="lucide-inline"`. No `size={N}`, no inline SVG, no emoji in
  rendered UI. (A few documented catalog/scene/theme exceptions.)
- **Data fetching: React Query** (`useQuery` / `useMutation`), not manual
  `useState` + `useEffect`. Query keys are `['resource-name']`.
- **a11y:** use `<Clickable>` (not `<div onClick>`); icon-only buttons need
  `aria-label`; modals need `role="dialog"` + focus trap + Escape.
- **Shared components:** prefer `Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`,
  `Badge`, `StatCard`, `SearchInput`, `EmptyState`, `PageHeader` from
  `src/components/ui.tsx` over raw `<button>`/`<input>`/`<select>`.
- **Typography:** body 14px; no text below 10px; **don't use `text-xs`** — use
  `text-[13px]`.
- **Animations:** Framer Motion for orchestrated transitions; Tailwind
  `transition-*`/`animate-*` for simple states. No new CSS `@keyframes`.
- **Security:** all `dangerouslySetInnerHTML` content goes through DOMPurify via
  `md()` / `sanitize()` in `src/api/helpers.ts`.

## Architecture

- **Entry:** `src/main.tsx` (Redux `<Provider>` + `<BrowserRouter>`).
- **Routing:** `App.tsx`; backend SPA-fallback serves `index.html` for non-API
  GETs.
- **State:** Redux store (`src/store/index.ts`) — `dashboardSlice`, `chatSlice`,
  `notificationsSlice`. Use React Query for server state.
- **Real-time:** single WebSocket at `/api/ws` multiplexes all events;
  `useWebSocket` reconnects with exponential backoff and re-fetches via Redux.
- **API client:** `src/api/client.ts` (typed wrapper over `/api/*`).
- **Build output:** `vite.config.ts` → `dist/`, staged into
  `src/kiro_crew/static/dist/`.

## Browser support

Chrome, Firefox, Safari, Edge. Use standard Web APIs only; guard
browser-specific ones (e.g. `typeof Notification !== 'undefined'`).
