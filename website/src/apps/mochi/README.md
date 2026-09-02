# Mochi — architecture map

Mochi is a desktop pet companion, migrated from a standalone Electron app into a
KiroCrew builtin. This file is the map: where everything lives, why the layout
looks the way it does, and where the seams are. Read it before moving files or
adding cross-boundary calls.

## The four roots

| Root | What lives there |
|---|---|
| `website/src/apps/mochi/` (here) | All frontend: window entry HTML + React, bridges, vendored original renderer |
| `src/kiro_crew/apps/builtins/mochi/` | Python backend: runtime (`hooks.py`), services, MCP server, routes, agents/skills |
| `website/electron/mochi/` | Electron shell layer: pet overlay windows, preload, instance resolution |
| `test/test_mochi_*.py` | Backend tests (flat `test/`, prefix-contained — see "Test placement") |

Dependency direction is one-way: mochi imports core, **core never imports
mochi**. Core names mochi in exactly two registry lines
(`apps/builtins/__init__.py`, `apps/builtinRegistry.ts`) plus one `require` in
`electron/main.js`. Removing the app = delete the four roots + those lines +
the `apps.mochi` i18n subtree.

## Why the double `src` (`src/apps/mochi/src/renderer/...`)

The migration vendors the original app's `src/renderer` and `src/shared` trees
**verbatim** — each vendored file changed by exactly one import line, pointing
at the `api` seam. Keeping the original's internal layout (including its own
`src/`) is what keeps diffs against upstream reviewable and lets fixes be
ported line-for-line. It is intentional, not a nesting mistake.

- `src/renderer/`, `src/shared/` — vendored original code. Change sparingly;
  prefer changing the seam.
- `src/mochiApi.ts` — **the** seam. The composed `api` handle every vendored
  file imports. Original IPC calls resolve here to HTTP routes, WS events, or
  Electron preload channels. Spread order matters (web transports win over
  shell channels so a browser tab works).
- `pet/`, `panel/` — entry wiring + bridges for the standalone windows
  (`petBridge.ts`, `panelBridge.ts`): subscribe gateway WS events and forward
  them into the vendored components.
- `*.html` — Vite multi-page window entries. They live in-app; the gateway
  serves them at `/mochi-<name>.html` via the generic app-window-entries
  discovery (`dashboard/server.py`), so the URL contract is independent of
  file location.
- `test/` — frontend tests (vitest picks up `src/**/*.test.*` with zero
  config).

## Backend shape (`src/kiro_crew/apps/builtins/mochi/`)

`hooks.py` owns the runtime: service graph, owner loop (all periodic work
ticks here — no free-floating tasks), notification gate, and the late-bound
spawn seam. Services are one module each, named after their upstream
counterpart so diffs stay portable (`queue_poller.py`, `watchlist_service.py`,
…). `mcp_server.py` runs as a **separate process**; it communicates with the
runtime only through the queue/watchlist files (`queue_file.py` is the shared
format contract) and validates every tool call against its declared
inputSchema before dispatch. `backend/routes.py` is the HTTP surface — all
routes gated on the app being enabled.

## Electron layer (`website/electron/mochi/`)

The pet needs real OS windows (transparent, click-through, multi-monitor), so
this layer runs in the Electron **main process** — a deliberate first-party
exception, not a generic extension point (arbitrary app JS in the main process
would bypass sandboxing). `main.js` touches it through exactly two calls
(`initMochi` / `shutdownMochi` in `index.js`); injected context is origin +
token fetcher + logger, nothing else. Shell tests live in `test/` here.

## Test placement

Frontend and Electron tests live inside the mochi folders. Backend tests stay
in the repo's flat `test/` with the `test_mochi_` prefix — moving them out
would silently lose `conftest.py`'s autouse fixtures (including the
`KIROCREW_HOME` isolation that keeps tests from writing to the real data
home), and in-package tests would ship in the wheel.

## Rules of thumb

- New capability the vendored code needs → add it to `mochiApi.ts`, not a new
  import path into core.
- New backend work → a service module ticked by the owner loop, not a new
  asyncio task.
- Anything that would put a mochi name in a core file → stop; find or build
  the generic mechanism instead (precedent: app window entries discovery).
- Prose the agent sees (`agents/`, skills, MCP tool descriptions) must match
  actual behavior — it ships to users.
