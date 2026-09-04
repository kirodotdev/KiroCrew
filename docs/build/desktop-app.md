# Kiro Crew Desktop App

The desktop app is an [Electron](https://www.electronjs.org/) shell that wraps
the Kiro Crew web dashboard and embeds a **self-contained Python backend**. The
backend uses a [python-build-standalone](https://github.com/indygreg/python-build-standalone)
(PBS) interpreter with all dependencies installed via `uv`/`pip` into the bundled
interpreter — end users need **no** Python, pip, npm, or node. They just
double-click the app and the dashboard opens.

The Electron sources live in [`website/electron/`](../../website/electron/); the
build is driven by [`packaging/build-desktop.sh`](../../packaging/build-desktop.sh).

## What `make desktop` produces

```bash
make desktop               # macOS: ONE universal DMG (arm64 + x86_64) · Linux: AppImage + deb + rpm
UNIVERSAL=0 make desktop   # macOS: faster host-arch-only DMG (local iteration)
```

Output lands in **`website/electron/dist/`**:

| Command | Platform | Artifact |
|---------|----------|----------|
| `make desktop` | macOS | `KiroCrew-<version>-universal.dmg` |
| `UNIVERSAL=0 make desktop` | macOS | `KiroCrew-<version>-arm64.dmg` (Apple Silicon host) or `KiroCrew-<version>.dmg` (Intel host) |
| `make desktop` | Linux | `KiroCrew-*.AppImage`, `*.deb`, `*.rpm` (host arch) |

The electron-builder configuration lives in
[`website/electron/package.json`](../../website/electron/package.json):

- **appId:** `dev.kirocrew.desktop`
- **productName:** `KiroCrew`
- macOS display name: `Kiro Crew` via `CFBundleDisplayName`; `CFBundleName`
  remains aligned with `productName` because Electron uses it to locate the
  `KiroCrew Helper` app bundles during startup
- mac target: `dmg` (category `public.app-category.developer-tools`). The DMG
  uses a 660×420 logical-size branded drag-to-Applications background, packaged
  as a multi-resolution TIFF with 660×420 (1×) and 1320×840 (2×) representations
  for Retina displays. The background is a flat light purple carrying the opening
  animation's white ghost cast and wordmark, with a single chevron between the
  96px app and `/Applications` targets. It holds no gradient: the brand guideline
  restricts them, so the accent is one tone. Nothing is painted behind the icon
  captions either — Finder draws them in dark text even under Dark Mode, so they
  read on the accent directly.
- Windows target: assisted NSIS. A 164×314 welcome/finish sidebar and a 150×57
  page header reuse the Kiro Crew logo while preserving native NSIS controls,
  localization, the per-user default, and the no-UAC default path. The installer
  cross-fades the native top-level dialog at page boundaries with Win32's
  alpha-blended window animation, honoring the client-area animation preference.
  It performs no timer-driven bitmap work or `Sleep` on the NSIS UI thread;
  Windows CI installs the real artifact, records its duration, and enforces a
  5-minute ceiling. Auto-updates skip the assisted wizard's decision pages but
  keep its native extraction progress visible, then relaunch Kiro Crew and close
  automatically. A legacy silent `/S --updated` invocation is converted to the
  same visible update path so the transition works from already-fielded clients.
- linux targets: `AppImage`, `deb`, `rpm` (category `Development`). One backend
  tree is packaged three times, with `scripts/stamp-distribution.sh` re-run
  between electron-builder invocations so each artifact's beacon `dist` names
  its OWN format -- a single stamp would label one artifact as another.
- `desktopName` + `linux.syncDesktopName` are what make window association
  work: Electron derives its app_id from `desktopName`, and electron-builder
  derives the `.desktop` file's name and `StartupWMClass` from the same value,
  so the three agree by construction instead of by coincidence. Overriding
  `StartupWMClass` by hand breaks that agreement.
- `deb.depends` declares alternatives (`libgtk-3-0 | libgtk-3-0t64`) because
  Ubuntu 24.04's 64-bit `time_t` transition renamed several libraries;
  `rpm.depends` needs no such thing but uses entirely different names
  (`gtk3`, `nss`, `alsa-lib`). Both lists are verified against a real
  `apt-get install` / `dnf` resolution by `scripts/smoke-linux-packages.sh`.

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
| Linux x86_64 | x86_64 Linux | x86_64 `.AppImage`, `.deb`, `.rpm` |
| Linux aarch64 (Graviton/ARM) | aarch64 Linux | aarch64 `.AppImage`, `.deb`, `.rpm` |

**Both Linux architectures ship.** `build-desktop.yml` builds them on
`ubuntu-22.04` and `ubuntu-22.04-arm`, and `publish-linux.yml` runs once per
arch — each writing its own immutable S3 key, its own electron-updater channel
file (`latest-linux.yml` for x64, `latest-linux-arm64.yml` for arm64) and its own
`latest` alias. Published basenames are `KiroCrew-<arch>.<ext>` for each of the
six (arch, format) pairs -- `KiroCrew-x86_64.deb`, `KiroCrew-aarch64.rpm`, and so
on. A package format also gets its own feed DIRECTORY
(`feed/<channel>/deb/latest-linux.yml`), because electron-updater derives the
channel FILE name from platform and arch with no hook to change it, so two
formats sharing a directory would overwrite each other's metadata.

Two properties are load-bearing and worth knowing before you touch that lane:

- **Linux is built natively per arch, never cross-compiled.** `build-desktop.sh`
  provisions a python-build-standalone interpreter and then *runs* it (pip
  install, plus the `python -m kiro_crew --version` self-containment gate), so a
  host that cannot execute the target architecture cannot build it. macOS gets
  away with one host only because Rosetta 2 executes the x86_64 slice.
- **The runner's glibc is the ceiling on what the artifacts may require.** The
  binaries link against it, so the runner bounds compatibility. The MEASURED
  requirement of the shipped binaries is lower than the runner's own version:
  the highest `GLIBC_*` symbol version across the Electron binary and every
  bundled `.so` is **2.34**, which covers Ubuntu 22.04+, Debian 12+, Fedora,
  CentOS Stream 9 and Amazon Linux 2023, and excludes Ubuntu 20.04, Debian 11
  and Amazon Linux 2. Read the requirement with
  `objdump -T <binary> | grep -oE 'GLIBC_[0-9.]+' | sort -uV | tail -1` rather
  than assuming it equals the runner's glibc. The AppImage links against
  it, which is why both Linux legs stay on 22.04 (glibc 2.35) rather than moving
  to 24.04 (2.39) — the newer floor would exclude AL2023, Debian 12 and RHEL 9.

**Building your own package locally.** `make desktop` needs no arch flags: it
detects the host and emits an AppImage for it, so running it on an ARM box
produces the aarch64 build with no CI involved. Filenames are arch-qualified
(`KiroCrew-<version>-<arch>.AppImage`) so several arches can sit in one directory
without overwriting each other. To validate a packaging change against every
platform *without* publishing anything, dispatch `build-desktop.yml` manually —
it builds the full matrix and uploads artifacts, with no publish lane attached.

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

> **Renaming `backend-dist/` is load-bearing at runtime.** The backend detects
> "am I the bundled interpreter?" via
> `platform_compat.is_bundled_interpreter()`
> (`BUNDLED_BACKEND_DIST_DIRNAME`), which is what stops `pip` from writing
> into the signed bundle during app builds. `test/test_platform_compat.py`
> pins that constant to both `extraResources` here and
> `packaging/build-desktop.sh`, so a rename fails a test — update the constant
> and the packaging layer in the same change.

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
feed) is arch-indifferent: the feed schema is unchanged, `latest-mac.yml`
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

> **npm registry (system-configured):** the `.npmrc` files deliberately do NOT
> pin a registry. `npm ci` inherits whatever registry the machine's `~/.npmrc`
> or environment configures, so mirrors and private registries work for
> builders who cannot reach `https://registry.npmjs.org/`. If your configured
> registry lacks a public package or its auth token expired, fix your registry
> config rather than adding a pin back.

## Build pipeline

`make desktop` runs `bash packaging/build-desktop.sh`, which executes the
pipeline end-to-end:

```
1. Build the React dashboard (npm)                    → website/dist
2. Provision a python-build-standalone interpreter    → via uv python install
3. pip-install kiro_crew + deps into the bundled interpreter
4. Stage the dashboard into the package's static dir
5. Prune caches/tests/unused stdlib to shrink bundle
6. Package with electron-builder                      → website/electron/dist/ (DMG / AppImage / NSIS)
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
   `PYTHONNOUSERSITE=1` to force the full closure into the bundle. The local
   speech recogniser and its runtime dependencies are required on Windows,
   Linux x64/arm64, and macOS Apple Silicon; a missing binary wheel fails the
   release build. macOS Intel is the sole unsupported exception.
4. **Stage dashboard** — copies the built SPA into the bundled
   `kiro_crew/static/dist` inside site-packages.
5. **Prune** — removes `__pycache__`, test dirs, and unused stdlib modules
   (tkinter, idlelib, etc.) to shrink the bundle.
6. **Package** — in `website/electron/`, runs electron-builder to produce the
   installer(s) in `website/electron/dist/`. The macOS DMG and Windows NSIS
   wizard consume the checked-in artwork under `packaging/installer-assets/`.
   The build reads only the committed rasters; edit the SVG sources beside them
   and run `node packaging/installer-assets/build-assets.mjs` to regenerate the
   TIFF and BMPs. That script is the only place that knows the output shapes
   the two installers require — a multi-representation TIFF for Retina, and
   24-bit BMPs, which NSIS cannot read at the 32-bit depth `sips` emits.

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
- **Stdlib probes verified** — `stdlib_probe_gate` fails the build if any package
  the launcher's readiness check probes is missing from the pruned tree, so a
  drifted probe list breaks the build instead of every user's launch (see
  [How the app finds and launches the backend](#how-the-app-finds-and-launches-the-backend)).
- **Self-containment verified** — the build script runs
  `PYTHONNOUSERSITE=1 bin/python3.12 -m kiro_crew --version` to catch any
  missing dependency before packaging.
- **Local dictation runtime bundled** — supported desktop builds include
  `pywhispercpp`, the platform `imageio-ffmpeg` executable used for compressed
  recordings, and all transitive runtime dependencies. The build imports the
  recognizer and executes the exact packaged decoder before publishing — and
  distinguishes a decoder that fails to AUTHENTICATE, which fails the build, from
  one that authenticates but will not run on the build host, which warns and
  ships (see [stt-streaming](../system-specs/features/stt-streaming.md)). Model
  weights are deliberately excluded from the installer: the user selects a
  model and clicks **Download now**, with no package manager or separate
  dependency step. Intel macOS is the unsupported recognizer exception.
  Every bundled executable ships **uncompressed** — the Apple notary service
  decompresses archive members and rejects an unsigned executable found inside
  one, which fails the whole macOS release (see
  [stt-streaming](../system-specs/features/stt-streaming.md) for how the runtime
  then authenticates a decoder whose bytes signing rewrote).
- **Dashboard bundled** — the SPA is staged into
  `lib/python3.12/site-packages/kiro_crew/static/dist/` inside the bundle.
- **Pruned** — `__pycache__`, test dirs, and unused stdlib (tkinter, idlelib,
  turtledemo, ensurepip, lib2to3) are removed to shrink the bundle.

## How the app finds and launches the backend

When the app starts, [`main.js`](../../website/electron/main.js) composes the
desktop lifecycle and delegates gateway ownership to
[`gateway-supervisor.js`](../../website/electron/gateway-supervisor.js). The
supervisor first checks whether a gateway is already running. An existing
gateway—including a local SSH forward to a remote gateway—is reused. Otherwise
it locates the backend binary via
[`find-bin.js`](../../website/electron/find-bin.js), spawns it as `kirocrew
gateway --no-open`, polls `/api/status`, and loads the dashboard once it is
healthy.

Host-runtime discovery stays behind the same main-process ownership boundaries.
The `wsl:detect` handler in
[`ipc-registrar.js`](../../website/electron/ipc-registrar.js) fails closed unless
the sender has the fixed primary origin,
[`window-lifecycle.js`](../../website/electron/window-lifecycle.js) proves that
its window uses a local gateway rather than a configured tunnel, and
[`gateway-supervisor.js`](../../website/electron/gateway-supervisor.js)
positively identifies the primary listener as Kiro Crew or its service. A
manual SSH tunnel, foreign listener, unbound port, or unavailable owner probe is
therefore refused; only then may
[`wsl-detection.js`](../../website/electron/wsl-detection.js) run the trusted
system `wsl.exe` path.

Before spawning a **bundled** backend the shell checks that the bundle's Python
stdlib is fully on disk
([`bundle-integrity.js`](../../website/electron/bundle-integrity.js)). The
Windows NSIS installer extracts `backend-dist/` incrementally and launches the
app as it finishes (`runAfterFinish`), so a launch inside that window finds
`python.exe` present while late-alphabet stdlib packages are not — the
interpreter then dies on `from urllib.parse import …` reached through
`pathlib`, which reads as a corrupt install rather than an unfinished one. The
check probes stdlib packages spread across the alphabet — via each one's
`__init__.py`, since an extractor creates a directory before filling it and a
top-level `.py` file lands with the early batch — and, when any are missing,
reports "still being installed" through the normal gateway-failure dialog, whose
**Retry** succeeds once extraction completes. It stays silent for the legacy
flat layout, which carries no interpreter tree to verify.

That pre-spawn check cannot be complete, and does not pretend to be: extraction
order *within* a package is not the app's to control, so `import zoneinfo` can
still fail moments after `zoneinfo/__init__.py` appears. A second, sound check
backstops it. The two are not redundant — the pre-spawn probe is **preventive but
unsound**, the backstop **sound but after-the-fact**, and each covers what the
other cannot. Refusing before `spawn()` keeps a doomed interpreter from running
module-scope work against the live data home (it creates the home and
`.local_secret`, and writes bytecode caches) and from failing in messier ways than
a clean `ModuleNotFoundError` while extraction is still writing underneath it;
the backstop can only ever explain a crash that already happened.

When a spawn dies on a **stdlib** import, the launch log is read and the failure
reclassified as an unfinished install. Two traceback forms are matched, because a
half-written package does not report the obvious one:

- `ModuleNotFoundError: No module named 'urllib'` — the package (or, for a dotted
  name, a submodule of a package that did land) is absent.
- `ImportError: cannot import name '_tzpath' from partially initialized module
  'zoneinfo'` — the package's `__init__.py` arrived before its siblings. This is
  what CPython actually raises in that case, verified against the shipped
  interpreter, and it is precisely the state the pre-spawn probe cannot see.

Three conditions keep it from excusing anything else. Judgement is by the
**top-level package name**, which must be in the stdlib set, so a missing
third-party or first-party module (a genuine packaging defect) is never relabelled.
Only a **bundled** backend qualifies — a user's own install or a `PATH` `kirocrew`
failing on a stdlib import is a broken environment, and "wait for the installer"
would be misleading advice there. And only the **current launch attempt** is read:
the log is append-only across launches, so the text is sliced from the last spawn
marker (`SPAWN_MARKER`, owned by `bundle-integrity.js` and logged by
`gateway-supervisor.js` so writer and reader cannot drift). Without that, an
older traceback could relabel
this attempt's unrelated failure — a `SIGKILL`, or a bound port whose real remedy
is force-stop rather than a bare Retry — and show a reassuring dialog over a live
fault. When the marker has scrolled out of the tail, attribution is unknowable and
the check declines.

**Why not an installer-written completion sentinel?** It looks like the obviously
sounder mechanism — the installer knows exactly when extraction finished, and
`installer.nsh` could write a marker from `customInstall`. It is rejected because
`nsis.perMachine` is `false` and updates run the new version's installer **over the
existing install directory**: after the first update the tree carries a sentinel
written by the *previous* installer, which cannot be told apart from a valid one
while a newer build is still extracting. That is precisely the reported failure (an
update, not a fresh install), so a naive sentinel would assert "complete" during the
exact race it was added to close. A sound version must be version-scoped, rewritten
atomically per install, and compared against the running app's own version. It would
also be Windows-only — the DMG and the Linux packages have no `customInstall` — so
it is an addition on top of the probe, never a replacement for it.

The build enforces the other direction: **`stdlib_probe_gate`** runs after
pruning in both backend build paths and **fails the build** if any probed package
is absent from the tree just built. A probe list that drifts from the shipped
stdlib (a Python bump turning a package back into a module, a rename, or a new
prune) would otherwise refuse *every* launch of a healthy app — a permanent
failure worse than the transient one the gate prevents. Like `resolver_gate` it
needs `node`, and logs a visible SKIP rather than failing when none is on PATH,
so a `node`-less build environment still produces a bundle (unvalidated).

The gateway-hosted dashboard then checks both prerequisites needed by the ACP
provider:

1. It discovers `kiro-cli` in the inherited `PATH`, `~/.local/bin`,
   `~/.cargo/bin`, Homebrew locations, or the macOS `Kiro CLI.app` bundle.
2. It verifies the first candidate selected by the shared ACP resolver with
   `kiro-cli --version`. A broken or untrusted higher-priority candidate blocks
   readiness instead of approving a later binary that ACP would not launch.
3. It verifies authentication with `kiro-cli whoami`.

If either check fails, the shared React setup gate appears in both the desktop
shell and browser dashboard. Kiro Crew performs neither setup step: the gate
links out to <https://kiro.dev/cli/> to obtain the CLI, and names the commands
the user runs to sign in — `kiro-cli login` for a personal account, or
`kiro-cli login --use-device-flow --license pro` for organization SSO. Both
tiers are shown because the browser portal the bare command opens offers a free
Builder ID alongside organization SSO, so an SSO user who picks the wrong one
authenticates successfully and only discovers the mismatch later as models
missing from their account. The gate's only control is **Check again**, which
re-probes the host; it opens the dashboard once `kiro-cli whoami` succeeds.
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

### Native window chrome

The dashboard's 42px top bar is also the window titlebar on macOS and Windows.
macOS insets the native traffic lights on the left. Windows uses Electron's
title-bar overlay to retain native minimize/maximize/close controls on the right.
The application menu rests as a compact hamburger on the left. Opening it shows
the File submenu and expands File/Edit/View/Connection/Window/Help inline;
hovering another label replaces the submenu without ending the menu session.
Escape, an outside click, selecting a command, or moving focus to another window
closes the popup and collapses the labels back to the hamburger. The menu surface
uses the dashboard theme because native Windows popups capture window input and
cannot support hover switching; a narrow IPC bridge keeps command execution and
standard Electron roles in the main process.
When a remote instance is connected, the instance switcher shares the same bounded
left region as the menu: it is a single trigger naming the instance on screen (see
InstanceTabBar's SwitcherMenu), not a row of per-instance tabs, so it costs constant
width whether the menu is collapsed to a hamburger or expanded to full labels.
The centered command palette yields that region rather than the reverse — the
correct priority while the menu is open is labels > instance status > an idle
search affordance — and the palette remains reachable through its keyboard
shortcut even while hidden.
The command-palette trigger is positioned from the window midpoint rather than
the remaining flex space, so asymmetric menu and status controls do not shift it.
Linux retains the window manager's native frame and menu bar.

#### Focus mode: verify these seams after an Electron or Radix bump

Focus mode (hide the shell chrome behind hover) rests on three mechanisms that
key on behavior no API contract guarantees, and each fails **silently** — the
unit tests mock these seams, so a broken one still passes CI and only manual
macOS testing catches it. Run this short checklist whenever you bump Electron or
Radix (`website/electron/package.json`, `@radix-ui/*` in `website/package.json`):

1. **Toggle focus mode, then drag the revealed header to move the window.**
   Exercises the drag-region re-send in
   [`website/electron/focus-chrome.js`](../../website/electron/focus-chrome.js):
   Electron's `setWindowButtonVisibility` mutates the window styleMask and drops
   the renderer's declared `-webkit-app-region:drag` regions, so the renderer
   re-declares them by briefly adding a 1px drag element. If a bump changes when
   Chromium re-sends the region set, the revealed header selects text instead of
   moving the window.
2. **Peek the header, then move the pointer down into the content.** The header
   should close. Peek the rail, then move the pointer right past the rail track —
   it should close too. Exercises the **positional** close in
   [`website/src/App.tsx`](../../website/src/App.tsx) (`departWhen: clientY > 48`
   for the top peek, `clientX > 248` for the rail): the revealed header doubles
   as the drag surface and a drag region eats pointer events before hit-testing,
   so the close is driven by pointer position, not by `mouseleave`. If a bump
   changes hover/pointer-event delivery, the peek sticks open or never opens.
3. **Peek the header, then open the instance switcher.** The header must stay on
   screen while the switcher menu is open. Exercises the header-pin heuristic in
   [`website/src/App.tsx`](../../website/src/App.tsx): Radix portals the menu to
   `document.body`, so the pin rides on a `[aria-haspopup][aria-expanded="true"]`
   query against the header rather than DOM containment. If a Radix bump changes
   the ARIA a trigger emits (`aria-haspopup` absent, or `aria-expanded="true"`
   emitted by default with nothing open), the header either slides away under the
   open menu or pins permanently from first paint.

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

### `gateway-supervisor.js` — owning the gateway lifecycle

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
- On every desktop platform, pins `PYTHONUTF8=1` and
  `PYTHONIOENCODING=utf-8:backslashreplace` at the Electron-to-Gateway spawn
  boundary. This applies before CPython constructs redirected stdout/stderr and
  is inherited by the Gateway's `os.execv` successor plus its MCP/session
  children. Consequently the initial launch, Tailnet/explicit restart, update
  and stale-asset re-exec, and Electron liveness respawn all use the same UTF-8
  contract instead of falling back to the Windows ANSI code page or an
  incompatible inherited POSIX encoding override.
- Leaves the inherited child `PATH` unchanged. The gateway prerequisite service
  probes supported Kiro CLI locations independently — including the Windows
  per-user install at `%LOCALAPPDATA%\Kiro-Cli` — so desktop launches find
  user-local installations without mutating the shell environment or requiring
  the already-running gateway to inherit an installer-updated `PATH`.
- [`window-lifecycle.js`](../../website/electron/window-lifecycle.js) hides the
  app to the tray on window close; the composition root delegates quit-time
  gateway teardown to the supervisor, which performs the graceful shutdown and
  signal escalation contract.

## Code signing & notarization (macOS)

An unsigned `.app`/DMG is quarantined by Gatekeeper and shows **"Kiro Crew is
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

For release builds, the unsigned Electron-built DMG is retained only as a
layout template. `packaging/signing/build-dmg.sh` converts it to a writable
image, verifies that its app name matches the signed/stapled app, replaces that
one bundle, shrinks and recompresses the image, and then the release workflow
signs and notarizes the resulting DMG. Recreating the image from a plain folder
would discard Finder's volume-bound background reference.

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

## macOS folder-access (TCC) prompts

macOS gates `~/Downloads`, `~/Documents`, `~/Desktop`, `~/Pictures`, `~/Movies`
and `~/Music` behind **TCC** (Transparency, Consent and Control). The first time
an app reads one of them, macOS shows a modal *"Kiro Crew would like to access
files in your Downloads folder"*, and consent is recorded **per (app, folder)
pair** — so an operation that incidentally touches three of those folders
produces **three separate prompts**, one after another.

Nothing Kiro Crew does at startup needs those folders. They were only ever
reached *incidentally*, by the `@`-mention file picker's filesystem walk when it
fell back to bare `$HOME` as a catch-all search root (no project selected). That
single unscoped walk descended into `Downloads`/`Documents`/`Desktop` and
tripped one prompt each.

Those walks now prune the TCC-protected folders when — and only when — the walk
root is `$HOME` itself
(`platform_compat.tcc_protected_dirs_for_walk`, applied in
`dashboard/file_index.py` and the `/api/file-search` fallback). Two consequences
worth knowing:

- **Explicit access is unaffected.** If you point Kiro Crew at a project inside
  `~/Documents`, browse to `~/Downloads` directly, or even name `$HOME` itself as
  the project, the root is scoped by definition and is walked in full — only the
  *unscoped* `$HOME` fallback prunes. macOS still shows its own one-time prompt
  for that deliberate access — that is the expected OS contract, and granting it
  once is enough.
- **Pre-declaring usage strings would not have fixed this.** Adding
  `NSDocumentsFolderUsageDescription` and friends to `Info.plist` only changes
  the *wording* of each prompt; it does not reduce the count. Not reading the
  folders is what removes the prompts.

A signed, stable bundle identity matters here too: TCC keys consent off the
app's code-signing identity, so an ad-hoc/unsigned local build can be treated as
a *different* app after a rebuild and re-prompt for grants you already gave.
Distributing the signed + notarized DMG (above) keeps grants sticky across
updates.

### Device resources (microphone) need an ENTITLEMENT, not just a usage string

Folder access above needs only consent. A **device** resource is different: under
the hardened runtime the capability is granted by a `com.apple.security.device.*`
entitlement, and the `Info.plist` usage string only supplies the prompt's
wording. Get this wrong and the failure is deeply misleading:

> **Symptom:** voice input reports *"Microphone permission denied"* instantly,
> **no** system prompt ever appears, and there is no Kiro Crew row under System
> Settings › Privacy & Security › Microphone to switch on. The same mic works in
> Chrome at the same origin on the same machine.

Because under the hardened runtime the microphone requires
`com.apple.security.device.audio-input` **in addition to** the usage string —
without it access is refused and no prompt appears, so there is nothing to
consent to and nothing to toggle. The entitlement is a Hardened Runtime
*Resource Access* capability (Xcode's "Audio Input" checkbox), **not** an
App-Sandbox-only key: this app is not sandboxed, and neither are Chrome, Slack
or Zoom — all three are hardened-runtime, non-sandboxed, and all three ship
audio-input. The usage string is not a substitute; both are load-bearing.

It is worth being precise about *where* the capability lives, because the
intuitive answer is wrong: in Chromium the audio capture runs in the **browser
(main) process** — the renderer only requests it over IPC — and TCC attributes
access to the responsible main bundle. Chrome's and Slack's *Renderer* helper
apps carry no audio-input entitlement at all, and their microphones work. So the
main bundle's `entitlements` is what matters; `entitlementsInherit` is set to the
same file so helpers keep their JIT/library-validation keys.

Two things follow, and both are pinned by `website/electron/test/packaging.test.js`:

- **There are TWO signing lanes reading TWO different files.** electron-builder
  signs local/dev builds with `website/electron/build/entitlements.mac.plist`;
  the release lane signs with `packaging/signing/Entitlements.entitlements`. An
  entitlement added to one and not the other ships a **broken bundle on the other
  lane** — keep them in sync.
- **The camera is deliberately absent.** `permission-handler.js` denies any
  request that explicitly asks for video, so requesting the camera entitlement
  would widen the TCC surface for a capability the app never uses.

The prompt is also **one-shot**: once a user denies the mic, macOS never asks
again. So `permission-handler.js` consults
`getMediaAccessStatus('microphone')` on each request and branches —
`not-determined` asks in-context (right when the user clicks the mic, rather than
spending the single prompt at launch on an unrelated moment), while
`denied`/`restricted` opens the Privacy pane via `showMicPermissionDialog()`,
since the OS will not re-prompt on its own. Every failure mode in that probe
fails **open**, so diagnosing permissions can never itself be what breaks the mic.
The sinks (breadcrumb log, recovery dialog) are deliberately kept off the
answer path: an earlier revision had them inside the promise chain upstream of a
fail-open `.catch`, so a throwing logger turned a user's explicit **refusal into
a grant**. Auditing must never be able to change a permission verdict.

#### Developer gotcha: a stale TCC row survives a fix

TCC rows are pinned to the app's **code-signing identity (cdhash)**, not just its
bundle id — and ad-hoc local builds share one collapsed `Identifier=Electron`
identity. So a machine that ran a dev build can hold a Microphone row for
`com.amazon.kiro.crew` whose `csreq` matches a cdhash the Developer-ID release
can never satisfy. The row reads *granted* in the TCC database and is still never
honored, which looks exactly like the entitlement bug and survives fixing it.

If the mic still fails after a rebuild, clear the row and let the app re-prompt:

```bash
tccutil reset Microphone com.amazon.kiro.crew
```

This is also why distributing the signed + notarized DMG matters (above): a
stable identity is what keeps grants sticky instead of silently orphaning them.

### Local network access needs a USAGE STRING, and no entitlement exists for it

macOS 15 (Sequoia) added local-network privacy for **every** app, sandboxed or
not. The mic's lesson does not transfer: there is no `device.*` entitlement to
add here, and adding one of the neighbouring network keys makes things worse.
This resource is TCC-only, and `NSLocalNetworkUsageDescription` is the entire
declaration.

> **Symptom:** an agent's shell command connects fine to the default gateway
> (`192.168.x.1`) and to any public host, but every **other** LAN address — a NAS,
> an IoT device, another dev box — fails **instantly** with errno 65
> (`EHOSTUNREACH`, "No route to host") in ~0.000s rather than timing out. `ping`
> and ARP to the same host succeed, so it reads as a routing fault. There is no
> Kiro Crew row under System Settings › Privacy & Security › Local Network, and
> `tccutil reset LocalNetwork com.amazon.kiro.crew` fails because no TCC record
> exists to reset.

The gateway-works / everything-else-fails split is the signature of the TCC gate,
not of the network. With no declared intent macOS creates no
`kTCCServiceLocalNetwork` record, so there is no prompt to answer and no toggle to
flip — the same dead end as the mic, reached by a different mechanism.

Three neighbouring keys look like the fix and are **not**:

- `com.apple.developer.networking.multicast` covers multicast and broadcast,
  requires an Apple-granted provisioning profile, and breaks signing when
  requested unprovisioned. Plain unicast LAN access does not need it.
- `com.apple.security.network.client` only means anything under **App Sandbox**,
  which this bundle does not use.
- `NSAllowsLocalNetworking` (which the bundle already carries) is an **App
  Transport Security** key that relaxes HTTPS requirements for local hostnames.
  It has nothing to do with the TCC gate — an easy one to mistake for a fix,
  since it is already present in a bundle that cannot reach the LAN.

`website/electron/test/packaging.test.js` pins both directions: the usage string
must be declared with real copy, and neither entitlement may appear in either
signing lane.

#### Why the CLI gateway is not affected the same way

Apple exempts several launch contexts from local-network privacy: daemons started
by `launchd`, anything running as root, and **command-line tools run from Terminal
or over SSH, including every child process they spawn**. So a gateway started with
`kirocrew gateway` from a terminal reaches the LAN normally, while the same agent
command run under the desktop app is gated by the app bundle's TCC record. That
asymmetry is a useful triage question ("how did you start the gateway?") and a
usable workaround, not evidence that the app is fine.

One caveat worth knowing before concluding the usage string alone fixed it: agent
shell commands are wrapped by `sandbox_exec_argv` in `src/kiro_crew/sandbox.py`,
which `exec`s the target through `/usr/bin/sandbox-exec` and replaces the process
image. The Seatbelt profile itself is `(allow default)` plus filesystem denies and
carries **no** network rules, so the sandbox does not block sockets — but whether
TCC's responsible-process attribution still lands on the app bundle across that
`exec` has to be confirmed on a real macOS 15 host rather than reasoned about.

## Externally-managed installs (repackagers)

A distro or enterprise packager that redistributes the desktop app through its
own package manager owns the install's update lifecycle: the package manager
replaces the whole install, so the built-in auto-updater would fight it (each
overwriting the other's bytes) and its feed check would compare against
releases the packager never ships.

Such a packager opts out by dropping an `EXTERNALLY-MANAGED` marker file
(named after the PEP 668 precedent) into the packaged resources directory —
the same outside-asar surface that carries `package-type` and `backend-dist`
(`Contents/Resources/` on macOS, `resources/` on Linux and Windows). Its
presence alone disables the updater: the feed is never contacted, and
Settings → About hides the release-channel switcher (the lanes it offers are
ones the packager never reads). The body is optional JSON metadata for the
About panel:

```json
{
  "managedBy": "your package manager's name",
  "updateCommand": "the command users run to update"
}
```

`managedBy` names the owning system in the "updates are managed by …"
message; `updateCommand` renders as a copyable command. An empty or
unparsable body still counts as managed — an operator who dropped the file
gets the safe behavior even when the metadata is wrong.

The body is only read when the marker's **provenance** can be established:
neither the marker nor its directory may be owned by the account the app runs
as, and neither may be group- or world-writable. Ownership rather than current
mode bits, because a POSIX owner can always `chmod +w` back — a marker the app's
own user owns is one a prompt-injected agent shell could have planted and then
made read-only. `updateCommand`/`checkCommand` are executed through a shell on
the managed auto-update path, so a marker in a user-owned resources directory
(Homebrew, `pip --user`, `~/Applications`) is treated as a bare marker: managed,
updater off, no metadata and nothing to run. Packagers that want the managed
commands honored must install the resources directory root-owned.

The commands run with a **constructed environment**, not the app's own. Only an explicit pass-through set reaches them — `USER`, `LOGNAME`, `TZ`, `TMPDIR`, the `LANG`/`LC_*` locale vars, and the proxy vars — plus a narrowed system-only `PATH` and `cwd=/`. `HOME` is deliberately excluded: Python derives its user-site directory from it, so passing it through would let a planted `sitecustomize.py` run on every `python` start. Everything else is absent by construction, because `shell: true` means a shell interprets the command and a shell reads its environment as code: the loader family (`LD_*`/`DYLD_*`), the interpreter family (`PYTHON*`, `NODE_OPTIONS`), the startup files (`BASH_ENV`, `ENV`), the tracing pair (`SHELLOPTS` plus a command-substituting `PS4`), word splitting (`IFS`), and exported shell functions (`BASH_FUNC_*`, which shadow a command name outright). A packager whose updater needs any other variable must set it inside its own command rather than relying on inheritance.

**On Windows the marker's commands are never honored.** There is no POSIX owner
to read and `access(W_OK)` does not model ACLs, so no honest provenance verdict
exists; the check fails closed by declaration and every Windows marker is
treated as bare (managed, updater off). A Windows packager drives updates with
its own installer, not through this marker.

For local testing, the `KIROCREW_EXTERNALLY_MANAGED` env var points at a marker
file (any other non-empty value marks the install managed with no metadata).
It is honored on unpackaged builds only — a packaged app ignores it, because
its launch environment is user-writable.

The gateway has the matching seam for its own surfaces: an operator's
`security_policy.json` `updates` block (`check_command` / `apply_command`)
routes the dashboard's update check, badge, and Update button through the
declared commands, and the gateway then reports no release channel at all.
The `check_command` runs on every check — the 12-hourly background poll AND
the manual Check button — so it must be side-effect-free and idempotent.

## Remote tunnel mode

The desktop app can also connect to a gateway running on a **remote** host (e.g.
an always-on server) over an SSH tunnel, fetching a fresh token via
`ssh <host> kirocrew token` on each launch instead of starting a local backend.
See [`website/electron/README.md`](../../website/electron/README.md) and
[remote-desktop-setup.md](../guides/remote-and-mobile.md) for setup.

## See also

- [install.md](../guides/install.md) — all three build/run methods and the build targets
- [README](../README.md) — project overview and Quick Start
