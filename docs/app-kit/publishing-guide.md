# Publishing Guide: from development to the App Store

The full path an app takes: develop, test locally, list it in a registry, users
install it. An "app" is a package that contributes agents, skills, SOPs, MCP
servers, cron jobs, backend routes, or dashboard UI pages to Kiro Crew.

This guide covers the publish-facing surface (store listing, assets, registry
entry, review). The complete field reference is
[manifest-reference.md](manifest-reference.md), and the first-app walkthrough is
[getting-started.md](getting-started.md).

## 1. Develop your app

Scaffold a skeleton, then edit it:

```bash
kirocrew app init my-app --ui --backend --cron
```

`app.json` at the app root is the single source of truth for identity,
resources, and the store listing. The registry entry (section 8) carries almost
nothing, so bumping a version or rewriting a description means editing only your
own repo.

## 2. The store-listing fields

```json
{
  "name": "my-app",
  "version": "1.0.0",
  "displayName": "My App",
  "description": "One paragraph describing what the app does.",
  "author": "your-name",
  "license": "MIT",
  "tags": ["productivity", "automation"],
  "highlights": [
    "What the app does that a list entry cannot convey",
    "One line per capability"
  ],
  "iconPath": "assets/icon.png",
  "screenshots": ["assets/screenshots/main.png"],
  "heroImage": "assets/hero-light.png",
  "heroImageDark": "assets/hero-dark.png",
  "heroImageDetail": "assets/hero-detail-light.png",
  "heroImageDetailDark": "assets/hero-detail-dark.png"
}
```

| Field | Rules |
|-------|-------|
| `name` | Required. Kebab-case, matched against `^[a-z0-9]+(?:-[a-z0-9]+)*$`, unique across all apps. This is the install id and the on-disk directory name. `system` is reserved (it would shadow the `system.*` notification-channel namespace), as are the Windows device stems `con`, `prn`, `aux`, `nul`, `com1`–`com9` and `lpt1`–`lpt9` (the name becomes a directory, and Windows resolves those inside every directory). Names that merely resemble one — `console`, `com10`, `null-app` — are fine. All are refused on every platform, so an app that installs on Linux also installs on Windows. |
| `version` | Required. Semver (`major.minor.patch`, optionally with a pre-release or build suffix). Bump on every release. |
| `displayName` | Required. Rendered in a fixed-width row that truncates, so keep it short. |
| `description` | Required. Plain text, no markdown. Discover's list row shows one truncated line; the feature cards clamp to two; the detail page shows it in full. Two or three sentences is the useful range. |
| `author` | Recommended. Shown as provenance next to the app's category and source registry. |
| `license` | Optional SPDX-style identifier, shown on the detail page. |
| `tags` | Lowercase discovery tags. They also decide the app's Discover category (section 3). |
| `highlights` | Feature bullets rendered as a list on the detail page. |

### Store metadata is fetched from your repo, not the registry

The App Store fetches each listed app's `app.json` with `git archive` and caches
it for 24 hours (an external registry's index is cached for 1 hour). Image paths
inside the manifest are rewritten to blob-proxy URLs, so nothing has to be
mirrored into the Kiro Crew repo. Push a new version and the store picks it up on
the next refresh.

## 3. Categories come from your tags

Discover groups apps into a fixed set of categories by scanning `tags` in
priority order, so a specific tag beats a generic one. An app whose tags match
nothing lands in **Other**.

| Category | Tags that select it |
|----------|---------------------|
| On-call & Ops | `oncall`, `operations`, `monitoring`, `tickets`, `pipelines` |
| Research & Writing | `research`, `writing`, `docs` |
| Designer Tools | `ux`, `critique`, `usability`, `heuristic-evaluation`, `designer-tools` |
| Developer Tools | `developer-tools`, `code-review`, `git`, `github`, `dev`, `worktrees`, `pods`, `issue-triage`, `code-quality`, `open-source`, `performance` |
| Agents & Automation | `agents`, `automation`, `workflows`, `orchestration`, `autonomy`, `autonudge`, `execution`, `collaboration`, `visualization` |
| Productivity | `productivity`, `tasks`, `inbox`, `slack`, `email`, `outlook`, `files`, `explorer`, `aggregation`, `reports`, `team` |

## 4. Image assets

All artwork is committed to your own repo. The App Store serves it through the
git blob proxy (`GET /api/apps/blob?repo=<repo>&path=<path>`), so there is no CDN
or external hosting to arrange. The proxy only serves repos that appear in a
configured registry (an SSRF guard), only these extensions: `.png`, `.jpg`,
`.jpeg`, `.gif`, `.webp`, `.svg`, `.ico`, and it rejects `..`, absolute paths,
and any path segment starting with a dot.

Path form depends on distribution: a registry app uses a repo-relative path
(rewritten to a blob-proxy URL), while a built-in uses an absolute served URL.

| Field | Rendered where | Aspect |
|-------|----------------|--------|
| `iconPath` / `iconPathDark` | Card and row icon, the sidebar glyph, and the gradient fallback's centerpiece | Square, **512x512**, **opaque** — no transparency. The dark variant is optional |
| `screenshots` / `screenshotsDark` | Detail-page gallery with a lightbox; the first screenshot is also the last-resort hero | Landscape; around 1200px wide |
| `heroImage` / `heroImageDark` | Discover rows, Library rows, the featured spotlight, feature cards, and the detail banner when no detail-specific art exists | 16:9 (for example 1200x675) |
| `heroImageDetail` / `heroImageDetailDark` | Detail-page banner only, preferred there over `heroImage` | 25:6 (for example 1200x288) |

The required icon must be **opaque**. An opaque tile carries its own
background, so it reads correctly on any surface — which is what makes
`iconPathDark` genuinely optional rather than a latent bug. A transparent icon
that looks right on light chrome turns into a dark smear on dark chrome, and an
app that then omits the dark variant ships a broken card.

Built-in first-party apps take a different path: their icon is an SVG under
`/app-assets/`, inlined and painted from the theme's `--ico-a` / `--ico-b`
tokens, so a single file covers both appearances and no dark variant exists.
Raster art cannot repaint, which is the whole reason the variant field is here.

Ship hero art. Every store surface uses it, and it is the difference between
looking like a product and looking like a list entry.

**Resolution order on every surface:** the current theme's art, then the
opposite theme's, then the first screenshot. If an app ships none, or an image
404s, the surface falls back to a name-seeded gradient carrying the app icon, so
a missing or broken hero degrades instead of leaving a blank panel. The detail
page sizes its container to whichever ratio it resolved, so a 16:9 hero used as
a banner is not cropped.

## 5. Setup and lifecycle scripts

```json
{
  "setup": {
    "onInstall": "bash setup.sh",
    "onUninstall": "bash scripts/uninstall.sh",
    "onEnable": "bash enable.sh",
    "onDisable": "bash disable.sh",
    "onEnableTimeout": 120,
    "onDisableTimeout": 60
  }
}
```

| Hook | When it runs | Timeout |
|------|--------------|---------|
| `onInstall` | After a **registry** install has cloned and built the source, before the files are copied into the data home | 300s |
| `onUninstall` | Before app files are removed, and only after cron cleanup has succeeded | 120s |
| `onEnable` | After resources are registered, the backend is started, and dependencies are resolved | 30s, or `onEnableTimeout` |
| `onDisable` | First step of disable, before hooks, backend stop, and deregistration | 30s, or `onDisableTimeout` |

Execution model:

- Every script is wrapped as `/bin/bash -c "set -euo pipefail\n<script>"`, so an
  unset variable or any failing command in a pipeline aborts the script. Write
  scripts assuming bash, and prefer `bash script.sh` over `source script.sh` so
  the intent is explicit.
- Scripts run sandboxed with a minimal environment (no gateway secrets) plus
  `NONINTERACTIVE=1`, with `cwd` set to the app directory, under a cgroup
  ceiling, in their own process group so a timeout kills the whole tree. They
  must exit 0 on success. Output is truncated to the last lines and passed
  through credential redaction before it reaches the client.
- `onUninstall` additionally receives `KEEP_DATA` and `PURGE_DATA` (`1`/`0`). If
  the user chose to keep app data, skip deleting user data directories.
- `onEnable` failure rolls the enable back: the backend is stopped, resources are
  deregistered, and the app stays disabled. Rationale: an app that cannot start
  should not be left enabled and broken.
- `onDisable` failure does **not** block the disable. It is reported in the
  response's `warnings` and logged. A misbehaving app must always be
  disableable; orphaned processes beyond what the backend stop handles are the
  app's own responsibility.
- Installing from a **local path** (`POST /api/apps/install`, `kirocrew app
  install <dir>`) copies and registers the app but does not run `onInstall`. Do
  any build step yourself while iterating locally.
- `setup.onUpdate` parses and round-trips through the manifest, but no code path
  executes it. Do not put work an update depends on there. Make `onInstall`
  idempotent instead, since a registry update re-runs it.

Only declare `onUninstall` for state Kiro Crew cannot see: app binaries outside
the data home, shell aliases, launchd plists. For `resources: "gateway"` apps the
gateway already deregisters agents, skills, MCP entries, and cron jobs, so do not
duplicate that. For `resources: "app"` apps the gateway deregisters nothing and
your script owns all of it.

## 6. Dependencies

```json
{
  "dependencies": {
    "managedBy": "gateway",
    "capabilities": {
      "mcp": ["some-documentation-mcp-server", { "id": "my-mcp", "managedBy": "app" }],
      "skills": ["SomeSkillPackage"]
    },
    "commands": ["node", "python3"],
    "optionalCommands": ["git"]
  }
}
```

| Field | Description |
|-------|-------------|
| `managedBy` | Default resolution strategy. `"gateway"` resolves each entry through the edition's `CapabilityManager` seam; `"app"` means Kiro Crew only checks existence. A per-entry object (`{"id": ..., "managedBy": ...}`) overrides it. |
| `capabilities.mcp` / `capabilities.skills` | Capability packages the app needs but does not provide. |
| `capabilities.agents` | Declarable, but no edition has an install operation for it, so it is always reported unresolved. Declare `managedBy: "app"` or install it out of band. |
| `commands` | REQUIRED host executables, probed with `which`. A miss is reported in `missing` and warned about; it does not block the install. |
| `optionalCommands` | Same probe, reported separately in `missingOptional`. Use it for a tool the app can work without or can provision itself. |

**The open-source edition ships no capability manager**, so `capabilities`
entries resolve as unresolved and the app installs anyway. Design for graceful
degradation: an app that hard-requires a capability package will not work on a
stock install.

`dependencies.capabilities.mcp` is not `mcpServers`. `mcpServers` are servers
your app itself provides and runs, and the gateway registers them into
Kiro Crew's own agent config. `capabilities.mcp` are external servers your app
merely consumes.

Resolved dependencies are recorded in a reference-counting ledger at
`~/.kiro/crew/dependency-ledger.json`. On uninstall each declared dependency is
classified as removable (this app is its only recorded owner), shared (another
app also owns it), or user-installed (absent from the ledger). Only removable
entries are cleaned, and the uninstall request can override that:
`keep_dependencies: true` skips cleanup entirely, `keep_specific: [...]` spares
named entries.

## 7. Platform and install mode

```json
{
  "platform": {
    "os": ["macos"],
    "installMode": "client",
    "clientInstall": {
      "shell": "git clone https://github.com/you/MyApp.git ~/MyApp && cd ~/MyApp && KIROCREW_HOST={{gateway_host}} bash setup.sh",
      "postInstall": "open ~/Applications/MyApp.app"
    },
    "requiresDesktopApp": false
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `os` | `["macos", "linux"]` | Platforms the app can run on: `macos`, `linux`, `windows`. This constrains the machine the GATEWAY runs on. |
| `arch` | any | Architecture restriction; empty means any. |
| `installMode` | `"server"` | `"server"`: Kiro Crew clones and installs. `"client"`: the app must be installed on the user's own machine. |
| `clientInstall.shell` | | One-liner the user runs in their local terminal. `{{gateway_url}}` and `{{gateway_host}}` are substituted with the dashboard origin and the gateway hostname. |
| `clientInstall.postInstall` | | Follow-up command shown as a hint (for example, launching the app). |
| `requiresDesktopApp` | `false` | The app's UI needs the Electron shell (native always-on-top windows, global shortcuts, tray). A UX gate only: the browser marker is client-side and spoofable, so nothing security-relevant may depend on it. |

With `installMode: "client"` on an incompatible platform, the store shows the
copy-paste instruction panel instead of running an install, and the app
registers itself on first launch via `POST /api/apps/register`. On a compatible
platform the normal clone-and-install path runs.

An `installMode: "client"` app's `onEnable` script is treated as **advisory**: it
launches a desktop application distributed separately, which may legitimately not be
installed on this host. A failure is reported as `onEnable.failed` and the app stays
enabled, and the script is skipped outright when the gateway's OS is not in the app's
`platform.os`. Rolling the enable back instead would make the app's dashboard half
unreachable on exactly the hosts that need it to explain how to get the desktop half.

## 8. Test locally

```bash
# Build the UI bundle if the app has one
cd my-app/ui && npm install && npm run build && cd ..

curl -X POST http://localhost:5476/api/apps/install \
  -H 'Content-Type: application/json' \
  -d '{"source": "./my-app"}'

curl -X POST http://localhost:5476/api/apps/my-app/enable
```

The dashboard's Sources menu on the Apps page can install from a local path too.

Verify:

1. Open the dashboard (`kirocrew token`, then the printed URL).
2. Library tab: the app is listed with the right badges.
3. If it ships UI, open its sidebar entry and confirm the page loads.
4. If it ships agents, ask one to do something from chat.
5. If it ships crons, confirm they appear on the Schedule page.

Debug:

```bash
curl http://localhost:5476/api/apps | python3 -m json.tool
curl http://localhost:5476/api/apps/my-app/manifest | python3 -m json.tool
```

Manifest validation errors are returned by the install call itself, so a
rejected install names the offending field.

Iterate:

```bash
cd ui && npm run build && cd ..
curl -X POST http://localhost:5476/api/apps/my-app/update
```

For a tighter loop, turn on dev mode (`kirocrew app dev my-app`, or `POST
/api/apps/my-app/dev`): UI files are then served with `Cache-Control: no-store`
and a gateway-side watcher broadcasts a reload event when anything under `ui/`
changes. Agent and skill edits take effect on the next agent invocation with no
rebuild.

## 9. What gets copied at install time

Install and update copy the source tree into `~/.kiro/crew/apps/{name}/` with two
safeguards:

- **Symlinks are never followed.** A symlink resolving inside your app source
  tree is preserved as a symlink; one resolving outside is omitted entirely.
  Committed runtime artifacts must be real files (or in-tree links), never
  reachable only through an external symlink. Absolute in-tree links are
  rewritten to relative form so the installed copy does not depend on your
  source directory.
- **Build-input and VCS directories are excluded** at any depth: `node_modules`,
  `.git`, `__pycache__`, `.venv`, and the gateway's own `.kirocrew-deps`
  provisioning output (plus its transient staging/prior siblings). Serve your
  UI from a committed `ui/dist/` bundle; nothing needed at runtime may live
  under those names.

`data/` is preserved across updates and, by default, across uninstall.

### Third-party executable code is off by default

Code shipped inside the Kiro Crew package (a built-in app) is exempt, but every
other app's **executable** surfaces refuse to run unless the operator sets
`agent.apps_allow_third_party` to the JSON boolean `true` in `config.json`. That
covers registry installs and their install scripts, `detectInstalled`, backend
processes, in-gateway Python hooks, lifecycle scripts, and `openCommand`. Only
the literal `true` admits: absence, a malformed value, and an unreadable config
all deny, and the env is not consulted, so an app cannot widen the boundary from
its own process.

Non-executable resources (agents, skills, MCP server declarations, cron
definitions, UI bundles) are unaffected. If your app needs any executable
surface, say so in your README: a user who has not flipped the setting will see
`app_execution_denied` rather than a working install.

Repository layout:

```
MyAppRepo/
├── app.json
├── agents/
├── skills/
├── ui/
│   ├── src/
│   └── dist/index.mjs      <- committed build artifact
├── README.md
└── setup.sh                <- optional, referenced by setup.onInstall
```

## 10. Add a registry entry

There are two listing surfaces, and they take different paths:

**The official App Store catalog** is the store's inventory, and it is
maintainer-curated. Its authoring repository is private and not publicly
writable, so outside authors do not open the listing pull request themselves —
there is no self-serve PR path for third-party apps. A published entry is what
makes your app appear in the store *and installable*, with **no Kiro Crew
release involved**: a maintainer authors a `git` source (URL + a branch or tag;
the publish pipeline resolves and pins the exact commit) plus a category against
the authored schema, and the pipeline emits the published document that clients
read. The authored-vs-published two-schema contract is unchanged — you supply
the source and category, the pipeline pins the commit and bakes `version` from
your `app.json` into the published entry. Clients install the pinned commit
exactly and read update availability from the published entry's `version` field,
so republishing a revised entry is also how an update reaches users.

To request a listing, open an **App Store listing request** issue using the
[listing request template](https://github.com/kirodotdev/KiroCrew/issues/new?template=app-store-listing.yml).
Provide the git source, the ref to pin, the display name, a summary, the
category/tags, and author credit, and confirm the section 14 review checklist.
A maintainer picks it up and authors the catalog entry with full author credit.
(Opening the catalog repository up to outside contributors is a possible
alternative, but it is a maintainer-only org-visibility decision, not something
an author can do.)

**The bundled seed** (`src/kiro_crew/apps/app-registry.json` in the Kiro Crew
repo) is the catalog's offline snapshot, not the listing surface: it is what a
client falls back to when the catalog host is unreachable. Entries here ride the
Kiro Crew release train. A catalog row for the same repository supersedes the
seed row, so the seed needs touching only when offline availability matters.

The seed (and any federated registry index) uses this row shape:

```json
[
  {
    "name": "my-app",
    "gitUrl": "https://github.com/yourname/my-app",
    "branch": "main"
  }
]
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Must match `app.json`'s `name`. |
| `gitUrl` | yes | Any git-cloneable URL (`https://github.com/...`, `git@host:...`). The legacy `repo` field is still read and used as the clone target when no `gitUrl` is present. |
| `repo` | | Repo identifier the blob proxy uses to serve committed images. |
| `branch` | | Branch to read and clone. Defaults to `main`. For an entry cloning the registry repo itself (the monorepo layout), the registry's **configured** branch overrides this declaration — the index was read from that branch, so a divergent declaration names a state that does not exist there; the divergence is warning-logged. Entries cloning a different repository keep their declared branch. |
| `subdirectory` | | Path within the repo holding `app.json`, for a monorepo layout. Treated as untrusted: it is joined with symlink-resolving containment and rejected if it escapes the clone root. |
| `resources` | | `"gateway"` (default) or `"app"`: who registers agents, skills, MCP servers, and crons. |
| `lifecycle` | | `"gateway"` (default), `"app"`, or `"locked"`: who owns updates and uninstall. |
| `detectInstalled` | | Shell command that exits 0 when the app is already present on the machine (for self-managed apps). It runs sandboxed with a 5s timeout. |
| `featured` | | Curator flag for the Discover editorial layer. `true` marks the app featured; a number both marks it and orders the slots (lower first). It lives on the registry entry, not in `app.json`, and is honored only for core-registry entries: a `featured` flag from an external registry is ignored, so adding a registry cannot seize the spotlight. With nothing flagged, the store falls back to a deterministic pick (apps with hero art first, then verified publishers, then name). |

To reach the official store, open an **App Store listing request** issue with the
[listing request template](https://github.com/kirodotdev/KiroCrew/issues/new?template=app-store-listing.yml)
— that is the reachable path for an outside author, since the catalog repository
is not publicly writable. A seed change in the Kiro Crew repo, by contrast,
follows the normal contribution flow and ships with the next release.

## 11. Federated external registries

A team can host its own registry without Kiro Crew review per app. Users opt in by
adding it to their config:

```json
{
  "registries": [
    { "name": "my-team", "repo": "https://github.com/my-team/AppRegistry", "branch": "main" }
  ]
}
```

The index is read as `app-registry.json` at the repo root, falling back to
discovering `apps/*/app.json` when no index file exists. Repo and branch values
are validated against strict patterns, and unsafe `subdirectory` values are
dropped from the index. Manage registries with `GET`/`PUT /api/apps/registries`
(the PUT blocks adding the Kiro Crew repo itself) and `POST
/api/apps/registries/refresh`.

**Trust model:** the user explicitly opts in by adding the registry, and the repo
must be git-accessible.

**Credential posture.** Apps listed in an external index are cloned
credential-free by default: an anonymous environment plus a strict sandbox that
hides `~/.ssh`. This is a confused-deputy defense, because an index the owner did
not author could otherwise point at a private sibling repo on the owner's own
trusted forge and have the gateway read it with ambient credentials.

**The same-repo carve-out** relaxes that for the monorepo layout. When an index
entry's effective clone URL is byte-identical to the registry repo URL the owner
configured, the owner did designate exactly that URL, so all three clone
chokepoints use owner credentials: the manifest fetch, the install clone, and
the App Store's icon/screenshot blob fetch. The comparison is exact string
equality with no normalization: sibling repos on the same host stay anonymous
and strict.

**Private-forge recipe.** On a credential-only forge (SSH keys, no anonymous
read), keep every app inside the registry repo so the carve-out applies:

```
app-registry.json          # or rely on apps/*/ auto-discovery
apps/
  my-tool/app.json
  other-app/app.json
```

With this layout the store lists, installs, and renders icons/screenshots for
those apps using the owner's credentials. Apps in separate repos on the same
private forge do not benefit from the carve-out: they fail to clone, and their
icons and screenshots fall back to the name-seeded gradient.

**Keep the configured URL byte-identical.** Because the carve-out is exact
string equality, editing the registry `repo` between otherwise-equivalent forms
— `ssh://host/x` versus `ssh://user@host/x`, or adding/removing a trailing
`.git` — silently drops every app back to anonymous + strict. On a credential-only
forge that means apps stop cloning and icons go blank, and the changed URL also
triggers a one-time move-aside re-clone of any already-installed app. This is
deliberate and safe (the new string is a URL the owner did not previously
designate), but if you see "apps stopped cloning after I changed the registry
URL", restore the byte-identical value or expect the one-time re-clone to settle.

## 12. How a user install runs

### Registry install

The store's Install button (`POST /api/apps/registry/install`, or the SSE variant
`/registry/install-stream` that streams the log live):

1. Fetch `app.json` with `git archive` and check it against the fleet admission
   policy, so a banned, non-allowlisted, or unsigned app is never cloned.
2. Check `platform`, and answer with client-install instructions instead if the
   gateway's OS cannot run it.
3. Check `minKiroCrewVersion`.
4. Clone into `~/.kiro/crew/app-sources/{name}/` (persistent, one workspace per
   app; 60s timeout) and run a detected build: `npm install` plus `npm run build`
   when `package.json` declares a build script, or `pip install .` /
   `pip install -r requirements.txt` for a Python source tree. A missing
   toolchain is a logged skip, not a failure. **An official-catalog entry does
   not clone a branch**: it fetches exactly the commit the published catalog
   pins and hard-fails on any mismatch, never reuses a pre-existing checkout
   (the old one is set aside and restored if the install fails), and clones
   credential-free.
5. Run `setup.onInstall` (300s).
6. Resolve declared dependencies.
7. For a gateway-managed app: copy into `~/.kiro/crew/apps/{name}/`, register
   resources, and start the backend. For `resources: "app"`: pre-register from
   the cloned manifest so the app appears immediately, and let the app finish its
   own registration on next launch.

Requirements: the repo must be git-accessible, `app.json` must sit at the repo
root or at `subdirectory`, and the install script must be non-interactive and
finish inside its timeout.

### Self-managed install

An app with its own installer (an Electron build, a native binary) registers
itself at runtime:

```
POST /api/apps/register
{
  "name": "my-app",
  "version": "1.0.0",
  "displayName": "My App",
  "manifest": { ...full app.json... },
  "origin": "external",
  "resources": "app",
  "lifecycle": "app"
}
```

The call is idempotent: registering again with a newer version updates the entry.
`origin`, `resources`, and `lifecycle` default to `external`/`app`/`app` when
omitted. Use this when the app has its own build or package system, needs
runtime-dynamic agent configuration, or manages its own agent, skill, and MCP
registration. See
[../system-specs/modules/app-kit-platform.md](../system-specs/modules/app-kit-platform.md)
for what each classification value changes.

## 13. Updates and versioning

Bump `version` in `app.json` and push. A seed or federated-registry entry
carries no version, so there is nothing to update there. **An official-catalog
entry is different**: the published document pins a commit and bakes `version`
from your `app.json` at publish time, so pushing to your branch changes nothing
for users — an update ships when the catalog republishes your entry with a new
pin, and clients detect it by comparing the published `version` against the
installed one.

- Patch for fixes, minor for features, major for breaking changes (agent config
  schema, MCP tool interface).
- `minKiroCrewVersion` is checked on install and update; too-old gateways get a
  clear error telling the user to update Kiro Crew first.
- Users update from the store or via `POST /api/apps/{name}/update`. For a
  registry-sourced app this re-clones, rebuilds, re-runs `onInstall`, and swaps
  resources only after the fresh install has succeeded, so a failed update leaves
  the working version registered.

## 14. Review checklist

- [ ] `app.json` validates: kebab-case `name`, semver `version`, all required
      fields non-empty
- [ ] No `..` or absolute paths in `agents`, `skills`, `sops`, `ui.entry`,
      `ui.pages[].entryPoint`, `backend.entryPoint`
- [ ] `permissions` are minimal, and each one is actually used
- [ ] Icon committed, square, 256x256 or larger
- [ ] At least one screenshot and one hero image committed
- [ ] `description` is plain text and reads well truncated to two lines
- [ ] `tags` are lowercase and land the app in the right category
- [ ] UI bundle built and committed (`ui/dist/index.mjs`)
- [ ] Agent JSON files are valid; skill `SKILL.md` files have proper frontmatter
- [ ] Install script is non-interactive, idempotent, and exits 0 within 300s
- [ ] `onUninstall` cleans up everything created outside
      `~/.kiro/crew/apps/{name}/`, and nothing the gateway already manages
- [ ] `README.md` explains what the app does and how to use it
- [ ] The app installs and enables cleanly from a local clone

Reviewers check that the manifest is complete, the permissions are proportionate,
resource paths do not traverse, any install script is safe to run, and the app is
useful to Kiro Crew users.

## 15. Quick reference

| Stage | Command or action |
|-------|-------------------|
| Scaffold | `kirocrew app init my-app` |
| Build UI | `cd ui && npm run build` |
| Install locally | `POST /api/apps/install`, or `kirocrew app install <dir>` |
| Enable | `POST /api/apps/{name}/enable`, or `kirocrew app enable <name>` |
| Live reload | `kirocrew app dev <name>` |
| Update local copy | `POST /api/apps/{name}/update` |
| List a registry app | Official catalog: open an [App Store listing request](https://github.com/kirodotdev/KiroCrew/issues/new?template=app-store-listing.yml) issue (maintainer-curated). Bundled seed or a federated registry: add an entry to `app-registry.json`, open a pull request |
| User install | Apps page, Discover, Install |
| Ship an update | Bump `version`, push |

Bugs and feature requests: [GitHub
issues](https://github.com/kirodotdev/KiroCrew/issues).
