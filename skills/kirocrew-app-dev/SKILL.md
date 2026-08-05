---
name: kirocrew-app-dev
description: "Build, package, and publish KiroCrew external apps. Covers app.json manifest, UI components, crons, skills, self-healing install, git publishing, and common pitfalls."
---

# KiroCrew App Development

Guide for building external KiroCrew apps that work reliably across KiroCrew versions without depending on buggy lifecycle hooks or skill scanner behavior.

## When to Use

- User says "create a kirocrew app", "build an app", "make an app for kirocrew"
- User says "package this as an app", "publish this to the app store"
- User is building UI pages, crons, or skills that should be distributable

## App Structure

```
my-app/
├── app.json              # Manifest (required)
├── ui/
│   ├── index.mjs         # ESM React component (default export)
│   └── icon.svg          # Sidebar icon (SVG or PNG)
├── skills/
│   └── my-skill/
│       └── SKILL.md      # Skill spec
├── scripts/
│   ├── install.sh        # Optional accelerator (NOT required)
│   └── uninstall.sh      # Cleanup script
└── README.md             # Optional docs
```

## app.json Manifest

```json
{
  "name": "my-app",
  "displayName": "My App",
  "version": "1.0.0",
  "description": "What it does in one sentence.",
  "author": "login",
  "tags": ["relevant", "tags"],
  "skills": [
    "skills/my-skill"
  ],
  "crons": [
    {
      "name": "my-cron",
      "message": "FIRST (self-heal, run these bash commands silently): ... THEN do the actual work.",
      "every": 900,
      "silent": true,
      "persistent_session": false
    }
  ],
  "permissions": {
    "mcpTools": ["browser_navigate", "local_knowledge_search"],
    "network": true
  },
  "ui": {
    "entry": "index.mjs",
    "pages": [
      {
        "route": "/my-app",
        "label": "My App",
        "iconUrl": "icon.svg"
      }
    ]
  },
  "setup": {
    "onInstall": "scripts/install.sh",
    "onUninstall": "scripts/uninstall.sh"
  }
}
```

### Critical Rules

| Field | Rule | Why |
|-------|------|-----|
| `skills` | Use string paths `["skills/my-skill"]` | Object format `[{name, path}]` breaks — parser stringifies dicts |
| `permissions` | Must be an **object** with keys `api` / `events` / `mcpTools` / `storage` / `network` / `memory` / `cron` — not a flat list | `AppManifest.from_dict` only calls `Permissions.from_dict` when the value is a dict; a list silently parses to an empty `Permissions()`, granting nothing |
| `resources` | Must be array of strings `["tool1", "tool2"]` | Object format or nested arrays break resource resolution |
| `ui.entry` | Must be `.mjs` ESM module | `.html` not in allowed extensions |
| `ui.pages[].iconUrl` | Use `icon.svg` file path | String `icon` field only works for builtin apps |
| `displayName` | Required | Gateway uses it for UI display |
| `version` | Semver string | Used by update-check crons for comparison |

## Self-Healing Pattern (CRITICAL)

**Never depend on:**
- `onInstall` hook firing (unreliable across KiroCrew versions)
- `_register_skills` parsing correctly (flat namespace bug)
- Manual user intervention post-install

**Instead, make the cron self-heal on first run:**

```
"message": "FIRST (self-heal, run these bash commands silently):
  (1) Skill symlink: if ~/.kiro/crew/skills/MY-SKILL/SKILL.md does not exist,
      run: ln -sfn ~/.kiro/crew/apps/MY-APP/skills/MY-SKILL ~/.kiro/crew/skills/MY-SKILL
  (2) State dir: mkdir -p ~/.kiro/crew/workspace/MY-APP && [ -f ~/.kiro/crew/workspace/MY-APP/state.json ] || echo '{}' > ~/.kiro/crew/workspace/MY-APP/state.json
  (3) Config for UI: mkdir -p ~/.kiro/crew/apps/MY-APP/data && [ -f ~/.kiro/crew/apps/MY-APP/data/config.json ] || echo '{...resolved paths...}' > ~/.kiro/crew/apps/MY-APP/data/config.json
  THEN do the actual work..."
```

**Keep install.sh as an optional accelerator** — users who want instant setup can run it manually, but the app works without it within one cron cycle.

## UI Development

### Design System (MANDATORY)

All KiroCrew apps MUST use the same visual language for consistency:

| Element | Style |
|---------|-------|
| **Styling method** | Inline `style={}` objects. NO Tailwind classes. |
| **Primary accent** | `#7c3aed` (purple) |
| **Light accent bg** | `#e8d5f5` (light purple) |
| **Success color** | `#047857` (green) |
| **Warning color** | `#b45309` (amber) |
| **Danger color** | `#b91c1c` (red) |
| **Buttons** | `borderRadius: '9999px'` (full pill), font 11px weight 500 |
| **Badges** | `borderRadius: '9999px'`, 10px bold, colored bg+text |
| **Cards** | `background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: '6px', padding: '14px'` |
| **Max width** | `maxWidth: '1200px'` |
| **Font sizes** | 10px (version/badges), 11px (body/buttons), 12px (table), 13px (section headers), 18px (title) |
| **Theme vars** | `var(--bg)`, `var(--text)`, `var(--muted)`, `var(--border)`, `var(--card)` |

**Example badge:**
```javascript
_jsx('span', {
  style: { background: '#e8d5f5', color: '#7c3aed', padding: '2px 7px', borderRadius: '9999px', fontSize: '10px', fontWeight: 600, letterSpacing: '0.02em' },
  children: 'LABEL'
})
```

**Example primary button:**
```javascript
_jsx('button', {
  style: { background: '#7c3aed', color: '#fff', border: 'none', padding: '5px 14px', borderRadius: '9999px', fontSize: '11px', fontWeight: 500, cursor: 'pointer' },
  children: 'Action'
})
```

**Example secondary/ghost button:**
```javascript
_jsx('button', {
  style: { background: 'transparent', color: '#7c3aed', border: '1px solid #e8d5f5', padding: '5px 14px', borderRadius: '9999px', fontSize: '11px', fontWeight: 500, cursor: 'pointer', whiteSpace: 'nowrap' },
  children: '↻ Refresh'
})
```

### Reference Implementation

For a working example, look at the bundled builtin apps under
`src/kiro_crew/apps/builtins/` — they follow the same manifest, UI, and cron
patterns described here and are a good starting point to copy from.

Visual mock of the target style:

```html
<!--
  This is what a KiroCrew app header + card should look like.
  Copy this pattern exactly — colors, spacing, border-radius, font sizes.
-->
<div style="max-width:1200px; margin:0 auto; padding:16px; font-family:system-ui; color:#e2e8f0; background:#1a1b26">

  <!-- Header -->
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px">
    <div style="display:flex; align-items:center; gap:10px">
      <span style="font-size:18px; font-weight:600">Review Tender</span>
      <span style="background:#e8d5f5; color:#7c3aed; padding:2px 8px; border-radius:9999px; font-size:10px; font-weight:600">Every 15 min</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px">
      <span style="font-size:11px; color:#6b7280">Last scan: 3m ago</span>
      <button style="background:transparent; color:#7c3aed; border:1px solid #e8d5f5; padding:5px 14px; border-radius:9999px; font-size:11px; font-weight:500">↻ Refresh</button>
      <span style="font-size:10px; color:#6b7280">v1.7.0</span>
    </div>
  </div>

  <!-- Card -->
  <div style="background:#1a1b26; border:1px solid #2d2f3d; border-radius:6px; padding:14px; margin-bottom:12px">
    <div style="font-size:13px; font-weight:600; color:#7c3aed; margin-bottom:8px">Open Reviews Being Tended (2)</div>

    <!-- Table row example -->
    <div style="display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid #2d2f3d; font-size:12px">
      <a style="color:#7c3aed; text-decoration:none">#1234</a>
      <span style="font-size:11px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">Fix manifest dict format</span>
      <span style="background:#fef3c7; color:#b45309; padding:2px 7px; border-radius:9999px; font-size:10px; font-weight:600">iterating</span>
      <span style="margin-left:auto">
        <button style="background:transparent; color:#7c3aed; border:1px solid #e8d5f5; padding:5px 14px; border-radius:9999px; font-size:11px; font-weight:500">💬 Chat</button>
      </span>
    </div>
  </div>

  <!-- Update banner -->
  <div style="background:#e8d5f5; color:#7c3aed; padding:8px 14px; border-radius:9999px; font-size:12px; display:flex; justify-content:space-between; align-items:center">
    <span>Update available: v1.6.0 → v1.7.0</span>
    <button style="background:#7c3aed; color:#fff; border:none; padding:5px 14px; border-radius:9999px; font-size:11px; font-weight:500">Update Now</button>
  </div>
</div>
```

Key visual rules from the mock:
- Dark theme uses `var(--bg)` / `var(--border)` / `var(--text)` — don't hardcode dark hex
- Light purple badge for metadata (`#e8d5f5` bg, `#7c3aed` text)
- Amber status pills (`#fef3c7` bg, `#b45309` text)
- Ghost buttons with purple text on light purple border
- Version as plain muted text (smallest element, 10px)
- Cards have 14px padding, 6px radius, 12px bottom margin

### Entry Module Pattern

```javascript
import { useState, useEffect } from 'react'
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from 'react/jsx-runtime'
import { useNavigate } from '@kirocrew/app-sdk'

let STATE_PATH = ''
let APP_VERSION = ''
let CONFIG_MISSING = false
const _configReady = fetch('/api/apps/MY-APP/config').then(r => r.ok ? r.json() : null).then(cfg => {
  if (cfg && cfg.statePath) { STATE_PATH = cfg.statePath }
  else { CONFIG_MISSING = true }
}).then(() => {
  const appJsonPath = STATE_PATH.replace('/workspace/MY-APP/state.json', '/apps/MY-APP/app.json')
  return fetch('/api/file-read?path=' + encodeURIComponent(appJsonPath))
    .then(r => r.ok ? r.text() : null)
    .then(t => { if (t) { try { APP_VERSION = JSON.parse(t).version || '' } catch {} } })
}).catch(() => { CONFIG_MISSING = true })

export default function MyApp() {
  const [state, setState] = useState({})
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    async function load() {
      await _configReady
      if (!STATE_PATH) { /* show initializing message */ return }
      const resp = await fetch('/api/file-read?path=' + encodeURIComponent(STATE_PATH))
      if (resp.ok) setState(JSON.parse(await resp.text()))
      setLoading(false)
    }
    load()
    const interval = setInterval(load, 30000) // 30s polling
    return () => clearInterval(interval)
  }, [])

  // ... render UI
}
```

### Version Source (IMPORTANT)

**Read version from `app.json` via `/api/file-read`**, not from `data/config.json` (stale install-time value) and not from `/api/apps/MY-APP` (requires session auth the iframe doesn't have).

```javascript
// After _configReady resolves and you know the app path:
const appJsonPath = '/Users/.../.kiro/crew/apps/my-app/app.json'
fetch('/api/file-read?path=' + encodeURIComponent(appJsonPath))
  .then(r => r.ok ? r.text() : null)
  .then(t => { if (t) APP_VERSION = JSON.parse(t).version })
```

Derive the path from `STATE_PATH` (replace `workspace/my-app/state.json` with `apps/my-app/app.json`) or hardcode it based on the app name.

### Header Layout Pattern

All apps use a consistent header: Icon + Title + badge on the left, last-scan + refresh + version on the right.

```javascript
_jsxs('div', {
  style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' },
  children: [
    _jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' }, children: [
      // Inline SVG icon (same as ui/icon.svg, rendered at 20px with purple stroke)
      _jsx('svg', { xmlns: 'http://www.w3.org/2000/svg', width: 20, height: 20, viewBox: '0 0 24 24',
        fill: 'none', stroke: '#7c3aed', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
        children: [/* your icon paths */] }),
      _jsx('h2', { style: { margin: 0, fontSize: '18px' }, children: 'My App' }),
      _jsx('span', { style: { background: '#e8d5f5', color: '#7c3aed', padding: '2px 8px', borderRadius: '9999px', fontSize: '10px', fontWeight: 600 }, children: 'Every 15 min' })
    ]}),
    _jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' }, children: [
      _jsx('span', { style: { fontSize: '11px', color: 'var(--muted)' }, children: lastScan }),
      _jsx('button', { /* refresh - see below */ }),
      _jsx('button', { /* version pill - see below */ })
    ]})
  ]
})
```

**Inline icon rules:**
- Render the same SVG from `ui/icon.svg` directly in JSX (20x20px, `stroke: '#7c3aed'`)
- Use `_jsx('svg', {...})` with child `_jsx('path', { d: '...' })` elements
- Don't fetch the icon file at runtime — inline it for instant render

### Background Actions (MANDATORY)

**All buttons that trigger agent work MUST run in the background** via `POST /api/chat?ws=1`. NEVER navigate to `/chat` for automated actions — the user should stay on the app page.

```javascript
// Refresh button (background)
_jsx('button', {
  disabled: refreshing,
  onClick: async () => {
    setRefreshing(true)
    await fetch('/api/chat?ws=1', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'Run the workflow...', slot: 'my-app-refresh' })
    }).catch(() => {})
    setTimeout(() => setRefreshing(false), 5000)
  },
  style: { background: 'transparent', color: refreshing ? 'var(--muted)' : '#7c3aed', border: '1px solid #e8d5f5', padding: '4px 10px', borderRadius: '9999px', fontSize: '11px', fontWeight: 500, cursor: refreshing ? 'default' : 'pointer' },
  children: refreshing ? '↻ Running…' : '↻ Refresh'
})

// Version pill (background update check)
_jsx('button', {
  disabled: checking,
  onClick: () => { /* POST to /api/chat?ws=1 with update-check slot */ },
  title: 'Check for updates',
  style: { background: 'none', color: 'var(--muted)', border: 'none', padding: '2px 6px', fontSize: '10px', cursor: checking ? 'default' : 'pointer' },
  children: checking ? 'checking...' : `v${appVersion || '?'}`
})
```

**Only use `navigate('/chat')` for actions that genuinely need human involvement** (responding to reviewer comments, complex setup wizards).

### UI Rules

| Do | Don't |
|----|-------|
| Use inline `style={}` objects everywhere | Use Tailwind classes |
| Use 30s polling via `setInterval` + `/api/file-read` | Use `/api/file-watch` SSE (overwrites React state on connect) |
| Use `useNavigate()` from `@kirocrew/app-sdk` for navigation | Use `window.location` (causes full reload) |
| Use theme vars for backgrounds/text/borders | Hardcode hex for theme-dependent colors |
| Use `#7c3aed` / `#e8d5f5` for accent elements | Use blue (`var(--accent)`) or other accent colors |
| Read version from `app.json` via `/api/file-read` | Read version from `data/config.json` (stale) or `/api/apps/MY-APP` (needs session auth) |
| Run background work via `POST /api/chat?ws=1` | Navigate to `/chat` for automated actions |
| Show "initializing" state when config missing | Show cryptic error messages |
| Read state from file via `/api/file-read?path=...` | Use non-existent endpoints like `/api/files/read` |

### Available Gateway APIs

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/apps/MY-APP` | GET | Full manifest JSON — requires session auth (NOT usable from app iframe) |
| `/api/apps/MY-APP/config` | GET | JSON from `data/config.json` (no auth needed, but version is stale) |
| `/api/file-read?path=...` | GET | text/plain file content |
| `/api/chat?ws=1` | POST | Launches agent work in background slot |
| `/api/file-watch` | GET (SSE) | Live file change stream (avoid — see above) |

**No directory listing API exists.** Use a `_index.json` manifest file if you need to list files.

### Available Imports (via shared import map)

- `react` (useState, useEffect, useRef, etc.)
- `react/jsx-runtime` (_jsx, _jsxs, Fragment)
- `react-dom`
- `@kirocrew/app-sdk` (useNavigate)

### Interactive Elements (Chat Launch)

Only for actions requiring human interaction:
```javascript
window.__mc_chat_launch = { message: "Your prompt here", ts: Date.now() }
navigate('/chat')
```

### Interactive Widgets (data-action)

For button callbacks within mcwidgets rendered by the agent (not app UI):
```html
<button data-action="approve" data-payload='{"id":"123"}'>Approve</button>
```
User receives: `[UI] approve: {"id":"123"}`

## Cron Design

### Notification Deduplication

Never ping the user for the same unchanged condition. Track what was communicated:

```json
"last_notified": {
  "event": "new_comment",
  "at": "2026-05-19T17:00:00Z",
  "details": "soopra: Can you explain..."
}
```

Before sending any DM:
1. Check `last_notified.event` and `last_notified.details`
2. If same → SKIP
3. If different → SEND and update `last_notified`

### Silent by Default

Set `"silent": true` in cron config. Only use `send_message` with `session="slack"` when there is a genuine new development the user needs to act on.

### Cron Message Structure

```
FIRST (self-heal): [bootstrap commands]
THEN: [actual workflow description]
```

Keep the self-heal idempotent — every command should be a no-op if already done (use `[ -f ... ] ||` guards).

## Publishing to App Store

### 1. Git Repository

Publish the app as a plain git repository (any git host — e.g. GitHub):
- Include: `app.json`, `ui/`, `skills/`, `scripts/`
- Any git-cloneable URL works (`https://github.com/<org>/<repo>`, `git@host:...`, `ssh://...`)

### 2. App Registry Entry

Open a pull request to the KiroCrew repo adding an entry to `app-registry.json`:
```json
{
  "name": "my-app",
  "gitUrl": "https://github.com/<org>/my-app",
  "branch": "main",
  "resources": [],
  "lifecycle": "stable"
}
```

Display metadata (description, tags, author) comes from `app.json` in the app's own repo (cached 24h).

### 3. Updates

Updates are automatic via semver diff — bump `version` in `app.json`, push to the repo. Users with update-check crons see a banner in the UI.

### Update-Check Cron Pattern

```json
{
  "name": "my-app-update-check",
  "message": "Fetch remote app.json via git archive, compare version to installed. Write result to ~/.kiro/crew/workspace/my-app/update-status.json. Always silent.",
  "every": 86400,
  "silent": true,
  "persistent_session": false
}
```

## Self-Update & Refresh Pattern

Apps should include a version check cron and UI elements for manual refresh and update. ALL actions run in the background — no navigation away from the app.

### Update-Check Cron

Add to `app.json` crons array:

```json
{
  "name": "my-app-update-check",
  "message": "Check if a newer version of MY-APP is available. READ-ONLY. Steps: (1) Remote version: run `git archive --remote=https://github.com/<org>/my-app main app.json | tar -xO` from $HOME. Parse 'version'. (2) Installed version: read ~/.kiro/crew/apps/my-app/app.json. (3) Compare semver. Write ONLY to ~/.kiro/crew/workspace/my-app/update-status.json: {checked:true, installedVersion, remoteVersion, updateAvailable:bool, checkedAt:ISO}. Silent.",
  "every": 86400,
  "silent": true,
  "persistent_session": false
}
```

### UI: Update Banner (Background)

```javascript
function UpdateBanner() {
  const [update, setUpdate] = useState(null)
  const [updating, setUpdating] = useState(false)

  // Load update-status.json on mount
  useEffect(() => { /* read update-status.json via /api/file-read */ }, [])

  if (!update?.updateAvailable) return null

  return _jsx('div', {
    style: { background: '#e8d5f5', color: '#7c3aed', padding: '8px 14px', borderRadius: '9999px', marginBottom: '8px', fontSize: '12px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
    children: _jsxs(_Fragment, { children: [
      _jsx('span', { children: `Update available: v${update.installedVersion} → v${update.remoteVersion}` }),
      _jsx('button', {
        onClick: async () => {
          setUpdating(true)
          await fetch('/api/chat?ws=1', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: 'Update MY-APP...', slot: 'my-app-update' }) }).catch(() => {})
          setTimeout(() => setUpdating(false), 10000)
        },
        disabled: updating,
        style: { background: updating ? '#e5e7eb' : '#7c3aed', color: updating ? '#6b7280' : '#fff', border: 'none', padding: '4px 10px', borderRadius: '9999px', fontSize: '11px', fontWeight: 500, cursor: updating ? 'default' : 'pointer' },
        children: updating ? 'Updating…' : 'Update Now'
      })
    ]})
  })
}
```

### Background Slot Pattern

For any action that should run without navigating away from the app page, use `POST /api/chat?ws=1` with a named slot:

```javascript
fetch('/api/chat?ws=1', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ message: 'Do the thing...', slot: 'my-app-action-name' })
})
```

- The slot is created if it doesn't exist, reused if it does
- Response is JSON `{ok: true}` — the work runs async in the background
- Results surface via state file changes picked up by the 30s polling cycle
- Use a disabled/spinner state to show feedback while in flight (5-10s timeout)

## Installation Flow (User Perspective)

1. `kirocrew app install <path-or-git-url>`
2. Gateway restart: `kirocrew gateway restart`
3. App appears in sidebar immediately (if UI defined)
4. First cron run (within `every` seconds) self-heals all setup
5. UI transitions from "initializing" to functional

## .gitignore (IMPORTANT)

Apps generate local install artifacts that must NOT be committed to the repo:

```
/build
/release-info
.app_secret
app-crons.json
installed.json
data/
```

The `data/` directory contains runtime config (`config.json`) with resolved paths and stale version info. Committing it causes version display bugs (showing install-time version) and path issues on other machines.

## Git Workflow for Installed Apps

The installed app at `~/.kiro/crew/apps/MY-APP/` IS a git repo with origin pointing to the source repository. You can commit and push directly from there:

```bash
cd ~/.kiro/crew/apps/my-app && git add -A && git commit -m "message" && git push origin main
```

No separate workspace needed. The installed app IS the workspace.

## Common Pitfalls

| Pitfall | Cause | Fix |
|---------|-------|-----|
| "no visual interface" in sidebar | Missing `ui.entry` or wrong extension | Use `.mjs` ESM with default export |
| Skills don't load after install | Scanner only checks flat `~/.kiro/crew/skills/<name>/SKILL.md` | Self-heal symlink in cron |
| UI shows stale data after app.json change | Browser caches manifest API response | Hard refresh |
| `onInstall` doesn't run | KiroCrew lifecycle hook bug | Don't depend on it — self-heal instead |
| SSE overwrites React state | `/api/file-watch` fires immediately on connect | Use polling instead |
| Cron spams DMs | No dedup — same condition triggers every cycle | Track `last_notified` in state |
| App icon doesn't show | Using `icon` string field | Use `iconUrl` pointing to SVG file |
| Stale app after gateway restart | Gateway reads app.json live but session predates | Open new dashboard session |
| Version shows "?" or old number | `/api/apps/MY-APP` needs auth, `config.json` is stale | Read `app.json` via `/api/file-read` |
| Install artifacts in git | `data/`, `.app_secret`, etc tracked | Add to `.gitignore`, `git rm --cached` |
| Buttons navigate away from app | Using `navigate('/chat')` for automated work | Use `POST /api/chat?ws=1` background slots |
| Refresh/update leaves app page | Using `window.__mc_chat_launch` + navigate | Background slot + disabled state + timeout |

## Testing Locally

1. Place app in any directory
2. Install: `kirocrew app install /path/to/my-app`
3. Restart gateway: `kirocrew gateway restart`
4. Open fresh dashboard session
5. Verify: UI loads, cron runs self-heal, skill appears in `/skills` list

## Versioning Convention

- Patch (1.0.x): Bug fixes, wording changes
- Minor (1.x.0): New features, new crons, UI additions
- Major (x.0.0): Breaking changes to state format, removed features
## In-Process Backend for External Apps (CRITICAL — differs from builtins)

External (installed) apps CAN ship a Python backend that runs inside the gateway
process — but the contract DIFFERS from builtins (`auto_research` etc.):

- Manifest: use ONLY `backend.hooks` (`"routes": "backend.routes:register_routes"`).
  Do NOT set the `backend.routes` base-path string — that field triggers the
  STANDALONE-PROCESS proxy, which serves dead stubs that shadow your handlers.
- `register_routes(ctx)` receives an AppContext and MUST return `list[AppRoute]`
  (`from kiro_crew.apps.route_registry import AppRoute`) with paths RELATIVE to
  `/api/apps/<name>`; `{params}` land in `request.match_info`.
- Handlers take `(request, ctx)`; gateway state is `request.app["state"]`;
  auth-check `request.get("user") is not None` → else 401.
- The builtin pattern (direct `app.router.add_get`) silently never dispatches for
  external apps — the RouteRegistry catch-all (`/api/apps/{app_name}/{path:.*}`)
  shadows it.
- Backend hook changes need a gateway restart OR an app disable→enable cycle
  (runtime deregister + module unload + fresh load). UI files reload without.
- Trust: backend code runs UNSANDBOXED with full gateway privileges (SEC-012
  warning logged; `agent.apps_allow_third_party=false` refuses it entirely).

## Dev Loop for App UIs

- **Preferred: dev mode** — `kirocrew app dev <name>` (off: `--off`). Serves that
  app's UI with `Cache-Control: no-store` and watches its `ui/` dir; changes
  broadcast `app_reload` and the dashboard hot-swaps the app in ~1s. The flag
  lives in `installed.json` and toggles live.
- To edit in your source tree, symlink the installed UI dir to source:
  `mv ~/.kiro/crew/apps/<n>/ui ~/.kiro/crew/apps/<n>/ui.bak && ln -s <src>/ui ~/.kiro/crew/apps/<n>/ui`
  (serving containment check and the dev watcher both follow symlinks).
  On native Windows use a directory junction instead (PowerShell):
  `Rename-Item "$env:USERPROFILE\.kiro\crew\apps\<n>\ui" ui.bak; New-Item -ItemType Junction -Path "$env:USERPROFILE\.kiro\crew\apps\<n>\ui" -Target "<src>\ui"`
  (`pathlib` resolves junctions the same way, so serving and the watcher work;
  the lifecycle clobber below applies identically — junctions are also never
  preserved by the install/update safe-copy).
- **⚠️ Symlinks do NOT survive the app lifecycle.** `install_app`/`update_app`
  (reinstall, App Store Update, registry refresh) re-copy source over the
  installed dir with a DELIBERATE symlink-stripping safe-copy (security: blocks
  `ui -> ~/.docker` style serving). Your symlink is silently replaced by a
  frozen snapshot: hot reload stops, UI goes stale, no error. Symptom:
  `ls -l ~/.kiro/crew/apps/<n>/ui` shows a real dir, not a link. Fix: re-create
  the symlink after ANY install/update, and re-check dev mode is still on.
- Same clobber applies to locally-edited SHIPPED skills: installed skill files
  under `~/.kiro/crew/skills/` re-sync from the KiroCrew package on update —
  durable skill changes must land in the repo (`skills/` in the KiroCrew source).
- Validate `.mjs` before relying on a reload: `node --check ui/index.mjs` —
  a parse error surfaces only as "Failed to load <App>: Unexpected token".
- Avoid deep `_jsx` nesting in one expression; prefer small named components.
- Dark mode: never pair a solid light accent bg with hardcoded dark text for
  selected states — use a translucent accent tint (e.g. `rgba(124,58,237,.14)`)
  with `var(--text)`/`var(--muted)`. Self-contained pills (own bg+fg) are fine.

## Don't Reinvent the Dashboard (default posture)

By DEFAULT, apps should look and behave like the dashboard they live in:

- **Theme tokens over hardcoded colors**: `var(--accent)`, `var(--accent-fg)`,
  `var(--accent-subtle)`, `var(--danger)`/`var(--danger-subtle)`, `var(--ok)`,
  `var(--bg)`/`var(--card)`/`var(--border)`/`var(--text)`/`var(--muted)`.
  Hardcoded hex breaks the moment a user picks a custom palette (and error
  banners hardcoded for light mode glow in dark mode). Give tokens fallbacks
  (`var(--accent, #7c3aed)`) so old hosts still render.
- **Host components over hand-rolled ones**: the `@kirocrew/ui` module-map
  export ships `Btn, Input, SearchInput, Badge, Toggle, EmptyState, Skeleton,
  ContentSkeleton, PageHeader, SegmentedControl, MarkdownRenderer` and more;
  `lucide-react` ships a subset of real icons. Feature-detect
  (`window.__kirocrew_modules?.['@kirocrew/ui']`) and keep a small fallback for
  old hosts — a thin wrapper per component (host when available, fallback
  otherwise) keeps call sites clean.

This is the default, **not a straitjacket**: if your app has a deliberate,
preferred custom style or a novel interaction with no host equivalent (bespoke
visualizations, a branded look, domain-specific widgets), a custom design is a
legitimate choice — make it consciously and consistently, not as an accident of
copy-pasted inline styles. Custom visuals should still respect the theme's
background/text tokens so they don't break light/dark/custom palettes.

## Embedded Chat (ChatEmbed) — native chat inside your app

The host SDK ships the dashboard's real chat renderer. Use it instead of
hand-rolling a transcript view — markdown, tool activity, streaming, and turn
grouping come for free and stay consistent with the main chat.

- Access: `const sdk = window.__kirocrew_modules?.['@kirocrew/app-sdk']`, then
  render `sdk.ChatEmbed` with `{ slotKey, agent?, placeholder? }`. Feature-detect
  and keep a lightweight fallback — the module map can lag one gateway version.
- `slotKey` binds the embed to a chat slot (`<app-name>-<entity>` is the
  convention). The embed polls `/api/chat/slots/<key>` (1s while running, 5s
  idle) and POSTs to `/api/chat`.
- **Manifest permissions (silent-failure trap):** the SDK gates fetches by the
  app's `permissions.api` allowlist. ChatEmbed needs `"/api/chat"` +
  `"/api/chat/*"`; if your users click Approve/Trust on tool cards you ALSO need
  `"/api/approvals"` + `"/api/approvals/*"` — without it the button 403s with no
  visible error.
- Chrome quirks (as of current main): the embed draws its own bordered card,
  a title strip, and an input row with a top border, and its initial auto-scroll
  fires before markdown/tool cards finish layout (long transcripts open
  mid-scroll). **Stopgap — tracked in issue #510** (ChatEmbed frameless/scroll
  props): until that lands, override from the app with scoped `!important` CSS
  on the embed's Tailwind classes for the chrome, and a small keeper that pins
  the embed's scroller to the bottom (release when the user scrolls up, re-pin
  near bottom). These overrides couple you to host DOM internals the repo has
  never promised — expect them to break on upstream changes, keep them minimal,
  and delete them when #510 ships.

## Worker Slots — apps that own agent sessions

**Stopgap — tracked in issue #509** (a supported `acquire_worker_slot(app,
project, trust=…)` helper): these are underscore-private slot internals, not a
promised API. Until #509 lands they are the only mechanism, but treat this
recipe as scaffolding — re-check it against the SDK when you update an app.

If your app creates chat slots for background/worker agents (spec writers,
researchers), stamp these attributes — and re-stamp on EVERY acquisition, not
just creation, because gateway restarts and other code paths (e.g. ChatEmbed's
own POST) can recreate slots without them:

- `slot._app = "<app-name>"` — keeps the session out of the main chat sidebar.
- **Trust — grant it BOUNDED, never blanket-forever.** Approval prompts render
  ONLY in the main chat UI, so an untrusted worker inside an app embed stalls
  silently on its first shell command — but the fix is a *scoped* grant, not a
  permanent one:
  - *Preferred:* pattern-scoped trust via `slot._trusted_patterns` (supported
    by `chat_runner`) — allowlist only the tool/command shapes your worker
    actually needs.
  - *If you must use blanket `slot._trust = True`:* time-box it. Mirror the
    in-repo precedent (`auto_research`: 24h TTL, then trust expires and
    re-authorization re-grants it) rather than re-stamping `True`
    unconditionally forever. A permanent unscoped auto-approve worker silently
    exempts a growing class of sessions from the interactive-approval layer —
    a security regression that compounds as apps adopt the pattern.
  - Always SEL-audit the grant, whichever form it takes.
- `slot.project = <working_dir>` — sets the CLI process cwd (chat_runner runs
  `cwd=slot.project`). Without it the agent prefixes every command with
  `cd <long-path> && …`, which turns every tool pill in the transcript into
  identical truncated noise; with it, commands are relative and readable, and
  the worker inherits project-scoped steering files.

## Positioning — your app is NOT in an iframe

App UIs mount directly into the dashboard DOM. `position: fixed` therefore
escapes your panel and covers the ENTIRE dashboard (sidebar, header). For
overlays/modals scoped to your app: set `position: relative` on your app root
and use `position: absolute; inset: 0` for the overlay.

## Backend Change Ergonomics

- UI hot-swaps in ~1s (dev mode); backend hooks load only on gateway restart or
  an app disable→enable cycle. Batch backend edits and plan one reload.
- Validation loop: `python3 -m py_compile backend/routes.py` then copy to the
  installed dir — it takes effect on the NEXT reload, silently. Track what's
  pending.
- Verify the served UI module actually updated before debugging "my change
  doesn't work": `curl -s <gateway>/apps/<name>/ui/index.mjs | md5sum` vs
  `md5sum <src>/ui/index.mjs`. A mismatch means clobbered symlink or dev mode
  off.
- Probe a backend route without auth plumbing: an auth-gated route returning
  401/403 proves it is REGISTERED; 404 means the module didn't load.

## Graduating an External App to a Builtin

When an app proves out and should ship with KiroCrew, port it into the repo —
the contracts CHANGE on both sides. Template: `src/kiro_crew/apps/builtins/issue_radar/`.

- Layout: `src/kiro_crew/apps/builtins/<snake_name>/` with `app.json`,
  `backend/routes.py`, optional `skills/<skill>/SKILL.md`, `tests/`.
- **Backend contract flips**: builtins use `register_routes(app: web.Application) -> None`
  registering FULL paths (`/api/apps/<name>/…`) directly on the router — the
  external AppRoute-list/RouteRegistry contract does not apply. Wrap every
  handler in an enabled-check gate (see issue_radar's `_require_enabled`):
  builtin routes exist at startup even while the app is disabled.
- **Wiring**: in-process builtin backends must be listed in
  `BUILTIN_NAMES` (`apps/builtins/__init__.py`) — that startup loop is what
  calls `register_routes`. (Subprocess-backend builtins like dev_fleet use
  `backend.entryPoint` + port instead and are NOT listed.) App Store discovery
  is separate and automatic via `discover_builtin_apps()` scanning `app.json`.
- **UI becomes a real React page**: `website/src/apps/<name>/…Page.tsx`
  registered in `website/src/apps/builtinRegistry.ts` (lazy import). You now
  import MarkdownRenderer, lucide-react, the ui kit, and app-sdk components
  directly — delete the module-map feature detection and CSS override hacks.
  Note ChatEmbed requires an `AppApiProvider` ancestor; builtin pages mount
  their own.
- Icon/assets: `website/public/app-assets/<name>/`; manifest `iconUrl`
  `/app-assets/<name>/icon.svg`. Skills ride `manifest.skills` (paths relative
  to the app root), registered at enable-time.
- Keep `defaultEnabled: false`; users opt in via the App Store.
- Full worktree + build-gate discipline applies (see the kirocrew-worktree-dev
  skill) — this is now KiroCrew source.
