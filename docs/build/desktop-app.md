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
make desktop               # macOS: universal DMG + update ZIP · Linux: AppImage + deb + rpm
UNIVERSAL=0 make desktop   # macOS: faster host-arch-only DMG + ZIP (local iteration)
UNIVERSAL=0 TARGET_ARCH=x86_64 make desktop   # macOS: single-arch DMG for a NAMED arch (arm64 | x86_64)
```

Output lands in **`website/electron/dist/`**:

| Command | Platform | Artifact |
|---------|----------|----------|
| `make desktop` | macOS | `KiroCrew-<version>-universal.dmg` plus `KiroCrew-<version>-universal-mac.zip` |
| `UNIVERSAL=0 make desktop` | macOS | Host-arch DMG plus the matching `*-mac.zip` update archive |
| `UNIVERSAL=0 TARGET_ARCH=<arch> make desktop` | macOS | `KiroCrew-<version>-arm64.dmg` / `KiroCrew-<version>-x64.dmg` plus `KiroCrew-<version>-<arch>-mac.zip` — the arch is always spelled, x64 included |
| `make desktop` | Linux | `KiroCrew-*.AppImage`, `*.deb`, `*.rpm` (host arch) |

The electron-builder configuration lives in
[`website/electron/package.json`](../../website/electron/package.json):

- **appId:** `com.amazon.kiro.crew`
- **productName:** `KiroCrew`
- macOS display name: `Kiro Crew` via `CFBundleDisplayName`; `CFBundleName`
  remains aligned with `productName` because Electron uses it to locate the
  `KiroCrew Helper` app bundles during startup
- mac targets: `dmg` and `zip` (category
  `public.app-category.developer-tools`). The DMG
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
- `build.files` is an explicit per-file allowlist, not a glob: electron-builder
  packs exactly those paths into `app.asar` at the same relative location, plus
  `package.json` and the production `node_modules`. The four lifecycle facades
  (`gateway-supervisor.js`, `window-lifecycle.js`, `auto-update.js`,
  `crash-collector.js`) sit beside `main.js`, and the owners they compose sit
  under `runtime/gateway/`, `runtime/window/`, `runtime/update/` and
  `runtime/crash/`, each listed one by one. The packaging closure is the set of
  files reachable from `main.js` through double-quoted relative `require()`s.
  `website/electron/test/shell-contract.test.js` checks every shipped source's
  relative requires against the allowlist and every stale entry, and
  `website/electron/test/packaging.test.js` walks the closure from `main.js`
  and fails on a single-quoted or template-literal relative require, which
  those scans cannot read, or on a runtime owner no facade composes.
  Runtime owners resolve no path from their own directory; the Electron directory
  (`loading.html`, `preload.js`, the icons, the baked `EXTERNALLY-MANAGED`
  marker) is always the facade's. Which facade owns which module is mapped in
  [`website/electron/README.md`](../../website/electron/README.md#main-process-owners).

### macOS default — one universal DMG for both arches

On macOS, `make desktop` produces a `KiroCrew-<version>-universal.dmg` for
first install and a matching `KiroCrew-<version>-universal-mac.zip` for the
update/signing handoff. Both run **natively** on Apple Silicon and Intel Macs.
It needs only
**one Apple-Silicon machine** — no Intel host, no second build. (It requires
an Apple-Silicon host with Rosetta 2; the script fails fast with instructions
otherwise, and `UNIVERSAL=0` is the opt-out.)

### Bundled kiro-cli — the app carries its own agent runtime

By default (`BUNDLE_KIRO_CLI=1`), the build stages a pinned, sha256-verified
kiro-cli into the app's resources at `backend-dist/kiro-cli/`. On macOS and
Linux, the staged payload is the single `kiro-cli-chat` binary, not upstream's
layout: the `kiro-cli` launcher resolves `kiro-cli-chat` through `$HOME/.local/bin` and
`PATH` and never through its own directory, so a bundle entered through the
launcher would silently run whatever copy the user has installed (or fail on a
clean machine) while still answering `--version` and `whoami` itself.
`kiro-cli-chat` is the process every session is anyway and carries every
subcommand the app uses. The Windows build administratively extracts the
upstream MSI without installing it or writing PATH/registry state, then stages
`kiro-cli.exe` plus the four Visual C++ runtime DLLs it imports
(`vcruntime140.dll`, `vcruntime140_1.dll`, `msvcp140.dll`, `msvcp140_1.dll`),
copied from the build host's `System32`; a build host without the Visual C++
2015-2022 x64 redistributable fails the build with that instruction. DirectML
(`directml.dll`) is left to the host, where desktop Windows supplies it.
`kiro_cli.bundled_kiro_cli_entry` owns
the platform split. The release is pinned in two files that travel together:
`packaging/kiro-cli-version`
names the version and `packaging/kiro-cli-sha256` holds the sha256 of each
artifact the build stages (the universal macOS DMG, the two Linux gnu zips, and
the Windows x64 MSI) in
`sha256sum` format keyed by the artifact's release path, `<version>/<file>`.
Upstream hosts every release under that prefix but publishes a manifest — the
only document naming sha256s — for `latest` alone, so a pinned build fetches the
pinned version's own artifact URL and verifies it against the committed sha: it
never reads the mutable manifest, a hotfix rebuild of an older tag keeps working
after upstream releases, and a version bump without matching sha lines fails
closed with the bump procedure in the error. `build-desktop.yml` passes the pin
explicitly so the lane log names the release it bundled; `KIRO_CLI_VERSION=latest`
resolves the manifest instead and takes the version and sha it names, for a local
build that wants the newest release (still sha256-verified). Bumping both files,
after testing the app against the new release, is the whole procedure for
shipping a newer kiro-cli ([release](release.md), step 1). On macOS the binary is
extracted from the universal `Kiro CLI.dmg` (one Mach-O serves both arches; the
DMG download is ~345 MB for kiro-cli 2.27); Linux uses the matching per-arch zip
(~150 MB); Windows uses the x64 MSI (~250 MB). Downloads are cached per user
under `~/.cache/kirocrew-build/kiro-cli`,
so a rebuild against the same pin fetches nothing. A `BUNDLED-VERSION` file
beside the payload records provenance; a clean-room smoke — empty `HOME`, minimal
`PATH`, so no install on the build host can answer for the staged binary — runs
three probes: `--version`, `login --help`, and one ACP `initialize` round trip
over stdio, the call every
Kiro Crew session opens with, so the lane log on each platform proves the staged
payload is self-contained there before it is sealed into the app; and a layout
gate refuses a payload that carries a nested `.app`/`.framework` under
`Resources/`, which the signing manifest cannot seal per file.

At runtime the Electron shell (`gateway-env.js`, `bundledKiroCliEnvironment`)
exports the directory as `KIROCREW_BUNDLED_KIRO_DIR` when it spawns the
gateway, and only when the directory shipped; the backend resolver
(`kiro_cli.known_kiro_cli_dirs`) ranks it **above** any system install (the app
was built against that exact version) but **below** the `KIROCREW_KIRO_BIN`
operator override. That override is the escape when the pinned release must be
swapped without waiting for an app update, and a Finder- or Dock-launched app
does not read the user's shell profile, so it is set the way
[macos-troubleshooting](../guides/macos-troubleshooting.md) sets `PATH` for the
app: `launchctl setenv KIROCREW_KIRO_BIN /absolute/path/to/kiro-cli`, then
relaunch the app (the Electron shell spawns the gateway with its own
environment, `main.js`, so a launchd-session variable reaches the resolver); on
Linux the equivalent is `systemctl --user set-environment` for a
desktop-session launcher. The same ranking feeds the pinned off-`PATH` spawns
(`pin_kiro_cli`), so the version check, the readiness probe and every ACP
session all run the bundled copy. The shell hands the directory over only after
the staged entry has answered `--version` on the user's machine (one bounded
`spawnSync` in `gateway-env.js`): a copy that is present but does not run there
-- a glibc below the binary's floor, a quarantine flag, a truncated payload --
is logged and NOT exported, so discovery falls through to the user's own
kiro-cli exactly as an unbundled build does instead of every session failing
on a binary the user never chose. Beside the directory, the shell sets
`KIRO_NO_AUTO_UPDATE=1` once for the whole gateway process tree, so every child
that runs the bundled copy — ACP sessions, the model listing, `whoami`, the
usage scrape, `kirocrew doctor`, the readiness probes — inherits kiro-cli's
documented switch for its startup update check by construction, rather than
each spawn site remembering to merge it. Upstream compiles that check for
Windows (the upstream chat-cli crate's `cli/mod.rs` at v2.24.0 gates the
whole block on `target_os = "windows"`; upstream's auto-update guide documents
the variable), so it stops the bundled Windows copy from self-updating. It also
guards upstream's stated "FUTURE: re-enable for all platforms", which would
otherwise write into the signed, sealed bundle. Accepted side effect: a system
kiro-cli an operator forces through `KIROCREW_KIRO_BIN` also skips the check
while running as a child of the app (its own terminal use is unaffected), and
the user's `app.disableAutoupdates` setting is never written. The setup gate
serves the copy's quoted absolute path as the click-to-copy sign-in command (it
is not on the user's shell `PATH`) with a one-line hint that the path is the
app's built-in kiro-cli (`bundled_cli` in the status payload). On Windows that
command also sets `KIRO_NO_AUTO_UPDATE` for itself (`Set-Item Env:…` inside the
`powershell.exe -Command` string, which an interactive PowerShell would
interpolate away if it were `$env:`): the user's terminal is outside the
gateway tree, and it is the Windows copy that compiles the self-update. The
gate refuses the
in-place **Update** (and the gateway auto-update's `kiro-cli update`
step, and `kirocrew update`'s) because the copy is replaced by the next app
update. `kiro-cli login` is still the user's own step — bundling covers the
binary, never the credential. The Windows install smoke (`scripts/smoke-windows-install.ps1`) then checks the INSTALLED tree: the staged `kiro-cli.exe` runs from where the installer put it and reports the pinned version, and the installed `kirocrew doctor` resolves that copy when `KIROCREW_BUNDLED_KIRO_DIR` names its directory. The macOS install smoke (`scripts/smoke-macos-install.sh`, job `smoke-install-macos` in `build-desktop.yml`) mounts the unsigned DMG, copies the app out, runs the installed launcher and the installed `kiro-cli-chat`, then launches the REAL app and reads the gateway child's environment back: `KIROCREW_BUNDLED_KIRO_DIR` and `KIRO_NO_AUTO_UPDATE=1` present, no probe fall-through line in `gateway-launch.log`, and the installed doctor resolving the bundled copy. That launch is the only place the shell's hand-off runs against a shipped bundle.

The payload adds one Mach-O of a few hundred MB to the macOS signing zip (about
1 GB in total), which is what `packaging/signing/sign.sh`'s poll window is sized
for.

`BUNDLE_KIRO_CLI=0 make desktop` opts a build out (the payload is large);
the app then detects a system kiro-cli exactly as before. The Windows installer
grows by about 190 MB before outer installer compression.

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
| macOS arm64 (Apple Silicon) | Apple Silicon Mac (`UNIVERSAL=0`) | arm64 `.dmg` + matching `*-mac.zip` |
| macOS x86_64 (Intel) | Intel Mac, **or** an Apple Silicon Mac with Rosetta 2 (`UNIVERSAL=0 TARGET_ARCH=x86_64`) | x86_64 `.dmg` + matching `*-mac.zip` |
| Linux x86_64 | x86_64 Linux | x86_64 `.AppImage`, `.deb`, `.rpm` |
| Linux aarch64 (Graviton/ARM) | aarch64 Linux | aarch64 `.AppImage`, `.deb`, `.rpm` |

**Naming an arch instead of taking the host's.** `TARGET_ARCH=arm64|x86_64`
(macOS, `UNIVERSAL=0` only) makes a single-arch build *for* that arch: the
script provisions that arch's python-build-standalone interpreter (x86_64
runs under Rosetta 2 on Apple Silicon, exactly as the universal build's
x86_64 half does), arch-gates the bundled backend with `file`, passes
`--arm64` / `--x64` to electron-builder, and post-gates the shell binary with
`lipo -archs` so a host-arch shell can never land in an x86_64-labelled DMG.
The artifact always spells its arch (`-arm64` / `-x64`), including for x64,
where electron-builder's default pattern would otherwise drop it; the ZIP
keeps electron-builder's `-mac.zip` suffix. `scripts/emit-symbols-manifest.mjs`
reads the same variable so the symbols pin records the arch actually built.
`TARGET_ARCH=arm64` needs an Apple Silicon host (an Intel Mac cannot run the
arm64 backend it would have to gate); any other value, any use outside
macOS, or setting it while `UNIVERSAL=1` is in effect, is refused.

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
  install, plus the `python -m kiro_crew --version` self-containment gate and
  its companion `import kiro_crew.cli` chain probe — bare `--version` answers
  before the heavy imports, so the probe carries the gate's meaning), so a
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
  The bundled kiro-cli 2.27.1 stays inside that floor: `kiro-cli-chat` requires
  `GLIBC_2.34` on x86_64 and `GLIBC_2.30` on aarch64, so it does not narrow the
  supported distro set. Re-measure both pinned zips when updating the CLI pin.

**Building your own package locally.** `make desktop` needs no arch flags: it
detects the host and emits an AppImage for it, so running it on an ARM box
produces the aarch64 build with no CI involved. Filenames are arch-qualified
(`KiroCrew-<version>-<arch>.AppImage`) so several arches can sit in one directory
without overwriting each other. To validate a packaging change against every
platform *without* publishing anything, dispatch `build-desktop.yml` manually —
it builds the full matrix and uploads artifacts, with no publish lane attached.

A **local** `UNIVERSAL=0` host-arch build is a local-machine artifact: distribute
the universal DMG, or the CI-built signed single-arch DMGs below
(`KiroCrew-arm64.dmg` / `KiroCrew-x64.dmg`), which nightly and both release
channels publish.

**Single-arch macOS DMGs in CI (opt-in).** `build-desktop.yml` has a second
macOS job, `build-desktop-mac-single-arch`, behind the boolean input
`mac_single_arch` (default `false`; `nightly.yml` and `release.yml` both pass
`true`). When on, it runs the script twice on `macos-15` —
`UNIVERSAL=0 TARGET_ARCH=arm64` and
`UNIVERSAL=0 TARGET_ARCH=x86_64` — and uploads `unsigned-build-darwin-arm64`
and `unsigned-build-darwin-x64` beside `unsigned-build-darwin-universal`. The
job is `continue-on-error` only when the caller passes
`soft_fail_mac_single_arch: true`, which `nightly.yml` does (a failed
single-arch build never holds the universal signing or the Linux publishers
there). `release.yml` keeps the default: both DMGs are required promotion-bundle
roles, so a failed build fails the aggregate before any publisher writes a key.

On nightly and on both release channels each single-arch artifact is then
signed, notarized and published by its own call of `sign-and-notarize.yml`
(`mac_variant: arm64 | x64` plus
`mac_artifact`), which suffixes every shared name with the arch: signing-bucket
keys, `desktop/<channel>/<version>/KiroCrew-<arch>.{zip,dmg}`, the alias
`desktop/<channel>/latest/KiroCrew-<arch>.dmg`, and the channel file at
`feed/<channel>/<arch>/latest-mac.yml`. The universal call passes neither input
and still excludes the single-arch artifact names when it flattens the run, so
its keys and `feed/<channel>/latest-mac.yml` are byte-identical to before. A
single-arch app knows which feed to follow from `desktopDistArch` in its own
`package.json` (`-c.extraMetadata.desktopDistArch=<arch>`, stamped by the
script on `TARGET_ARCH` builds only): `auto-update.js` resolves that directory
as the feed `variant` and offers `KiroCrew-<arch>.dmg` as the reinstall link;
the universal app carries no stamp and keeps the channel root. On `release.yml`
the two single-arch callers are required lanes: the stable promotion bundle
carries all three zip/DMG pairs (the single-arch zip travels as
`notarized-<arch>.zip` so the flat bundle can hold it beside the universal
`notarized.zip`), a byte promotion republishes all three, and the GitHub
Release page lists `KiroCrew-<v>-{universal,arm64,x64}.dmg`. To get the two
DMGs unsigned from any ref, dispatch `build-desktop.yml` manually with the box
ticked, or pick them up from the nightly run.

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
roughly **2× the size** of a per-arch DMG. That is the price of one artifact
for both arches. The universal app follows the channel's default feed,
`feed/<channel>/latest-mac.yml`; the single-arch DMGs above follow their own
`feed/<channel>/<arch>/latest-mac.yml`.

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

**CI:** the `macos-15` (Apple Silicon) entry in `build-desktop.yml` runs
`make desktop` (universal by default on macOS — GitHub's arm64 macOS runners
include Rosetta 2)
and uploads a single `unsigned-build-darwin-universal` artifact. Everything
downstream (codesigning both slices, notarization, stapling, the update
feed) is arch-indifferent: `feed/<channel>/latest-mac.yml` points at the one
universal zip, and installed arm64 and Intel apps on the universal build
auto-update onto it. No Intel runner is needed; the single-arch DMGs come from
the separate `build-desktop-mac-single-arch` job and their per-arch feeds.

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
6. Package with electron-builder                      → website/electron/dist/ (DMG/ZIP, AppImage/deb/rpm, or NSIS)
```

On macOS (universal by default) the pipeline repeats steps 2–5 once per
architecture — natively into `kirocrew-backend-arm64/`, then with an x86_64
PBS interpreter under Rosetta into `kirocrew-backend-x64/` — and step 6
packages with `electron-builder --mac --universal`. With `UNIVERSAL=0` (and
always on Linux) steps 2–5 run once for the host arch into the unsuffixed
`kirocrew-backend/`.

Step by step:

1. **Frontend** — in `website/`, runs `npm ci` (or `npm install`) + `npm run
   build` into `website/dist`; it does not write `src/kiro_crew/static/dist`
   (step 4 stages `website/dist` into the bundle). The script aborts if
   `website/dist/index.html` is missing.
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
| `TARGET_ARCH=arm64` / `TARGET_ARCH=x86_64` | macOS, with `UNIVERSAL=0`: build the single-arch app for the NAMED arch rather than the host's (x86_64 on Apple Silicon runs under Rosetta 2). Arch-gates the backend and the shell; the artifact always carries `-arm64` / `-x64`. Refused outside macOS or with any other value |
| `SKIP_FRONTEND=1` | Reuse an already-built `website/dist` |
| `SKIP_ELECTRON=1` | Stop after the bundled backend (no electron-builder) |
| `BUNDLE_KIRO_CLI=0` | Ship without the bundled kiro-cli (default `1` stages it; see [Bundled kiro-cli](#bundled-kiro-cli--the-app-carries-its-own-agent-runtime)) |
| `KIRO_CLI_VERSION=<version>` / `latest` | Override the `packaging/kiro-cli-version` pin; `latest` resolves the upstream manifest instead (still sha256-verified) |
| `KIROCREW_MANAGED_INSTALL_MARKER=<file>` | Bake that JSON file into the app as the externally-managed marker; a missing or malformed file fails the build (see [Baking the marker into the app](#baking-the-marker-into-the-app-editions)) |

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
  `bin/python3.12 -s -P -m kiro_crew "$@"`. `-s` drops the user site; `-P`
  keeps the caller's working directory off `sys.path`, so a stdlib-named
  directory there (`~/concurrent/`, `~/json/`) cannot shadow the bundled
  standard library. The Windows `bin\kirocrew.cmd` shim, the Electron
  supervisor's direct `python.exe` spawn, and the CI replicas of both
  (`.github/workflows/build.yml`'s shim, the installer test's gateway spawn)
  pass the same two flags; `test/test_stdlib_shadow.py` pins every spelling.
- **Stdlib probes verified** — `stdlib_probe_gate` fails the build if any package
  the launcher's readiness check probes is missing from the pruned tree, so a
  drifted probe list breaks the build instead of every user's launch (see
  [How the app finds and launches the backend](#how-the-app-finds-and-launches-the-backend)).
- **Self-containment verified** — the build script runs
  `PYTHONNOUSERSITE=1 bin/python3.12 -s -P -m kiro_crew --version` (the
  launcher's exact argv) followed by
  `PYTHONNOUSERSITE=1 bin/python3.12 -c 'import kiro_crew.cli'` to catch any
  missing dependency before packaging. Bare `--version` is a pre-dispatch
  fast-path (see `docs/system-specs/modules/cli.md`), so the import probe is
  the half that resolves the chain.
- **Local dictation runtime bundled** — supported desktop builds include
  `pywhispercpp`, the platform `imageio-ffmpeg` executable used for compressed
  recordings, and all transitive runtime dependencies. The macOS Intel backend
  installs no `imageio-ffmpeg`; the Intel app uses a system FFmpeg. The build
  imports the recognizer and executes the exact packaged decoder before
  publishing — and
  distinguishes a decoder that fails to AUTHENTICATE, which fails the build, from
  one that authenticates but will not run on the build host, which warns and
  ships (see [stt-streaming](../system-specs/modules/stt-streaming.md)). Model
  weights are deliberately excluded from the installer: the user selects a
  model and clicks **Download now**, with no package manager or separate
  dependency step. Intel macOS is the unsupported recognizer exception.
  Every bundled executable ships **uncompressed** — the Apple notary service
  decompresses archive members and rejects an unsigned executable found inside
  one, which fails the whole macOS release (see
  [stt-streaming](../system-specs/modules/stt-streaming.md) for how the runtime
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
gateway that this machine runs (a Kiro Crew process or its service) is reused.
A port held by a process this app cannot attribute to itself, which is what a
local SSH forward to a remote gateway looks like, is reused only when a remote
crew is recorded for that port; otherwise the supervisor refuses it
(`foreign-holder`) and the gateway-failure dialog offers **Add Remote Crew**
(see [The gateway-failure dialog](#the-gateway-failure-dialog)). Adopting an
unattributed holder would post this machine's local secret to it. Before
reusing a same-family local gateway on a fixed-path POSIX install, the shell
checks whether its sole listener is running from this app's current bundled
backend path. If that bundled gateway reports an older version than the app,
the shell warns that updated features may be unavailable and offers Continue or
Quit. The warning explains how to stop the old gateway before reopening the
app; it adds service guidance only when the listener is service-classified.
Unknown owners, remote tunnels, separate CLI installs, same or newer versions,
Windows, and moved AppImages keep the existing reuse behavior. The shell does
not restart or force-stop a stale gateway automatically.

When nothing answers the first health check but the port is still held, the
shell checks whether the sole listener is a local Kiro Crew gateway for this
app's data folder: `gateway.lock` must name its pid, and that pid must have
`gateway.lock` open. If that gateway keeps failing its health check for 15
seconds, the same dialog offers Stop and restart or Quit. Stop and restart runs
`kirocrew stop --port <port> --expect-pid <pid>` with the pid it proved, never a
raw kill. Right before the signal, the CLI re-reads the listener and this data
folder's `gateway.lock`, and refuses (signalling nothing) unless that pid still
holds both. The signal goes through a pidfd opened before the checks, so it
cannot reach a recycled pid. Only Linux has a pidfd: on macOS the same dialog
instead names the `kirocrew stop --port <port>` command and offers only Quit,
and `--expect-pid` itself refuses wherever no pidfd exists. The shell then waits for the port to clear and starts the bundled
backend. When a service
manager brings the gateway straight back, the shell re-checks the new holder
instead of starting a second one. A failed stop surfaces the start-failure
dialog, naming the pid still holding the port, instead of spawning into it. SSH
forwards, other data folders, unknown owners and Windows keep the existing spawn
path, and a gateway that answers inside the window is reused as before.

With no gateway to reuse, the shell locates the backend binary via
[`find-bin.js`](../../website/electron/find-bin.js), spawns it as `kirocrew
gateway --no-open`, polls `/api/status`, and loads the dashboard once it is
healthy.

For a bundled backend, the shell removes inherited `PYTHONPATH` and `PYTHONHOME`
before spawning Python. Windows cleanup also removes mixed-case spellings of
these keys. This keeps a desktop app launched from a development shell from
importing a checkout instead of its packaged backend. Source-tree gateway
launches retain those environment variables for development. The bundled
launcher already uses `-s` to disable user-site imports for the gateway itself;
the shell does not set `PYTHONNOUSERSITE`, which would also disable user-site
dependencies in operator Python, ACP, and MCP child processes. Because the
bundled gateway's environment is what it hands to the operator Python, MCP
server, and cron child processes it spawns, those descendants no longer inherit
a shell-exported `PYTHONPATH` or `PYTHONHOME` either; a child that relied on one
to resolve its imports under the desktop app must set it for itself.

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
reports "still being installed" through the normal gateway-failure dialog. It
stays silent for the legacy flat layout, which carries no interpreter tree to
verify.

That dialog does not wait for a click. While it is open it re-runs the same
refusal predicate (`launchBlockingBundleParts`, shared with the launcher so the
probe can never be laxer than the refusal) every five seconds, repaints the
remaining-component count in place, and — once every part is on disk — shows
"Installation finished", lingers briefly, and fires its own **Retry**. That is
the very action the button fires, resolved through the same window-closed
handshake, so it re-enters `startGateway()` exactly once by the ordinary path
and introduces no second respawn owner beside `recoverWedgedGateway` or the
liveness monitor. A click always wins over the probe, and the probe stands down
when an update install is dispatched (the updater stops the gateway on purpose)
or the app is quitting. Only the pre-spawn refusal arms it; the reclassified
crash below keeps the manual **Retry**, because there the probe cannot see what
is missing and a permanently truncated bundle would otherwise respawn and crash
on every tick.

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

1. It discovers the app's own bundled copy — `kiro-cli-chat` in
   `KIROCREW_BUNDLED_KIRO_DIR`, when built with one — then `kiro-cli` on the inherited
   `PATH`, `~/.local/bin`, `~/.cargo/bin`, Homebrew locations, or the macOS
   `Kiro CLI.app` bundle.
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

### The gateway-failure dialog

When this launch ends with no usable gateway, the shell shows one dialog
(`gateway-wait.js` renders it; the supervisor's start-failure record decides
the state). It always offers **Retry**, plus the actions the state allows:

- **Add Remote Crew** / **Edit Remote Crew** opens the remote-crew form
  (`runtime/gateway/remote-crew-prompt.js`) to save or correct the crew this
  port reaches. Saving it is what makes a forwarded port adoptable next launch.
- **Start Local Gateway** is the in-app way back from the "Run a local gateway"
  opt-out ([remote-and-mobile](../guides/remote-and-mobile.md)), whose settings
  page is served by the gateway that is not running. It turns the setting back
  on. On this launch's own port it starts the gateway in place; on a port a
  recorded crew uses it re-execs the app with a free, crew-free fallback port
  pinned, because a gateway bound there would shadow that crew.

The dialog also explains the refusals that start nothing:

- **`crew-configured`**: the local gateway is on, but a recorded crew owns this
  port, so the shell does not bind it. Start Local Gateway re-execs on a free
  port when the app can relaunch itself; otherwise the dialog names the
  terminal route (`KIROCREW_PORT` set to a port with no remote host).
- **`foreign-holder`**: a program this app did not start serves the port and no
  crew is recorded for it. The remedies are Add Remote Crew, or quitting that
  program and retrying; Start Local Gateway is not offered.

After a Start Local Gateway click, the record names the outcome so the next
dialog describes it: the restarted copy never served a gateway (offered again),
the fallback port was already busy (free it, then click again), or the new
gateway answered but its port holder could not be identified (the app stops it;
the terminal route is named instead of the button).

### Crash diagnostics

The shell keeps its own crash ledger beside `gateway-launch.log` in the app's
logs directory (`runtime/crash/`, driven by `crash-collector.js`). The scan runs
lazily, the first time the dashboard's crash notice asks for it. It reads the
Crashpad dump directory (`app.getPath("crashDumps")`) and, on macOS, the user's
`.ips` diagnostic reports:

- `crashes.log` records each new crash of this app, capped at 500 lines.
- `crashes-seen.json` acknowledges each artifact once, so a crash is reported
  once rather than on every launch, capped at 2000 keys.
- At most 25 unseen artifacts are inspected per app session, and a dump that
  reads short or unreadable on three consecutive scans is aged out.
- A minidump proven to belong to another process (a child that inherited the
  Crashpad handler) is acknowledged and deleted, since nothing else prunes it.
  Unreadable dumps, Electron-shaped strangers and this app's own dumps are
  never deleted.

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
When a remote crew is connected, the instance switcher shares the same bounded
left region as the menu: it is a single trigger naming the crew on screen (see
InstanceTabBar's SwitcherMenu), not a row of per-crew tabs, so it costs constant
width whether the menu is collapsed to a hamburger or expanded to full labels.
The centered command palette yields that region rather than the reverse — the
correct priority while the menu is open is labels > instance status > an idle
search affordance — and the palette remains reachable through its keyboard
shortcut even while hidden.
The command-palette trigger is positioned from the window midpoint rather than
the remaining flex space, so asymmetric menu and status controls do not shift it.
Linux retains the window manager's native frame and menu bar.

In macOS native fullscreen AppKit owns the top strip (pointing at it slides the
menu bar down), so the dashboard pads the app frame (`data-testid="app-frame"`)
down by `round(24 / zoomFactor)` CSS px (`MAC_FULLSCREEN_TOP_RESERVE_PX` in
`website/src/lib/electron.ts`), which keeps the header clickable below that
strip.

#### The frameless window-drag band

On every frameless platform (`IS_MAC || IS_WIN || LINUX_FRAMELESS`),
[`window-lifecycle.js`](../../website/electron/window-lifecycle.js) injects a
42px `-webkit-app-region: drag` band, `#electron-drag-bar`, as the FIRST child
of `<body>`, plus a document-wide exemption list marking
`a, button, input, select, textarea, [role="button"], [tabindex], iframe` as
`no-drag`. The band spans the full width on macOS and stops short of the caption
controls elsewhere: 138px from the right on Windows, 108px on frameless Linux.
That inset is load-bearing rather than cosmetic, because a drag rectangle over
Close moves the window instead of closing it. `body.mc-focus-mode` collapses the
band to `0` and `body.mc-focus-mode.mc-focus-chrome` restores it, so the band
exists exactly when a header does.

Two properties of it are easy to break by accident.

**The band is prepended, and a later rectangle overrides an earlier one.**
Electron accumulates the window's draggable region from element rectangles,
unioning the `drag` ones and subtracting the `no-drag` ones in the order the
renderer reports them, so the last rectangle over a pixel decides. The band goes
at the front of the body so the app's own exemptions come after it and subtract
from it; at the end it would re-add the whole strip on top of all of them. Two
in-tree notes record that ordering from real windows: `.host-drag-strip`
elements rendered after a remote pane's iframe re-add drag where the iframe's
blanket `no-drag` took it away (`InstancesViewport`), and a full-width `drag`
block following a `no-drag` button swallowed that button's lower half in the
companion panel (`PanelCard`). Chromium contracts none of this, which is why the
check below is manual.

**The exemption list is document-wide on purpose.** `app-region` is resolved
geometrically rather than by DOM containment, so a control anywhere in the page
that merely overlaps the band needs `no-drag` to stay clickable. A list scoped
to the bar's own subtree could not express that.

Plain text is not exempt, so text under the band is unselectable and shows an
arrow cursor. That is the deliberate half of the trade, and the failure on the
other side is a window that cannot be moved. No conversation text is under the
band in practice: docked, the band covers the top bar;
in focus mode without chrome it is `0`; with the chrome peeked the transcript
scroller carries `tabIndex={-1}`
([`TranscriptScrollShell.tsx`](../../website/src/pages/chat/TranscriptScrollShell.tsx)),
matches the exemption list, and subtracts its own column from the band. So
widening the exemption to text, or narrowing the band's reach, would spend
draggability on text the band does not cover.
[`shell-contract.test.js`](../../website/electron/test/shell-contract.test.js)
pins the five parts a change could silently drop: the 42px height, the
per-platform right inset, the focus-mode collapse, the prepend, and `[tabindex]`
together with the scroller attribute it keys on.

#### Focus mode: verify these seams after an Electron or Radix bump

Focus mode (hide the shell chrome behind hover) rests on five mechanisms that
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
   [`website/src/shell/focus/focusChrome.ts`](../../website/src/shell/focus/focusChrome.ts) (`departWhen: clientY > topReservePx + 48`
   for the top peek, where `topReservePx` is the macOS-fullscreen offset and `0`
   otherwise; `clientX > railWidth + 12` for the rail, where the rail width
   follows its collapse toggle): the revealed header doubles
   as the drag surface and a drag region eats pointer events before hit-testing,
   so the close is driven by pointer position, not by `mouseleave`. Only one
   overlay is revealed at a time. If a bump
   changes hover/pointer-event delivery, the peek sticks open or never opens.
3. **Peek the header, then open the instance switcher.** The header must stay on
   screen while the switcher menu is open. Exercises the header-pin heuristic in
   [`website/src/shell/focus/focusChrome.ts`](../../website/src/shell/focus/focusChrome.ts): Radix portals the menu to
   `document.body`, so the pin rides on a `[aria-haspopup][aria-expanded="true"]`
   query against the header rather than DOM containment. If a Radix bump changes
   the ARIA a trigger emits (`aria-haspopup` absent, or `aria-expanded="true"`
   emitted by default with nothing open), the header either slides away under the
   open menu or pins permanently from first paint.
4. **Peek the header, then try to select conversation text in the top 42px, and
   try to drag the window from that same strip.** Exercises the drag band
   described above: peeked, it is 42px again, and the transcript scroller's
   `tabIndex={-1}` is what subtracts it over the conversation. Text there should
   select with an I-beam cursor, and the peeked header's own empty regions should
   still move the window. If a bump changes the order in which Chromium reports
   app-region rectangles, one of those two stops working: either the top strip of
   the conversation goes dead and shows an arrow cursor, or the header no longer
   drags. A headless display cannot answer this one, because the region set is
   resolved by the window rather than by the page.
5. **Slam the pointer out through the top edge to reveal the header, park it
   just outside the window, then move it well away.** The header should stay
   while the pointer is parked and close once it travels away. Exercises the
   cursor-distance dismissal in
   [`website/electron/focus-cursor.js`](../../website/electron/focus-cursor.js):
   off-window the renderer gets no mouse events, so the main process polls
   `screen.getCursorScreenPoint()` and reports through the preload
   `watchCursorAway` bridge (embedded instance panes relay it with the
   `mc-cursor-away` messages, `website/src/lib/cursorAway.ts`). If a bump
   changes cursor-point or window-bounds reporting, an edge-summoned overlay
   either never dismisses or closes the moment it opens.

### `find-bin.js` — locating the binary

`findKirocrewBin()` checks well-known paths in order and returns the first
executable it finds, falling back to bare `kirocrew` on `PATH` (refused for a
packaged Windows app, step 5). The running
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
5. Bare `"kirocrew"` (resolved via `PATH`). A **packaged Windows** app refuses
   to spawn this fallback (`isPathFallback`, checked in
   `gateway-supervisor.js`): it means the bundled backend tree is gone, as an
   install manager leaves it mid-update, and what `PATH` names is not this app's
   backend. The launch shows the "installation still finishing" dialog instead,
   whose probe re-runs this resolution and starts the backend once a probed path
   is back. Unpackaged (source checkout) launches keep the `PATH` fallback.

The function is pure — `fs`, `os`, `path`, `process.resourcesPath`,
`__dirname`, and the arch are injected — so both arch branches are
unit-testable without mocking globals.

### `gateway-supervisor.js` — owning the gateway lifecycle

The supervisor keeps every piece of gateway state in its own closure — the
child, its ownership classification, the start-failure record, the liveness
monitor and the update handoff — together with the spawn site, the port
occupancy and identity decisions, the connect flow and recovery. It composes
five owners under `runtime/gateway/` and hands each only the host modules and
state getters it reads: `launch-preflight.js` (which backend binary to run,
whether the bundle is complete, the project directory, the AppImage sandbox
advice, the launchd `PATH`, and whether the app can relaunch itself),
`port-holders.js` (the lsof/ps/netstat probes, trusted Windows gateway
commands, the incumbent snapshot and exit wait, and force-stop),
`family-takeover.js` (quitting the other release family's app),
`token-sources.js` (the local-secret mint and the SSH token fetch), and
`remote-crew-prompt.js` (the failure dialog's remote-crew form).

- Ensures `KIROCREW_HOME` (default `~/.kiro/crew`, overridable via the
  `KIROCREW_HOME` env var) exists, then spawns the backend with
  `["gateway", "--no-open"]`. If a real pre-move `~/.kirocrew` directory exists,
  the shell reads its startup config first while the backend performs the
  one-time migration; token lookup then falls through to the canonical home.
  A clean install never creates the legacy directory.
- Honors the **`KIROCREW_PORT`** env var for the dashboard port when it is a
  selectable port (`1–65535` except `80`, whose URL drops its port); an invalid
  value is logged as a warning and treated as unset. Unset, the port comes from
  `selectLaunchPort` (`host-config.js`), run after the legacy `remoteHost`
  setting is migrated into `remoteHosts`: with the local gateway off it takes a
  `dashboard.url` port that names a recorded crew, else the recorded crew's
  port; otherwise the `dashboard.url` port, else `5476`. `BACKEND_URL` / health
  checks target that port.
- Sets `KIROCREW_PROJECT_DIR` to the packaged tree that contains `agents/` and
  `skills/`. POSIX builds use the Electron app's parent; Windows probes one and
  two levels above the Electron sources and takes the first tree carrying both
  directories.
- On every desktop platform, pins `PYTHONUTF8=1` and
  `PYTHONIOENCODING=utf-8:backslashreplace` at the Electron-to-Gateway spawn
  boundary. This applies before CPython constructs redirected stdout/stderr and
  is inherited by the Gateway's `os.execv` successor plus its MCP/session
  children. Consequently the initial launch, Tailnet/explicit restart, update
  and stale-asset re-exec, and Electron liveness respawn all use the same UTF-8
  contract instead of falling back to the Windows ANSI code page or an
  incompatible inherited POSIX encoding override.
- Leaves the inherited child `PATH` unchanged on Linux and Windows. A
  GUI-launched macOS app appends only the user launchd domain's additions, after
  the inherited entries, so an existing resolution cannot be shadowed. The
  gateway prerequisite service also probes supported Kiro CLI locations
  independently — including the Windows per-user install at
  `%LOCALAPPDATA%\Kiro-Cli` — so an already-running gateway does not depend on
  inheriting an installer-updated `PATH`.
- [`window-lifecycle.js`](../../website/electron/window-lifecycle.js) hides the
  app to the tray on window close; the composition root delegates quit-time
  gateway teardown to the supervisor, which performs the graceful shutdown and
  signal escalation contract. On POSIX,
  [`gateway-stop.js`](../../website/electron/gateway-stop.js) lists the child's
  descendants before its SIGTERM, so a gateway a launcher shim forked is not
  orphaned: after the child exits, each listed pid that is still a
  `kirocrew gateway` command gets SIGTERM, then SIGKILL after a grace window.
  Identity is re-read before each signal, so a recycled pid or a `kirocrew mcp-*`
  child is never signalled.
- When the other release family's app (Kiro Crew vs Kiro Crew Nightly) holds the
  port, macOS
  offers **Quit <other> & Continue**. Other platforms cannot quit that app for
  the user, so `runtime/gateway/family-takeover.js` asks the user to quit it and
  offers **I quit it — Retry** / **Cancel** for up to three rounds, then cancels
  the launch. It refuses up front when the incumbent's PIDs cannot be captured,
  so a respawn cannot race `gateway.lock`.
- After the gateway answers, `upgrade-cache.js` (`clearCacheOnUpgrade`) clears
  the renderer's HTTP cache, service workers and cache storage once per
  app+gateway version pair (store key `lastCacheVersion`), then connects the
  window again. A failed clear leaves the pair unrecorded, so the next launch
  retries.
- A renderer-recovery reload
  ([`renderer-recovery.js`](../../website/electron/renderer-recovery.js)) adds
  `?safe=1` to the dashboard URL, and the token retry keeps it. The SPA strips it
  before routing (`website/src/lib/safeReload.ts`) and skips the first chat
  auto-open, so a chat that crashes the renderer is not reopened on every reload.
  If the app recovers to the dashboard without the chat you had open, that is
  this guard; open the chat again from the sidebar.
- On macOS, leaving native fullscreen is an asynchronous AppKit transition that
  can stall: the Space switches back and the real window is re-ordered in, but
  the full-display snapshot overlay AppKit animates during the exit stays on
  screen and `leave-full-screen` never fires. The overlay is not one of the
  shell's windows (no traffic lights, cannot be moved or resized, covers every
  other app), and hiding the real window underneath it leaves the user with only
  the overlay. Two guards in the shell handle this:
  [`hide-to-tray.js`](../../website/electron/hide-to-tray.js) waits for
  `leave-full-screen` plus a short settle and then hides the **application**;
  if the event never comes, its backstop takes the same app-level path. In both
  cases `app.hide()` can order the overlay out together with the real window,
  whereas `win.hide()` would leave the overlay behind;
  [`fullscreen-transition-watch.js`](../../website/electron/fullscreen-transition-watch.js)
  watches every transition from its first `resize` and, when an exit has not
  completed after four seconds, logs `fullscreen: exit transition did not
  complete` to `gateway-launch.log` and cycles `app.hide()` / `app.show()`,
  which clears the overlay and restores the real window at its normal frame.
  Both terminal fullscreen events are journaled (`fullscreen: entered` /
  `fullscreen: left`) so a stalled transition is legible after the fact.
- The same overlay is reachable without any stall, and that route is the common
  one: AppKit does **not** queue a fullscreen toggle issued while one of its own
  transitions is animating. It abandons the running transition, leaving that
  transition's overlay orphaned on screen while every terminal event still
  arrives normally, so no missing-event detector can see it. Its completion
  callback is not the all-clear either — measured on macOS 26,
  `enter-full-screen` lands well before the Space animation ends, and a close
  issued after it still orphaned an overlay in roughly a third of runs (and the
  hide itself was swallowed, so the window stayed on screen). The close path
  therefore gates its exit on **quiet time** rather than on an event:
  `fullscreen-transition-watch.js` exposes `quietFor()` (milliseconds since the
  window last moved in fullscreen terms) and `hide-to-tray.js` issues
  `setFullScreen(false)` only once that clears 700ms, then hides after the usual
  settle and re-asserts the hide once a second later. Measured on the same
  harness: 0 orphaned overlays in 20 randomized runs and the window hidden every
  time, against 7 of 20 and 10 of 20 without the gate. A transition abandoned
  some other way (a user toggling fullscreen twice inside one animation) is
  reported as `fullscreen: … transition abandoned` and repaired like a stall,
  with the unhide suppressed while a close-to-tray hide is pending so the repair
  never re-surfaces a window the user just dismissed.
- Because the fullscreen close hides the **application**, every user-intent show
  path (`showMainWindow`, `activateMainWindow`, and therefore the tray items and
  the summon hotkey) calls `app.show()` first: a hidden app ignores
  `win.show()`.

## Code signing & notarization (macOS)

An unsigned `.app`/DMG is quarantined by Gatekeeper and shows **"Kiro Crew is
damaged and can't be opened"** when downloaded on another Mac. To distribute a
DMG that opens cleanly you must sign it with a **Developer ID Application**
certificate and **notarize** it with Apple. (Local builds without credentials
still work — they produce an ad-hoc–signed DMG you can open on the build machine
after right-click → Open or `xattr -dr com.apple.quarantine KiroCrew.app`.)

The build is already wired for this — `website/electron/package.json` enables
`hardenedRuntime` with `build/entitlements.mac.plist`, and the
`website/electron/scripts/notarize.js` afterSign hook notarizes when credentials are present and
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

Nothing Kiro Crew does at startup needs those folders, and its own file walks
stay out of them:

- **`/api/file-search` has no bare-`$HOME` fallback root.** With no project it
  searches `KIROCREW_PROJECT_DIR`, then `<data_home>/workspace`. A request that
  names a root, including `?project=$HOME`, is scoped and walked in full.
- **Walks rooted at `$HOME` itself are pruned.** The `@`-mention file index
  (`dashboard/file_index.py`) and the unscoped `/api/file-search` walk call
  `platform_compat.tcc_prune_walk_dirs`. At a `$HOME` root it drops the
  TCC-protected top-level folders, and at `<root>/Library` it keeps only the
  walkable children (the cloud mounts) and drops the rest, most of which is
  Full-Disk-Access gated. Every other root and every deeper position is walked
  unchanged.

Two consequences worth knowing:

- **Explicit access is unaffected.** If you point Kiro Crew at a project inside
  `~/Documents` or browse to `~/Downloads` directly, that root is walked in full.
  macOS still shows its own one-time prompt for that deliberate access — that is
  the expected OS contract, and granting it once is enough.
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

## Updates: two updaters, two switches

The desktop app's updater replaces the whole bundle, embedded gateway included.
A gateway the app spawns carries a desktop distribution stamp and defers its
update check to the app's updater, unless an `updates` block in
`security_policy.json` names update commands, because the provider is resolved
before the deferral (on Windows those commands never run; see
[governance.md → Update pins](../system-specs/modules/governance.md#update-pins-updates--policy-only)). A gateway the app reuses defers the same
way when it runs the app's own bundled backend. A separately installed one (from
the CLI, as a service, or reached over an SSH tunnel) follows its own
`auto_update` ([where to set it](../../src/kiro_crew/docs/configuration.md#turning-it-off-and-updating-by-hand)). A
container defers to its image. Both updaters running on one install is
[#15797](https://github.com/kirodotdev/KiroCrew/issues/15797).

Settings → About renders the app's update section in the desktop app's own
window and the gateway's in a browser. A desktop window also draws the gateway's
**Update the gateway automatically** switch when that gateway installs updates itself
(`update_auto_effect` is `install` or `mandatory`): a gateway the app attaches to
rather than bundles follows its own switch. The What's-new modal and the
settings searches offer the same switches under the same rule,
`website/src/utils/updateSwitches.ts`. Where `config.local.json` sets
`auto_update`, the switch says so (the note appears once a save, a refusal or a
check reports the pin) and stays usable, since a click is how a removed
override is noticed. The app's switch, its default
and install-on-quit are owned by
[release.md → Client auto-update](release.md#client-auto-update); the policy
floor by [governance.md → Update pins](../system-specs/modules/governance.md#update-pins-updates--policy-only). The user-facing summary is
[configuration.md → Updates](../../src/kiro_crew/docs/configuration.md#updates).

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
presence takes the install off the release feed: the feed is never contacted, and
Settings → About hides the release-channel switcher (the lanes it offers are
ones the packager never reads). The body is optional JSON:

```json
{
  "managedBy": "your package manager's name",
  "checkCommand": "the command that prints an available version",
  "updateCommand": "the command that applies it"
}
```

A marker without `updateCommand` turns the updater off, and About shows the
"updates are managed by …" message naming `managedBy`. A marker with
`updateCommand` runs the managed lane below instead, and About shows the normal
update card, with no managed-by message and no copyable command. That lane also
needs `checkCommand`: without it every check fails with "this managed install
has no checkCommand", so no update is ever offered or applied
([#15799](https://github.com/kirodotdev/KiroCrew/issues/15799)). An empty or unparsable body still counts as managed — an
operator who dropped the file gets the safe behavior even when the metadata is
wrong.

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

On the managed path the app treats a `checkCommand` that exits 0 and prints a
version as an available update. With the app's update switch on, it then runs
`updateCommand` on the next quit.

**A package manager's own update pause holds only if the check command honours
it**: on this managed lane while the app's update switch is on, and on the
gateway's policy `check_command` (below) while `auto_update` is on or a policy
floor forces the update. Neither lane compares versions after an apply: if the
check still prints a version and the apply command exits 0 having changed
nothing, the gateway restarts and checks again at boot, and the app runs
`updateCommand` and relaunches on every quit, so both loop
([#15798](https://github.com/kirodotdev/KiroCrew/issues/15798)).

The commands run with a **constructed environment**, not the app's own. Only an explicit pass-through set reaches them — `USER`, `LOGNAME`, `TZ`, `TMPDIR`, the `LANG`/`LC_*` locale vars, and the proxy vars — plus a narrowed system-only `PATH` and `cwd=/`. `HOME` is deliberately excluded: Python derives its user-site directory from it, so passing it through would let a planted `sitecustomize.py` run on every `python` start. Everything else is absent by construction, because `shell: true` means a shell interprets the command and a shell reads its environment as code: the loader family (`LD_*`/`DYLD_*`), the interpreter family (`PYTHON*`, `NODE_OPTIONS`), the startup files (`BASH_ENV`, `ENV`), the tracing pair (`SHELLOPTS` plus a command-substituting `PS4`), word splitting (`IFS`), and exported shell functions (`BASH_FUNC_*`, which shadow a command name outright). A packager whose updater needs any other variable must set it inside its own command rather than relying on inheritance.

**On Windows a loose marker's commands are never honored.** There is no POSIX
owner to read and `access(W_OK)` does not model ACLs, so no honest provenance
verdict exists; the check fails closed by declaration and every loose Windows
marker is treated as bare (managed, updater off). A Windows packager either
drives updates with its own installer or bakes the marker in (next).

### Baking the marker into the app (editions)

The provenance rule above refuses every install the app's own user owns, which
is every per-user package manager (a Toolbox, Homebrew, `~/Applications`), and
can never pass on Windows. An **edition** — a build that IS produced by the
package manager's owner — does not need to drop a file beside the app after the
fact; it declares the marker at build time:

```bash
KIROCREW_MANAGED_INSTALL_MARKER=/path/to/marker.json bash packaging/build-desktop.sh
```

`build-desktop.sh` validates the file (a JSON object of string fields
`managedBy` / `updateCommand` / `checkCommand`, under 8 KiB, with an
`updateCommand` — a marker that disables updates while offering none fails the
build rather than shipping silently) and copies it to
`website/electron/EXTERNALLY-MANAGED`, which electron-builder packs **into
`app.asar` next to `main.js`**. The running app reads that copy first and
trusts it without any ownership probe, on every platform: it is part of the
application's own code, so anyone positioned to rewrite it is already
positioned to rewrite the code that reads it, and no file-ownership check could
add to that. On macOS the baked copy is additionally sealed by codesign. A baked
marker outranks a loose one when both exist — a build-time declaration by the
edition that produced the binary beats a file dropped next to it later.

The default build ships no baked marker (the file is gitignored and removed at
the start of every build), so a plain checkout keeps the loose-marker contract
exactly as described above.

The commands themselves still run under the constructed environment described
above: **no app environment variable reaches them** — not `HOME`, and not
anything the edition's own wrapper exported before launching the app. So a
command must not rely on `$HOME` or `~` expanding (derive the home directory
from `USER`, which is passed through, or name paths that do not depend on it),
and must not reference a variable it expects the app to have inherited. On
Windows that failure is silent: `cmd.exe` leaves an undefined `%VAR%` in the
command line **as the literal text `%VAR%`**, not as an empty string, so a
wrapper argument such as `"%SOME_VAR%"` arrives as that string. The one value
the constructed environment does derive for the command is
`KIROCREW_MANAGED_ARGV0` — the running app executable's absolute path
(`process.execPath`, taken from the process, never from the environment) — so a
wrapper that verifies its relaunch target has a trustworthy answer without any
inheritance.

For local testing, the `KIROCREW_EXTERNALLY_MANAGED` env var points at a marker
file (any other non-empty value marks the install managed with no metadata).
It is honored on unpackaged builds only — a packaged app ignores it, because
its launch environment is user-writable.

The gateway has the matching seam for its own surfaces: an operator's
`security_policy.json` `updates` block (`check_command` / `apply_command`)
routes the dashboard's update check, badge, and Update button through the
declared commands, and the gateway then reports no release channel at all.
The `check_command` runs on every gateway check
([when](../../src/kiro_crew/docs/configuration.md#when-the-gateway-checks)) and
whenever the dashboard asks for one, so it must be side-effect-free, idempotent
and quick: a check still running after 60 seconds is stopped and counts as
failed. One `apply_command` run may take 600 s (`_APPLY_TIMEOUT_SECS`), its
exit included; a command still running past that is stopped and the update
reported failed. The keys and the command contract are in
[governance.md → Update pins](../system-specs/modules/governance.md#update-pins-updates--policy-only).
If the `apply_command` installs into a new versioned tree and prunes the old one,
it deletes the interpreter the running gateway was launched from. The gateway then
has nothing to re-enter, and it says so rather than trying: the restart is refused
while every session is still answerable, and on the orchestrator path it is
deferred. Restore the interpreter and the deferred update finishes on its own.
When the pruned tree also held the served dashboard bundle, the stale-asset
watchdog does not wait for that repair: it exits so a supervisor relaunches the
gateway through its own command, but only when that command can start the
gateway (`supervisor_reentry`, for example a stable link that points at the new
tree) and no update chose to stay up instead of restarting. Otherwise it keeps
the gateway up on its loaded code and logs why
([slack-gateway](../system-specs/modules/slack-gateway.md)).

What it will not do is drain first and find out afterwards. That was the old
failure. It saved, fenced, closed every session and only then found the
interpreter gone, leaving a gateway alive and serving nobody with no way back
except a manual relaunch.

Checking early cannot cover every case, though: the target can be replaced
between the check and the restart, and a present, executable file can still be an
image this kernel refuses. When that happens the gateway EXITS rather than
survive. Look for a CRITICAL line naming the target, followed by the process
ending with status 1. That is deliberate — the sessions are already closed, so a
surviving process would serve nothing while still holding the port your relaunch
needs. Repair the install and start the gateway again.

There is no policy key naming a fallback executable to re-enter instead, because
such a key cannot be validated. A pathname's bytes do not decide what the kernel
execs: a `#!` wrapper delegates to an interpreter the check never sees, and a
header that parses can still belong to a truncated binary. Learning the answer
for certain means exec'ing the candidate, which is either running an arbitrary
binary or booting a second gateway. The supported recovery is to repair the
install and let the retry finish, or to relaunch by hand.

## Remote tunnel mode

The desktop app can also connect to a gateway running on a **remote** host (e.g.
an always-on server) over an SSH tunnel, fetching a fresh token via
`ssh <host> kirocrew token` on each launch instead of starting a local backend.
See [`website/electron/README.md`](../../website/electron/README.md) and
[remote-and-mobile.md](../guides/remote-and-mobile.md) for setup.

## See also

- [install.md](../guides/install.md) — all three build/run methods and the build targets
- [README](../README.md) — project overview and Quick Start
