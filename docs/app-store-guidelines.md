# KiroCrew App Store — Publishing Guidelines

This guide walks you through publishing an app to the KiroCrew App Store. An "app" is a package that contributes agents, skills, MCP servers, cron jobs, or UI pages to KiroCrew.

## Quick Start

1. Create `app.json` at your repo root
2. Add your app to `src/kiro_crew/apps/app-registry.json`
3. Open a Pull Request against the KiroCrew repo

## 1. The App Manifest (`app.json`)

Every app needs an `app.json` at the repo root. This is the single source of truth for your app's identity, resources, and store listing.

### Required Fields

```json
{
  "name": "my-app",
  "version": "1.0.0",
  "displayName": "My App — Short Tagline",
  "description": "One paragraph describing what your app does. This appears in the App Store browse view and detail page.",
  "author": "your-alias"
}
```

| Field | Rules |
|-------|-------|
| `name` | Kebab-case, lowercase, unique across all apps. This is the install ID. |
| `version` | Semver (`major.minor.patch`). Bump on every release. |
| `displayName` | Human-readable. Keep under 40 chars for clean card layout. |
| `description` | 1-3 sentences. No markdown. Appears in browse cards (truncated to 2 lines) and detail page (full). |
| `author` | Your Amazon alias. |

### Store Listing Fields

These fields control how your app appears in the App Store:

```json
{
  "repo": "MyPackage",
  "iconPath": "assets/icon/logo.png",
  "screenshots": [
    "assets/screenshots/main.png",
    "assets/screenshots/settings.png"
  ],
  "highlights": [
    "Feature one — short description",
    "Feature two — short description"
  ],
  "tags": ["productivity", "oncall", "monitoring"]
}
```

| Field | Purpose |
|-------|---------|
| `repo` | Git repository name. Used by the blob proxy to serve images. |
| `iconPath` | Path to app icon relative to repo root. PNG, square, min 256x256px. |
| `screenshots` | Array of image paths relative to repo root. PNG or JPG, max 5. |
| `highlights` | Feature bullet points shown on the detail page. Max 10 items. |
| `tags` | Discovery tags for search/filter. Lowercase, max 15. |

### Resource Declarations

Declare what your app contributes to KiroCrew:

```json
{
  "agents": ["agents/my-agent.json"],
  "skills": ["skills/my-skill"],
  "mcpServers": {
    "my-mcp": {
      "command": "python3",
      "args": ["backend/mcp_server.py"]
    }
  },
  "crons": [
    {
      "name": "my-check",
      "every": 300,
      "agent": "my-agent",
      "message": "Run the periodic check"
    }
  ]
}
```

### Cron Registration Bridge

When `resources: "gateway"`, KiroCrew automatically registers and deregisters cron jobs declared in your `app.json` manifest:

- `_register_crons` serializes all `CronEntry` fields (including `agent_sequence`, `env`, `persistent_session`, `silent`)
- `register_app_crons_with_service()` uses CronSDK for idempotent registration, ownership-tagged via `created_by='app:{name}'`
- `deregister_app_crons_from_service()` cleans up on disable/uninstall
- Wired into: `on_app_enable`, `on_gateway_startup`, CLI disable/uninstall, and HTTP uninstall
- SEL audit on all registration/deregistration paths

For `resources: "app"` apps, the gateway does NOT manage cron registration — your app handles its own lifecycle.

### Permissions

Declare what your app needs access to:

```json
{
  "permissions": {
    "api": ["/api/chat/*", "/api/status"],
    "events": ["chat_chunk", "notification"],
    "mcpTools": ["tool_name_1", "tool_name_2"],
    "storage": true,
    "cron": false,
    "network": false
  }
}
```

### Setup

If your app needs a build step or dependency installation:

```json
{
  "setup": {
    "onInstall": "bash setup.sh",
    "onUpdate": "bash update.sh",
    "onUninstall": "bash scripts/uninstall.sh",
    "onEnable": "bash enable.sh",
    "onDisable": "bash disable.sh",
    "onEnableTimeout": 120,
    "onDisableTimeout": 60
  }
}
```

Rules:
- `onInstall` — runs after first install. Required if your app needs build steps, dependency installation, or creates resources outside `~/.kiro/crew/apps/{name}/`.
- `onUpdate` — runs after update (new code in place, `data/` preserved). Use for recompilation, migrations, or restarting backend processes.
- `onUninstall` — runs before removing files. Only needed if your app creates resources outside KiroCrew's managed directories (e.g. `~/Applications/MyApp.app`, shell aliases, launchd plists). KiroCrew automatically cleans up everything it manages.
- `onEnable` — runs when the user enables the app. Use for starting backend processes or registering external services.
- `onDisable` — runs when the user disables the app. Use for stopping backend processes or deregistering external services.
- `onEnableTimeout` / `onDisableTimeout` — optional, defaults to 30 seconds. Increase for apps that need to start/stop Docker containers or heavy backends.
- All scripts run with `set -euo pipefail` enforced by KiroCrew. This means:
  - Unset variables cause immediate exit (prevents `rm -rf $EMPTY_VAR/` disasters)
  - Any command failure stops execution (no silent errors)
  - Pipe failures propagate (no hidden failures in `cmd1 | cmd2`)
- All scripts run with `NONINTERACTIVE=1` in the environment. They must exit 0 on success.
- `onUninstall` also receives `KEEP_DATA=1` or `KEEP_DATA=0` — if the user chose "Keep app data", the script should skip deleting user data directories.
- Timeout limits: `onInstall`/`onUpdate` = 300s, `onUninstall` = 120s, `onEnable`/`onDisable` = configurable (default 30s).
- `onUninstall` should only clean up resources that KiroCrew cannot manage (e.g. app binaries, shell aliases, external config directories). For `resources: "gateway"` apps, agent configs, skills, MCP entries, and cron jobs are handled by the gateway automatically — do not duplicate that cleanup in your uninstall script. For `resources: "app"` apps, the gateway does not deregister resources — your `onUninstall` script is responsible for cleaning up its own agent configs, skills, MCP entries, and cron jobs.
- If `onEnable` fails, the enable is rolled back (app stays disabled).
- If `onDisable` fails, the disable proceeds anyway (with warnings in the response).
- If `onUpdate` fails, the update is rolled back to the previous version.

### Dependencies

Declare external dependencies that KiroCrew should install for your app.
The open-source edition ships no capability manager, so `capabilities` entries
are reported as unresolved and the app still installs — degrade gracefully:

```json
{
  "dependencies": {
    "managedBy": "gateway",
    "capabilities": {
      "mcp": ["some-documentation-mcp-server"],
      "skills": ["SomeSkillPackage"],
      "agents": ["SomeAgentPackage"]
    },
    "commands": ["node", "python3"],
    "optionalCommands": ["git"]
  }
}
```

| Field | Description |
|-------|-------------|
| `managedBy` | `"gateway"` (default) = KiroCrew resolves deps through the edition's capability manager. `"app"` = app handles its own deps, KiroCrew only checks existence. |
| `capabilities.mcp` | MCP servers to install. |
| `capabilities.skills` | Skill packages to install. |
| `capabilities.agents` | Declarable, but never gateway-installed — always reported unresolved. |
| `commands` | REQUIRED system commands to check (not installed, just verified). Missing commands produce a warning and are reported in `missing`. |
| `optionalCommands` | Same check, but absence is not a problem — reported in `missingOptional`. Use it for a tool the app can work without, or can provision itself (Papyrus lists `tectonic` here because it installs a pinned copy when the host has no TeX). |

Per-dependency override: use object format to override `managedBy` for individual entries:

```json
{
  "dependencies": {
    "managedBy": "gateway",
    "capabilities": {
      "mcp": [
        "some-docs-mcp",
        { "id": "my-custom-mcp", "managedBy": "app" }
      ]
    }
  }
}
```

Dependencies are tracked in a reference-counting ledger. On uninstall, KiroCrew shows which dependencies can be safely removed (only used by this app) vs. which are shared with other apps or user-installed.

### Platform

Declare platform requirements if your app only runs on specific operating systems:

```json
{
  "platform": {
    "os": ["macos"],
    "installMode": "client",
    "clientInstall": {
      "shell": "git clone https://github.com/you/MyApp.git ~/MyApp && cd ~/MyApp && KIROCREW_HOST={{gateway_host}} bash setup.sh",
      "postInstall": "open ~/Applications/MyApp.app"
    }
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `os` | `["macos", "linux"]` | Platforms the app can run on. Values: `macos`, `linux`. |
| `installMode` | `"server"` | `"server"` = KiroCrew installs directly. `"client"` = must be installed on the user's local machine. |
| `clientInstall.shell` | — | One-liner for the user to run in their local terminal. Shown when KiroCrew is on an incompatible platform. Supports template variables: `{{gateway_url}}` (full dashboard URL), `{{gateway_host}}` (cloud desktop hostname for SSH tunnel). |
| `clientInstall.postInstall` | — | Command to run after install (e.g. launch the app). Shown as a hint. |

When `installMode` is `"client"` and KiroCrew runs on an incompatible platform (e.g. Linux cloud desktop), the App Store shows a copy-paste instruction panel instead of running the install script. The app registers itself with KiroCrew on first launch via `POST /api/apps/register`.

When KiroCrew runs on a compatible platform (e.g. macOS local), the install proceeds normally — clone + run `setup.onInstall`.

### Optional Fields

```json
{
  "license": "Amazon-Internal",
  "minKiroCrewVersion": "1.2.0",
  "detectInstalled": "test -d ~/Applications/MyApp.app",
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [{ "route": "/apps/my-app", "label": "My App", "icon": "Zap" }]
  },
  "backend": {
    "entryPoint": "backend/app.py",
    "port": "auto",
    "healthCheck": "/health"
  }
}
```

## 2. Image Assets

### App Icon

- Format: PNG with transparency
- Size: minimum 256x256px, square aspect ratio
- Location: commit to your repo (e.g. `assets/icon/logo.png`)
- The App Store serves icons via a git blob proxy — no CDN or external hosting needed

### Screenshots

- Format: PNG or JPG
- Recommended: 1200px wide, 16:9 or similar aspect ratio
- Location: commit to your repo (e.g. `assets/screenshots/`)
- Max 5 screenshots per app

### Hero Images

Hero art is the main way an app looks like a product rather than a list entry. Ship one — every store surface uses it.

| Field | Rendered where | Recommended size |
|-------|----------------|------------------|
| `heroImage` | Discover list rows and Library rows (16:9 capsule), featured spotlight, feature cards, detail-page banner | 1200x675 (16:9) |
| `heroImageDark` | Same surfaces when the user's theme is dark | 1200x675 (16:9) |
| `heroImageDetail` / `heroImageDetailDark` | Detail-page banner only, preferred over `heroImage` there | 1200x288 (25:6) |

Resolution order on every surface: the current theme's art, then the opposite theme's, then the first screenshot. When an app ships none — or the image fails to load — the store falls back to a deterministic gradient with the app icon, so a missing hero degrades cleanly instead of leaving a blank panel.
- First screenshot is the hero image on the detail page

## 3. Registry Entry

To list your app in the App Store, add an entry to `src/kiro_crew/apps/app-registry.json`:

```json
{
  "name": "my-app",
  "repo": "MyPackage",
  "branch": "mainline"
}
```

That's it. All display information (description, screenshots, highlights, tags, version) is fetched automatically from your repo's `app.json`. The registry entry is intentionally minimal — your `app.json` is the single source of truth.

### Required Fields

| Field | Description |
|-------|-------------|
| `name` | Must match the `name` in your `app.json`. |
| `repo` | Git repository name. Used to fetch `app.json` and serve images. |
| `versionSet` | Version set for dependency resolution (e.g. `"KiroCrew/development"`). |

### Optional Fields

| Field | Default | Description |
|-------|---------|-------------|
| `branch` | `mainline` | Git branch to fetch from. |
| `subdirectory` | `""` | Path within the repo where `app.json` lives (if not at root). |
| `resources` | `"gateway"` | `"gateway"` = KiroCrew manages agent/skill/cron registration via symlinks. `"app"` = app handles its own resource registration. |
| `lifecycle` | `"gateway"` | `"gateway"` = KiroCrew handles updates and uninstall. `"app"` = app handles its own updates. |
| `detectInstalled` | `""` | Shell command that exits 0 if the app is already installed (e.g. `test -d ~/Applications/MyApp.app`). |
| `featured` | unset | Curator flag for the Discover editorial layer. `true` marks the app featured; a number both marks it and orders the slots (lower first — `1` takes the spotlight, `2`/`3` the secondary cards). Lives on the registry entry (curator-controlled), not in `app.json`. Honored only for core-registry entries — a `featured` flag from an external registry is ignored, so adding a registry cannot seize the Discover spotlight. When no entries are flagged, the store falls back to a deterministic pick (hero art, then verified publishers). |

### How It Works

The App Store fetches each app's `app.json` from its repo (via `git archive`) and caches it locally for 24 hours. Image paths in `app.json` (icon, screenshots) are automatically converted to blob proxy URLs. You never need to manually sync metadata between your repo and the registry.

When you update your app, just bump the version in your `app.json` and push. The App Store picks up the changes within 24 hours (or immediately if the user refreshes).

### Federated External Registries

Teams can host their own app registries without requiring KiroCrew team review for each app. Users opt in by adding external registries to their config:

```json
{
  "registries": [
    {"name": "identityservices", "repo": "IdentityServicesKirocrewAppRegistry", "branch": "mainline"}
  ]
}
```

**How external registries work:**

- `ExternalRegistryConfig` dataclass validates `name`, `repo`, and `branch` fields
- `_fetch_external_registry_index()` uses `git archive` to fetch `app-registry.json` from the external repo; falls back to `apps/*/app.json` discovery if no index file exists
- Results are cached for 1 hour with `ignore_ttl` for synchronous lookups (stale > missing)
- `get_registry_app()` searches external registry caches after the built-in registry
- Input validation: repo/branch names validated against strict regex patterns to reject path traversal

**Trust model:** user explicitly opts in by adding the registry to their config. The repo must be accessible via git.

**Credential posture and the same-repo carve-out:**

By default, apps listed in an external registry index are cloned **credential-free** (anonymous env + strict sandbox hiding `~/.ssh`) — a confused-deputy defense preventing an untrusted index from reading private sibling repos on the owner's trusted forge. This means private-forge registries whose apps live in *separate* repos require each app repo to be independently accessible (typically only works with public forges).

The **same-repo carve-out** relaxes this for the common **monorepo layout**: when an index entry's effective clone URL is **byte-identical** to the owner-configured registry repo URL (the URL the owner typed when adding the registry), the confused-deputy argument does not apply — the owner explicitly designated that exact URL. Such entries use owner credentials (`minimal_env` + context sandbox mode) for both manifest fetches and installs. The comparison is exact string equality with no URL normalization; sibling repos on the same host remain anonymous+strict.

**Private-forge recipe (SSH-key-only forges):**

For credential-only forges (SSH-key auth, no anonymous access), use the monorepo `apps/*` layout so all apps are inside the registry repo:

1. Create a single registry package/repo containing:
   ```
   app-registry.json          # or just use apps/*/ auto-discovery
   apps/
     my-tool/app.json
     my-tool/src/...
     other-app/app.json
     other-app/src/...
   ```

2. Add the registry with the SSH URL:
   ```json
   {"name": "my-team", "repo": "ssh://git.example.com/team/MyTeamApps", "branch": "main"}
   ```

3. Because every app's clone URL matches the registry repo URL (the monorepo layout ensures this), the same-repo carve-out applies and all manifest fetches + installs succeed with the owner's SSH credentials.

**Important:** apps in *separate* repos on the same private forge will NOT benefit from the carve-out (their URLs differ from the registry URL) and will fail to clone. Keep apps inside the registry repo for private forges.

**Management API** (`/api/apps/registries`):

| Method | Purpose |
|--------|---------|
| `GET /api/apps/registries` | Returns current registries list from config |
| `PUT /api/apps/registries` | Validates and replaces the registries array |

PUT validation: repo regex, branch regex, blocked repos (the KiroCrew repo itself is blocked). SEL audit on successful updates.

## 4. Installation Modes

Your app can be installed in two ways:

### Registry Install (recommended)

Users click "Install" in the App Store. KiroCrew clones the package into a workspace.

1. KiroCrew creates a workspace at `~/.kiro/crew/app-sources/{app_name}/` (one per app)
2. Clones the repo at the specified branch
3. Runs the build step (npm/pip depending on the package type)
4. Runs `setup.onInstall` if declared

Requirements:
- Repo must be accessible via git
- `app.json` must be at the repo root (or at `subdirectory` if specified)
- Install script must be non-interactive and complete within 5 minutes

### Self-Managed Install

For apps with their own installer (like Electron apps), the app registers itself with KiroCrew at runtime via `POST /api/apps/register`. KiroCrew tracks metadata but doesn't manage the app's resources.

Use this when:
- Your app has its own build/package system (Electron, native binary)
- Your app needs runtime-dynamic agent configuration
- Your app manages its own agent/skill/MCP registration

API:
```
POST /api/apps/register
{
  "name": "my-app",
  "version": "1.0.0",
  "displayName": "My App",
  "source": "self-managed",
  "origin": "external",
  "resources": "app",
  "lifecycle": "app",
  "manifest": { ... full app.json content ... }
}
```

The three classification fields control behavior:
- `origin` — where the app came from: `"builtin"`, `"registry"`, `"local"`, `"external"`
- `resources` — who manages agent/skill/cron registration: `"gateway"` or `"app"`
- `lifecycle` — who manages updates/uninstall: `"gateway"`, `"app"`, or `"locked"`

## 5. Review Checklist

Before submitting your registry Pull Request:

- [ ] `app.json` passes validation (`name` is kebab-case, `version` is semver, required fields present)
- [ ] No path traversal in resource paths (`..` not allowed)
- [ ] Icon is square PNG, min 256x256px, committed to repo
- [ ] At least 1 screenshot committed to repo
- [ ] `description` is 1-3 sentences, no markdown
- [ ] `highlights` has 3-8 bullet points
- [ ] `tags` are lowercase, relevant, max 15
- [ ] Install script is non-interactive and exits 0
- [ ] Install script completes within 5 minutes
- [ ] If `onInstall` is present, `onUninstall` is also present
- [ ] Uninstall script cleans up all resources created outside `~/.kiro/crew/apps/{name}/`
- [ ] App works with `kirocrew app install /path/to/local/clone`
- [ ] Permissions are minimal — only declare what you actually use

## 6. App Types

### Agent-Only App

Contributes agents and skills, no UI. Example: an oncall triage agent.

```json
{
  "name": "oncall-triage",
  "agents": ["agents/triage.json"],
  "skills": ["skills/ticket-analysis"]
}
```

### Full-Stack App

Agents + skills + backend process + dashboard UI page. Example: a monitoring dashboard.

```json
{
  "name": "service-monitor",
  "agents": ["agents/monitor.json"],
  "backend": { "entryPoint": "backend/app.py" },
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [{ "route": "/apps/service-monitor", "label": "Monitor", "icon": "Activity" }]
  }
}
```

### Self-Managed App

External app (Electron, CLI tool) that registers with KiroCrew at runtime. Example: Mochi desktop pet.

The app calls `POST /api/apps/register` on startup with `resources: "app"` and manages its own agent configs, skills, and MCP servers.

## 7. Version Compatibility

Apps can declare `minKiroCrewVersion` in `app.json`. KiroCrew checks this during install and update — if the current version is too old, the operation is rejected with a clear error message telling the user to update KiroCrew first.

## 8. Package Build Systems

Apps can use npm (for TypeScript/React) or pip (for Python) as their build system. The App Store clones the repo and runs the appropriate build command.

### How It Works

1. On install, KiroCrew creates a workspace at `~/.kiro/crew/app-sources/{app_name}/` (one per app)
2. Clones the package at the specified branch
3. Runs `npm install && npm run build` (for JS/TS packages) or `pip install .` (for Python packages)
4. `setup.onInstall` runs after build (for post-build steps like `electron-builder`)

### Registry Entry Example

```json
{
  "name": "mochi-pet",
  "repo": "Mochi",
  "branch": "main",
  "resources": "app",
  "lifecycle": "app",
  "detectInstalled": "test -d ~/Applications/Mochi.app"
}
```

### Setup Script

Since the standard build step handles dependency resolution and compilation, the `onInstall` script should only do post-build packaging:

```json
{
  "setup": {
    "onInstall": "bash setup.sh",
    "onUninstall": "bash scripts/uninstall.sh"
  }
}
```

```bash
#!/usr/bin/env bash
set -euo pipefail
# Build already ran — node_modules and build artifacts are ready.
# This script only does post-build packaging.
[ -d "node_modules" ] || exit 1  # sanity check

npx electron-builder --mac --dir
cp -R release/mac-arm64/MyApp.app ~/Applications/MyApp.app
```

## 9. Versioning

- Use semver: `major.minor.patch`
- Bump `patch` for bug fixes
- Bump `minor` for new features
- Bump `major` for breaking changes (agent config schema, MCP tool interface)
- Just bump the version in your `app.json` and push — the registry entry has no version field to update

## 10. Support

- Questions: `#kirocrew-contributors` on Slack
- Bugs: file a [GitHub issue](https://github.com/kirodotdev/KiroCrew/issues)
- Feature requests: same, label `app-store`
