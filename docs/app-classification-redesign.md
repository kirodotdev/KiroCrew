# App Classification Redesign

> Design document for replacing the single `managed` field with a proper
> multi-axis classification model, lifecycle scripts, and dependency management.

## 1. Problem Statement

The current `managed` field on `InstalledApp` conflates three orthogonal concerns:

| Current value | Actual meaning |
|---------------|----------------|
| `"builtin"` | origin=built-in + resources=gateway + lifecycle=locked |
| `"kirocrew"` | origin=registry-or-local + resources=gateway + lifecycle=gateway |
| `"self"` | origin=external + resources=app + lifecycle=app |

This makes it impossible to express valid combinations like:
- A registry app whose resources are managed by the app itself
- A self-registered app that wants gateway to manage its symlinks
- A builtin feature that could be uninstalled

The setup hooks are also incomplete — only `onInstall` and `onUninstall`
exist, with no support for update or enable/disable scripts.

Finally, apps can declare `mcpServers` and capability dependencies in their
manifest, but KiroCrew never auto-installs them.

---

## 2. Three-Field Classification Model

Replace `managed: str` with three independent fields:

```
origin     — where the app came from (read-only, set at install time)
resources  — who manages agent/skill/cron registration
lifecycle  — who manages updates, uninstall, and removability
```

### 2.1 Field Definitions

#### `origin`

| Value | Meaning |
|-------|---------|
| `"builtin"` | Baked into the KiroCrew dashboard (agent-worlds, channels, secretary) |
| `"registry"` | Installed from the curated app registry (app-registry.json) |
| `"local"` | Installed from a local directory path |
| `"external"` | Self-registered via the `/api/apps/register` endpoint or SDK |

Read-only after installation. Determines how the app was acquired.

#### `resources`

| Value | Meaning | Behavior |
|-------|---------|----------|
| `"gateway"` | KiroCrew manages agent/skill/cron registration via bridges.py | enable → `register_app()`, disable → `deregister_app()` |
| `"app"` | App manages its own resource registration | enable/disable skip bridge operations |

#### `lifecycle`

| Value | Meaning | Behavior |
|-------|---------|----------|
| `"gateway"` | KiroCrew handles updates and uninstall | update endpoint re-clones + re-installs; uninstall removes files |
| `"app"` | App handles its own updates | update endpoint returns 400; uninstall removes metadata only |
| `"locked"` | Cannot be uninstalled (builtin only) | uninstall returns 400; only enable/disable allowed |

### 2.2 App Type Matrix

| App type | origin | resources | lifecycle | Example |
|----------|--------|-----------|-----------|---------|
| Built-in dashboard feature | `builtin` | `gateway` | `locked` | agent-worlds, channels, secretary |
| Standard registry app | `registry` | `gateway` | `gateway` | oncall-watchtower (future) |
| Client-side registry app | `registry` | `app` | `app` | Mochi (new user, App Store install) |
| Local dev app | `local` | `gateway` | `gateway` | developer testing from local path |
| Self-registered app | `external` | `app` | `app` | Mochi (existing user, SDK auto-register) |

### 2.3 New Combinations Unlocked

| Scenario | origin | resources | lifecycle |
|----------|--------|-----------|-----------|
| Registry app with its own MCP server management | `registry` | `app` | `gateway` |
| Builtin feature that can be fully removed | `builtin` | `gateway` | `gateway` |
| Local app where app manages its own agents | `local` | `app` | `gateway` |

### 2.4 Data Model

```python
@dataclass
class InstalledApp:
    name: str = ""
    version: str = ""
    displayName: str = ""
    enabled: bool = True
    installedAt: str = ""
    updatedAt: str = ""
    source: str = ""              # concrete provenance: path, URL, "registry:name", "builtin"
    # New fields — replace managed: str
    origin: str = "registry"      # builtin | registry | local | external
    resources: str = "gateway"    # gateway | app
    lifecycle: str = "gateway"    # gateway | app | locked
```

**`origin` vs `source` clarification:** `origin` is a categorical enum
describing the acquisition channel (one of four values). `source` is a
free-form string holding the concrete provenance — a filesystem path
(`/Users/dev/my-app`), a registry marker (`registry:mochi-pet`), or the
literal `"builtin"`. Both fields coexist: `origin` drives behavioral
branching in code, `source` is for display and re-install lookups.

### 2.5 Frontend Behavior

```typescript
// Derived booleans
const isBuiltin = app.origin === 'builtin'
const isSelfManaged = app.resources === 'app'
const canUpdate = app.lifecycle === 'gateway'
const canUninstall = app.lifecycle !== 'locked'

// Badge display (using Badge component, not emojis)
// origin === "builtin"   → <Badge variant="aim">Built-in</Badge>
// origin === "registry"  → <Badge>Registry</Badge>
// origin === "local"     → <Badge>Local</Badge>
// origin === "external"  → <Badge variant="ok">External</Badge>
// resources === "app"    → additional <Badge>Self-managed</Badge>
```

### 2.6 Registry JSON

```jsonc
// Old
{
  "name": "mochi-pet",
  "repo": "Mochi",
  "branch": "mainline",
  "managed": "self",
  "detectInstalled": "test -d ~/Applications/Mochi.app"
}

// New
{
  "name": "mochi-pet",
  "repo": "Mochi",
  "branch": "mainline",
  "resources": "app",
  "lifecycle": "app",
  "detectInstalled": "test -d ~/Applications/Mochi.app"
}
```

`origin` is not in the registry JSON — it's always `"registry"` for
registry-installed apps. For standard server-side apps, `resources` and
`lifecycle` default to `"gateway"` and can be omitted.

---

## 3. Lifecycle Scripts

### 3.1 Script Definitions

Expand `setup` in `app.json` from two hooks to five:

```jsonc
{
  "setup": {
    "onInstall": "bash setup.sh",
    "onUpdate": "bash update.sh",
    "onUninstall": "bash scripts/uninstall.sh",
    "onEnable": "bash enable.sh",
    "onDisable": "bash disable.sh"
  }
}
```

| Script | Trigger | Typical use |
|--------|---------|-------------|
| `onInstall` | After first install, code is in place | Compile, install deps, create .app bundle, init data |
| `onUpdate` | After update, new code in place, data/ preserved | Recompile, run migrations, restart backend |
| `onUninstall` | Before removing files | Kill processes, delete .app, clean external resources |
| `onEnable` | User clicks Enable | Start backend process, register external services |
| `onDisable` | User clicks Disable | Stop backend process, deregister external services |

### 3.2 Execution Rules

1. All scripts run in the app source directory (`cwd = app_source`)
2. Environment filtered through `minimal_env()` — no gateway secrets leaked
3. Sandboxed via `wrap_argv()`. The gateway wraps every script value as
   `/bin/bash -c "set -euo pipefail\n{script}"`, so even `source setup.sh`
   runs inside a bash subprocess with strict error handling. App authors
   should always write scripts assuming bash execution context.
4. Timeout limits:
   - `onInstall` / `onUpdate`: 300s
   - `onUninstall`: 120s
   - `onEnable` / `onDisable`: 30s
5. `onUpdate` failure triggers rollback — see section 3.8 for details.
6. `onEnable` / `onDisable` failure semantics:
   - `onEnable` failure: the enable operation is rolled back (app stays
     disabled), error returned to the user. Rationale: if the app can't
     start properly, enabling it would leave it in a broken state.
     **Exception — `platform.installMode: "client"` apps.** Their script
     launches a desktop application distributed separately, which may
     legitimately not be installed on this host, so the failure is advisory
     (reported as `onEnable.failed`, app stays enabled) and the script is
     skipped outright when the gateway's OS is not in the app's `platform.os`.
     Rolling back there made the app's dashboard half unreachable on exactly
     the hosts that needed it to explain how to get the desktop half.
   - `onDisable` failure: the disable operation still proceeds, but the
     response includes a `warnings` array with the script output. The
     gateway logs the failure at WARNING level. Rationale: we must be
     able to disable a misbehaving app even if its cleanup script fails.
     Orphaned processes are the app's responsibility — the gateway does
     not retry or force-kill beyond what `stop_backend()` already does.

### 3.3 Execution Order

**Install:**
```
resolve_dependencies()  →  register_app()  →  onInstall  →  start_backend()
```

**Update:**
```
resolve_dependencies()  →  deregister_app()  →  replace files  →
register_app()  →  onUpdate  →  start_backend()
```

**Enable:**
```
register_app()  →  start_backend()  →  onEnable
```

**Disable:**
```
onDisable  →  stop_backend()  →  deregister_app()
```

**Uninstall:**
```
onUninstall  →  stop_backend()  →  deregister_app()  →
clean dependencies  →  delete files
```

### 3.4 Which App Types Need Which Scripts

| App type | onInstall | onUpdate | onUninstall | onEnable | onDisable |
|----------|-----------|----------|-------------|----------|-----------|
| Builtin | — | — | — | — | — |
| Registry server-side | optional | optional | optional | usually not | usually not |
| Registry client-side | required | recommended | recommended | optional | optional |
| Local | optional | optional | optional | usually not | usually not |
| External | — | — | optional | — | — |

### 3.5 Example: Mochi (registry client-side)

```jsonc
{
  "setup": {
    "onInstall": "bash setup.sh",
    "onUpdate": "bash update.sh",
    "onUninstall": "bash scripts/uninstall.sh"
  }
}
```

> Note: Use `bash script.sh` (not `source script.sh`) in setup hooks.
> Although the gateway wraps all scripts in a bash subprocess (see 3.2),
> using `bash` explicitly makes the intent clear and avoids confusion.

### 3.6 Example: Standard Server-Side App

```jsonc
{
  "setup": {
    "onInstall": "pip install -r requirements.txt",
    "onUpdate": "pip install -r requirements.txt"
  }
}
```

### 3.7 Data Model

```python
@dataclass
class SetupConfig:
    onInstall: str = ""
    onUpdate: str = ""       # NEW
    onUninstall: str = ""
    onEnable: str = ""       # NEW
    onDisable: str = ""      # NEW
    configSchema: dict[str, Any] = field(default_factory=dict)
```

### 3.8 Update Rollback Mechanism

When `onUpdate` fails, the gateway must restore the previous working state.
The update flow with rollback:

```
1. Preserve data/ → tmp
2. Preserve entire old app dir → ~/.kirocrew/apps/.{name}-rollback/
3. Replace app files with new version
4. Restore data/ from tmp
5. register_app() with new manifest
6. Run onUpdate script
7a. SUCCESS → delete rollback dir
7b. FAILURE →
    - deregister_app()
    - delete new app dir
    - move rollback dir back to app dir
    - register_app() with old manifest
    - return error with onUpdate script output
```

The rollback directory is created atomically via `shutil.move()` before
any destructive operations. If the gateway crashes mid-update, the
rollback dir's presence signals an incomplete update — startup recovery
can detect this and restore.

Dependency state is unchanged by rollback — `resolve_dependencies()` is
only called before the file replacement step, and any dependencies it
installed remain in place regardless of whether the update succeeds or
rolls back.

---

## 4. Dependency Management

### 4.1 Declaring Dependencies

New `dependencies` field in `app.json`:

```jsonc
{
  "dependencies": {
    "managedBy": "gateway",

    "capabilities": {
      "mcp": ["some-documentation-mcp-server"],
      "skills": ["SomeSkillPackage"],
      "agents": ["SomeAgentPackage"]
    },

    "commands": ["node", "python3"]
  }
}
```

### 4.2 Who Installs: `managedBy`

The `managedBy` field at the top level sets the default strategy for
**dependency installation** — this is distinct from the `resources` field
on `InstalledApp` which controls **resource registration** (agents/skills/crons).

| Concept | Field | Scope |
|---------|-------|-------|
| Who installs external dependencies (capability packages) | `dependencies.managedBy` | manifest |
| Who registers app-provided resources (symlinks) | `InstalledApp.resources` | installed metadata |

| Value | Behavior |
|-------|----------|
| `"gateway"` (default) | KiroCrew installs each dependency through the edition's `CapabilityManager` seam |
| `"app"` | KiroCrew only checks existence, does not install |

Per-dependency override is supported for mixed cases:

```jsonc
{
  "dependencies": {
    "managedBy": "gateway",
    "capabilities": {
      "mcp": [
        "some-documentation-mcp-server",
        { "id": "my-custom-mcp", "managedBy": "app" }
      ]
    }
  }
}
```

String entries use the default `managedBy`. Object entries can override.

### 4.3 Dependency Types

| Type | Install mechanism | Uninstall mechanism |
|------|-------------------|---------------------|
| `capability.mcp` | `CapabilityManager.install_mcp({id})` | `CapabilityManager.uninstall_mcp({id})` |
| `capability.skills` | `CapabilityManager.install_skill({pkg})` | `CapabilityManager.uninstall_skill({pkg})` |
| `capability.agents` | *(none — declarable but never gateway-installed)* | *(none)* |
| `commands` | Check only (`shutil.which`) | Never removed |

The core names no external binary: resolution goes through the
`CapabilityManager` CPP seam, and the edition owns which package manager (if
any) backs it. The public edition ships none (`available()` → `False`), so
capability entries report as unresolved and the app still installs.
`capability.agents` has no seam install op, so it always reports unresolved —
declare it `managedBy: app` or install it out of band.

The wire key is `capabilities`; the former `aim` key is still READ as a
deprecated alias but is never written back, so a manifest round-trip migrates it.

For `commands`: if a required command is missing, `resolve_dependencies()`
adds it to the `missing` list in `DependencyResult`. The install proceeds
(non-blocking), and the frontend displays a warning like "node not found —
some features may not work. Install Node.js to resolve." The app is
responsible for graceful degradation when optional commands are absent.

### 4.4 Key Distinction: `dependencies.capabilities.mcp` vs `mcpServers`

These are different things:

- **`mcpServers`** — MCP servers that the app itself provides and runs
  (e.g. Mochi's `mochi-pet` server at `localhost:7778`). Registered into
  the agent config by bridges.py.

- **`dependencies.capabilities.mcp`** — External MCP servers the app needs but
  doesn't provide. Installed system-wide through the `CapabilityManager` seam.

### 4.5 Install Failure Policy

Dependency install failures are **non-blocking**. Reasons:
- No capability manager may be available (public edition, Cloud Desktop)
- Network issues cause transient failures
- Some dependencies may be optional for degraded operation

`resolve_dependencies()` returns a result object; the caller decides
whether to proceed. Results are included in the API response for the
frontend to display warnings.

```python
@dataclass
class DependencyResult:
    installed: list[str]    # Successfully installed
    skipped: list[str]      # Already existed, no action needed
    failed: list[str]       # Install failed (non-fatal)
    missing: list[str]      # Missing system commands (warning)
```

### 4.6 Dependency Ledger (Reference Tracking)

To safely clean up dependencies on uninstall, KiroCrew maintains a
lightweight reference ledger.

**Location:** `~/.kirocrew/dependency-ledger.json`

**Concurrency:** All reads and writes to the ledger file use `fcntl.flock()`
(consistent with KiroCrew's existing file locking pattern in `cron.py`).
This prevents corruption when two apps install concurrently and both need
the same dependency.

```jsonc
{
  "capability/mcp/some-documentation-mcp-server": {
    "installedBy": ["oncall-watchtower"],
    "installedAt": "2026-04-20T10:00:00Z",
    "type": "capability.mcp"
  },
  "capability/skills/SomeSkillPackage": {
    "installedBy": ["oncall-watchtower", "another-app"],
    "installedAt": "2026-04-18T08:00:00Z",
    "type": "capability.skills"
  }
}
```

**Recording rules:**
- When `resolve_dependencies()` installs a dependency, add an entry with
  the current app in `installedBy`.
- If the dependency already exists in the ledger (another app installed it),
  append the current app to `installedBy`.
- If the dependency already exists but is NOT in the ledger (user installed
  it manually), do not create a ledger entry — it's user-owned.

### 4.7 Uninstall: Preview + Confirm

Uninstalling an app with dependencies is a two-step process.

**Step 1: Preview**

`GET /api/apps/{name}/uninstall/preview`

Returns an impact analysis classifying each dependency:

```jsonc
{
  "app": "oncall-watchtower",
  "resources": {
    "agents": ["oncall-watchtower/alert-agent"],
    "skills": ["oncall-watchtower/triage"],
    "crons": ["oncall-watchtower/check-alarms"]
  },
  "dependencies": {
    "removable": [
      {
        "id": "aws-documentation-mcp-server",
        "type": "capability.mcp",
        "reason": "Only used by this app"
      }
    ],
    "shared": [
      {
        "id": "SomeSkillPackage",
        "type": "capability.skills",
        "usedBy": ["another-app"],
        "reason": "Also used by another-app"
      }
    ],
    "userInstalled": [
      {
        "id": "some-other-mcp",
        "type": "capability.mcp",
        "reason": "Installed by user (not tracked)"
      }
    ]
  }
}
```

**Classification logic:**
- `removable`: In ledger, `installedBy` contains only the current app
- `shared`: In ledger, `installedBy` contains multiple apps
- `userInstalled`: Not in ledger at all (user installed it)

**Step 2: Confirm**

`POST /api/apps/{name}/uninstall`

```jsonc
{
  "purge_data": true,
  "keep_dependencies": false,
  "keep_specific": ["aws-documentation-mcp-server"]
}
```

- `purge_data: true` → explicitly delete the app's `data/` subtree; omission preserves it
- `keep_dependencies: true` → skip all dependency cleanup
- `keep_dependencies: false` → remove `removable` deps not in `keep_specific`
- `shared` and `userInstalled` are never removed regardless of flags

**Ledger cleanup:**
- For removed deps: delete the ledger entry entirely
- For shared deps: remove current app from `installedBy` array

### 4.8 Frontend UX: Uninstall Dialog

The wireframe below is illustrative — the actual implementation uses
Lucide icons (Trash2, Bot, Zap, Clock, Lock) per the project's icon
conventions, not emojis.

```
┌─────────────────────────────────────────────────┐
│  [Trash2]  Uninstall oncall-watchtower?         │
│            v1.2.0                               │
│                                                 │
│  This will remove all resources:                │
│    [Bot] 1 agent  [Zap] 1 skill  [Clock] 1 cron│
│                                                 │
│  Dependencies:                                  │
│    [Trash2] aws-documentation-mcp-server        │
│       Only used by this app                     │
│       ☐ Keep this dependency                    │
│                                                 │
│    [Lock] SomeSkillPackage                      │
│       Kept — used by another-app                │
│                                                 │
│    [Lock] some-other-mcp                        │
│       Kept — installed by you                   │
│                                                 │
│  ☐ Keep app data                                │
│                                                 │
│                    [Cancel]  [Uninstall]         │
└─────────────────────────────────────────────────┘
```

Removable deps show a checkbox to keep. Shared and user-installed deps
show a Lock icon and cannot be toggled.

### 4.9 Data Model

```python
# manifest.py
@dataclass
class CapabilityDependencies:
    mcp: list[str | dict[str, str]] = field(default_factory=list)
    skills: list[str | dict[str, str]] = field(default_factory=list)
    agents: list[str | dict[str, str]] = field(default_factory=list)

@dataclass
class Dependencies:
    managedBy: str = "gateway"
    capabilities: CapabilityDependencies = field(default_factory=CapabilityDependencies)
    commands: list[str] = field(default_factory=list)

# dependency_ledger.py (new file)
@dataclass
class LedgerEntry:
    installedBy: list[str]
    installedAt: str
    type: str  # "capability.mcp" | "capability.skills" | "capability.agents"

def record_install(dep_key: str, app_name: str, dep_type: str) -> None: ...
def record_uninstall(dep_key: str, app_name: str) -> None: ...
def get_entry(dep_key: str) -> LedgerEntry | None: ...
def list_by_app(app_name: str) -> list[tuple[str, LedgerEntry]]: ...
def classify_for_uninstall(app_name: str) -> dict: ...
```

---

## 5. MCP Server Registration in Bridges

Currently `bridges.py` registers agents, skills, and crons but not MCP
servers. App-provided `mcpServers` (from the manifest) need to be
registered into the global MCP config.

### 5.1 New Bridge: `_register_mcp_servers()`

When `resources=gateway`, bridges.py writes app-provided MCP servers
into `~/.kiro/settings/mcp.json`. `_read_mcp_json()` / `_write_mcp_json()`
use `fcntl.flock()` file locking (consistent with the dependency ledger)
to prevent corruption from concurrent writes by kiro-cli or other processes.

```python
def _register_mcp_servers(app_name: str, manifest: AppManifest) -> list[str]:
    """Register app-provided MCP servers into global mcp.json."""
    # Namespace: {app_name}:{server_name} to avoid collisions
    # Colon separator chosen because:
    #   - app names are kebab-case (validated by KEBAB_RE: [a-z0-9-]+)
    #   - server names are typically kebab-case
    #   - colon is not valid in either, so parsing is unambiguous
    # For url-based servers: write as-is
    # For command-based servers: resolve command path first
    ...

def _deregister_mcp_servers(app_name: str) -> int:
    """Remove app MCP servers from global mcp.json."""
    # Remove entries with "{app_name}:" prefix
    ...
```

Updated `register_app()`:
```
register_app(app_name)
  ├── _register_agents()        (existing)
  ├── _register_skills()        (existing)
  ├── _register_crons()         (existing)
  └── _register_mcp_servers()   (new)
```

---

## 6. Gateway Behavior Matrix

### 6.1 Enable / Disable

| Operation | resources=gateway | resources=app |
|-----------|-------------------|---------------|
| Enable | `register_app()` + `start_backend()` + run `onEnable` | run `onEnable` only |
| Disable | run `onDisable` + `stop_backend()` + `deregister_app()` | run `onDisable` only |

### 6.2 Update / Uninstall

| Operation | lifecycle=gateway | lifecycle=app | lifecycle=locked |
|-----------|-------------------|---------------|-----------------|
| Update | re-clone + `update_app()` + run `onUpdate` | return 400 | return 400 |
| Uninstall | preview → confirm → clean deps → remove files | preview → confirm → remove metadata | return 400 |

---

## 7. Complete Install Flows

### 7.1 Registry Server-Side App (standard)

```
1. Clone repo → ~/.kirocrew/app-sources/{repo}/
2. resolve_dependencies()          ← capability install for managedBy=gateway deps
3. Copy to ~/.kirocrew/apps/{name}/
4. register_app()                  ← symlink agents/skills/crons + register MCP
5. Run onInstall script            ← compile, initialize
6. Start backend                   ← if backend.entryPoint declared
```

### 7.2 Registry Client-Side App (e.g. Mochi, new user)

```
1. Clone repo → ~/.kirocrew/app-sources/{repo}/
2. resolve_dependencies()          ← capability install for managedBy=gateway deps
3. Run onInstall script            ← build + package .app + copy to ~/Applications
4. App launches → SDK authenticate() → register_external_app()
   (resources=app, so no bridges.py registration)
```

### 7.3 External App (e.g. Mochi, existing user)

```
1. App already running
2. SDK authenticate() → register_external_app()
3. No dependency resolution (app handles its own deps)
```

---

## 8. Files to Change

| File | Changes |
|------|---------|
| `apps/manager.py` | `InstalledApp` dataclass: replace `managed` with `origin`/`resources`/`lifecycle`; update `register_builtin_apps()`, `register_external_app()`, `install_app()`, `uninstall_app()` |
| `apps/manifest.py` | Add `SetupConfig.onUpdate`/`onEnable`/`onDisable`; add `Dependencies`, `CapabilityDependencies` dataclasses; add to `_KNOWN_FIELDS` |
| `apps/bridges.py` | Add `_register_mcp_servers()` / `_deregister_mcp_servers()`; update `register_app()` / `deregister_app()` |
| `apps/routes.py` | Update all `managed` checks to use new fields; add `GET .../uninstall/preview`; update uninstall to support `keep_dependencies`/`keep_specific`; run `onEnable`/`onDisable`/`onUpdate` scripts |
| `apps/registry.py` | Update `_enrich_with_install_status()` to use new fields; update `install_from_registry()` to call `resolve_dependencies()` |
| `apps/dependency_ledger.py` | New file: ledger CRUD, `classify_for_uninstall()` |
| `apps/dependencies.py` | New file: `resolve_dependencies()` via the `CapabilityManager` seam |
| `apps/app-registry.json` | Replace `managed` with `resources`/`lifecycle` for Mochi entry |
| `KiroCrewWebsite/src/pages/AppsPage.tsx` | Update badge logic, button visibility; update uninstall dialog with dependency preview |
| `KiroCrewWebsite/src/api/client.ts` | Add `uninstallPreview()` API call |

---

## 9. Backward Compatibility

### 9.1 Uninstall API

The uninstall endpoint (`POST /api/apps/{name}/uninstall`) remains
backward-compatible for non-destructive requests. Existing clients that send
`{ "keep_data": true }` continue to preserve data. Legacy `keep_data: false`
is intentionally ignored because deletion now requires the dedicated literal
`{ "purge_data": true }` action. The new `keep_dependencies` and
`keep_specific` fields default to `false` and `[]` respectively, which means
"clean up removable dependencies" (the safe default).

The new preview endpoint (`GET /api/apps/{name}/uninstall/preview`) is
additive — existing clients that don't call it simply skip the preview
step and go straight to confirm, which works identically to the current
behavior (no dependency cleanup, since no ledger entries exist yet).

### 9.2 SDK Compatibility

The SDK's `authenticate()` method calls `POST /api/apps/register`.
The endpoint is updated to accept the new `origin`/`resources`/`lifecycle`
fields. Default values (`external`/`app`/`app`) apply when fields are
omitted, so existing SDK versions that don't send these fields continue
to work correctly.

### 9.3 Frontend

The frontend is updated to use the new `origin`/`resources`/`lifecycle`
fields directly for badge/button logic. The old `managed` field is no
longer read.

---

## 10. Testing Strategy

### 10.1 Unit Tests

| Module | Test focus |
|--------|-----------|
| `manager.py` | `InstalledApp` serialization round-trip with new fields; migration from old `managed`; `uninstall_app()` with `lifecycle=locked` rejection |
| `manifest.py` | `SetupConfig` with new hooks; `Dependencies` / `CapabilityDependencies` parsing; validation of `managedBy` values |
| `bridges.py` | `_register_mcp_servers()` writes correct entries to mcp.json; `_deregister_mcp_servers()` removes only namespaced entries; colon separator parsing |
| `dependency_ledger.py` | CRUD operations; `classify_for_uninstall()` with removable/shared/userInstalled cases; concurrent access with file locking |
| `dependencies.py` | `resolve_dependencies()` with the capability manager faked; `managedBy` override logic; missing commands detection |
| `routes.py` | Enable/disable with `onEnable`/`onDisable` scripts; update with `onUpdate` + rollback; uninstall preview + confirm flow |
| `registry.py` | `_enrich_with_install_status()` with new fields; `install_from_registry()` calling `resolve_dependencies()` |

### 10.2 Integration Tests

- Install a test app from a local path, verify all three fields are set correctly
- Enable/disable cycle with `onEnable`/`onDisable` scripts that create/remove marker files
- Update with intentionally failing `onUpdate` script, verify rollback restores old version
- Install two apps sharing a dependency, uninstall one, verify dependency is kept (shared)
- Uninstall the second app, verify dependency is removed (last reference)

### 10.3 Frontend Tests

- Badge rendering for each `origin` value
- Button visibility matrix based on `resources` and `lifecycle`
- Uninstall dialog with dependency preview (mock API responses for removable/shared/userInstalled)

### 10.4 Lifecycle Script Testing

Lifecycle scripts are tested via integration tests with minimal shell
scripts that create/check marker files. The sandbox (`wrap_argv`) is
tested separately in existing test infrastructure. CI does not execute
any real package manager — the `CapabilityManager` seam is faked in all dependency tests.
