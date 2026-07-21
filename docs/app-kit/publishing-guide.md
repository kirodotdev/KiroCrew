# Publishing Guide — From Development to App Store

Complete workflow: develop → test locally → submit to registry → users install.

## 1. Develop Your App

Create an app directory with an `app.json` manifest:

```bash
mkdir my-app
# Create app.json, agents/, skills/, ui/ as needed
```

See [Getting Started](getting-started.md) for details.

## 2. Test Locally

### Install from local path

```bash
# Build UI if you have one
cd my-app/ui && npm install && npm run build && cd ..

# Install via REST API
curl -X POST http://localhost:5476/api/apps/install \
  -H 'Content-Type: application/json' \
  -d '{"source": "./my-app"}'

# Enable it
curl -X POST http://localhost:5476/api/apps/my-app/enable
```

Or use the App Store UI in the dashboard to install from a local path.

### Verify in dashboard

1. Open KiroCrew dashboard (`kirocrew token` → open URL)
2. Check App Store → Installed tab — your app should appear
3. If it has UI, click the sidebar entry and verify the page loads
4. If it has agents, test them from chat: ask the agent to do something
5. If it has crons, check Schedule page — your cron should be listed

### Debug tips

```bash
# Check app is registered (via REST API)
curl http://localhost:5476/api/apps | python3 -m json.tool

# Check app manifest is valid
curl http://localhost:5476/api/apps/my-app/manifest | python3 -m json.tool

# Check Gateway logs for errors
# Look for "app" or your app name in the log output
```

### Iterate

```bash
# Edit code
vim ui/src/App.tsx

# Rebuild UI
cd ui && npm run build && cd ..

# Update installed app (re-copies files)
curl -X POST http://localhost:5476/api/apps/my-app/update

# Refresh dashboard — changes are live
```

## 3. Prepare for Publishing

### Checklist

Before submitting to the registry:

- [ ] `app.json` passes validation: check via `GET /api/apps/{name}/manifest`
- [ ] `name` is kebab-case, globally unique, descriptive
- [ ] `version` follows semver (`1.0.0`)
- [ ] `displayName` and `description` are clear and concise
- [ ] `author` is set
- [ ] `tags` help with discovery
- [ ] `permissions` are minimal — only declare what you actually use
- [ ] UI bundle is built and committed (`ui/dist/index.mjs`)
- [ ] Agent JSON files are valid
- [ ] Skill SKILL.md files have proper frontmatter
- [ ] README.md explains what the app does and how to use it
- [ ] If you have `setup.onInstall`, test it on a clean machine

### What gets copied at install time

When KiroCrew installs or updates your app, it copies the source tree into
`~/.kirocrew/apps/{name}/` with two safeguards:

- **Symlinks are never followed.** A symlink whose target resolves inside
  your app source tree is preserved as a symlink; a symlink resolving
  outside the source tree is omitted from the installed copy entirely.
  Committed runtime artifacts must be real files (or in-tree relative
  links), never reachable only through an external symlink.
- **Build-input and VCS directories are excluded**: `node_modules`, `.git`,
  `__pycache__`, and `.venv` (at any depth) are dropped from the installed
  copy. Serve your UI from the committed `ui/dist/` bundle — nothing your app
  needs at runtime may live under those names.

### Repository structure

Your app should live in its own Git repo (or a subdirectory of an existing repo):

```
MyAppRepo/
├── app.json
├── agents/
├── skills/
├── ui/
│   ├── src/
│   └── dist/index.mjs    ← committed build artifact
├── README.md
└── setup.onInstall        ← optional install script
```

## 4. Submit to App Registry

The App Registry is a curated list in `src/kiro_crew/apps/app-registry.json`.
Adding your app means opening a pull request against the KiroCrew repo.

### Add registry entry

Edit `app-registry.json` in the KiroCrew repo:

```json
[
  {
    "name": "my-app",
    "gitUrl": "https://github.com/yourname/my-app",
    "branch": "main"
  }
]
```

If your app is in a subdirectory of a larger repo:

```json
{
  "name": "my-app",
  "gitUrl": "https://github.com/yourname/monorepo",
  "branch": "main",
  "subdirectory": "apps/my-app"
}
```

### Registry entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Must match `app.json` name |
| `gitUrl` | yes | Any git-cloneable URL (e.g. `https://github.com/...`, `git@host:...`). The legacy `repo` field is also accepted and used as the clone target when no `gitUrl` is given. |
| `branch` | yes | Branch to clone from (usually `main`) |
| `subdirectory` | no | Path within repo if app isn't at root |
| `resources` | no | `"gateway"` (default) or `"app"` — who registers agents/skills/MCP |
| `lifecycle` | no | `"gateway"` (default), `"app"`, or `"locked"` — who manages lifecycle |
| `detectInstalled` | no | Shell command to check if already installed (for self-managed apps) |

### Submit a pull request

```bash
cd /path/to/KiroCrew
git checkout -b add-my-app
# Edit src/kiro_crew/apps/app-registry.json
git add src/kiro_crew/apps/app-registry.json
git commit -m "feat(apps): add my-app to registry"
git push origin add-my-app
# Open a pull request titled "Add my-app to App Store registry"
```

### What happens during review

Reviewers check:
1. App manifest is valid and complete
2. Permissions are reasonable (no unnecessary access)
3. No path traversal in resource paths
4. Install script (if any) is safe
5. App provides value to KiroCrew users

## 5. User Installation Flow

After your pull request merges, users can install your app:

### From App Store UI

1. Open KiroCrew dashboard → App Store
2. Browse tab → find your app
3. Click Install
4. Wait for clone + install script to complete
5. App appears in Installed tab and sidebar

### From CLI

```bash
curl -X POST http://localhost:5476/api/apps/registry/install \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-app"}'
```

### What happens during install

1. KiroCrew clones your repo (shallow clone, specific branch)
2. Runs `setup.onInstall` script if defined (e.g. `cd ui && npm install && npm run build`)
3. Copies app to `~/.kirocrew/apps/my-app/`
4. Registers agents, skills, crons via symlinks
5. App appears in dashboard

## 6. Updates

### Push an update

1. Update your app code in your repo
2. Bump `version` in `app.json`
3. Commit and push

### Users update

Users can update from the App Store UI (refresh button) or REST API:

```bash
curl -X POST http://localhost:5476/api/apps/my-app/update
```

This re-clones, re-runs install script, and re-registers resources.

## 7. Self-Managed Apps

Some apps manage their own installation and resource registration. They
register with KiroCrew for App Store visibility but handle their own
agent/skill/MCP setup independently.

```json
{
  "name": "my-desktop-app",
  "repo": "MyDesktopApp",
  "branch": "mainline",
  "resources": "app",
  "lifecycle": "app",
  "detectInstalled": "test -d ~/Applications/MyDesktopApp.app"
}
```

Self-managed apps:
- Show in App Store as "Self-managed"
- Handle their own install/update/uninstall
- Register via `POST /api/apps/register` at runtime
- KiroCrew only tracks metadata

## Quick Reference

| Stage | Command / Action |
|-------|-----------------|
| Create | Create app directory with `app.json` |
| Build UI | `cd ui && npm run build` |
| Install locally | `POST /api/apps/install` or App Store UI |
| Enable | `POST /api/apps/{name}/enable` |
| Test | Open dashboard, verify UI + agents + crons |
| Submit | Add to `app-registry.json`, open a pull request |
| User install | App Store → Browse → Install |
| Update | Bump version, push, users re-install |
