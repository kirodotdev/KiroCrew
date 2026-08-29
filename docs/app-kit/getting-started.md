# Getting Started with KiroCrew Apps

Build, install, and run your first KiroCrew app in 5 minutes.

## Prerequisites

- KiroCrew installed and running (`kirocrew gateway`)
- Node.js 22+ (24 LTS recommended) (for apps with UI)

## 1. Create an App Directory

Create a new directory with an `app.json` manifest:

```
my-dashboard/
├── app.json                 ← App manifest (required)
├── agents/
│   └── sample-agent.json    ← Agent definition
├── skills/
│   └── sample-skill/
│       └── SKILL.md         ← Skill knowledge file
├── ui/                      ← Frontend (if app has UI)
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/App.tsx
│   └── .gitignore
└── README.md
```

## 2. Edit Your App

### app.json — The Manifest

Every app needs an `app.json`. See [Manifest Reference](manifest-reference.md) for all fields.

```json
{
  "name": "my-dashboard",
  "version": "0.1.0",
  "displayName": "My Dashboard",
  "description": "A KiroCrew app: My Dashboard",
  "author": "yourname",
  "agents": ["agents/sample-agent.json"],
  "skills": ["skills/sample-skill"],
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/apps/my-dashboard",
        "label": "My Dashboard",
        "icon": "Package"
      }
    ]
  }
}
```

### UI Page — React Component

Edit `ui/src/App.tsx`. Your app is a standard React component that uses
`@kirocrew/app-sdk` hooks and `@kirocrew/app-sdk/ui` shared components.

> **You do not `npm install` `@kirocrew/app-sdk`.** The dashboard host provides
> it (and React, ReactDOM, lucide-react) at runtime through its import map: the
> bare `@kirocrew/app-sdk` specifier resolves to the host's vendored copy via
> `window.__kirocrew_modules`. This guarantees your app shares the host's exact
> React instance (so hooks work) and stays a small bundle. Mark these as
> externals in your build (don't bundle them).

```tsx
import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'
import { Card, CardTitle, PageHeader, StatCard } from '@kirocrew/app-sdk/ui'
import { useState, useEffect } from 'react'

export default function MyDashboard() {
  const api = useAppApi()
  const [data, setData] = useState(null)

  useEffect(() => {
    api.get('/api/status').then(setData)
  }, [])

  // Listen to real-time events
  useAppEvents('notification', (event) => {
    console.log('New notification:', event)
  })

  return (
    <>
      <PageHeader title="My Dashboard" subtitle="Custom app page" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Status" value={data ? 'Online' : '...'} accent />
        </div>
        <Card>
          <CardTitle>Content</CardTitle>
          <p className="text-sm text-muted">Your app content here.</p>
        </Card>
      </div>
    </>
  )
}
```

### Agent — AI Configuration

Edit `agents/sample-agent.json` to customize your agent:

```json
{
  "name": "my-agent",
  "model": "auto",
  "description": "Analyzes data and generates reports",
  "prompt": "You are a data analyst assistant.",
  "tools": ["@kirocrew-core"]
}
```

### Skill — Domain Knowledge

Edit `skills/sample-skill/SKILL.md` to teach your agent domain knowledge.

## 3. Build the UI

```bash
cd my-dashboard/ui
npm install
npm run build
```

This produces `dist/index.mjs` — the ESM bundle loaded by the dashboard.

## 4. Install and Enable

Install via the KiroCrew dashboard REST API or the App Store UI:

```bash
# Via curl (REST API)
curl -X POST http://localhost:5476/api/apps/install \
  -H 'Content-Type: application/json' \
  -d '{"source": "/path/to/my-dashboard"}'

curl -X POST http://localhost:5476/api/apps/my-dashboard/enable
```

Or open the KiroCrew dashboard → App Store → install from local path.

Your app now appears in the KiroCrew dashboard sidebar.

## 5. Iterate

During development:

1. Edit `ui/src/App.tsx`
2. Run `cd ui && npm run build`
3. Update the installed app:
   ```bash
   curl -X POST http://localhost:5476/api/apps/my-dashboard/update
   ```
4. Refresh the dashboard — changes are live

Agent and skill changes take effect on the next agent invocation (no rebuild needed).

## App SDK Hooks

Available in `@kirocrew/app-sdk`:

| Hook | Purpose |
|------|---------|
| `useAppApi()` | Permission-scoped HTTP client (GET/POST/PUT/DELETE) |
| `useAppEvents(event, cb)` | Subscribe to real-time WebSocket events |
| `useTheme()` | Reactive theme (mode, accent, colorTheme) |
| `useAppInfo()` | App metadata (name, version, permissions) |
| `useNavigate()` | Navigate to KiroCrew routes |
| `useNotify()` | Show toast notifications |
| `useNavBadge()` | Update sidebar badge count |
| `useChatLauncher()` | Navigate to chat with optional agent and message |

## Chat Marker Protocol

Also in `@kirocrew/app-sdk`, for an app that renders agent messages itself. An agent puts follow-up
choices and steer acknowledgements inline in its prose (`[OPTIONS: a | b]`,
`[STEERING steer-<id>: …]`); these parse them out so your UI can show buttons instead of raw syntax.

A third marker, `[OPTION-ACTIONS: close=<label>]`, offers a LOCAL UI action rather than text to
send — today only `close`, which closes the session, not just the tab. It is deliberately a separate field rather than
a magic label, because labels are model-authored prose. Two things follow for a renderer, and both
matter: the action is dashboard-only, so every other surface must strip-and-DROP it (a Slack button
cannot close a dashboard tab, so a button built from one would lie); and nothing may auto-dispatch
it, so a spurious marker yields a button, never an effect. `parseOptions` returns these separately
from the content choices, and the two markers never parse each other's syntax.

**The evolution rule, because this is a one-way door.** `[OPTION-ACTIONS:]` is a **closed enum with
exactly one member today**, and a renderer MUST treat an action it does not recognise as absent:
drop it, render nothing, dispatch nothing. That is the compatibility contract, not merely current
behaviour — an action added later is defined as invisible to every renderer that predates it, which
is what makes adding one a non-breaking change. Two consequences you can rely on: a renderer never
needs a fallback UI for an unknown action, and an app pinned to an older SDK cannot be made to
render a control it has no code for. Do NOT infer an action from a label, and do NOT pass an
unrecognised action through to a generic handler — a marker is at most a request for a button, and
one nobody understands is a request for nothing. The field is singular for the same reason: a list
would make partial recognition expressible, and a renderer honouring the half it knows is exactly
the failure this rule forecloses.

| Export | Purpose |
|--------|---------|
| `parseOptions(content)` | Split the prose from the choices offered with it |
| `deriveFollowUpOptions(messages, isStreaming)` | The choices that still apply to the conversation |
| `extractSteeringAcks(content)` | Pull the steer acknowledgement out of the text |
| `stripPartialOptionMarker(text)` | Hide a marker that is still arriving mid-stream |

Types: `ParsedOptions`, `FollowUpDerivation`, `ChatMessage`. React-free, so it also works in a worker
or a test. Worked examples: [api-reference.md](api-reference.md#chat-marker-protocol).

To render a transcript rather than parse one, `ChatMessageList` draws it and its row
**registry** lets you add a row type or replace one without forking the list — including the
roles the dashboard leaves undrawn. See
[api-reference.md](api-reference.md#chat-transcript-rendering).

## Shared UI Components

Available in `@kirocrew/app-sdk/ui`:

`Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`, `SearchInput`, `Badge`,
`AimBadge`, `StatCard`, `Skeleton`, `ContentSkeleton`, `EmptyState`,
`PageHeader`, `Toggle`, `InfoTip`, `SegmentedControl`, `MarkdownRenderer`

## Permissions

Declare what your app can access in `app.json`:

```json
{
  "permissions": {
    "api": ["/api/crons", "/api/status"],
    "events": ["notification", "slots"],
    "mcpTools": ["cron_add", "cron_list"],
    "storage": true,
    "cron": true,
    "network": false
  }
}
```

The App SDK checks declared permissions before each request — accessing
undeclared paths throws an error.

## Next Steps

- **Backend communication**: Your dashboard UI can call your app's backend through the gateway reverse proxy at `/apps/{name}/api/*` — no CORS issues. Verify requests with `verifyProxyRequest()` from the SDK.
- See [App Manifest Reference](manifest-reference.md) for all `app.json` fields
- See [API Reference](api-reference.md) for TypeScript and Python client APIs
- See [Publishing Guide](publishing-guide.md) for publishing to the App Store registry


## Python Client

For Python apps, CLI tools, or services that need to talk to KiroCrew Gateway:

```bash
pip install kirocrew-client
```

```python
import asyncio
from kirocrew_client import KiroCrewClient

async def main():
    async with KiroCrewClient(app_name="my-tool") as mc:
        # Check connectivity
        ok = await mc.ping()
        print(f"Gateway reachable: {ok}")

        # Dispatch an agent
        task_id = await mc.dispatch_agent_async("my-agent", "Analyze ticket T-123")
        result = await mc.get_task_result(task_id)
        print(f"Result: {result}")

        # Manage crons
        await mc.add_cron("refresh", message="Check for updates", every=3600)
        crons = await mc.list_crons()

        # Inject silent context (for background info)
        await mc.inject_context("slot-id", "PR #456 was approved", source="watch")

asyncio.run(main())
```

The `kirocrew-client` package is async (uses `aiohttp`) and standalone — no
dependency on the KiroCrew main package. It covers the full Gateway API surface.

See [API Reference](api-reference.md) for the full method list.

## Publishing Your App

Once your app works locally, publish it to the App Store registry so other
KiroCrew users can install it with one click.

See [Publishing Guide](publishing-guide.md) for the full workflow.
