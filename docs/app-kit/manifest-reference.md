# App Manifest Reference

The app manifest (`app.json`) declares your app's identity, resources, and requirements.

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier, kebab-case (e.g. `"oncall-watchtower"`) |
| `version` | string | Semver version (e.g. `"1.0.0"`) |
| `displayName` | string | Human-readable name shown in App Store |
| `description` | string | Short description of what the app does |

## Recommended Fields

| Field | Type | Description |
|-------|------|-------------|
| `author` | string | Author name or team |
| `license` | string | License identifier |
| `minKiroCrewVersion` | string | Minimum Gateway version required |
| `tags` | string[] | Discovery tags (e.g. `["oncall", "monitoring"]`) |
| `jobFamilies` | string[] | Job families this app is relevant to |

## Resources

| Field | Type | Description |
|-------|------|-------------|
| `agents` | string[] | Paths to agent JSON files (relative to app root) |
| `skills` | string[] | Paths to skill directories |
| `sops` | string[] | Paths to SOP (Standard Operating Procedure) files |
| `mcpServers` | object | MCP server definitions (same format as `mcp.json`) |

## Scheduling

### `crons` — Cron Job Definitions

```json
{
  "crons": [
    {
      "name": "ticket-refresh",
      "every": 300,
      "message": "Check for new high-severity tickets"
    },
    {
      "name": "daily-digest",
      "cron_expr": "0 9 * * 1-5",
      "message": "Generate daily digest",
      "agent": "digest-agent"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Job identifier |
| `every` | number | Interval in seconds (mutually exclusive with `cron_expr`) |
| `cron_expr` | string | Cron expression (mutually exclusive with `every`) |
| `message` | string | Prompt sent to the agent on each run |
| `agent` | string | Agent to run (optional, uses default if omitted) |
| `enabled` | boolean | Default `true`. Must be a JSON boolean — any other type is rejected at manifest validation. When `false` the cron is registered **paused** (visible in the Schedule view, resumable) instead of firing on install/enable — for jobs that need user configuration first |

> **Caveat:** disabling an app deletes its registered cron jobs, and re-enabling
> the app re-registers them from the manifest. A cron shipped with
> `"enabled": false` that a user later resumed will therefore be reset back to
> the paused state after an app disable → re-enable cycle and must be resumed
> again.

## Frontend UI

### `ui` — Dashboard Integration

```json
{
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/apps/my-app",
        "label": "My App",
        "icon": "Shield",
        "entryPoint": "dist/page.mjs",
        "mountFunction": "mount"
      }
    ],
    "sidebar": {
      "section": "Apps",
      "order": 10
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ui.entry` | string | | Path to ESM bundle (relative to app root) |
| `ui.pages[].route` | string | | URL path for the page |
| `ui.pages[].label` | string | | Sidebar label |
| `ui.pages[].icon` | string | | Lucide icon name (e.g. `"Shield"`, `"Package"`) |
| `ui.pages[].iconUrl` | string | | Custom icon image path (relative to ui/) |
| `ui.pages[].entryPoint` | string | | Per-page ESM bundle path (overrides `ui.entry`) |
| `ui.pages[].mountFunction` | string | `"mount"` | Exported function name in the ESM bundle |
| `ui.sidebar.section` | string | `"Apps"` | Sidebar section name |
| `ui.sidebar.order` | number | `10` | Sort order within section |

### Hero Images

Top-level manifest fields that supply the artwork rendered on App Store browse
and detail cards. The path form depends on how the app is distributed:

- **Builtin apps** use an absolute served URL under `/apps/{name}/ui/` (the
  builtin registry serves the app's bundled `ui/` directory there):

  ```json
  {
    "heroImage": "/apps/my-app/ui/hero-light.svg",
    "heroImageDark": "/apps/my-app/ui/hero-dark.svg"
  }
  ```

- **Federated / registry apps** use a repo-relative path (e.g. `ui/hero-light.svg`);
  `registry.py` rewrites it to a blob-proxy URL (`/api/apps/blob?repo=<repo>&path=<path>`)
  so the artwork resolves without the app being locally installed:

  ```json
  {
    "heroImage": "ui/hero-light.svg",
    "heroImageDark": "ui/hero-dark.svg"
  }
  ```

| Field | Type | Description |
|-------|------|-------------|
| `heroImage` | string | Hero image shown on the App Store card (light theme) |
| `heroImageDark` | string | Hero image variant used in dark theme |

## Backend

### `backend` — App Backend Process

```json
{
  "backend": {
    "entryPoint": "backend/server.py",
    "port": "auto",
    "healthCheck": "/health",
    "routes": "/api/apps/oncall-watchtower"
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend.entryPoint` | string | | Script to run (relative to app root), or a dotted Python module path launched via `python -m` (used by built-in apps like `file-explorer`, e.g. `kiro_crew.apps.builtins.file_explorer.server`) |
| `backend.port` | string | `"auto"` | Port number or `"auto"` for auto-assignment |
| `backend.healthCheck` | string | `"/health"` | Health check endpoint path |
| `backend.routes` | string | | Base route path for the backend |
| `backend.type` | string | `""` | Backend runtime: `"python"`, `"asgi"`, `"node"`, `"exec"` (execute the entry point file as-is), or `""` (auto-detect from `entryPoint` — a `.sh` file or an extensionless executable with a non-Python shebang is treated as a shell launcher) |

> **Note:** the shell-launcher auto-detect reads the entry point's shebang
> line, so a compiled/binary launcher (e.g. an ELF executable) cannot be
> auto-detected — declare `"type": "exec"` explicitly for those. Exec
> backends are POSIX-only: on native Windows the backend is refused at spawn
> with a logged error (use a Python or Node entry point instead).

App backends are accessible through the Gateway's reverse proxy at
`/apps/{name}/api/{path}`, which avoids CORS issues for dashboard UI pages.

#### `backend.hooks` — In-Gateway Python Entry Points

Instead of (or alongside) a standalone backend process, an app can register
Python entry points that run **inside** the Gateway process. Each value is a
dotted path in the format `module.path:callable`, resolved relative to the app
root (validated against `HooksConfig._HOOK_PATH_RE`).

```json
{
  "backend": {
    "hooks": {
      "routes": "backend.routes:register_routes",
      "on_startup": "backend.hooks:on_startup",
      "on_shutdown": "backend.hooks:on_shutdown"
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `backend.hooks.routes` | string | `module.path:callable` that registers handlers into the Gateway's in-process `RouteRegistry` catch-all dispatcher |
| `backend.hooks.on_startup` | string | `module.path:callable` invoked when the app's hooks are wired up |
| `backend.hooks.on_shutdown` | string | `module.path:callable` invoked when the app is disabled/torn down |

`hooks.routes` handlers are wired up when the app is enabled (via
`on_app_enable`, also re-run at gateway startup via `on_gateway_startup`), so
they go live without waiting for a Gateway restart.

## Permissions

### `permissions` — Declared Capabilities

```json
{
  "permissions": {
    "api": ["/api/crons", "/api/status", "/api/agents"],
    "events": ["notification", "slots"],
    "mcpTools": ["cron_add", "cron_list"],
    "storage": true,
    "cron": true,
    "memory": "app-scoped",
    "network": false,
    "spawn": false
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `permissions.api` | string[] | Allowed API path prefixes |
| `permissions.events` | string[] | Allowed WebSocket event types |
| `permissions.mcpTools` | string[] | Allowed MCP tool names |
| `permissions.storage` | boolean | Can use app-scoped storage |
| `permissions.cron` | boolean | Can create cron jobs |
| `permissions.memory` | string | Memory access: `""` (none), `"app-scoped"`, or `"shared"` |
| `permissions.network` | boolean | Can make external network requests |
| `permissions.spawn` | boolean | May start a background agent through the host's subagent manager (`ctx.spawn`) |

#### `permissions.spawn` — Background Agents

Unlike the advisory fields above, this one **gates a real capability**: `ctx.spawn`
is absent from the app context unless the manifest declares it, so an app that
did not ask cannot start an agent even by importing the SDK. Declared rather than
inferred so "which apps can start an agent" is answerable from the manifest
instead of from an app's import graph.

Spawns run through the HOST's subagent manager, which means they inherit the
host's spawn accounting and approval mode rather than getting a private path.
Cost is the app's to bound: an app that spawns on a timer needs its own budget
(see the activity-budget pattern in `builtins/mochi/activity_budget.py`), because
the platform does not rate-limit spawns per app today.

API: `apps/spawn_sdk.py` — `SpawnSDK`, `build_spawn_impl`, `build_done_probe`,
`SpawnError`.

> **Advisory today, not enforced in-process.** These fields are **not** a runtime sandbox. The validator functions in `apps/permissions.py` (`validate_permissions`, `format_permissions_summary`) are currently **not wired into the install or runtime path** — they are only exercised by unit tests — so the manifest `permissions` block is neither enforced nor even surfaced today: `mcpTools` is not gated at tool dispatch and an empty `mcpTools` list is treated as unrestricted. What actually confines an app today is the HTTP app-token scope (`permissions.api` allowlist, deny-by-default — see `security.md`) plus the OS sandbox. Install-time path traversal is blocked separately by `_check_path_safety(name)` + `manifest.validate()`, not by the permission validator. Full in-process enforcement is tracked in [app-sandbox-roadmap.md](./app-sandbox-roadmap.md).

### Sidebar nav status (`app_nav_status`)

An app can report a **runtime status** that the dashboard renders as a small
colored dot on the app's sidebar icon (idle, running, healthy, warning, error at
a glance — even while the app's page is closed). This is one reserved event, gated
like any other: declare it and app-scoped storage, then push from a backend hook.

```json
{
  "permissions": { "events": ["app_nav_status"], "storage": true }
}
```

From a backend hook (e.g. `backend.hooks.on_startup`, a poll loop, or a route
handler) call `set_nav_status` on the injected `AppContext`:

```python
def on_startup(ctx):
    ctx.set_nav_status("busy", "Indexing 42%")   # tone + optional label
```

- `tone` is one of a small **render vocabulary** the core owns:
  `neutral` (idle — renders no dot), `busy` (pulses), `positive`, `caution`,
  `critical`. Your app owns its *own* domain states and maps them to a tone; the
  core validates render-safety only, never semantics. An unrecognized tone
  degrades to `neutral` (never raises).
- `label` is optional free-form text (length-capped, currently 48 chars) shown as
  the dot's hover tooltip / accessible name. It is never drawn inline.
- Tone colors map to the active theme's existing tokens
  (`--muted`/`--accent`/`--ok`/`--warn`/`--danger`) via `--nav-status-*`
  indirection, so every built-in and custom theme is covered with no per-theme
  edits.

`set_nav_status` no-ops unless the app declared the `app_nav_status` event
permission (so `ctx.events` is wired). With `storage: true` the last status is
persisted, so a freshly loaded dashboard shows current state (via the
`nav_status` field on `GET /api/apps`) before the next live push. See
[api-reference.md](./api-reference.md#app-nav-status-sidebar-icon).

## Setup Hooks

### `setup` — Lifecycle Scripts

```json
{
  "setup": {
    "onInstall": "cd ui && npm install && npm run build",
    "onUninstall": "echo cleanup done",
    "onUpdate": "cd ui && npm install && npm run build",
    "onEnable": "echo enabled",
    "onDisable": "echo disabled",
    "configSchema": {}
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `setup.onInstall` | string | `""` | Shell command run after install |
| `setup.onUninstall` | string | `""` | Shell command run before uninstall |
| `setup.onUpdate` | string | `""` | Shell command run after update |
| `setup.onEnable` | string | `""` | Shell command run when app is enabled |
| `setup.onDisable` | string | `""` | Shell command run when app is disabled |
| `setup.onEnableTimeout` | number | `30` | Timeout in seconds for `onEnable` script |
| `setup.onDisableTimeout` | number | `30` | Timeout in seconds for `onDisable` script |
| `setup.configSchema` | object | `{}` | JSON Schema for app configuration |

If `onEnable` fails (non-zero exit), the enable is rolled back — the app
stays disabled and any registered resources are deregistered. `onDisable`
failures are logged as warnings but do not block the disable operation.

Install scripts run in a sandboxed environment with a minimal set of
environment variables (PATH, HOME, SSH_AUTH_SOCK, etc.) to prevent
leaking secrets from the gateway process.

## Dependencies

### `dependencies` — External Dependency Declarations

Declare external dependencies your app requires. The gateway tracks these
in a reference-counted ledger so shared dependencies are not removed when
only one app is uninstalled.

```json
{
  "dependencies": {
    "managedBy": "gateway",
    "capabilities": {
      "mcp": [
        { "id": "some-mcp-server", "source": "registry" }
      ],
      "skills": [
        { "id": "some-skill", "source": "registry" }
      ],
      "agents": [
        { "id": "some-agent", "source": "registry" }
      ]
    },
    "commands": ["jq", "node", "python3"]
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dependencies.managedBy` | string | `"gateway"` | Who manages dependency lifecycle: `"gateway"` or `"app"` |
| `dependencies.capabilities` | object | `{}` | Capability-package dependencies (MCP servers, skills, agents) resolved through the edition's capability manager. The open-source edition ships none, so these entries are reported as **unresolved** (they appear in the install result's `failed` list) and the app still installs — design for graceful degradation. |
| `dependencies.capabilities.mcp` | object[] | `[]` | Required MCP server dependencies |
| `dependencies.capabilities.skills` | object[] | `[]` | Required skill dependencies |
| `dependencies.capabilities.agents` | object[] | `[]` | **Deprecated for `managedBy: "gateway"`** — no capability-manager install operation exists for agents in any edition, so a gateway-managed entry can never succeed and is always reported unresolved. Declare `managedBy: "app"` (or install out of band) instead. |
| `dependencies.commands` | string[] | `[]` | System commands that must be on PATH (checked via `which`) |

> The former `dependencies.aim` key is still accepted as a deprecated alias, but
> it is never written back — a manifest round-trip migrates it to
> `dependencies.capabilities`. Use `capabilities` in new manifests.

## Lifecycle & Resource Management

### `lifecycle` and `resources`

Control how KiroCrew manages the app:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lifecycle` | string | `"gateway"` | `"gateway"` (managed), `"app"` (self-managed), or `"locked"` (cannot uninstall) |
| `resources` | string | `"gateway"` | `"gateway"` (KiroCrew registers agents/skills/MCP) or `"app"` (app handles its own) |

## Platform

### `platform` — Compatibility & Install Mode

```json
{
  "platform": {
    "os": ["macos", "linux"],
    "arch": [],
    "requiresDesktopApp": false,
    "installMode": "server",
    "clientInstall": {
      "shell": "curl -fsSL https://example.com/install.sh | bash",
      "postInstall": "open ~/Applications/MyApp.app"
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `platform.os` | string[] | `["macos", "linux"]` | Supported platforms |
| `platform.arch` | string[] | `[]` (any) | Supported architectures |
| `platform.requiresDesktopApp` | boolean | `false` | App's own UI needs the Electron desktop shell |
| `platform.installMode` | string | `"server"` | `"server"` or `"client"` |
| `platform.clientInstall.shell` | string | | One-liner for local install |
| `platform.clientInstall.postInstall` | string | | Command to run after install |

When `installMode` is `"client"`, the App Store shows copy-paste terminal
instructions instead of running the install on the server. This is used for
apps that must run on the user's local machine (e.g. Electron desktop apps
when KiroCrew runs on a remote host).

#### `platform.requiresDesktopApp` — Desktop-Only UI

Declares that the app's OWN interface needs the Electron shell (a transparent
always-on-top window, a tray surface, global shortcuts — things a browser tab
cannot provide). A different axis from `os`: `os` says which machines the app can
run on at all, this says which CLIENT can render it.

**It gates rendering, not enabling.** Enabling is a server-side state change —
the app's backend, hooks, agents and crons all run in the gateway — so a browser
user can still turn the app on and its autonomous side works. Only the app's own
window is unavailable. The App Store therefore keeps the Enable action in a
browser and shows a "Desktop app" hint beside it (`AppListRow`, `FeatureCard`,
`AppDetailPage`); replacing the button with a static claim left remote users with
no way to enable the app at all.

**UX gate, not a security boundary.** The marker is evaluated client-side
(`lib/electron.ts::needsDesktopApp`), so it must never be the only thing standing
between a caller and a capability. Anything that must not happen in a browser
belongs behind an app-token scope or a server-side check.

## Open Command

### `openCommand` — Launch Apps Outside the Dashboard

For apps that run outside the dashboard (e.g. Electron apps), the top-level
`openCommand` declares a shell string that launches the app.

```json
{
  "openCommand": "open ~/Applications/MyApp.app"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `openCommand` | string | `""` | Shell command launched by `POST /api/apps/{name}/open` |

`POST /api/apps/{name}/open` runs this command in the background. On a
cloud/remote environment with no display, the endpoint returns the command for
the user to run locally instead of executing it on the server.

## Validation Rules

- `name` must match `/^[a-z0-9]+(?:-[a-z0-9]+)*$/` (kebab-case)
- `version` must match semver (`X.Y.Z`)
- Paths in `agents`, `skills`, `sops`, `ui.entry`, `ui.pages[].entryPoint`, and `backend.entryPoint` must be relative and stay inside the app root: absolute paths and `..` traversal are rejected (canonical resolve + containment when the app dir is known). `backend.hooks.*` are format-checked (`module.path:callable`, which cannot express traversal) and containment-checked again at load time. `mcpServers` entries use `command`/`args`/`url`/`env` (not app-relative file paths) and are not path-checked.
- All required fields must be non-empty strings
- Each cron entry must specify either `every` or `cron_expr`
- Each UI page must have `route` and `label`

## Full Example

```json
{
  "name": "oncall-watchtower",
  "version": "1.0.0",
  "displayName": "Oncall Watchtower",
  "description": "Monitor tickets, pipelines, and alarms for your on-call rotation",
  "author": "kirocrew",
  "tags": ["oncall", "monitoring"],
  "agents": ["agents/ticket-analyst.json"],
  "skills": ["skills/oncall-runbook"],
  "crons": [
    {
      "name": "ticket-refresh",
      "every": 300,
      "message": "Check for new high-severity tickets"
    }
  ],
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/apps/oncall-watchtower",
        "label": "Oncall",
        "icon": "Shield"
      }
    ]
  },
  "permissions": {
    "api": ["/api/crons", "/api/status"],
    "events": ["notification"]
  },
  "platform": {
    "os": ["macos", "linux"]
  }
}
```

## Forward Compatibility

Unknown fields in `app.json` are preserved during parsing and round-tripped
through `to_dict()` / `to_json()`. This allows newer manifest features to
coexist with older KiroCrew versions without breaking validation.
