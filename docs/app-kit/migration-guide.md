# Migration Guide — Adopting the App SDK

For apps that already talk to the KiroCrew Gateway via raw `fetch()`,
`urllib`, or custom HTTP wrappers, this guide shows how to migrate to the
supported integration paths step by step:

- **Dashboard UI pages (TypeScript/React)** → the host-provided
  `@kirocrew/app-sdk` hooks (`useAppApi`, `useAppEvents`, …).
- **Python apps / external CLI tools** → the standalone `kirocrew-client`
  package (`pip install kirocrew-client`).

## Why Migrate

- No more guessing endpoint paths and response shapes
- Permission-scoped API access declared in `app.json` (UI hooks)
- Built-in retry with exponential backoff (5xx, 429, network errors) — `kirocrew-client`
- Auth handling (localhost skip, token injection, app-secret auto-exchange)
- WebSocket reconnection with backoff
- Context injection with local buffering
- Structured errors instead of raw HTTP status codes
- Less hand-rolled HTTP/WS code

## Migration is Incremental

These paths call the same Gateway endpoints your code already uses.
You can replace one `fetch()` call at a time — no big-bang rewrite needed.

## TypeScript / Dashboard UI Apps

Dashboard UI pages use the `@kirocrew/app-sdk` hooks, which the dashboard host
provides at runtime via its import map — there is **no `npm install`** and no
published gateway-client npm package. Mark `@kirocrew/app-sdk` (and React,
ReactDOM, lucide-react) as build externals.

### Step 1: Import the hooks

```typescript
import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'
```

### Step 2: Create the client

```tsx
import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'

function MyPage() {
  const api = useAppApi()   // permission-scoped GET/POST/PUT/PATCH/DELETE
  // ...
}
```

The host injects auth automatically and scopes requests to the `permissions.api`
paths declared in your `app.json` — accessing an undeclared path throws.

### Step 2: Declare permissions

Add the API paths and WebSocket events your app uses to `app.json`:

```json
{
  "permissions": {
    "api": ["/api/status", "/api/chat/slots", "/api/crons", "/api/lessons"],
    "events": ["chat_chunk", "chat_done", "notification"]
  }
}
```

### Step 3: Replace HTTP calls

| Before (raw fetch) | After (`useAppApi`) |
|---------------------|-------------|
| `fetch('/api/status').then(r => r.json())` | `api.get('/api/status')` |
| `fetch('/api/chat/slots', { method: 'POST', body: JSON.stringify({name, agent}) })` | `api.post('/api/chat/slots', {name, agent})` |
| `fetch('/api/chat/slots').then(r => r.json())` | `api.get('/api/chat/slots')` |
| `fetch('/api/chat/slots/' + id, { method: 'DELETE' })` | `api.del('/api/chat/slots/' + id)` |
| `fetch('/api/chat', { method: 'POST', body: JSON.stringify({message, slot}) })` | `api.post('/api/chat', {message, slot})` |
| `fetch('/api/spawn', { method: 'POST', body: JSON.stringify({task}) })` | `api.post('/api/spawn', {task})` |
| `fetch('/api/crons').then(r => r.json())` | `api.get('/api/crons')` |
| `fetch('/api/lessons').then(r => r.json())` | `api.get('/api/lessons')` |

### Step 4: Replace WebSocket code

```tsx
// Before — custom WS with manual reconnect
const ws = new WebSocket(wsUrl)
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data)
  if (msg.type === 'chat_chunk' && msg.data?.slot === mySlot) {
    handleChunk(msg.data.content)
  }
}

// After — useAppEvents subscribes via the host's shared WebSocket and
// auto-unsubscribes on unmount (no manual reconnect or connection management)
useAppEvents('chat_chunk', (data) => {
  if (data.slot === mySlot) handleChunk(data.content)
})
useAppEvents('chat_done', () => handleDone())
```

### Step 5: Delete old auth + wrapper code

The host injects auth (cookies, app-secret token exchange, refresh) — you no
longer read secrets or build headers. Once all calls are migrated, remove your
custom HTTP client, WS manager, and auth helper files.

## Python Apps

### Step 1: Install

```bash
pip install kirocrew-client
```

### Step 2: Replace sync calls with async

```python
# Before — kiro_crew.apps.sdk (removed; was a sync client embedded in the main package)
from kiro_crew.apps.sdk import KiroCrewClient
mc = KiroCrewClient(app_name="my-tool")
result = mc.dispatch_agent("my-agent", "Do something")
mc.cron_add("refresh", every=3600, message="Check updates")

# After — kirocrew_client (async, standalone)
from kirocrew_client import KiroCrewClient
async with KiroCrewClient(app_name="my-tool") as mc:
    task_id = await mc.dispatch_agent_async("my-agent", "Do something")
    result = await mc.get_task_result(task_id)
    await mc.add_cron("refresh", message="Check updates", every=3600)
```

Key differences:

> **Note:** `kiro_crew.apps.sdk` no longer ships — it was removed in a prior
> release. Do not try to import it; use `kirocrew_client` for all Python apps.

| | Old (`kiro_crew.apps.sdk`, removed) | New (`kirocrew_client`) |
|---|---|---|
| I/O model | Sync (`urllib`) | Async (`aiohttp`) |
| Dependencies | Requires `kiro_crew` package | Standalone (only `aiohttp`) |
| Errors | `{"_error": True}` dicts | `KiroCrewError` exceptions |
| Retry | None | Built-in (exponential backoff) |
| Auth | Localhost only | Localhost + remote with app-secret auto-exchange |

### When to use which

| Scenario | Recommended |
|----------|-------------|
| App backend managed by KiroCrew (behind the gateway reverse proxy) | `kirocrew_client` (async) for outbound calls |
| External CLI tool or service (Python) | `kirocrew_client` (async, standalone) |
| Dashboard UI page (TypeScript/React) | `@kirocrew/app-sdk` hooks (host-provided) |
| Electron / Node.js app | Call the Gateway REST/WS endpoints directly via `fetch()` / a WebSocket |

## Backward Compatibility with Older Gateways

These paths call the same endpoints that have existed since KiroCrew 1.0.
Core APIs (slots, chat, spawn, cron, lessons) work with any Gateway version.

For newer features (like context injection), the Gateway returns 404 if the
endpoint doesn't exist. Handle this gracefully:

```tsx
// Dashboard UI via useAppApi() — a missing endpoint surfaces as a 404
const api = useAppApi()
try {
  await api.post('/api/context/inject', { slot: slotId, content: backgroundInfo, source: 'watch' })
} catch (err) {
  if (String(err).includes('404')) {
    fallbackMethod(backgroundInfo)   // Gateway doesn't support this yet
  } else {
    throw err
  }
}
```

```python
from kirocrew_client import KiroCrewError

try:
    await mc.inject_context(slot_id, background_info, source="watch")
except KiroCrewError as e:
    if e.code.value == "NOT_FOUND":
        fallback_method(background_info)
    else:
        raise
```

This pattern lets your app work with both old and new Gateway versions
without requiring users to upgrade.

## Migration Checklist

- [ ] Choose the path: `@kirocrew/app-sdk` hooks (dashboard UI) or `kirocrew-client` (Python / external)
- [ ] Python: `pip install kirocrew-client` and create `KiroCrewClient` at startup
- [ ] UI: import `useAppApi` / `useAppEvents` and declare `permissions.api` / `permissions.events` in `app.json`
- [ ] Replace raw HTTP calls (one at a time) with `api.get/post/...` or `kirocrew-client` methods
- [ ] Replace custom WebSocket code with `useAppEvents` (UI) or the client's `on*()` methods (Python)
- [ ] Add try/catch fallbacks for newer endpoints (context injection)
- [ ] Remove old HTTP/WS wrapper code
- [ ] Test against your target Gateway version
