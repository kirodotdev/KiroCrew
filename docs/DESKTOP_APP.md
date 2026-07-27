# KiroCrew Desktop App

The desktop app is an [Electron](https://www.electronjs.org/) shell that wraps
the KiroCrew web dashboard and embeds a **self-contained Python backend**. The
backend uses a [python-build-standalone](https://github.com/indygreg/python-build-standalone)
(PBS) interpreter with all dependencies installed via `uv`/`pip` into the bundled
interpreter — end users need **no** Python, pip, npm, or node. They just
double-click the app and the dashboard opens.

The Electron sources live in [`website/electron/`](../website/electron/); the
build is driven by [`packaging/build-desktop.sh`](../packaging/build-desktop.sh).

## What `make desktop` produces

```bash
make desktop               # macOS: ONE universal DMG (arm64 + x86_64) · Linux: AppImage
UNIVERSAL=0 make desktop   # macOS: faster host-arch-only DMG (local iteration)
```

Output lands in **`website/electron/dist/`**:

| Command | Platform | Artifact |
|---------|----------|----------|
| `make desktop` | macOS | `KiroCrew-<version>-universal.dmg` |
| `UNIVERSAL=0 make desktop` | macOS | `KiroCrew-<version>-arm64.dmg` (Apple Silicon host) or `KiroCrew-<version>.dmg` (Intel host) |
| `make desktop` | Linux | `KiroCrew-*.AppImage` (host arch) |

The electron-builder configuration lives in
[`website/electron/package.json`](../website/electron/package.json):

- **appId:** `dev.kirocrew.desktop`
- **productName:** `KiroCrew`
- macOS display name: `Kiro Crew` via `CFBundleDisplayName`; `CFBundleName`
  remains aligned with `productName` because Electron uses it to locate the
  `KiroCrew Helper` app bundles during startup
- mac target: `dmg` (category `public.app-category.developer-tools`)
- linux target: `AppImage` (category `Development`)

### macOS default — one universal DMG for both arches

On macOS, `make desktop` produces a single `KiroCrew-<version>-universal.dmg`
running **natively** on both Apple Silicon and Intel Macs. It needs only
**one Apple-Silicon machine** — no Intel host, no second build. (It requires
an Apple-Silicon host with Rosetta 2; the script fails fast with instructions
otherwise, and `UNIVERSAL=0` is the opt-out.)

### macOS opt-out and Linux — host-arch-only builds

`UNIVERSAL=0 make desktop` (and every Linux build) produces an installer for
the **host OS *and* host CPU architecture only.** The python-build-standalone
interpreter is architecture-specific (honors the host arch) and, in this mode,
the bundled backend's architecture is **coupled** to the installer's — you
cannot mix (e.g. an arm64 DMG carrying an x86_64 backend). Use it for faster
local iteration on macOS (~half the build time and disk of universal), or on
an Intel Mac where the universal build cannot run. Per-arch targets:

| Target | Build host | Produces |
|--------|-----------|----------|
| macOS arm64 (Apple Silicon) | Apple Silicon Mac (`UNIVERSAL=0`) | arm64 `.dmg` |
| macOS x86_64 (Intel) | Intel Mac | x86_64 `.dmg` |
| Linux x86_64 | x86_64 Linux | x86_64 `.AppImage` |
| Linux aarch64 (Graviton/ARM) | aarch64 Linux | aarch64 `.AppImage` |

Anything you **distribute** for macOS should be the universal DMG — the
host-arch build is a local-machine artifact.

Prerequisite: **Rosetta 2** on the build machine
(`softwareupdate --install-rosetta --agree-to-license`) — the x86_64 PBS
interpreter runs under Rosetta during the build (pip install + verification).
The script preflights this (`arch -x86_64 /usr/bin/true`) and aborts with the
`softwareupdate` hint if missing.

How it works — **universal shell + dual embedded backends**:

- The Electron shell binaries (`Contents/MacOS/`, `Frameworks/`) are
  lipo-merged fat binaries via electron-builder's `--universal` target.
- The PBS backend tree cannot be lipo-merged (thousands of files, no
  universal2 PBS — see [below](#why-no-true-universal2-backend)), so the app
  ships **two complete backend trees** and picks one at launch:

```
KiroCrew.app/Contents/
├── MacOS/ + Frameworks/…                 ← fat binaries (arm64 + x86_64)
└── Resources/backend-dist/
    ├── kirocrew-backend-arm64/           ← full PBS bundle, arm64
    └── kirocrew-backend-x64/             ← full PBS bundle, x86_64
```

The build runs the normal backend steps twice: natively for
`kirocrew-backend-arm64/`, then again with an x86_64 PBS interpreter
(`uv python install cpython-3.12-macos-x86_64-none`, executed under Rosetta)
for `kirocrew-backend-x64/`. The frontend is built once (arch-independent).
Each backend passes the same self-containment gate as a per-arch build — the
x64 gate doubles as proof the bundle runs under Rosetta. In
`website/electron/package.json`, `build.mac.x64ArchFiles` allowlists
`backend-dist/**` (single-arch Mach-O files inside a universal app are
intentional there), and `extraResources` ships the `backend-dist/` directory
wholesale so single- and dual-backend layouts both package.

**Trade-off:** the DMG carries two full Python backend trees, so it is
roughly **2× the size** of a per-arch DMG — expect ~350–400 MB. That is the
price of one artifact + one update feed; a per-arch feed split was
explicitly deferred.

Verify a universal build:

```bash
V=<version>
hdiutil attach -nobrowse -readonly "website/electron/dist/KiroCrew-$V-universal.dmg"
APP="/Volumes/KiroCrew $V-universal/KiroCrew.app"

# 1. The shell binary is fat:
lipo -archs "$APP/Contents/MacOS/KiroCrew"
#   → x86_64 arm64

# 2. EACH backend carries the matching interpreter:
file "$APP/Contents/Resources/backend-dist/kirocrew-backend-arm64/bin/python3.12"
#   → …executable arm64
file "$APP/Contents/Resources/backend-dist/kirocrew-backend-x64/bin/python3.12"
#   → …executable x86_64

hdiutil detach "/Volumes/KiroCrew $V-universal"
```

(The build script performs these `lipo -archs` / `file` checks itself as
post-gates, plus a resolver-agreement gate asserting `find-bin.js` resolves
the arch-suffixed launcher.)

**CI:** the `macos-14` (Apple Silicon) entry in `build-desktop.yml` runs
`make desktop` (universal by default on macOS — GitHub's arm64 macOS runners
include Rosetta 2)
and uploads a single `unsigned-build-darwin-universal` artifact. Everything
downstream (codesigning both slices, notarization, stapling, the update
feed) is arch-indifferent: the feed schema is unchanged, `latest-mac.json`
points at the one universal zip, and installed arm64 apps auto-update onto
it seamlessly. No Intel runner and no per-arch feed split are needed.

#### Why no *true* universal2 backend?

A genuinely lipo-merged (universal2) **backend** stays off the table: there is
no universal2 python-build-standalone distribution, the backend tree is
thousands of files (a fragile file-by-file merge with no tool support), and
not all native dependencies publish paired wheels to merge (numpy, aiohttp,
lxml, PyYAML…). The dual-backend layout above is how universality is achieved
instead — two single-arch trees, selected at launch by `process.arch`.

### Refreshing / cleaning the DMGs

The `dist/` directory is **not** cleaned between builds, so old artifacts pile up
(e.g. a `KiroCrew-1.0.0.dmg` from before a version bump, or a stale `mac/`
app-staging dir). After a version change or a re-build, remove the stale ones so
only the current set remains:

```bash
cd website/electron/dist
rm -f KiroCrew-<old-version>*.dmg            # stale DMGs from a prior version
rm -rf mac mac-arm64 mac-universal*           # app-staging dirs (regenerated each build)
rm -f builder-debug.yml
```

The desktop app's version comes from `website/electron/package.json` (`version`)
— **keep it in sync with the backend `version` in `pyproject.toml`**. When you
bump one, bump the other and the root `version` fields in
`website/electron/package-lock.json` (the top-level `version` and
`packages[""].version`, NOT the dependency entries that coincidentally share a
version), or `npm ci` will complain about a lock mismatch.

> **npm registry pin (required):** both `website/.npmrc` *and*
> `website/electron/.npmrc` pin `registry=https://registry.npmjs.org/`. The
> electron pin is load-bearing — without it `npm ci` in `website/electron/`
> inherits whatever registry the machine's global `~/.npmrc` sets and can fail
> with an auth error on a non-public registry. Any new npm subproject needs its
> own public-registry `.npmrc`.

## Build pipeline

`make desktop` runs `bash packaging/build-desktop.sh`, which executes the
pipeline end-to-end:

```
1. Build the React dashboard (npm)                    → website/dist
2. Provision a python-build-standalone interpreter    → via uv python install
3. pip-install kiro_crew + deps into the bundled interpreter
4. Stage the dashboard into the package's static dir
5. Prune caches/tests/unused stdlib to shrink bundle
6. Package with electron-builder                      → website/electron/dist/ (DMG / AppImage)
```

On macOS (universal by default) the pipeline repeats steps 2–5 once per
architecture — natively into `kirocrew-backend-arm64/`, then with an x86_64
PBS interpreter under Rosetta into `kirocrew-backend-x64/` — and step 6
packages with `electron-builder --mac --universal`. With `UNIVERSAL=0` (and
always on Linux) steps 2–5 run once for the host arch into the unsuffixed
`kirocrew-backend/`.

Step by step:

1. **Frontend** — in `website/`, runs `npm ci` (or `npm install`) + `npm run
   build`, then copies `website/dist` into `src/kiro_crew/static/dist`. The
   script aborts if `website/dist/index.html` is missing.
2. **PBS interpreter** — uses `uv python install cpython-3.12` to provision a
   self-contained python-build-standalone interpreter. PBS interpreters use
   `@executable_path`-relative dylib references, making the bundle portable
   across machines without needing the same system Python.
3. **Install into bundle** — copies the PBS interpreter into
   `website/electron/backend-dist/kirocrew-backend/`, removes the
   `EXTERNALLY-MANAGED` marker, then runs `pip install` with
   `PYTHONNOUSERSITE=1` to force the full closure into the bundle.
4. **Stage dashboard** — copies the built SPA into the bundled
   `kiro_crew/static/dist` inside site-packages.
5. **Prune** — removes `__pycache__`, test dirs, and unused stdlib modules
   (tkinter, idlelib, etc.) to shrink the bundle.
6. **Package** — in `website/electron/`, runs electron-builder to produce the
   installer(s) in `website/electron/dist/`.

### Build flags

The script honors these environment flags:

| Flag | Effect |
|------|--------|
| `UNIVERSAL=0` | macOS: opt out of the universal default — host-arch-only build (faster local iteration; the only option on an Intel Mac). Universal (`UNIVERSAL=1`) is the macOS default; Linux is always host-arch |
| `SKIP_FRONTEND=1` | Reuse an already-built `website/dist` |
| `SKIP_ELECTRON=1` | Stop after the bundled backend (no electron-builder) |

## The bundled backend (python-build-standalone)

The build produces a self-contained Python interpreter with all dependencies
installed, located at `website/electron/backend-dist/kirocrew-backend/`
(per-arch mode) or `…/backend-dist/kirocrew-backend-arm64/` +
`…/kirocrew-backend-x64/` (universal mode — electron-builder ships the whole
`backend-dist/` directory as `extraResources`, so both layouts package the
same way). Key details:

- **Interpreter** is a python-build-standalone CPython 3.12 with `@executable_path`-
  relative dylib references (genuinely portable, no system Python dependency).
- **Entry point** is `bin/kirocrew` — a shell script that execs
  `bin/python3.12 -s -m kiro_crew "$@"`.
- **Self-containment verified** — the build script runs
  `PYTHONNOUSERSITE=1 bin/python3.12 -m kiro_crew --version` to catch any
  missing dependency before packaging.
- **Dashboard bundled** — the SPA is staged into
  `lib/python3.12/site-packages/kiro_crew/static/dist/` inside the bundle.
- **Pruned** — `__pycache__`, test dirs, and unused stdlib (tkinter, idlelib,
  turtledemo, ensurepip, lib2to3) are removed to shrink the bundle.

## How the app finds and launches the backend

When the app starts, [`main.js`](../website/electron/main.js) first checks
whether a gateway is already running. An existing gateway—including a local SSH
forward to a remote gateway—is reused. Otherwise the shell locates the backend
binary via [`find-bin.js`](../website/electron/find-bin.js), spawns it as
`kirocrew gateway --no-open`, polls `/api/status`, and loads the dashboard once
it is healthy.

The gateway-hosted dashboard then checks both prerequisites needed by the ACP
provider:

1. It discovers `kiro-cli` in the inherited `PATH`, `~/.local/bin`,
   `~/.cargo/bin`, Homebrew locations, or the macOS `Kiro CLI.app` bundle.
2. It verifies the first candidate selected by the shared ACP resolver with
   `kiro-cli --version`. A broken or untrusted higher-priority candidate blocks
   readiness instead of approving a later binary that ACP would not launch.
3. It verifies authentication with `kiro-cli whoami`.

If either check fails, the shared React setup gate appears in both the desktop
shell and browser dashboard. On macOS, Linux, and native Windows, the user can
explicitly choose **Install Kiro CLI**. The gateway downloads the fixed official
HTTPS installer for the host platform, validates its size and
platform-specific marker in memory, and pipes those same bytes to the fixed
system interpreter. The installer itself verifies the downloaded Kiro CLI
package. The second step runs `kiro-cli login --use-device-flow`, displays the
secure sign-in URL and code, and opens the Kiro Crew dashboard only after
`kiro-cli whoami` succeeds.
An installed candidate that cannot start is shown as needing repair rather than
as merely signed out; one that runs is directly usable for sign-in regardless of
install source (toolbox, Homebrew, winget, the official installer, or a
self-updated bundle) — trust is "the CLI runs, and it has a valid login", not
where it was installed. A broken existing macOS app bundle or Linux user-local
binary is repaired through the official interactive guide when the upstream
installer requires terminal confirmation before replacing it. Installation and
sign-in never start silently in the background. Setup subprocesses receive a
minimal allowlisted environment rather than the desktop shell's credentials;
version probes use the strict OS sandbox and hide every known Kiro identity
store. `whoami` and device-login run for any runnable candidate; they use a
standard sandbox with a temporary home containing only Kiro identity token
files, so unrelated AWS, SSH, GitHub, Kubernetes, and Kiro Crew state remain
unavailable, and POSIX auth still executes a private snapshot of the exact
resolved bytes. Timed-out commands signal a POSIX process group only
while its leader still anchors that identity; on Windows, exact retained process
handles terminate observed descendants without trusting recycled PIDs. Cleanup
finishes before the gateway permits a retry.
Hosting setup in the gateway provides one implementation and one UI for the
desktop app, local browser, remote browser, Linux, and Windows.

### `find-bin.js` — locating the binary

`findKirocrewBin()` checks well-known paths in order and returns the first
executable it finds, falling back to bare `kirocrew` on `PATH`. The running
process's CPU architecture (`process.arch`, injected as a parameter) selects
the matching backend in a universal app:

1. `<resourcesPath>/backend-dist/kirocrew-backend-<arch>/bin/kirocrew`, then
   `<__dirname>/…` — the arch-suffixed PBS backend inside a **universal**
   packaged `.app` (or unpackaged in development), where `<arch>` is `arm64`
   or `x64` per `process.arch` (a fat Electron shell runs as exactly one
   slice, so `process.arch` is the native arch of the Mac — Apple Silicon
   loads `kirocrew-backend-arm64/`, Intel loads `kirocrew-backend-x64/`).
   Ranked above the unsuffixed layout so a universal bundle never falls back
   to a wrong-arch tree; per-arch bundles don't ship these dirs, so the
   probes miss and fall through.
2. `<resourcesPath>/backend-dist/kirocrew-backend/bin/kirocrew`, then
   `<__dirname>/…` — the unsuffixed fallback: the bundled PBS backend inside
   a **per-arch** packaged `.app` (or unpackaged in development).
3. `<__dirname>/../bin/kirocrew`
4. Well-known install paths under `$HOME` (e.g. `~/.local/bin/kirocrew`,
   `~/.kirocrew-app/.venv/bin/kirocrew`).
5. Bare `"kirocrew"` (resolved via `PATH`).

The function is pure — `fs`, `os`, `path`, `process.resourcesPath`,
`__dirname`, and the arch are injected — so both arch branches are
unit-testable without mocking globals.

### `main.js` — spawning the gateway

- Ensures `KIROCREW_HOME` (default `~/.kiro/crew`, overridable via the
  `KIROCREW_HOME` env var) exists, then spawns the backend with
  `["gateway", "--no-open"]`. If a real pre-move `~/.kirocrew` directory exists,
  the shell reads its startup config first while the backend performs the
  one-time migration; token lookup then falls through to the canonical home.
  A clean install never creates the legacy directory.
- Honors the **`KIROCREW_PORT`** env var for the dashboard port (default `5476`,
  validated to `1–65535`). `BACKEND_URL` / health checks target that port.
- Sets `KIROCREW_PROJECT_DIR` to the Electron app's parent directory so the
  bundled `agents/` and `skills/` are discovered.
- Leaves the inherited child `PATH` unchanged. The gateway prerequisite service
  probes supported Kiro CLI locations independently, so Finder-launched macOS
  apps and Linux desktop launchers still find user-local installations without
  mutating the shell environment.
- On window close the app hides to the tray; quitting sends `SIGTERM` to the
  gateway process.

## Code signing & notarization (macOS)

An unsigned `.app`/DMG is quarantined by Gatekeeper and shows **"KiroCrew is
damaged and can't be opened"** when downloaded on another Mac. To distribute a
DMG that opens cleanly you must sign it with a **Developer ID Application**
certificate and **notarize** it with Apple. (Local builds without credentials
still work — they produce an ad-hoc–signed DMG you can open on the build machine
after right-click → Open or `xattr -dr com.apple.quarantine KiroCrew.app`.)

The build is already wired for this — `website/electron/package.json` enables
`hardenedRuntime` with `build/entitlements.mac.plist`, and the
`scripts/notarize.js` afterSign hook notarizes when credentials are present and
silently skips when they aren't. You only supply the secrets at build time via
env vars (nothing is committed):

```bash
# 1. Signing identity — a Developer ID Application cert exported as .p12
#    (Xcode → Settings → Accounts, or developer.apple.com → Certificates).
export CSC_LINK=/abs/path/DeveloperIDApplication.p12   # or its base64
export CSC_KEY_PASSWORD='<p12 export password>'

# 2. Notarization credentials — EITHER an App Store Connect API key …
export APPLE_API_KEY=/abs/path/AuthKey_XXXXXXXXXX.p8
export APPLE_API_KEY_ID=XXXXXXXXXX
export APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#    … OR an Apple ID + app-specific password (appleid.apple.com → Sign-In
#    & Security → App-Specific Passwords):
export APPLE_ID='you@example.com'
export APPLE_APP_SPECIFIC_PASSWORD='abcd-efgh-ijkl-mnop'
export APPLE_TEAM_ID=XXXXXXXXXX

# 3. Build — electron-builder signs, the hook notarizes + staples.
make desktop
```

Verify the result: `spctl -a -vv "KiroCrew.app"` should report
`source=Notarized Developer ID` and `codesign -dv` should show your Team ID
(not `Signature=adhoc`).

Requires a paid Apple Developer account ($99/yr) for the Developer ID cert and
notary access. Without one, distribute via Homebrew cask or instruct users to
clear the quarantine flag.

## Remote tunnel mode

The desktop app can also connect to a gateway running on a **remote** host (e.g.
an always-on server) over an SSH tunnel, fetching a fresh token via
`ssh <host> kirocrew token` on each launch instead of starting a local backend.
See [`website/electron/README.md`](../website/electron/README.md) and
[REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) for setup.

## See also

- [INSTALL.md](INSTALL.md) — all three build/run methods and the Makefile targets
- [README](../README.md) — project overview and Quick Start
