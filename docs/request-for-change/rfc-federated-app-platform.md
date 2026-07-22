# RFC: Federated App Platform — Dynamic ESM Loading with Import Maps

**Author:** KiroCrew contributors  
**Date:** 2026-04-18  
**Status:** Draft

---

## 1. Problem Statement

KiroCrew's app system today supports installing agent/skill/cron bundles from local directories. The UI side is limited: apps can either host their own HTTP backend (embedded via iframe) or have no visual presence at all. This creates three problems:

1. **No federated UI loading** — apps cannot contribute React pages that feel native to the KiroCrew dashboard. The iframe approach has well-known UX limitations (scrolling, focus, accessibility, no shared theme/components, double scrollbars).

2. **No remote registry** — apps can only be installed from local file paths. There's no way for a developer to publish an app and have other users discover and install it from within KiroCrew.

3. **Built-in features that should be apps** — features like Agent Worlds (pixel art scenes), Channels (multi-agent chat), and Schedule (cron UI) are hardcoded into the KiroCrew frontend. They can't be independently versioned, disabled, or replaced. New features require modifying the core codebase.

The goal is a platform where:
- A developer creates a new package, writes a React component, publishes it
- A user discovers it in KiroCrew's App Store, clicks Install, and gets a new sidebar page
- The app looks and feels native — same theme, same components, same React tree
- No iframes, no Web Components, no separate backend processes

## 2. Design Principles

1. **Apps are React components.** The contract is: export a default React component. No framework abstraction, no custom rendering layer. App developers use the same React + Tailwind + Lucide stack as KiroCrew core.

2. **Import maps for shared dependencies.** React, ReactDOM, and KiroCrew's UI library are provided by the host via browser-native [import maps](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/script/type/importmap). Apps externalize these dependencies at build time. Result: tiny app bundles, single React instance, hooks and context work across the boundary.

3. **Permission-scoped API surface.** Apps declare which API endpoints and real-time events they need. The SDK provides a fetch wrapper that enforces these permissions client-side. In a future hosted deployment, the server enforces them too.

4. **Supply chain trust, not browser sandboxing.** Apps run in the same origin and DOM as the host. Security comes from registry review, bundle hash verification, and permission scoping — not from iframes or Shadow DOM. This is the same model as VS Code extensions, Backstage plugins, and Figma's non-iframe plugin mode.

5. **Progressive complexity.** An agent-only app (no UI) is just a manifest + agent JSON + skill files. A UI app adds a React component. A full app adds crons, custom agents, and multiple pages. The simplest case requires no build tooling at all.

## 3. Architecture

### 3.1 High-Level Flow

```
┌──────────────────────────────────────────────────────────────┐
│  KiroCrew Host                                               │
│                                                              │
│  index.html                                                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ <script type="importmap">                              │  │
│  │   "react" → /vendor/react.mjs                          │  │
│  │   "react-dom" → /vendor/react-dom.mjs                  │  │
│  │   "@kirocrew/ui" → /vendor/kirocrew-ui.mjs             │  │
│  │   "@kirocrew/app-sdk" → /vendor/kirocrew-app-sdk.mjs   │  │
│  │ </script>                                              │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  App.tsx (router)                                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ <Route path="/chat" element={<ChatPage />} />          │  │
│  │ <Route path="/overview" element={<OverviewPage />} />  │  │
│  │ ...                                                    │  │
│  │ {installedApps.map(app =>                              │  │
│  │   <Route path={app.route} element={                    │  │
│  │     <AppHost app={app} />     ← dynamic import here    │  │
│  │   } />                                                 │  │
│  │ )}                                                     │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  AppHost.tsx                                                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 1. Read manifest → get permissions                     │  │
│  │ 2. Create permission-scoped API context                │  │
│  │ 3. React.lazy(() => import(bundlePath))                │  │
│  │ 4. Wrap in ErrorBoundary + Suspense                    │  │
│  │ 5. Render <AppComponent /> in the same React tree      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 Import Map Strategy

The host's `index.html` declares an import map that maps bare specifiers to vendored ESM bundles served by the KiroCrew backend:

```html
<script type="importmap">
{
  "imports": {
    "react": "/vendor/react.mjs",
    "react-dom": "/vendor/react-dom.mjs",
    "react-dom/client": "/vendor/react-dom-client.mjs",
    "react/jsx-runtime": "/vendor/react-jsx-runtime.mjs",
    "@kirocrew/ui": "/vendor/kirocrew-ui.mjs",
    "@kirocrew/app-sdk": "/vendor/kirocrew-app-sdk.mjs",
    "lucide-react": "/vendor/lucide-react.mjs",
    "framer-motion": "/vendor/framer-motion.mjs"
  }
}
</script>
```

When an app bundle does `import { Card } from '@kirocrew/ui'`, the browser resolves it to the host's vendored copy. No duplicate React instances, no hook violations, no bundle bloat.

The vendored files are generated at KiroCrew build time by extracting ESM builds of each shared dependency. The Vite build already produces these — we just need to copy them to a `/vendor/` static directory.

### 3.3 App Manifest Schema

```json
{
  "name": "agent-worlds",
  "version": "0.1.0",
  "displayName": "Agent Worlds",
  "description": "Pixel art visualizations of your agents at work",
  "author": "priyag",
  "tags": ["visualization", "agents", "fun"],
  "kirocrew": ">=1.3.0",

  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/worlds",
        "label": "Worlds",
        "icon": "Gamepad2",
        "group": "Apps"
      }
    ]
  },

  "permissions": {
    "api": ["/api/agents", "/api/status"],
    "events": ["agent:status", "agent:spawn", "agent:done"]
  },

  "agents": ["agents/world-narrator.json"],
  "skills": ["skills/world-lore"],
  "crons": [],
  "sops": []
}
```

Fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique identifier (kebab-case) |
| `version` | string | yes | Semver version |
| `displayName` | string | no | Human-readable name |
| `description` | string | no | One-line description |
| `author` | string | no | Author alias |
| `tags` | string[] | no | Searchable tags |
| `kirocrew` | string | no | Minimum KiroCrew version (semver range) |
| `ui.entry` | string | no | Path to ESM bundle relative to app root |
| `ui.pages` | Page[] | no | Routes to register in the sidebar |
| `ui.pages[].route` | string | yes | URL path (e.g. `/worlds`) |
| `ui.pages[].label` | string | yes | Sidebar label |
| `ui.pages[].icon` | string | no | Lucide icon name |
| `ui.pages[].group` | string | no | Sidebar group (`Apps` default) |
| `permissions.api` | string[] | no | Allowed API path prefixes |
| `permissions.events` | string[] | no | Allowed WebSocket event types |
| `agents` | string[] | no | Agent definition files |
| `skills` | string[] | no | Skill directories |
| `crons` | CronEntry[] | no | Cron job definitions |
| `sops` | string[] | no | SOP files |

The `backend` field from the current manifest is **removed**. Apps do not host their own backend processes.

### 3.4 AppHost Component

```tsx
// frontend/src/components/AppHost.tsx
import { Suspense, lazy, useMemo } from 'react'
import ErrorBoundary from './ErrorBoundary'
import { AppApiProvider } from '@kirocrew/app-sdk'
import { ContentSkeleton } from './ui'

interface AppHostProps {
  app: {
    name: string
    bundlePath: string  // e.g. /apps/agent-worlds/dist/index.mjs
    permissions: {
      api: string[]
      events: string[]
    }
  }
}

export default function AppHost({ app }: AppHostProps) {
  const LazyApp = useMemo(
    () => lazy(() => import(/* @vite-ignore */ app.bundlePath)),
    [app.bundlePath]
  )

  return (
    <ErrorBoundary
      fallback={<AppCrashFallback appName={app.name} />}
      onError={(error) => {
        console.error(`[AppHost] ${app.name} crashed:`, error)
        // Future: report to telemetry
      }}
    >
      <AppApiProvider
        appName={app.name}
        allowedApiPaths={app.permissions.api}
        allowedEvents={app.permissions.events}
      >
        <Suspense fallback={<ContentSkeleton rows={8} />}>
          <LazyApp />
        </Suspense>
      </AppApiProvider>
    </ErrorBoundary>
  )
}

function AppCrashFallback({ appName }: { appName: string }) {
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="text-center max-w-md">
        <div className="text-[48px] mb-4">💥</div>
        <h3 className="text-text font-medium mb-2">{appName} crashed</h3>
        <p className="text-sm text-muted mb-4">
          The app encountered an error. You can reload it or disable it.
        </p>
        <div className="flex gap-2 justify-center">
          <button onClick={() => window.location.reload()}>Reload</button>
          <button onClick={() => { /* disable app */ }}>Disable</button>
        </div>
      </div>
    </div>
  )
}
```

### 3.5 App SDK (`@kirocrew/app-sdk`)

The SDK is a lightweight package that apps import. It provides React hooks backed by a context that `AppHost` sets up.

```typescript
// @kirocrew/app-sdk — public API

// Hooks
export function useAppApi(): AppApi
export function useAppEvents(event: string, callback: (data: any) => void): void
export function useTheme(): { mode: 'dark' | 'light'; accent: string; colorTheme: string }
export function useAppInfo(): { name: string; version: string; permissions: Permissions }
export function useNavigate(): (path: string) => void
export function useNotify(): (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void

// Types
export interface AppApi {
  get<T = any>(path: string, init?: RequestInit): Promise<T>
  post<T = any>(path: string, body?: any): Promise<T>
  put<T = any>(path: string, body?: any): Promise<T>
  delete<T = any>(path: string): Promise<T>
}

export interface Permissions {
  api: string[]
  events: string[]
}

// Vite plugin (for app build tooling)
export function kirocrewApp(): VitePlugin

// Context provider (used by AppHost, not by apps directly)
export function AppApiProvider(props: {
  appName: string
  allowedApiPaths: string[]
  allowedEvents: string[]
  children: React.ReactNode
}): JSX.Element
```

The `AppApiProvider` wraps the standard `fetch` in a permission check:

```typescript
function createScopedApi(allowedPaths: string[]): AppApi {
  const check = (path: string) => {
    if (!allowedPaths.some(p => path.startsWith(p))) {
      throw new Error(`App not permitted to access ${path}`)
    }
  }
  return {
    async get(path, init) {
      check(path)
      const res = await fetch(path, init)
      return res.json()
    },
    // ... post, put, delete similarly
  }
}
```

### 3.6 UI Component Library (`@kirocrew/ui`)

The existing shared components in `frontend/src/components/ui.tsx` are extracted into a standalone package. App developers `import { Card, Btn, Badge, PageHeader } from '@kirocrew/ui'` and get themed components that match the host.

The package exports:
- `Card`, `CardTitle`
- `Btn`, `SendBtn`
- `Input`, `SearchInput`
- `Badge`, `AimBadge`
- `StatCard`
- `Skeleton`, `ContentSkeleton`
- `EmptyState`
- `PageHeader`
- `Toggle`
- `InfoTip`
- `SegmentedControl`

These components consume CSS custom properties (`--bg`, `--text`, `--accent`, etc.) that are defined on the host's `<body>`. Since apps run in the same DOM, the theme just works.

### 3.7 Registry

The registry is a JSON index file hosted on a CDN (or internal S3 bucket). The existing `registry.py` already supports fetching and caching a remote index. We extend `RegistryEntry` with bundle-related fields:

```json
{
  "version": "1",
  "apps": [
    {
      "name": "agent-worlds",
      "displayName": "Agent Worlds",
      "description": "Pixel art visualizations of your agents at work",
      "version": "0.1.0",
      "author": "priyag",
      "tags": ["visualization", "agents", "fun"],
      "kirocrew": ">=1.3.0",
      "icon": "Gamepad2",
      "bundleUrl": "https://kirocrew-apps.example.com/agent-worlds/0.1.0/index.mjs",
      "bundleHash": "sha384-abc123...",
      "manifestUrl": "https://kirocrew-apps.example.com/agent-worlds/0.1.0/app.json",
      "permissions": {
        "api": ["/api/agents", "/api/status"],
        "events": ["agent:status"]
      }
    }
  ]
}
```

Install flow:
1. User clicks "Install" in App Store UI (or runs `kirocrew app install agent-worlds`)
2. KiroCrew downloads `app.json` from `manifestUrl`
3. KiroCrew downloads `index.mjs` from `bundleUrl`
4. Verifies `bundleHash` matches the downloaded file
5. Stores both in `~/.kirocrew/apps/agent-worlds/`
6. Registers agents, skills, crons via existing bridge system
7. Adds route to sidebar — no restart needed

### 3.8 App File Layout (Installed)

```
~/.kirocrew/apps/
  agent-worlds/
    app.json              ← manifest
    ui/
      index.mjs           ← ESM bundle (downloaded from registry)
    agents/
      world-narrator.json ← agent definition (from package)
    skills/
      world-lore/
        SKILL.md           ← skill file (from package)
    installed.json         ← install metadata (version, date, source)
```

The KiroCrew backend serves `~/.kirocrew/apps/{name}/ui/*` at `/apps/{name}/ui/*` as static files. The import in `AppHost` resolves to `/apps/agent-worlds/ui/index.mjs`.

## 4. Developer Experience

### 4.1 Scaffold

```bash
kirocrew app init agent-worlds --with-ui
```

Creates a package `KiroCrewApp-AgentWorlds` with:
- `app.json` (manifest with sensible defaults)
- `vite.config.ts` (pre-configured externals, library mode)
- `src/index.tsx` (hello-world React component)
- `agents/` and `skills/` directories
- `README.md` with getting-started instructions

### 4.2 Local Development

```bash
kirocrew app dev
```

1. Starts Vite dev server on port 3001 with HMR
2. Registers the app with the running KiroCrew instance in dev mode
3. KiroCrew loads the app from `http://localhost:3001/src/index.tsx` (Vite's native ESM)
4. Hot module replacement works — edit, save, see changes instantly in KiroCrew

The dev server proxies API calls to the KiroCrew backend, so the app has access to real data.

### 4.3 Build

```bash
npm run build
```

Runs Vite in library mode. Output: `build/dist/index.mjs` (~20-100KB depending on app complexity). The `kirocrewApp()` Vite plugin:
- Validates `app.json` against the manifest schema
- Checks that declared API permissions reference valid endpoint prefixes
- Generates `build/dist/manifest.json` with content hash

### 4.4 Publish

```bash
kirocrew app publish
```

1. Builds the package
2. Uploads `index.mjs` and `app.json` to the app CDN
3. Computes and records the bundle hash
4. Updates the registry index with the new entry

### 4.5 What an App Author Writes

```tsx
// src/index.tsx — the entire app
import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'
import { PageHeader, Card, Badge } from '@kirocrew/ui'
import { useState, useEffect } from 'react'
import OfficeScene from './scenes/OfficeScene'

export default function AgentWorldsApp() {
  const api = useAppApi()
  const [agents, setAgents] = useState([])

  useEffect(() => {
    api.get('/api/agents').then(setAgents)
  }, [api])

  useAppEvents('agent:status', () => {
    api.get('/api/agents').then(setAgents)
  })

  return (
    <>
      <PageHeader title="Agent Worlds" subtitle="Watch your agents work" />
      <OfficeScene agents={agents} />
    </>
  )
}
```

The app author doesn't think about:
- Import maps (handled by KiroCrew host)
- Theme integration (CSS custom properties just work)
- Permission enforcement (SDK handles it)
- Bundle optimization (React is externalized automatically)
- Deployment (publish command handles upload + registry)

## 5. Security Model

### 5.1 Threat Model

| Threat | Mitigation |
|--------|------------|
| Malicious app steals user data | Registry review process + bundle hash pinning |
| Supply chain tampering after review | Subresource Integrity (hash verification on install) |
| App accesses unauthorized APIs | Permission-scoped API proxy (client-side now, server-side in hosted mode) |
| App crashes takes down KiroCrew | ErrorBoundary per app |
| App causes memory leak / CPU abuse | Future: runtime monitoring + kill switch |
| Code injection at network level | CSP headers in hosted mode |

### 5.2 Trust Tiers

| Tier | Trust Level | Example | Verification |
|------|-------------|---------|--------------|
| Built-in | Full | Chat, Overview, Settings | Part of KiroCrew core, no dynamic loading |
| Curated | High | Apps in official registry | Reviewed, signed, hash-pinned |
| Community | Medium | Apps from community registry | Hash-pinned, permissions displayed at install |
| Local | User-controlled | `kirocrew app install ./my-app` | No verification, user takes responsibility |

### 5.3 Permission Enforcement

**Phase 1 (local):** Client-side only. The `AppApiProvider` wraps `fetch` and checks paths against the declared `permissions.api` list. A determined app could bypass this by calling `fetch` directly. This is acceptable for the local-first model where apps are installed by the user.

**Phase 2 (hosted):** Server-side enforcement. The backend associates each request with the originating app (via a session token or request header injected by the SDK). API handlers check the app's declared permissions before processing. Bypass is not possible.

## 6. Migration Path

### Phase 1: Foundation (current sprint)

- Remove `backend.py`, `BackendConfig`, iframe `AppLoader`, process management
- Keep existing app system for agent/skill/cron bundles
- Extract `@kirocrew/ui` from `frontend/src/components/ui.tsx`
- Add import map to `index.html`
- Build `AppHost.tsx` with dynamic `import()` + ErrorBoundary
- Build `@kirocrew/app-sdk` with `useAppApi`, `useAppEvents`, `useTheme`
- Serve installed app bundles from `/apps/{name}/ui/*`

### Phase 2: First App Extraction

- Extract Agent Worlds as the proof-of-concept app
  - Create `KiroCrewApp-AgentWorlds` package
  - Move scene components from `frontend/src/pages/scenes/` to the app
  - Move `WorldsPage.tsx` logic into the app's `index.tsx`
  - Remove Worlds from KiroCrew core's router and sidebar
  - Install the app via `kirocrew app install` — it appears in the sidebar as before
- Validate the full cycle: scaffold → develop → build → install → render

### Phase 3: Developer Tooling

- `kirocrew app init` scaffold command
- `kirocrew app dev` local development with HMR
- `kirocrewApp()` Vite plugin for build validation
- `kirocrew app publish` upload to registry
- Documentation and app developer guide

### Phase 4: Registry & Discovery

- Host registry index on CDN (S3 + CloudFront)
- App Store "Browse" tab fetches registry and displays available apps
- One-click install from Browse tab
- Bundle hash verification on install
- Version update detection and upgrade flow

### Phase 5: Hosted Mode Hardening (future)

- Server-side permission enforcement
- CSP headers restricting script sources
- App session tokens for request attribution
- Runtime monitoring (CPU, memory, DOM mutation rate)
- App sandboxing via Web Workers for compute-heavy apps (optional)

## 7. What Gets Removed

The following modules are removed in Phase 1, replaced by the federated loading system:

| Module | Purpose | Replacement |
|--------|---------|-------------|
| `apps/backend.py` | Spawn local HTTP servers for apps | Removed — apps don't host backends |
| `apps/manifest.py` `BackendConfig` | Backend config in manifest | Removed from schema |
| `frontend/src/components/AppLoader.tsx` | iframe embedding | `AppHost.tsx` with dynamic import |
| `frontend/src/pages/AppPage.tsx` | iframe + backend status UI | `AppHost.tsx` handles all app rendering |

The following modules are **kept** and enhanced:

| Module | Current Purpose | Enhancement |
|--------|----------------|-------------|
| `apps/manager.py` | Install/uninstall/enable/disable | Add bundle download + hash verification |
| `apps/registry.py` | Registry fetch + cache | Add `bundleUrl`, `bundleHash` fields |
| `apps/bridges.py` | Register agents/skills/crons | No change needed |
| `apps/scaffold.py` | `kirocrew app init` | Add `--with-ui` flag, Vite config generation |
| `apps/routes.py` | REST API for app management | Add bundle serving endpoint |
| `frontend/src/pages/AppsPage.tsx` | App Store UI | Add Browse tab with registry integration |

## 8. Open Questions

1. **Import map generation** — Should the import map be static in `index.html` or dynamically generated by the backend based on installed apps? Static is simpler but can't add per-app entries. Dynamic allows apps to declare additional shared dependencies. (Proposal: static for Phase 1, dynamic if needed later.)

2. **CSS isolation** — Apps share the host's Tailwind classes. A poorly written app could accidentally style host elements. (Proposal: acceptable risk for curated apps. Future: CSS Layers or scoped class prefixes if needed.)

3. **Multi-page apps** — An app can declare multiple pages. Should each page be a separate lazy-loaded chunk, or one bundle for all pages? (Proposal: single bundle per app for simplicity. Apps can code-split internally with `React.lazy`.)

4. **App-to-app communication** — Should apps be able to communicate with each other? (Proposal: not in scope. Apps communicate with KiroCrew core via the SDK. If needed later, a pub/sub event bus can be added.)

5. **Offline support** — Installed app bundles are cached locally. Should KiroCrew work fully offline with installed apps? (Proposal: yes — bundles are downloaded at install time, not fetched on every page load.)

6. **Version pinning** — When a new version of an app is published, should KiroCrew auto-update or require user action? (Proposal: show "update available" badge, user clicks to update. No auto-update for apps — different from KiroCrew core updates.)

7. **Shared state** — Some apps may want to read KiroCrew's Redux store (e.g. connection status, active slot). Should the SDK expose this? (Proposal: expose read-only selectors for common state like `useConnectionStatus()`, `useActiveSlot()`. Don't expose the full store.)

## 9. Success Criteria

- Phase 1: Agent Worlds renders as a dynamically loaded app with the same UX as the current built-in page. No visual regression. Import map resolves shared dependencies correctly. ErrorBoundary catches app crashes without affecting the host.
- Phase 2: A developer can scaffold, develop, build, and install an app end-to-end using CLI commands. Hot reload works during development.
- Phase 3: The App Store Browse tab shows apps from the registry. One-click install works. Bundle hash is verified.
- Phase 4: At least 3 apps published by different developers. No iframe, no backend process, no build-time coupling to KiroCrew core.
