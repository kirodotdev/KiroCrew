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
make desktop
```

Output lands in **`website/electron/dist/`**:

| Platform | Artifact |
|----------|----------|
| macOS | `KiroCrew-*.dmg` |
| Linux | `KiroCrew-*.AppImage` |

The artifact for the host OS is built (DMG on macOS, AppImage on Linux). The
electron-builder configuration lives in
[`website/electron/package.json`](../website/electron/package.json):

- **appId:** `dev.kirocrew.desktop`
- **productName:** `KiroCrew`
- mac target: `dmg` (category `public.app-category.developer-tools`)
- linux target: `AppImage` (category `Development`)

### Builds are host-architecture-only — one build per target arch

> **Important:** `make desktop` produces an installer for the **host OS *and*
> host CPU architecture only.** It is not a universal/fat binary.

The python-build-standalone interpreter is architecture-specific (honors the host
arch) and the electron-builder config sets no `arch` key (defaults to the host
arch). The bundled backend's architecture is therefore **coupled** to the
installer's — you cannot mix (e.g. an arm64 DMG carrying an x86_64 backend). To
cover all four supported targets you must run the build on a machine of each
architecture:

| Target | Build host | Produces |
|--------|-----------|----------|
| macOS arm64 (Apple Silicon) | Apple Silicon Mac | arm64 `.dmg` |
| macOS x86_64 (Intel) | Intel Mac, or an Apple-Silicon Mac via Rosetta (see [Building BOTH macOS DMGs](#building-both-macos-dmgs-from-one-apple-silicon-machine-rosetta)) | x86_64 `.dmg` |
| Linux x86_64 | x86_64 Linux | x86_64 `.AppImage` |
| Linux aarch64 (Graviton/ARM) | aarch64 Linux | aarch64 `.AppImage` |

A maintainer on an Apple-Silicon Mac who runs `make desktop` ships an
**arm64-only** DMG; Intel-Mac users cannot run it. For a public release, build
each artifact on its own runner (e.g. a CI matrix of `macos-14` (arm64),
`macos-13` (x86_64), `ubuntu-latest` (x86_64), and an arm64 Linux runner).
There is intentionally no `universal2` macOS target — it would require
universal2 wheels for every native dependency (numpy, aiohttp, lxml, PyYAML),
which not all publish.

### Building BOTH macOS DMGs from one Apple-Silicon machine (Rosetta)

You can produce both the arm64 and the x86_64 DMG on a single Apple-Silicon Mac
without an Intel machine, by running the x86_64 toolchain under **Rosetta 2**.
The PBS interpreter is architecture-specific, so the trick is to install an
x86_64 PBS interpreter via `uv` under Rosetta and build with that.

Prerequisites: Rosetta 2 (`softwareupdate --install-rosetta --agree-to-license`).

```bash
# 0. Build the frontend ONCE (arch-independent); both DMGs reuse it.
cd website && npm ci && npm run build && cd ..

# 1. arm64 (native):
SKIP_FRONTEND=1 bash packaging/build-desktop.sh
#    → website/electron/dist/KiroCrew-<version>-arm64.dmg

# 2. x86_64 (under Rosetta): uv installs the x86_64 PBS interpreter.
arch -x86_64 uv python install cpython-3.12
# Then run the build script under Rosetta to pick up the x86_64 interpreter:
arch -x86_64 bash -c 'SKIP_FRONTEND=1 bash packaging/build-desktop.sh'
#    → website/electron/dist/KiroCrew-<version>.dmg (x64)
```

electron-builder names the host-arch (arm64) DMG `KiroCrew-<v>-arm64.dmg` and the
x64 DMG `KiroCrew-<v>.dmg` (no suffix), so the two coexist in
`website/electron/dist/`. Verify each actually carries the matching backend:

```bash
# The embedded backend's arch MUST match the DMG's arch (an arm64 DMG carrying
# an x86_64 backend would crash on launch). Mount and check:
hdiutil attach -nobrowse -readonly website/electron/dist/KiroCrew-<v>-arm64.dmg
file "/Volumes/KiroCrew <v>-arm64/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend/kirocrew-backend"
#   → …executable arm64
hdiutil detach "/Volumes/KiroCrew <v>-arm64"
```

> CI is still the cleaner path for releases (`macos-14` for arm64, `macos-13`
> for x86_64) — the Rosetta route is for producing both locally when you don't
> have an Intel runner.

### Refreshing / cleaning the DMGs

The `dist/` directory is **not** cleaned between builds, so old artifacts pile up
(e.g. a `KiroCrew-1.0.0.dmg` from before a version bump, or a stale `mac/`
app-staging dir). After a version change or a re-build, remove the stale ones so
only the current set remains:

```bash
cd website/electron/dist
rm -f KiroCrew-<old-version>*.dmg            # stale DMGs from a prior version
rm -rf mac mac-arm64                          # app-staging dirs (regenerated each build)
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

### Build escape hatches

The script honors two environment flags:

| Flag | Effect |
|------|--------|
| `SKIP_FRONTEND=1` | Reuse an already-built `website/dist` |
| `SKIP_ELECTRON=1` | Stop after the bundled backend (no electron-builder) |

## The bundled backend (python-build-standalone)

The build produces a self-contained Python interpreter with all dependencies
installed, located at `website/electron/backend-dist/kirocrew-backend/`. Key
details:

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
whether a gateway is already running; if not, it locates the backend binary via
[`find-bin.js`](../website/electron/find-bin.js) and spawns it as
`kirocrew gateway --no-open`, then polls `/api/status` (up to 2 minutes)
and loads the dashboard once it is healthy.

### `find-bin.js` — locating the binary

`findKirocrewBin()` checks well-known paths in order and returns the first
executable it finds, falling back to bare `kirocrew` on `PATH`:

1. `<resourcesPath>/backend-dist/kirocrew-backend/bin/kirocrew` — the bundled
   PBS backend inside the packaged `.app` (electron-builder ships
   `backend-dist/kirocrew-backend` as `extraResources`).
2. `<__dirname>/backend-dist/kirocrew-backend/bin/kirocrew` — the same binary
   when running unpackaged from `website/electron/` in development.
3. `<__dirname>/../bin/kirocrew`
4. Well-known install paths under `$HOME` (e.g. `~/.local/bin/kirocrew`,
   `~/.kirocrew-app/.venv/bin/kirocrew`).
5. Bare `"kirocrew"` (resolved via `PATH`).

The function is pure — `fs`, `os`, `path`, `process.resourcesPath`, and
`__dirname` are injected — so it is unit-testable without mocking globals.

### `main.js` — spawning the gateway

- Ensures `KIROCREW_HOME` (default `~/.kirocrew`, overridable via the
  `KIROCREW_HOME` env var) exists, then spawns the backend with
  `["gateway", "--no-open"]`.
- Honors the **`KIROCREW_PORT`** env var for the dashboard port (default `5476`,
  validated to `1–65535`). `BACKEND_URL` / health checks target that port.
- Sets `KIROCREW_PROJECT_DIR` to the Electron app's parent directory so the
  bundled `agents/` and `skills/` are discovered.
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
