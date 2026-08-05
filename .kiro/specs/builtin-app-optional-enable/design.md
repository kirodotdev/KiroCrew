# Design Document: Builtin App Optional Enable

## Overview

This feature extends KiroCrew's builtin app registration system to support apps that default to disabled. The change is minimal: add a `defaultEnabled` field to the builtin app definition dictionary, use it during first-time registration, and adjust the frontend Browse tab to show disabled builtin apps alongside registry apps.

The existing architecture already handles most of the work:
- `register_builtin_apps()` already preserves user state on restart (only updates version/displayName)
- The sidebar already filters by `enabled && manifest.ui.pages.length > 0`
- The enable/disable API already works for all installed apps including builtins
- `lifecycle="locked"` already prevents uninstall

The primary changes are:
1. Backend: Read `defaultEnabled` from the builtin app definition when creating a new InstalledApp entry
2. Frontend: Include disabled builtin apps in the Browse tab display
3. Documentation: Add inline developer documentation to the `_BUILTIN_APPS` definition format

## Architecture

```mermaid
flowchart TD
    subgraph Gateway Startup
        A[register_builtin_apps] --> B{App already in installed.json?}
        B -->|Yes| C[Update version/displayName only<br/>Preserve enabled state]
        B -->|No| D[Read defaultEnabled from definition<br/>Default: true if not specified]
        D --> E[Create InstalledApp with enabled=defaultEnabled]
    end

    subgraph Frontend - Browse Tab
        F[fetchRegistry] --> G[Registry apps from /api/registry]
        H[fetchApps] --> I[All installed apps from /api/apps]
        I --> J[Filter: origin=builtin AND enabled=false]
        G --> K[Merge: registry apps + disabled builtins]
        J --> K
        K --> L[Render Browse Tab cards]
    end

    subgraph Frontend - Sidebar
        M[refreshAppNav via /api/apps] --> N[Filter: enabled=true AND has ui.pages]
        N --> O[Render nav items]
    end

    subgraph User Actions
        P[User clicks Enable in Browse] --> Q[POST /api/apps/{name}/enable]
        Q --> R[mc:apps-changed event]
        R --> M
        R --> H
    end
```

## Components and Interfaces

### Backend: `_BUILTIN_APPS` Definition Schema

The builtin app definition dictionary gains one new optional field:

```python
{
    "name": str,              # Required. Kebab-case identifier
    "version": str,           # Required. Semver version string
    "displayName": str,       # Required. Human-readable name
    "description": str,       # Required. Short description for App Store
    "author": str,            # Required. Author name
    "tags": list[str],        # Optional. Categorization tags
    "defaultEnabled": bool,   # Optional. Default: True. Initial enabled state on first registration
    "permissions": dict,      # Optional. API and event permissions
    "ui": {                   # Optional. UI configuration
        "pages": [            # Pages to show in sidebar when enabled
            {
                "route": str,   # React Router path
                "label": str,   # Sidebar label
                "icon": str,    # Lucide icon name
            }
        ]
    },
}
```

### Backend: `register_builtin_apps()` Change

```python
def register_builtin_apps() -> int:
    count = 0
    for app_data in _BUILTIN_APPS:
        name = app_data["name"]
        existing = _read_installed(name)

        dest = app_dir(name)
        dest.mkdir(parents=True, exist_ok=True)

        if existing:
            # Only update version + displayName, preserve user state (including enabled)
            existing.version = app_data["version"]
            existing.displayName = app_data["displayName"]
            existing.updatedAt = _now_iso()
            existing.origin = "builtin"
            existing.resources = "gateway"
            existing.lifecycle = "locked"
            _write_installed(name, existing)
        else:
            # NEW: Use defaultEnabled from definition (defaults to True for backward compat)
            default_enabled = app_data.get("defaultEnabled", True)
            meta = InstalledApp(
                name=name,
                version=app_data["version"],
                displayName=app_data["displayName"],
                enabled=default_enabled,
                installedAt=_now_iso(),
                source="builtin",
                origin="builtin",
                resources="gateway",
                lifecycle="locked",
            )
            _write_installed(name, meta)

        # Persist manifest so dashboard can show full info
        atomic_write(
            dest / APP_MANIFEST_FILENAME,
            json.dumps(app_data, indent=2) + "\n",
        )
        count += 1

    if count:
        logger.info("Registered %d built-in app(s)", count)
    return count
```

### Backend: Validation for Builtin App Definitions

Add a validation function that runs at startup to catch developer errors early:

```python
_REQUIRED_BUILTIN_FIELDS = {"name", "version", "displayName", "description", "author"}

def _validate_builtin_app(app_data: dict[str, Any]) -> list[str]:
    """Validate a builtin app definition. Returns list of errors (empty = valid)."""
    errors: list[str] = []
    for field in _REQUIRED_BUILTIN_FIELDS:
        if not app_data.get(field):
            errors.append(f"missing required field: {field}")
    if "defaultEnabled" in app_data and not isinstance(app_data["defaultEnabled"], bool):
        errors.append("defaultEnabled must be a boolean")
    name = app_data.get("name", "")
    if name and not _check_path_safety(name):
        errors.append(f"unsafe app name: {name!r}")
    return errors
```

### Frontend: Browse Tab Enhancement

The Browse tab currently only shows registry apps. It needs to also show disabled builtin apps.

```typescript
// In AppsPage.tsx, within the browse tab section:

// Shared display type for Browse tab items (avoids RegistryApp type hack)
type BrowseItem = {
  name: string
  displayName: string
  description: string
  version: string
  author: string
  icon?: string
  iconUrl?: string
  tags?: string[]
  installed: boolean
  enabled?: boolean
  origin?: string
  lifecycle?: string
  // Registry-only fields (undefined for builtins)
  highlights?: string[]
  screenshots?: string[]
  repo?: string
  branch?: string
  updateAvailable?: boolean
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string } }
}

// Derive disabled builtins from the installed apps list
const disabledBuiltins: BrowseItem[] = apps
  .filter(a => a.origin === 'builtin' && !a.enabled)
  .map(a => ({
    name: a.name,
    displayName: a.displayName || a.name,
    description: a.manifest?.description || '',
    version: a.version,
    author: a.manifest?.author || 'kirocrew',
    tags: a.manifest?.tags,
    installed: true,  // They ARE installed, just disabled
    enabled: false,
    origin: 'builtin',
    lifecycle: 'locked',
  }))

// Merge with registry apps for display
const browseApps: BrowseItem[] = [...disabledBuiltins, ...registry]
```

**Design decision:** We define a `BrowseItem` type rather than reusing `RegistryApp` directly. This avoids a type hack where builtin apps pretend to be registry apps. If `RegistryApp` gains registry-specific required fields later, this won't break.

The card rendering for disabled builtins shows an "Enable" button instead of "Get", with a "Built-in" badge for visual distinction:

```typescript
{app.origin === 'builtin' && !app.enabled ? (
  <div className="flex items-center justify-between">
    <Badge variant="aim">Built-in</Badge>
    <Btn onClick={(e) => { e.stopPropagation(); handleAction(app.name, 'enable') }}>
      <Power className="lucide-inline" /> Enable
    </Btn>
  </div>
) : app.installed ? (
  <Badge variant="ok">Installed</Badge>
) : (
  <span className="text-[13px] text-accent font-medium">Get</span>
)}
```

**UX note:** The "Built-in" badge distinguishes these from registry apps (which show "Get"). Users understand that "Enable" means activating a feature already bundled in KiroCrew, while "Get" means downloading something new.

### Frontend: Installed Tab Behavior

Disabled builtins are excluded from the Installed tab — they only appear in the Browse tab. The filter is:
```typescript
const installedApps = apps.filter(a => !(a.origin === 'builtin' && !a.enabled))
```

Enabled builtins appear in the Installed tab with:
- "Hide" button (not "Disable") to move them back to Browse
- No uninstall button (`lifecycle === "locked"`)
- "Built-in" badge for `origin === "builtin"`

### Frontend: Sidebar Behavior

No changes needed. The `refreshAppNav` function in `App.tsx` already filters by:
```typescript
apps.filter(a => a.enabled && a.manifest?.ui?.pages?.length > 0)
```

Disabled builtin apps are automatically excluded from the sidebar.

## Data Models

### Builtin App Definition (Extended)

No new data model — the existing `_BUILTIN_APPS` list of dictionaries gains one optional key:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | `str` | Yes | — | Kebab-case app identifier |
| `version` | `str` | Yes | — | Semver version |
| `displayName` | `str` | Yes | — | Human-readable name |
| `description` | `str` | Yes | — | App Store description |
| `author` | `str` | Yes | — | Author name |
| `tags` | `list[str]` | No | `[]` | Categorization tags |
| `defaultEnabled` | `bool` | No | `True` | Initial enabled state on first registration |
| `permissions` | `dict` | No | `{}` | API/event permissions |
| `ui` | `dict` | No | `{}` | UI pages configuration |

### InstalledApp Metadata (Unchanged)

The `InstalledApp` dataclass is not modified. The `enabled` field already exists and is used as-is. The only behavioral change is that new builtin apps may be created with `enabled=False` based on the definition's `defaultEnabled` value.


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: First-time registration respects defaultEnabled

*For any* builtin app definition with a `defaultEnabled` value (or without one), when that app is registered for the first time (no existing `installed.json`), the resulting `InstalledApp.enabled` field SHALL equal `defaultEnabled` if specified, or `True` if not specified.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Re-registration preserves user state

*For any* builtin app that already has an `installed.json` with any `enabled` value (True or False), calling `register_builtin_apps()` SHALL not change the `enabled` field — only `version`, `displayName`, and `updatedAt` may change.

**Validates: Requirements 1.4, 2.2, 6.2, 6.3**

### Property 3: All builtin apps have lifecycle=locked

*For any* builtin app after registration (whether new or re-registered), the `InstalledApp.lifecycle` field SHALL equal `"locked"`, and calling `uninstall_app()` on it SHALL return an error result.

**Validates: Requirements 2.3, 5.4**

### Property 4: Browse filter shows exactly disabled builtins

*For any* list of installed apps, the browse tab filter SHALL include an app if and only if `origin == "builtin"` AND `enabled == False`. Enabled builtins SHALL NOT appear in the browse list. Non-builtin apps are handled separately by the registry.

**Validates: Requirements 3.1, 3.4, 4.4, 5.3**

### Property 5: Sidebar filter shows only enabled apps with pages

*For any* list of installed apps, the sidebar navigation SHALL include an app if and only if `enabled == True` AND `manifest.ui.pages` has at least one entry.

**Validates: Requirements 4.2, 5.2**

### Property 6: Enable/disable round-trip persists state

*For any* installed builtin app, enabling it then reading its metadata SHALL show `enabled=True`, and disabling it then reading its metadata SHALL show `enabled=False`. The state change SHALL be immediately reflected in the persisted `installed.json`.

**Validates: Requirements 4.1, 5.1, 6.1**

### Property 7: API returns complete manifest for all builtins

*For any* builtin app (enabled or disabled), the `list_apps()` response SHALL include `origin`, `enabled`, `lifecycle` fields AND the full manifest data including `description`, `tags`, and `ui.pages`.

**Validates: Requirements 3.2, 7.1, 7.3**

### Property 8: Invalid definitions are skipped without affecting others

*For any* `_BUILTIN_APPS` list containing a mix of valid and invalid definitions, `register_builtin_apps()` SHALL successfully register all valid apps and skip invalid ones, logging a warning for each skipped app.

**Validates: Requirements 8.4**

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `defaultEnabled` is not a boolean | Log warning, skip that app definition, continue with others |
| Missing required fields in definition | Log warning, skip that app definition, continue with others |
| `installed.json` is corrupted on restart | Treat as new app, use `defaultEnabled` from definition. **Note:** This means a user who manually disabled an app with `defaultEnabled: true` will see it re-enabled after corruption. This is acceptable — the alternative (defaulting to disabled) would hide previously-enabled apps, which is worse. |
| Enable/disable API called for non-existent app | Return error result (existing behavior, unchanged) |
| Uninstall called on builtin app | Return error: "cannot be uninstalled (lifecycle=locked)" (existing behavior) |
| Filesystem error writing `installed.json` | Return error result, do not change in-memory state |

## Testing Strategy

### Property-Based Testing

Use `pytest` with `hypothesis` for property-based tests. Each property test runs minimum 100 iterations with generated inputs.

**Test file**: `test/test_builtin_app_optional_enable.py`

| Property | Test Strategy |
|----------|---------------|
| Property 1 | Generate random app definitions with/without `defaultEnabled`. Call registration on empty state. Assert `enabled` matches expectation. |
| Property 2 | Generate random app definitions + random existing `InstalledApp` states. Call registration. Assert `enabled` unchanged. |
| Property 3 | Generate random app definitions. Register them. Assert `lifecycle=="locked"`. Call `uninstall_app()`. Assert error. |
| Property 4 | Generate random lists of apps with varying `origin`/`enabled`. Apply browse filter. Assert result contains exactly disabled builtins. |
| Property 5 | Generate random lists of apps with varying `enabled`/`ui.pages`. Apply sidebar filter. Assert result contains exactly enabled apps with pages. |
| Property 6 | Generate random app name. Register, enable, read, assert True. Disable, read, assert False. |
| Property 7 | Generate random builtin definitions with full manifests. Register. Call `list_apps()`. Assert all fields present. |
| Property 8 | Generate lists mixing valid and invalid definitions. Register. Assert valid ones registered, invalid ones skipped. |

### Unit Tests

- Verify the 5 existing builtin apps all have effective `defaultEnabled=True`
- Verify a new builtin app with `defaultEnabled: false` registers as disabled
- Verify the Browse tab card renders "Enable" button for disabled builtins
- Verify the Browse tab card renders "Installed" badge for enabled builtins (they shouldn't appear, but defensive)
- Verify `_validate_builtin_app()` catches missing required fields
- Verify `_validate_builtin_app()` catches non-boolean `defaultEnabled`

### Integration Tests

- Full lifecycle: add a new builtin app with `defaultEnabled: false` → verify it appears in Browse → enable it → verify it appears in Sidebar and Installed → disable it → verify it returns to Browse

## Known Limitations & Future Work

1. **No real `defaultEnabled: false` app ships with this change.** This is pure infrastructure. The first consumer will be determined by the team — likely a new feature module currently in development. CR reviewers should be told which app will use this first.

2. **`mc:apps-changed` event debounce.** Rapid enable/disable toggles will fire multiple sidebar refreshes. The existing `refreshAppNav` in `App.tsx` already uses React state batching which provides natural debounce within a single render cycle. If this becomes a problem with many apps, a 200ms debounce can be added later — not needed for the current 5+N builtin apps.

3. **`installed.json` corruption recovery.** As noted in Error Handling, corruption resets to `defaultEnabled` state. This is the least-bad option but means user preferences can be lost. A future improvement could be a backup/shadow file, but this is out of scope.
