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
| `highlights` | string[] | Concise feature bullets for the detail page |
| `useCases` | string[] | Short, operator-oriented situations where the app is useful |
| `configuration` | string[] | Concise setup or configuration steps shown on the detail page |
| `screenshots` | string[] | Real product screenshots; paths follow the same distribution rules as hero art |
| `screenshotsDark` | string[] | Optional dark-appearance screenshot variants |

## Resources

| Field | Type | Description |
|-------|------|-------------|
| `agents` | string[] | Paths to agent JSON files (relative to app root) |
| `skills` | string[] | Paths to skill directories |
| `sops` | string[] | Paths to SOP (Standard Operating Procedure) files |
| `mcpServers` | object | MCP server definitions (same format as `mcp.json`) |

### How a stdio `command` is resolved at registration

A stdio entry's `command` (no `url`) is not always written verbatim — registration
resolves it so the server starts under the interpreter its dependencies were
installed against:

- **A bare Python launcher** (`python`, `python3`, `py`, or the same with `.exe`)
  resolves to the app's own venv interpreter (`.venv/bin/python3`, or
  `.venv\Scripts\python.exe` on Windows) when it exists as a runnable file, else
  to the gateway's own interpreter — never a PATH lookup. Exception: a server
  whose `args` launch a `kiro_crew` module (`-m kiro_crew...`) always gets the
  gateway's interpreter, since app venvs cannot import `kiro_crew`.
- **Any other bare name** (no path separator, no drive qualifier) is rewritten
  only when the app's venv provides that exact binary as a runnable file (a pip
  console script — invisible to PATH because the venv is never activated). Note
  this means a venv-provided binary shadows a same-named PATH dependency.
  `node`, `npx`, `docker` and friends are otherwise left for PATH, as declared.
- **A command carrying a path** (absolute or relative) is never rewritten. If it
  does not point at a runnable file at registration time, a warning naming the
  app, server, and command is logged — the entry is still written.
- The host CLI name `kirocrew` is pinned to the running gateway before any of
  the above applies.

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
    },
    {
      "name": "market-open",
      "cron_expr": "30 9 * * 1-5",
      "message": "Summarise the overnight tape",
      "timezone": "America/New_York",
      "skip_dates": ["2026-12-25"]
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
| `timezone` | string | IANA zone name the schedule and `skip_dates` are evaluated in, e.g. `America/New_York`. Optional, but an empty value falls back to the gateway config's timezone and then to **UTC** — so `"cron_expr": "0 6 * * *"` without it fires at 06:00 UTC, the wrong calendar day for most users. An unknown zone is rejected at manifest validation. A per-**user** zone is not manifest data: pass `timezone=` to `ctx.cron.add_job` instead |
| `skip_dates` | string[] | Calendar dates the job must not fire on, evaluated in `timezone`. Must be zero-padded `YYYY-MM-DD` — `2026-1-1` parses but never matches the padded fire-time rendering, so it is rejected at manifest validation rather than silently skipping nothing |
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
| `ui.overlays[].id` | string | | Overlay id; must match a bundled overlay component (see below) |
| `ui.overlays[].replaces` | string | | Host overlay slot this app takes over while enabled |

### `ui.overlays` — Replacing a Host Overlay Surface

An overlay is a surface that floats above whatever the user is looking at and is
opened by a gesture the host owns, so unlike `ui.pages` it has no route and no
sidebar placement. Declaring one lets an enabled app take over a host surface:

```json
{
  "ui": {
    "overlays": [
      { "id": "command-bar", "replaces": "quick-search" }
    ]
  }
}
```

`replaces` names a host slot. `quick-search` is the only slot the dashboard
currently offers -- it is the Cmd+K / Ctrl+K surface -- and an unknown slot name is
reported and ignored rather than silently dropping the overlay.

**Host-internal until App Kit adopts it.** Both fields are validated by the backend
for any manifest, but only an app whose `origin` is `builtin` can actually claim a
slot: an overlay `id` must name a component compiled into the dashboard bundle, and
there is no ESM `entryPoint` for overlays the way `ui.pages` has one. An installed app
declaring `ui.overlays` is refused at install, and a self-registered one is refused
when slots are resolved -- `builtin` provenance is assigned only by the builtin
registration Kiro Crew runs at startup and cannot be self-reported. Treat this as the
mechanism builtin apps use to replace a host surface, not yet as a third-party
extension point.

A builtin declaring `ui.overlays` must NOT also declare `ui.entry`: builtin
registration re-derives `origin` on every startup and downgrades an app that ships a
UI bundle to `local`, which would then be refused its own slot. A test enforces this
so the combination fails the build rather than silently reverting the surface.

At most one enabled app owns a slot. When two enabled apps declare the same
`replaces`, the first by app name wins and the collision is reported -- the winner
does not depend on which app was enabled or installed more recently.

## Contributions

### `contributes.commands` — Adding Rows to the Command Bar

Adds command rows to the host's Command Bar. This is the lightest thing an app can
be: a command-contributing app needs no page, no frontend bundle, no backend and no
process — a manifest, plus whatever skill its prompt names.

```json
{
  "contributes": {
    "commands": [
      {
        "id": "approve-all",
        "title": "Approve all PRs",
        "subtitle": "Approve every pull request behind a link",
        "icon": "Check",
        "keywords": ["pr", "lgtm"],
        "argument": {
          "placeholder": "Paste a GitHub link…",
          "hint": "A PR search, a label, or a single pull request.",
          "kind": "url",
          "hosts": ["github.com"],
          "patternError": "Not a github.com link."
        },
        "prompt": "Load the $my-skill skill and approve every PR behind {argument}",
        "autoSend": true
      }
    ]
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | lowercase alphanumeric + dashes; unique within the app |
| `title` | yes | row label, up to 120 characters |
| `prompt` | yes | the action: a new session is seeded with this text, up to 4000 characters |
| `subtitle` | no | defaults to the app's display name |
| `icon` | no | a name from the host's glyph set; an unknown name falls back |
| `keywords` | no | hidden match aliases |
| `argument` | no | the ONE value the command collects before it runs; must be an object |
| `autoSend` | no | send the seeded prompt instead of leaving it in the composer |

Every entry of `commands` must be an object, and every length above is counted in UTF-16
code units -- what the launcher itself counts. Both matter for the same reason: the host
validates your manifest twice, once on install and once when it renders, and anything the
two would measure differently is a command that installs clean and then does not appear.
So a title of 100 emoji is 200 units, not 100, and a single non-object entry is refused
rather than quietly skipped past.

Inside `argument`:

| Field | Required | Notes |
|---|---|---|
| `kind` | no | `url` or `text`; defaults to `text`. An unknown kind is refused |
| `hosts` | no | `url` only: allowed hostnames, up to 20. Empty means any host |
| `placeholder` | no | field placeholder |
| `hint` | no | one line under the field |
| `patternError` | no | shown when the value is not accepted |

`contributes` sits beside `ui`, not inside it: `ui` declares surfaces the app owns,
while a contribution is a row inside a surface the host owns and renders.

**A contribution is data, never code.** There is no way to ship a function or an icon
URL: the launcher would be running app-authored JavaScript inside the host's surface
on every keystroke, and the root page promises to issue no network request. Ask for a
new glyph name by pull request.

**The host owns the matcher; a manifest names one rather than supplying it.** The
collected value is spliced into an instruction handed to an agent with tools, so it has
to be checked before the prompt is built — but `kind` selects one of a fixed set the
host implements, and there is no way to ship a regex of your own. An earlier revision
of this contract accepted `argument.pattern`; a pattern from a manifest runs against
the field on every keystroke on the thread that draws the launcher, and shapes like
`^(a+)+$` or `^(a|aa)+$` are a few characters long and exponential, so an `argument`
that still carries `pattern` is now REFUSED rather than migrated — leaving it to fall
back on `text` would accept any non-empty string with `autoSend` still on. An unknown
`kind` is refused for the same reason.

`kind: "url"` parses the value with the runtime's own URL parser and then applies
`hosts`. The allowlist is exact unless an entry starts with a dot: `github.com` does
not admit `github.com.evil.test`, while `.github.com` admits `gist.github.com`. Only
`http` and `https` are accepted. `kind: "text"` takes any non-empty value.

This is less precise than a regex, deliberately: a pattern could demand `/pull/<n>`,
while `url` + `hosts` admits any URL on the host and leaves what the link DENOTES to
the agent — or to your skill, which is the better place for your own product's URL
taxonomy.

Declaring an argument the prompt never interpolates is an error — the reader would be
asked for a value the command then ignores. A command whose prompt needs no value
simply omits `argument`; activating it is the whole action.

**What the reader sees with `autoSend`.** The host shows the resolved prompt — the
template with the reader's value already spliced in — in the argument field before
the send, so the instruction is visible at the moment it fires. Write prompts on the
assumption they will be read.

**`autoSend` requires an `argument`.** That preview is what makes the send informed and
it lives in the argument step, so a command that collects nothing never reaches it and
the combination is refused rather than silently downgraded. Such a command still works:
its prompt lands in the composer and one keystroke sends it. `autoSend` is also
honoured only for the JSON boolean `true`, never for the string `"true"`.

A malformed command is skipped with a console warning and the app's other commands
still load. Commands from a disabled app do not appear at all.

**If your app is SIGNED, set `minKiroCrewVersion`.** Contributions are covered by the
admission signature -- a contributed prompt goes to an agent with tools and `autoSend`
fires it, so leaving it unsigned would make your rows the one part of a signed app an
attacker could rewrite with the signature still verifying. The consequence for you is
that a signed manifest declaring `contributes` does not verify on a gateway older than
this change, because that gateway computes the signed bytes without the
`contributes` key. It fails CLOSED -- a refused install, not a silent downgrade -- but
the error will not obviously point here, so declare the floor and the install refuses
for a legible reason instead. Unsigned apps are unaffected, as are signed apps that
contribute nothing: the key is only added to the payload when non-empty, so every
signature issued before this existed still verifies.

### App Icon

`iconPath` is the App Store's card and row icon, and it is **top-level** — not
under `ui`. `ui.pages[].icon` and `ui.pages[].iconUrl` above are the sidebar glyph
for an app that is already *installed*, a different surface; neither one supplies
a store icon, and an app that declares only those publishes no icon at all.

```json
{
  "iconPath": "assets/icon.png"
}
```

`kirocrew app init` scaffolds `assets/icon.png` and this field, so a new app
starts with a working icon rather than a placeholder card. Replace the generated
placeholder with real artwork before publishing.

For the artwork requirements — path form, dimensions, why the icon must be
opaque, and how the dark variant relates — see
[Publishing an app](publishing-guide.md), which owns that spec for every art
field.

### Hero Images

Top-level manifest fields that supply the artwork rendered on App Store browse
and detail cards. The path form depends on how the app is distributed:

- **Builtin apps** use an absolute served URL under `/apps/{name}/ui/` (the
  builtin registry serves the app's bundled `ui/` directory there):

  ```json
  {
    "heroImage": "/apps/my-app/ui/hero-light.svg",
    "heroImageDark": "/apps/my-app/ui/hero-dark.svg",
    "heroImageDetail": "/apps/my-app/ui/hero-detail-light.svg",
    "heroImageDetailDark": "/apps/my-app/ui/hero-detail-dark.svg"
  }
  ```

- **Federated / registry apps** use a repo-relative path (e.g. `ui/hero-light.svg`);
  `registry.py` rewrites it to a blob-proxy URL (`/api/apps/blob?repo=<repo>&path=<path>`)
  so the artwork resolves without the app being locally installed:

  ```json
  {
    "heroImage": "ui/hero-light.svg",
    "heroImageDark": "ui/hero-dark.svg",
    "heroImageDetail": "ui/hero-detail-light.svg",
    "heroImageDetailDark": "ui/hero-detail-dark.svg"
  }
  ```

| Field | Type | Description |
|-------|------|-------------|
| `heroImage` | string | Hero image shown on the App Store card (light theme) |
| `heroImageDark` | string | Hero image variant used in dark theme |
| `heroImageDetail` | string | Wide banner preferred by the detail page (light theme) |
| `heroImageDetailDark` | string | Wide detail banner used in dark theme |

Hero images are illustrative marketing art. `screenshots` are separate and must
show the real product UI; the detail page renders both when both are declared.

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
| `backend.healthCheck` | string | `"/health"` | Absolute health-check path beginning with `/`; unsafe or ambiguous paths are refused. Polled until it answers at startup, then re-polled for the life of the backend — keep the handler cheap and dependency-free. A backend that stops answering it is dropped from the reverse proxy and its MCP servers are deregistered until it answers again. |
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

`hooks.routes` handlers are wired up when the app is enabled **through the
Gateway** -- the dashboard's enable action (`on_app_enable`), also re-run at
gateway startup (`on_gateway_startup`) -- so on that path they go live without
waiting for a Gateway restart.

`kirocrew app enable` is not that path. The CLI is a separate process with no
handle on a running Gateway's imported modules, so it cannot load or replace
hooks: a Gateway that is already up keeps executing the hook module it imported
earlier, even though the command succeeds and `app info` reports the new
version. Restart the Gateway, or disable and re-enable the app from the
dashboard, for hook changes to take effect. The CLI prints this reminder after
enabling any app that declares `backend.hooks`.

**Importing your own modules.** Hook entry files are loaded from their file path
into a synthetic package named after the app, never via `sys.path`, so use a
**relative** import to reach a sibling module:

```python
# backend/routes.py
from . import config          # backend/config.py
from .render import to_html   # backend/render.py
```

A relative import resolves inside the app's own directory tree and cannot walk
above the app root (`from ... import x` is refused). It is not a sandbox: app
Python already runs in the Gateway process with full filesystem access, so a
symlinked sibling resolves wherever it points. Do not use a bare
`import config`: `sys.modules["config"]` is process-global, so two apps each
shipping a `config.py` would end up sharing one module. `from kiro_crew...`
absolute imports are for built-in apps only.

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

> **Advisory today, not enforced in-process.** These fields are **not** a runtime sandbox. The validator functions in `apps/permissions.py` (`validate_permissions`, `format_permissions_summary`) are currently **not wired into the install or runtime path** — they are only exercised by unit tests — so the manifest `permissions` block is neither enforced nor even surfaced today: `mcpTools` is not gated at tool dispatch and an empty `mcpTools` list is treated as unrestricted. What actually confines an app today is the HTTP app-token scope (`permissions.api` allowlist, deny-by-default — see `security.md`) plus the OS sandbox. Install-time path traversal is blocked separately by `_check_path_safety(name)` + `manifest.validate()`, not by the permission validator. Full in-process enforcement is tracked in [app-sandbox-roadmap.md](../request-for-change/rfc-app-sandbox-isolation.md).

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

**Exception — `platform.installMode: "client"` apps.** For a client app the
script is **advisory**: a failure is reported on the response as
`onEnable.failed` but the app stays enabled, and the script is skipped entirely
(`onEnable.skipped: "unsupported_platform"`) when the gateway's OS is not in the
app's `platform.os`. Such an app's real payload is a desktop application the user
installs on their own machine, so its script addresses something that may
legitimately be absent here — rolling back would make the app's dashboard half
impossible to enable on exactly the hosts that need it to explain how to get the
desktop half.

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
- `name` must not be `system` (it would shadow the `system.*` notification-channel
  namespace)
- `name` must not be `library` (the dashboard serves `/apps/library` as a static
  page — the installed-app management surface — and it registers ahead of the
  `/apps/:name` route, so an app by that name would have an unreachable page).
  Refused at every install door, including registry installs before any
  clone/build work, with the machine-readable error code `reserved_app_name`.
- `name` must not be a Windows reserved device stem — `con`, `prn`, `aux`, `nul`,
  `com1`–`com9`, `lpt1`–`lpt9` — because the app name becomes a directory and
  Windows resolves those inside every directory. Names that merely resemble one
  (`console`, `com10`, `null-app`) are fine. Refused on every platform: an app
  name is a persistent published identity, so it must mean the same thing on
  whichever host installs the app.
- `version` must match semver (`X.Y.Z`)
- Paths in `agents`, `skills`, `sops`, `ui.entry`, `ui.pages[].entryPoint`, and `backend.entryPoint` must be relative and stay inside the app root: absolute paths and `..` traversal are rejected (canonical resolve + containment when the app dir is known). `backend.hooks.*` are format-checked (`module.path:callable`, which cannot express traversal) and containment-checked again at load time. `mcpServers` entries use `command`/`args`/`url`/`env` (not app-relative file paths) and are not path-checked.
- All required fields must be non-empty strings
- Each cron entry must specify either `every` or `cron_expr`
- Each UI page must have `route` and `label`
- Each UI overlay must have `id` and `replaces`; both must be kebab-case, and `id`
  must be unique within the manifest

## Full Example

```json
{
  "name": "oncall-watchtower",
  "version": "1.0.0",
  "displayName": "Oncall Watchtower",
  "description": "Monitor tickets, pipelines, and alarms for your on-call rotation",
  "author": "kirocrew",
  "tags": ["oncall", "monitoring"],
  "useCases": ["Keep a shared view of firing alerts and active investigations"],
  "configuration": ["Connect an alert provider in Settings, then start in read-only mode"],
  "screenshots": ["ui/screenshots/board.png"],
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
