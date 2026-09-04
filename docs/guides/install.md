# Installing and Running Kiro Crew

This guide covers every way to install Kiro Crew, the first-run setup, how to
verify the install, and how to troubleshoot the failures that actually happen.

Builds use plain `pip` + `npm`/Vite + `pytest`, driven by the repo-root
[`Makefile`](../../Makefile). There is no proprietary build tooling.

> **Platforms: macOS, Linux, and Windows.** macOS and Linux use the `Makefile` /
> `setup.sh` paths below. Windows runs natively from a Python source install
> (`pip install -e ".[voice]"`, launched via `python -m kiro_crew gateway`); all
> POSIX-only process, signal, file-lock and metrics calls route through
> `kiro_crew.platform_compat`. See
> [windows-install.md](windows-install.md) for the Windows walkthrough.

---

- [Prerequisites](#prerequisites)
- [Install paths](#install-paths)
- [Build targets](#build-targets)
- [First run](#first-run)
- [Configuration](#configuration)
- [Verify the install](#verify-the-install)
- [Running as a service](#running-as-a-service)
- [Linux: the agent sandbox and unprivileged user namespaces](#linux-the-agent-sandbox-and-unprivileged-user-namespaces)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Removing user data](#removing-user-data)
- [Clean reinstall](#clean-reinstall)
- [Data retention details](#data-retention-details)
- [Next steps](#next-steps)

---

## Prerequisites

| Requirement | Needed for | Floor |
|-------------|------------|-------|
| **Python** | Backend | `>= 3.12` (`requires-python` in `pyproject.toml`; `make build` provisions a 3.12 `.venv` by default) |
| **Node.js + npm** | Building the dashboard | `>= 22` (`website/package.json` `engines`; Node 24 LTS recommended); on x86_64 Amazon Linux 2, the glibc-217 fallback installs Node 24 because official builds need a newer glibc (no glibc-217 arm64 build is available) |
| **`kiro-cli`** | Driving the LLM | Required; see below |

Node is only needed to *build* the dashboard. The prebuilt wheel, the DMG, the
AppImage, and the Linux `.deb` / `.rpm` packages all ship the dashboard already
bundled, so end users of those artifacts need neither Node nor a compiler.

### Agent backend: `kiro-cli` (required)

Kiro Crew drives an LLM through the **`kiro-cli`** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP). It is the only provider: `agent.provider` is fixed to `acp`, and the
gateway spawns `kiro-cli acp --agent <name>`.

Install `kiro-cli` per its own docs, put it on your `PATH`, and log in:

```bash
kiro-cli login
```

If `kiro-cli` is not on `PATH`, spawning a session fails with
`kiro-cli not found in PATH`. On the first dashboard launch the **Set up Kiro**
page walks through installing the CLI and completing device-code sign-in.
`kirocrew doctor` reports both the binary and the login state.

### Embeddings: nothing to install

Semantic memory and the knowledge library need no setup step. Embeddings run
**in-process** through the vendored llama-cpp-python runtime, so there is no
separate server and no HTTP hop. On first start the gateway downloads the
Qwen3-Embedding-0.6B GGUF (about 610 MB) in the background over HTTPS, verifies
it against a pinned sha256, and installs it under `~/.kiro/crew/models/`.

While the model is absent (first boot, download in flight, or a failed
download), memory search degrades to keyword/FTS search and picks embeddings up
automatically once the model lands, with no gateway restart. Two escape
hatches exist for mirrored or airgapped installs:

- `KIROCREW_EMBED_MODEL_URL` (or `memory.embed_model_url`) points the download
  at a mirror. The sha256 pin still verifies whatever it fetches.
- `KIROCREW_EMBED_MODEL_PATH` (or `memory.embed_model_path`) runs a local GGUF
  of your own instead. In that mode the default model is never downloaded.

`memory.embedding_provider` accepts only `llama_cpp`; any other value in an old
config is coerced to it on load.

## Install paths

### Which path on Linux

**Start with the one-line install below.** It is the smoothest Linux path and
the one that needs the fewest decisions: it puts `kirocrew` on your `PATH` at a
stable location, which is what makes `kirocrew service install` — and therefore
the [AppArmor profile](#linux-the-agent-sandbox-and-unprivileged-user-namespaces)
the agent sandbox needs on Ubuntu 23.10+ — reachable in the first place. You
work in the dashboard through your browser at `localhost:5476`.

Install a **desktop package** (`.deb` / `.rpm`) *in addition* when you want the
things only the Electron shell provides: an application-menu entry and icon, a
native window with persisted geometry, a dock/taskbar badge, a system-wide hotkey
to summon the dashboard, a gateway that starts and stops with the app, and
in-app updates. The packages install to a fixed path under `/opt`, so the same
PATH and AppArmor mechanics that make the one-line install work apply to them
too.

The **AppImage** stays available for hosts where you cannot install a system
package (no root, an unsupported distro). It needs FUSE present, and because it
runs from a randomized temporary mount there is no durable path to attach an
AppArmor profile to or to point a `kirocrew` launcher at — so on a distro that
restricts unprivileged user namespaces it needs the extra manual step described
in the sandbox section. Prefer a package where you can.

| You want | Use |
|---|---|
| The dashboard, a terminal, scheduled work, a server | **One-line install** (a) |
| A desktop app on Debian/Ubuntu | **`.deb`** (d) |
| A desktop app on Fedora / RHEL / CentOS Stream / Amazon Linux 2023 | **`.rpm`** (d) |
| A desktop app with no root and no package manager | **AppImage** (d) |
| A container host | **Docker** ([guide](docker.md)) |

### a. One-line install (fastest)

Installs a prebuilt, sha256-verified wheel from the release CDN. No clone, no
npm, no local build:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
```

`stable` is the default channel. Track a faster one, or pin an exact version:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --version 0.1.0
```

`stable` suits everyone, `insider` is for power users who want features days to
weeks early and accept the new bugs that arrive with them, and `nightly` is
untested `main` HEAD for us and contributors. The
[Release channels](../../README.md#release-channels) table has the full
comparison; re-running the installer with a different `--channel` is how a CLI
install moves between lanes.

The installer verifies the wheel's digest against the signed manifest and
refuses to install on a mismatch; there is no checksum-only fallback. It uses
`pipx` when available, otherwise it creates a managed venv **beside** the data
home (`~/.kiro/crew-venv`, override with `KIROCREW_VENV`) and symlinks
`~/.local/bin/kirocrew` at it. The venv is deliberately not nested inside the
data home, so no whole-home operation can ever delete the live interpreter. The
selected channel is recorded to `~/.kiro/crew/channel`.

If the host has no Python 3.12+, the installer provisions one itself instead of
touching the system: it downloads a SHA-256-pinned [uv](https://docs.astral.sh/uv/)
binary (or uses an already-installed `uv` on `PATH`), then installs a
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
CPython 3.12 into a user-owned directory beside the data home
(`~/.kiro/crew-python`, override with `KIROCREW_PYTHON_DIR`). No package
manager, no sudo, and the prebuilt interpreter runs on old-glibc distros
(CentOS 7) whose base repos never reach 3.10. Pass `--managed-python` (or set
`KIROCREW_MANAGED_PYTHON=1`) to always use the uv-provisioned interpreter and
skip the system ones entirely — useful when the system Python is fragile or
version-managed. The choice is sticky: it is recorded in the data home
(`python-mode`, next to `channel`), so later installer runs — including the
re-run `kirocrew update` performs — keep it without the flag; opt back out
with `--system-python`. The signed installer never pipes an unsigned
third-party script into a shell: uv is fetched as a tarball and verified
against pinned digests, exactly like the wheel itself. When it finishes it
prints the next step: `kirocrew gateway` to start now, or `kirocrew service
install` to run it as a service.

### b. From source (development)

Build the dashboard, install the backend into a local virtualenv (`.venv`), and
run the gateway straight out of `src/`:

```bash
make build                                   # npm build + editable backend install into .venv
PYTHONPATH=src python -m kiro_crew gateway   # -> http://localhost:5476
```

On Windows the same targets run through `make.ps1`, because `make` is not part
of a Windows install and the Makefile's recipes are POSIX-shaped
(`.venv/bin/pip`, `rm -rf`, `cp -R`, `bash ensure-*.sh`):

```powershell
.\make.ps1 build                             # same two steps, same artifacts
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m kiro_crew gateway
```

The venv interpreter is named explicitly rather than a bare `python`: the
dependencies live only in `.venv`, and on Windows a bare `python` resolves to the
system interpreter (or the Microsoft Store alias stub), which would fail at
import. `.\.venv\Scripts\Activate.ps1` first is the other way, after which
`python` and `kirocrew` both resolve inside the venv.

The two drivers expose the same target set, and
`test/test_build_target_parity.py` fails the build if one gains a target the
other lacks. Differences are confined to what the platform forces: a Windows
venv puts its executables in `.venv\Scripts\`, and the macOS-only
`resign-macos-libs.sh` step has no Windows counterpart.

`make build` runs two steps:

1. **`frontend`**: `npm ci` (or `npm install`) + `npm run build` in `website/`,
   then copies `website/dist` into `src/kiro_crew/static/dist` so the backend
   serves the SPA, and installs `website/electron`'s own deps last — it is a
   separate npm package the `website/` install never reaches, and `npm test`
   in `website/` needs it.
2. **`backend`**: creates `.venv` and runs an editable install with the `dev`
   extra (`pip install -e ".[dev]"`).

Both targets bootstrap their toolchain first (`ensure-node.sh`,
`ensure-python.sh`) and fall back to whatever is on `PATH` if that fails. The
backend target refuses to build a venv from an interpreter older than 3.10
rather than letting the install backtrack forever.

`make.ps1` resolves the same toolchain but installs none of it: the bootstrap
scripts' install paths are `curl … | sh`, so on Windows it searches (`py`
launcher first, then `PATH`, skipping the Microsoft Store alias stub) and prints
the `winget` command to run if nothing usable is found. It honors the same
`<data-home>/python-bin` and `node-bin-dir` markers those scripts record. The
`desktop` and `backend-bin` targets are the exception: they delegate to
`packaging/build-desktop.sh`, which provisions its own `uv` and
python-build-standalone interpreter on every platform.

After the backend target runs, `bin/kirocrew` resolves its real install root,
sets `KIROCREW_PROJECT_DIR`, and delegates to `.venv/bin/kirocrew`. That console
script comes from the editable package metadata (`kiro_crew._bootstrap:main`),
so the virtual environment makes `src/kiro_crew` importable without the wrapper
modifying `PYTHONPATH`; caller-provided entries pass through unchanged.

Any CLI subcommand works the same way, for example
`PYTHONPATH=src python -m kiro_crew setup` or `... doctor`.

The equivalent by hand:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew
cd website && npm install && npm run build && cd ..
pip install -e ".[voice]"    # [voice] adds the optional speech-to-text extras
```

### c. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
with a suitable Python:

```bash
make wheel                # builds the frontend, then python -m build --wheel -> dist/
pip install dist/*.whl
kirocrew gateway          # -> http://localhost:5476
```

Kiro Crew is pure Python, so the wheel is platform-independent:
`dist/kirocrew-<version>-py3-none-any.whl` (for example
`kirocrew-0.1.2-py3-none-any.whl`). One wheel serves every OS. The dashboard is
folded in by the custom `BuildWithFrontend` build step in
[`setup.py`](../../setup.py), which also bundles `CHANGELOG.md` so the
dashboard's changelog view works on a wheel install with no source tree.

The pip install name is **`kirocrew`**; the import package is `kiro_crew`.

Installed console script:

| Command | Entry point |
|---------|-------------|
| `kirocrew` | `kiro_crew._bootstrap:main` |

`pyproject.toml`'s `[project.scripts]` declares `kirocrew` and nothing else.
Because a `[project]` table exists, setuptools reads the entry points from
there and ignores `setup.cfg`'s `console_scripts`, so `kirocrew` is the only
command installed on `PATH`.

Optional extras. Kiro Crew is not on PyPI, so `pip install "kirocrew[voice]"`
cannot resolve — install an extra's own distributions into the environment that
runs the gateway instead (e.g. `pip install 'boto3>=1.34,<2'
'amazon-transcribe>=0.6,<1' 'pywhispercpp>=1.5,<2'` for `voice`). The dashboard
prints the exact command, already pointed at the right interpreter.

| Extra | Adds | For |
|-------|------|-----|
| `voice` | `boto3`, `amazon-transcribe`, `pywhispercpp` | Speech-to-text transcription |
| `otlp` | `opentelemetry-exporter-otlp-proto-http` | OTLP/HTTP metrics export. Installing it does not enable egress; that still needs an explicit `telemetry.otlp_endpoint` |
| `perf` | `py-spy` | Out-of-process profiling (`kirocrew perf sample --pid`). The in-process sampler needs nothing extra |
| `teams` | `PyJWT[crypto]` | Microsoft Teams channel (validates the inbound Bot Framework RS256 JWT) |
| `dev` | pytest, black, isort, flake8, mypy, ... | Contributor tooling; what `make build` installs |

`make desktop` and `make backend-bin` need no extra: both run
`packaging/build-desktop.sh`, which provisions a python-build-standalone
interpreter and pip-installs the project into it.

### d. Bundled desktop app

A double-clickable app that embeds a python-build-standalone (PBS) interpreter
plus uv-installed deps inside an Electron shell. End users need no Python, pip,
npm, or Node:

```bash
make desktop
```

Output is a DMG (plus a zip) on macOS, an AppImage plus a `.deb` and an `.rpm`
on Linux, and an assisted NSIS Setup.exe on Windows, under
`website/electron/dist/`. The macOS DMG opens to a branded
drag-to-Applications layout carrying the opening animation's artwork. The
Windows wizard keeps native controls and its
per-user default while carrying matching Kiro Crew artwork through its sidebar
and header. On macOS the default is ONE universal DMG: the Electron shell is
lipo-merged, and the backend, which cannot be lipo-merged, ships as two complete
PBS trees selected at launch by `process.arch`. The x86_64 backend is built
under Rosetta 2, so a universal build needs an Apple-Silicon host;
`UNIVERSAL=0` forces a faster host-arch-only build. Linux is always host-arch,
and the three Linux formats come from one backend tree packaged three times.

#### Installing a Linux desktop package

```bash
sudo apt install ./KiroCrew-x86_64.deb     # Debian, Ubuntu
sudo dnf install ./KiroCrew-x86_64.rpm     # Fedora, RHEL, CentOS Stream, AL2023
```

Either one installs to `/opt/KiroCrew`, registers the application-menu entry and
MIME database, refreshes the icon cache, links `/usr/bin/kirocrew-desktop`, and —
on a host whose AppArmor supports the bundled profile — installs and loads the
`userns` profile the agent sandbox needs, so no manual `sandbox install-profile`
step is required. The fixed install path is what makes all of that durable.

Updates arrive through the app (**About → Check for updates**), which downloads
the new package and hands it to `dpkg` / `rpm`. That needs root, so expect one
elevation prompt (`pkexec` or `sudo`) at install time — it is the package
manager doing the write, not the app. `sudo apt remove kirocrew` /
`sudo dnf remove kirocrew` uninstalls; see
[Uninstalling](#uninstalling) for what happens to your data.

The AppImage needs no root and no package manager, which is the reason to pick
it, but it also needs FUSE present (`sudo dnf install fuse` on Amazon Linux
2023, which ships without it; `--appimage-extract-and-run` is the escape hatch)
and it runs from a randomized temporary mount, so it carries the manual
sandbox-profile step and the move-breaks-it caveat documented in the
[sandbox section](#linux-the-agent-sandbox-and-unprivileged-user-namespaces).

All three Linux formats are built from the same glibc floor as the build runner.
Verified requirement at the time of writing is **glibc 2.34**, which covers
Ubuntu 22.04+, Debian 12+, Fedora, CentOS Stream 9 and Amazon Linux 2023, and
excludes Ubuntu 20.04, Debian 11 and Amazon Linux 2 — on those, use the
[one-line install](#a-one-line-install-fastest) instead.

Prebuilt downloads for the release channels are linked from the
[README](../../README.md#app-downloads). The Windows desktop installer remains
a preview artifact; see [windows-install.md](windows-install.md) for its current
publishing and signing status. The source install remains the fully supported
Windows path.

See [desktop-app.md](../build/desktop-app.md) for the full pipeline (frontend,
PBS provisioning, pip install, pruning, electron-builder) and how the app
locates and launches the bundled backend.

### e. Docker

For always-on servers the gateway also ships as a public multi-arch image on
GHCR. See [docker.md](docker.md).

## Build targets

Every target has the same name on both drivers: `make <target>` on macOS and
Linux, `.\make.ps1 <target>` on Windows.

| Target | What it does |
|--------|--------------|
| `make build` | Frontend (npm/Vite) + backend into `.venv` |
| `make frontend` | Frontend only: npm build staged into `src/kiro_crew/static/dist`, plus `website/electron` deps |
| `make backend` | Backend only: `.venv` + editable install with the `dev` extra |
| `make wheel` | Self-contained pip wheel with the dashboard bundled, into `dist/` |
| `make backend-bin` | Frozen standalone backend binary (host arch only) |
| `make desktop` | Full desktop app: DMG on macOS, AppImage on Linux, NSIS installer on Windows |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, and caches |

Override the Python interpreter with `make PY=python3.12 build`, or
`.\make.ps1 build -Py C:\path\to\python.exe`.

Both desktop targets run `packaging/build-desktop.sh` on every platform,
including Windows, where `make.ps1` invokes it through the Git for Windows bash:
the script already normalizes MSYS `uname` output and has a Windows PBS branch,
and CI's Windows lane calls it the same way. The plain `build` path needs no
bash. Note that a locally built Windows installer is **unsigned** — only CI's
signing lane has the signing identity — so SmartScreen shows an "unrecognized
app" interstitial.

## First run

After installing by any path:

```bash
kirocrew setup            # interactive wizard
kirocrew doctor           # verify everything is wired up
kirocrew gateway          # start the server, then open http://localhost:5476
```

From a source checkout, use `PYTHONPATH=src python -m kiro_crew <subcommand>`
in place of `kirocrew`.

### What `kirocrew setup` asks

The wizard installs the agent config, then walks through the workspace
directory, timezone, dashboard URL, and (on macOS) the desktop app. It does NOT
configure any messaging channel: pass `--slack` to opt into the guided Slack
credential and slash-command setup. It also does NOT install a browser: browsing
is available when `playwright-cli` is on PATH, and you install it separately (see
[Browser](#browser)).

**Want the Playwright CLI at your own shell?** That is a separate tool from the
Browser Mode above, and it has its own installer, which bootstraps Node when your
machine has none and reports enterprise-registry failures (mirror login, proxy,
blocked browser CDN) as specific remedies rather than a raw npm dump:

```bash
curl -fsSLO https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.sh
less playwright-cli.sh          # read it before you run it
sh playwright-cli.sh --version 0.1.18
```

The download-and-read form is listed first on purpose: piping a script into a
shell is prohibited on many corporate machines, and `raw.githubusercontent.com`
itself is blocked or rate-limited on some — which is the same audience whose
network this script exists to cope with. If yours allows it, `curl -fsSL … | sh`
works as a one-liner; if it does not, take the file from a checkout or a release
and run it locally. Either way the script reaches only three hosts: the npm
registry (or the mirror you point it at), the Node mirror when it has to
bootstrap a toolchain, and the Playwright CDN for the browser binaries — that
last one only unless you pass `--skip-browsers`, and `--download-host` points it
at an internal mirror instead.

Windows uses `playwright-cli.ps1` with the same flags in PowerShell spelling.
`--help` lists all of them; `--dry-run` prints the plan without changing anything.
Design notes and the exit-code table:
[browser module spec](../system-specs/modules/browser.md).

**Messaging channels are optional.** The default wizard configures none, and the
web dashboard is fully functional without any messaging credentials. Connect a
channel later -- Slack (`kirocrew setup --slack` or
[slack-setup.md](slack-setup.md)),
[Discord](../../src/kiro_crew/docs/discord-integration.md),
[Telegram](../../src/kiro_crew/docs/telegram-integration.md),
[Teams](../../src/kiro_crew/docs/teams-integration.md),
[Webex](../../src/kiro_crew/docs/webex-integration.md),
[WeCom](../../src/kiro_crew/docs/wecom-integration.md),
[WeChat](../../src/kiro_crew/docs/weixin-integration.md), or
[WhatsApp](../../src/kiro_crew/docs/whatsapp-integration.md) --
when you want to reach the same agent away from your desk.

These flags narrow the wizard:

| Flag | Effect |
|------|--------|
| `--agent-only` | Install the agent config and stop, skipping the workspace and every credential prompt |
| `--slack` | Run the guided Slack credential + slash-command setup (opt-in) |
| `--clean` | Fresh agent config: ignore the existing `kirocrew.json` and regenerate from defaults instead of merging your MCP servers and tools forward |
| `--electron-only` | Install only the macOS desktop app |

The two combine: `kirocrew setup --agent-only --clean` rebuilds the agent config
from scratch and touches nothing else. That is the fix for a broken or stale MCP
configuration, because without `--clean` the existing file is used as the base
so all user customizations survive.

## Browser

Browsing is optional and installed separately. The agent drives a browser by
running `playwright-cli` commands, so it needs Node.js 20 or newer:

```bash
npm install -g @playwright/cli@latest
playwright-cli install-browser              # --with-deps on Debian/Ubuntu only
playwright-cli install --skills agents --global
```

`--with-deps` installs OS libraries through `apt` and needs root. Playwright
implements it for apt alone, so on Fedora, RHEL, CentOS or Amazon Linux it
misfires against Ubuntu package names; install the libraries with your own
package manager instead. The Settings → Browser install button adapts to the
host and reports the command to run when it needs root — see
[the browser module spec](../system-specs/modules/browser.md#os-dependencies).

The dashboard's **Browser** panel embeds the CLI's own dashboard over loopback,
which shows the live session and lets you take over with real mouse and keyboard.
That is how you complete a CAPTCHA or a 2FA prompt, and how you log in once so a
session can be captured with `playwright-cli state-save`.

**Installing the CLI makes browsing available; it does not auto-approve it.**
There is no separate capability toggle because the CLI has no way to expose only
a subset of its verbs. Every `playwright-cli` shell command still follows the
ordinary approval flow. Under normal mode the first command prompts; you can
approve once, trust its command pattern for the session, or deliberately enable a
wider trust mode. This matters most for `playwright-cli attach --extension`, which
drives your own running Chrome with the sessions you are already logged into.

## Configuration

- Config file: `~/.kiro/crew/config.json`, managed with
  `kirocrew config get/set/edit`.
- Credentials: `~/.kiro/crew/.env` holding messaging-channel tokens (for Slack:
  `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`; other channels use
  their own keys). See [slack-setup.md](slack-setup.md) for creating the Slack
  app.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KIROCREW_HOME` | `~/.kiro/crew` | Data directory (config, credentials, databases) |
| `KIROCREW_PORT` | `5476` | Port the gateway / dashboard listens on |
| `KIROCREW_EMBED_MODEL_URL` | CDN default | Mirror for the embedding model download |
| `KIROCREW_EMBED_MODEL_PATH` | unset | Run a local GGUF instead of the bundled model |

`KIROCREW_PORT` is an environment variable validated at CLI entry, not a config
key. `--port` on the CLI overrides it (`--port auto` binds an OS-assigned
ephemeral port). The `dashboard.url` config key only advertises a remote URL.
For the installed service the port is baked into the unit at install time — see
[Running as a service](#running-as-a-service) for how to set and later change
it.

### The data home lives under `~/.kiro/`

Kiro Crew stores its data in `~/.kiro/crew`, sharing the `~/.kiro/` base with
other Kiro-family apps. An existing top-level `~/.kirocrew` install migrates
automatically on first launch: its data (config, credentials, session history,
databases) is copied into `~/.kiro/crew`, **overwriting** any file already at
the same relative path, then verified, then the legacy data is deleted. There is
no rollback copy and no backup of anything overwritten.

Details worth knowing before you upgrade:

- Re-downloadable bulk content (`models/`, `cache/`) is **not** copied; the new
  home regenerates it on first start, exactly as a fresh install does.
- Virtual environments at the legacy root (`venv`, `.venv`, `venvs`) are neither
  copied nor deleted, because a venv is not relocatable and may be the very
  interpreter running the migration. The legacy root survives to hold them.
- If a live gateway holds either home's `gateway.lock`, the move is skipped for
  that run and completes on the next clean cold start.
- The migration only runs on the default path. Setting `KIROCREW_HOME` skips it
  entirely, so set it **before** upgrading if you want the two homes to stay
  separate.

**There is no rollback.** Once the move completes, `~/.kirocrew` is gone, and an
older release knows nothing of `~/.kiro/crew`, so it would start empty. Back up
first if you need to be able to go back:

```bash
cp -a ~/.kirocrew ~/.kirocrew.manual-backup
```

## Verify the install

```bash
kirocrew doctor
```

`doctor` reports, section by section: the composed platform edition, the data
home (including a legacy-home conflict warning), Linux pod session bus, the
project directory, the agent config, configuration values (provider, model,
approval mode, dashboard URL), the managed MCP servers and their tool counts,
the runtime, vector memory and the in-process embedding model (including whether
the model URL is reachable), speech-to-text, the Slack integration,
loop-stall crash dumps, and connectivity.

## Running as a service

For always-on operation (channel bots, cron jobs, background tasks):

```bash
kirocrew service install    # systemd on Linux, launchd on macOS
kirocrew service status
kirocrew service uninstall
```

On Linux this writes `/etc/systemd/system/kirocrew.service` (sudo is prompted
for the unit file and the `systemctl` calls; the gateway itself runs as your own
user, never under sudo). When you are already root — a minimal container or
`root` login — no `sudo` binary is required. On macOS it writes a launchd plist
and needs no sudo.

The gateway runs untrusted agent tools, so it must run as a **non-root** user:
the installer sets `User=` to the account behind `sudo` (`$SUDO_USER`, else
`$USER`), and **refuses to install a `User=root` service**. From a bare `root`
login (or `sudo` with no `$SUDO_USER`), first create or pick a normal account and
install as it, e.g. `sudo -u <user> KIROCREW_KIRO_BIN=... kirocrew service
install` (the official Docker image already runs as the `kirocrew` user).

### SELinux-enforcing hosts with kirocrew under `$HOME`

On an SELinux-enforcing host whose kirocrew lives under `$HOME` — the default on
Bazzite, Fedora Silverblue/Kinoite and other atomic desktops — a **system**
service cannot start at all. systemd (PID 1) runs in the `init_t` domain, the
policy does not allow that domain to `execute` a file carrying a home label
(`user_home_t`), and the unit fails every start with `status=203/EXEC` until it
exhausts its restart limit.

The binary is fine. `test -x` on it succeeds, the shebang is correct, and the
permissions are right — the policy's execute check is the only thing that fails,
which is why `203/EXEC` here looks identical to a genuinely missing or
non-executable path. `kirocrew service install` therefore asks the loaded policy
before writing anything, and if the unit provably cannot start it **refuses up
front** and prints a ready-to-paste per-user unit instead of leaving a
crash-looping service enabled at every boot.

A per-user unit is not affected, because the per-user systemd manager does not
run in PID 1's domain. Follow the commands the refusal prints, then manage the
service with `systemctl --user status|restart kirocrew` and `journalctl --user -u
kirocrew -f`. Note that `kirocrew service status` / `uninstall` only look at the
system unit, so they will not see a user unit ([#7165] tracks adding a first-class
`--user` scope). Installing kirocrew onto a system-labelled path such as
`/usr/local/bin` also avoids the problem.

[#7165]: https://github.com/kirodotdev/KiroCrew/issues/7165

### Setting the service port

A system service inherits none of your shell environment, so `export
KIROCREW_PORT=…` in your shell does **not** reach it. Set the port when you
install so it is baked into the unit:

```bash
KIROCREW_PORT=5477 kirocrew service install
```

To change it later without reinstalling, edit the overrides file the installer
creates and restart:

```bash
sudo sed -i 's/^#\?KIROCREW_PORT=.*/KIROCREW_PORT=5477/' /etc/kirocrew/kirocrew.env
sudo systemctl restart kirocrew
```

`/etc/kirocrew/kirocrew.env` is read by the unit via `EnvironmentFile=`, so its
values override the install-time snapshot and survive a reinstall. Use this to
move the service off the default `5476` when that port is already taken (for
example by a local crew you also run on this host — there is one
`kirocrew.service` unit, so re-running `service install` updates it in place
rather than creating a second service).

**The `EnvironmentFile=` directive only exists in units written by v0.2.0 or
later.** Upgrading the package never rewrites an already-installed unit, so a
unit installed by an older release (v0.1.3 and earlier) silently ignores
`/etc/kirocrew/kirocrew.env` — editing it changes nothing. Check which kind you
have:

```bash
grep EnvironmentFile /etc/systemd/system/kirocrew.service
```

No output means the directive is missing. Either re-run `kirocrew service
install` (it rewrites the unit in place, keeping the same service), or set
variables with a [systemd drop-in](#setting-other-environment-variables-systemd-drop-in),
which works on any unit version.

### Setting other environment variables (systemd drop-in)

For variables the seeded overrides file does not cover — proxy settings are the
common case — use a systemd drop-in. Drop-ins are systemd's own override
mechanism: they apply to the unit no matter which release wrote it, and they
survive reinstalls, `service install` re-runs, and even
`kirocrew service uninstall` (which removes the unit but never touches
`/etc/systemd/system/kirocrew.service.d/`).

```bash
sudo mkdir -p /etc/systemd/system/kirocrew.service.d
sudo tee /etc/systemd/system/kirocrew.service.d/proxy.conf > /dev/null <<'EOF'
[Service]
Environment="HTTPS_PROXY=http://proxy.example.com:3128"
Environment="HTTP_PROXY=http://proxy.example.com:3128"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF
sudo systemctl daemon-reload
sudo systemctl restart kirocrew
```

A new or edited drop-in is not picked up until `systemctl daemon-reload` runs —
restarting alone is not enough. Verify what the unit resolved to with
`systemctl cat kirocrew` (drop-ins are printed below the unit) or
`systemctl show kirocrew --property=Environment`.

For remote hosts, see [remote-and-mobile.md](remote-and-mobile.md).

## Linux: the agent sandbox and unprivileged user namespaces

On Linux, Kiro Crew isolates the agent by entering a **user namespace** and then
a **mount namespace**, over-mounting credential paths such as `~/.aws` and
`~/.ssh` so the agent cannot read them. If that sandbox cannot be built,
Kiro Crew **refuses to run the agent** rather than run it unisolated: spawns fail
closed. This is deliberate and is not something to work around casually.

**Ubuntu 23.10 and newer ship `kernel.apparmor_restrict_unprivileged_userns=1`**,
which moves any process that creates a user namespace into a restricted AppArmor
profile with no `CAP_SYS_ADMIN`. The first `unshare` succeeds, the second fails
with `EPERM`, and you see:

```
sandbox: unshare(NEWNS) failed: errno 1
```

### The remedy: let `service install` add an AppArmor profile

```bash
kirocrew service install
```

Where, and only where, this mechanism is the one in play, the installer also
writes `/etc/apparmor.d/kirocrew-userns` and loads it. The profile grants
exactly one permission (`userns`) and is **attached** to the resolved kirocrew
launcher script (the same absolute path `service install` uses as `ExecStart`,
typically something like `~/.kiro/crew-venv/bin/kirocrew`) — the same approach
stock Ubuntu already uses for `chrome` and `brave`. An earlier version of this
profile was named-but-unattached and applied purely via `AppArmorProfile=` in
the unit; that shipped first (#1210) but was found not to actually confine the
gateway's sandbox probe (#3463) — the directive labels only the unit's own
top-level process, and the probe runs in a child reached through a fork the
directive's labelling never reaches. The directive is no longer used.

This uses the sudo prompt `service install` already needs for the unit file, so
it costs no additional privilege, and it **cannot fail your install**: if the
profile cannot be written, loaded, or verified, you get a warning and the
install continues. `kirocrew service uninstall` unloads and removes it, so a
host is left as it was found rather than carrying an orphaned userns permission.

The installer skips the profile silently, with a reason it can print, when
AppArmor is not an active LSM, when the sysctl is not `1`, when
`apparmor_parser` is absent, or when the parser is older than 4.x (the `userns`
rule needs 4.x or newer). So on Debian, Arch, RHEL and Amazon Linux nothing
changes.

**Running the gateway outside systemd** (for example `kirocrew gateway` in a
terminal) is covered *only when the launch goes through the attached launcher
path*: the kernel applies a path-attached profile at every `execve()` of that
exact file, unit or no unit, so once `service install` has attached the profile,
a foreground `kirocrew gateway` typed at a shell that resolves to the same
launcher script runs confined too. A launch that does **not** go through that
path — `python -m kiro_crew`, a different venv's entry point, a re-created venv
the profile has not been re-pointed at — stays unconfined, and `kirocrew doctor`
reports the attachment as stale in the re-created-venv case. Prefer running the
gateway as the service: it pins `ExecStart` to the attached path and restarts on
boot.

### The AppImage (desktop app) needs its own profile

A **`.deb` or `.rpm` install needs none of this section**: the package's
post-install step writes and loads a `userns` profile attached to its own fixed
path under `/opt`, so the sandbox works on a stock Ubuntu 23.10+ host with no
manual step and nothing to re-point later. That fixed path is the whole
difference — everything below exists because an AppImage does not have one.

The profile above is applied **by systemd**, so it covers the installed service
and nothing else. Launching the AppImage directly gives systemd no part to play:
the app execs the bundled backend itself, so neither process gets a profile and
agent spawns fail closed exactly as before. Attach a profile to the AppImage
instead:

```bash
kirocrew sandbox install-profile --path ~/Applications/kirocrew.AppImage
```

**If you only ever downloaded the AppImage, you have no `kirocrew` on your
PATH** — the CLI is bundled inside the app, which is the whole point of that
download. Use the bundled copy instead. The sandbox error message in the app
prints the exact absolute path for you; it looks like this, and it is valid while
the app is running:

```bash
'/tmp/.mount_XXXXXX/resources/backend-dist/kirocrew-backend/bin/kirocrew' \
  sandbox install-profile --path ~/Applications/kirocrew.AppImage
```

Do **not** prefix that with `sudo`. The command elevates only the three steps
that need it (`install`, `apparmor_parser`, `aa-exec`) and prompts you for a
password when it does; running the whole thing as root would execute application
code with privilege for no reason.

Then restart the app. To check whether the launch you are looking at is covered:

```bash
kirocrew sandbox status
```

This writes `/etc/apparmor.d/kirocrew-launcher`, granting the same single
`userns` permission — but **attached** to that executable path, which is how the
kernel can apply it at exec time with no cooperation from the process. The
backend the app spawns inherits it. It is the same mechanism stock Ubuntu uses
for `/etc/apparmor.d/chrome`, `brave`, `1password` and `Discord`.
`kirocrew sandbox remove-profile` unloads and removes it.

Three things the command refuses to do, because an attachment is a permission
grant keyed on a path:

- **A path you do not own.** An AppImage you downloaded is owned by you, which is
  the case this serves. A root-owned binary in a system location is shared with
  every user of the machine, so attaching there would hand the grant to all of
  them - and no blocklist of shared runtimes can be complete (`java`, `mono`,
  `dotnet`, `php`, `wine` and friends are all in the same position as
  `/usr/bin/python3`). If you need to confine a system-wide install, ship a
  packaged profile the way the distro does for `chrome` and `brave`.
- **A world-writable location** (`/tmp`, `/var/tmp`, `/dev/shm`, `/run`, or any
  directory in the path whose permissions let others write). Anyone with a local
  account could put their own file at that path and inherit the grant. Keep the
  AppImage somewhere durable such as `~/Applications`. This also rules out the
  AppImage's own `/tmp/.mount_XXXXXX` runtime directory, which is a fresh random
  path on every launch and could never match twice.
- **A shared interpreter** such as `/usr/bin/python3`. That would grant
  unprivileged user namespaces to every program on the host that runs it.

Because the profile is attached to a path, **moving or renaming the AppImage
silently stops it applying** — the kernel reports no error, the profile just
never matches. `kirocrew sandbox status` detects that and names the stale path;
re-running `install-profile` re-points it. Replacing the file in place (an
in-place update) keeps working, since the path is unchanged.

**Running the gateway in a terminal** (`kirocrew gateway`) is not covered by
either profile. Use `kirocrew service install` and let systemd run it. There is
no correct profile to attach for a foreground run: the only executable involved
is a shared Python interpreter, and attaching there would hand unprivileged user
namespaces to every Python process on the machine.

> Earlier versions of this page suggested `aa-exec -p kirocrew-userns -- kirocrew
> gateway`. That does not work and has been removed. Entering a **named** profile
> requires `aa_change_onexec`, which an unprivileged unconfined process is not
> permitted to do, and `aa-exec` does not fail loudly when it cannot transition —
> it execs the command unconfined, so the gateway appears to start under the
> profile while running without it. Running it under `sudo aa-exec` does
> transition, but then the gateway runs as root.

**Please do not "fix" this by setting the sysctl to 0.** That disables a
kernel-wide protection for every application on the machine to satisfy one
app-scoped need. The per-application profile exists precisely so you do not
have to.

### Other reasons user namespaces can be denied

The AppArmor profile addresses only the Ubuntu restriction. These are different
mechanisms with different remedies, and they report different errnos. The
sandbox probe names the failing step so you can tell them apart:

| Symptom | Mechanism | Remedy |
|---|---|---|
| `unshare(CLONE_NEWNS)` fails `EPERM`, sysctl is `1` | Ubuntu >= 23.10 AppArmor userns restriction | `kirocrew service install`, or `kirocrew sandbox install-profile` for the AppImage (this page) |
| `unshare(CLONE_NEWUSER)` fails `ENOSPC` / `EUSERS` | `user.max_user_namespaces=0` (CIS-hardened host) | Raise that sysctl |
| `unshare` fails and `kernel.unprivileged_userns_clone=0` | Debian-family legacy knob (defaults to 1 since Debian 11) | Set it to 1 |
| `unshare` fails `EINVAL` / `ENOSYS` | Kernel built without `CONFIG_USER_NS` | None short of a different kernel |
| Fails inside Docker/Podman | The container's seccomp filter denies `unshare` | Container run flags, **not** host config |
| RHEL/Fedora/Rocky/AL2023 | SELinux, not AppArmor | userns is enabled there; the profile is inert |

To see which step is failing on your host:

```bash
python3 -c "
import kiro_crew.sandbox as sb
sb.reset_backend(); print(sb.detect_backend(), sb._last_unshare_failure)"
```

`kirocrew doctor` reports the same verdict without the one-liner, and the
dashboard's **Sandbox unavailable** screen names the mechanism and the command
for it directly — the probe classifies the failing step into one of
`apparmor_userns`, `max_user_namespaces`, `userns_denied` or `no_user_ns`, which
is the row of the table above that applies to you.

## Troubleshooting

Always start with `kirocrew doctor`.

### `AcpTimeoutError: ACP prompt timed out`

The `kiro-cli` backend did not answer in time. Five common causes:

1. **`kiro-cli` is not installed.** The gateway raises
   `kiro-cli not found in PATH`. Install it, or use the dashboard's
   **Set up Kiro** page.
2. **Not logged in.** Run `kiro-cli login`. An expired session normally
   surfaces as the distinct, non-retryable "kiro-cli is not logged in" error
   rather than a timeout, so check this even when the message differs.
3. **A broken or stale MCP config.** Rebuild it with
   `kirocrew setup --agent-only --clean`. A single unreachable MCP server can
   consume the whole initialization window.
4. **First launch is genuinely slow.** MCP servers can be slow to initialize,
   so the handshake allows up to 4 minutes before giving up. Later timeouts have
   their own watchdogs: a turn that streams text and then goes quiet for 90
   seconds is treated as finished, and a dispatched tool that returns nothing at
   all for 10 minutes is treated as a dead stall and the agent is killed to
   recover the slot.
5. **The host needs a proxy and the service does not have one.** On a
   corporate network, `kiro-cli` must reach its backend through your proxy. A
   systemd service inherits none of your shell's `HTTPS_PROXY`/`HTTP_PROXY`
   exports, so a gateway that works when run from your terminal can still time
   out as a service. Set the proxy variables on the unit with a
   [systemd drop-in](#setting-other-environment-variables-systemd-drop-in),
   then `sudo systemctl daemon-reload && sudo systemctl restart kirocrew`.
   Agent sessions inherit the service environment, so this fixes them. One
   known gap: the first-run setup gate's login probe currently filters proxy
   variables out even when the unit carries them, so it can stay stuck on
   "Sign in to Kiro CLI" on a proxied host — that is
   [issue #2648](https://github.com/kirodotdev/KiroCrew/issues/2648).

### Memory or knowledge search returns nothing

The embedding model is probably still downloading. Check the **Vector Memory**
section of `kirocrew doctor`, which reports the model state and whether the
model URL is reachable, and look for the GGUF under `~/.kiro/crew/models/`.
Search falls back to keyword matching until the model lands, then switches over
on its own with no restart. For an airgapped or firewalled host, point
`KIROCREW_EMBED_MODEL_URL` at a mirror; the sha256 pin still verifies the file.

### The gateway will not start

```bash
kirocrew doctor
kirocrew gateway --port auto   # bind an OS-assigned port if 5476 is taken
```

## Uninstalling

Uninstalling removes the Kiro Crew binary and its runtime but **preserves your
data home** (`~/.kiro/crew`) — configuration, credentials, memory, sessions,
apps, and the audit chain remain intact. This is intentional: reinstalling picks
up where you left off without re-running setup or losing history.

Before uninstalling, stop any running gateway and remove the system service if
one is installed:

```bash
kirocrew stop                # stop a running foreground gateway
kirocrew service uninstall   # removes the systemd unit / launchd plist
```

Then follow the section matching your install method.

### One-line install via `cli.sh` (pipx)

If `cli.sh` used `pipx` (the default when pipx is on `PATH`):

```bash
pipx uninstall kirocrew
```

### One-line install via `cli.sh` (managed venv)

If `pipx` was not available, `cli.sh` created a managed venv and a symlink.
Remove both:

```bash
rm -f ~/.local/bin/kirocrew
rm -rf "${KIROCREW_VENV:-${KIROCREW_HOME:-$HOME/.kiro/crew}-venv}"
```

If you set `KIROCREW_VENV` to a custom path, verify its contents before
removing it — `cli.sh` overlays that directory with venv files, and an `rm -rf`
on a path you already used for something else will take that too.

### pip / pip wheel install

```bash
pip uninstall kirocrew
```

If installed in a dedicated virtualenv:

```bash
rm -rf /path/to/your/venv
```

### From source (development)

Remove the editable install and the build artifacts:

```bash
# Chained so a failed `cd` (wrong path) can never run `rm -rf` in your current directory:
cd /path/to/KiroCrew \
  && pip uninstall kirocrew \
  && rm -rf .venv build dist   # editable install, local venv, and build outputs
```

### macOS desktop app (DMG / zip)

1. Quit Kiro Crew from the menu bar or Dock.
2. Drag **KiroCrew.app** from `/Applications` to the Trash (or `rm -rf`).

There is no installer package or receipt to clean — the DMG is a drag-install.

### Linux AppImage

Delete the AppImage file wherever you placed it:

```bash
rm -f ~/Applications/KiroCrew-x86_64.AppImage   # or wherever you saved it
```

### Windows (NSIS installer)

Use **Settings → Apps → Installed apps**, find "Kiro Crew", and click
**Uninstall**. The NSIS uninstaller removes the application directory and
shortcuts but does not touch `~/.kiro/crew`.

### Docker

```bash
docker stop kirocrew && docker rm kirocrew   # graceful stop, then remove
docker rmi ghcr.io/kirodotdev/kirocrew:stable # remove the image (match the tag you pulled)
```

`docker stop` sends SIGTERM and gives the gateway time to run its shutdown
flush; `docker rm -f` sends SIGKILL immediately, which can drop up to one
5-second flush interval of recent session state.

## Removing user data

Uninstalling via any method above leaves your data home intact. To also remove
all user data:

```bash
# Back up first — this is irreversible. `kirocrew snapshot` only captures
# memory/config/skills/workspace, not `.env`, credentials, or installed apps,
# so a full copy of the data home is the only complete backup. Chained with
# `&&` so a failed copy (e.g. disk full) blocks the delete rather than racing
# ahead of it. Point the copy at a location you control OUTSIDE the data home:
cp -a "${KIROCREW_HOME:-$HOME/.kiro/crew}" ~/kirocrew-backup \
  && rm -rf "${KIROCREW_HOME:-$HOME/.kiro/crew}"
```

The backup is a full copy of your credentials (`.env`), signing keys, and
session data, and it does NOT inherit the agent's path protections that guard
the live data home — store it somewhere the agent cannot reach and delete it
once the reinstall is confirmed good.

This deletes configuration, credentials (`.env`), session history, memory
databases, installed apps, and the embedding model cache.

> **Docker:** the commands above target a host data home. A Docker install keeps
> everything in the `kirocrew-home` named volume instead, so remove that rather
> than `~/.kiro/crew`: `docker volume rm kirocrew-home` (or `docker compose down -v`).

> **Note:** App Kit data is preserved per-app by default. To remove an
> individual app's data before or instead of purging the whole home:
> `kirocrew app uninstall NAME --purge-data`

## Clean reinstall

A clean reinstall removes both the binary and the data home, then installs
fresh. Use this when `kirocrew doctor` reports issues that setup cannot fix,
or when you want a completely fresh start.

```bash
# 1. Stop any running gateway, then remove the service
kirocrew stop                        # stop a foreground gateway (service uninstall won't)
kirocrew service uninstall 2>/dev/null

# 2. Uninstall the binary (use the matching command from above)
pipx uninstall kirocrew          # or: pip uninstall kirocrew, rm the AppImage, etc.

# 3. Back up and remove the data home (chained so a failed copy blocks the delete)
cp -a "${KIROCREW_HOME:-$HOME/.kiro/crew}" ~/kirocrew-backup \
  && rm -rf "${KIROCREW_HOME:-$HOME/.kiro/crew}"

# 4. Reinstall
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh

# 5. Run first-time setup
kirocrew setup
kirocrew doctor
```

Replace step 4 with your preferred install method (pip wheel, source build,
desktop app) as described in the [Install paths](#install-paths) section above.

## Data retention details

For reference, the data home structure and what each uninstall path touches:

- `kirocrew service uninstall` removes only the systemd unit (plus the AppArmor
  profile it installed) or the launchd plist.
- Python and npm package removal has no `preuninstall` or `postuninstall`
  cleanup hook.
- The macOS DMG/zip and the Linux AppImage have no cleanup hook, so removing the
  application bundle or image leaves the data home intact.
- A Linux `.deb` / `.rpm` removal DOES run the package's own post-remove step,
  which drops `/usr/bin/kirocrew-desktop` and the installed AppArmor profile. It
  deliberately leaves the data home alone: `~/.kiro/crew` holds your sessions,
  memory and credentials, so it is yours to remove (see
  [Removing user data](#removing-user-data)), not the package manager's.
- App Kit uninstall preserves `apps/<name>/data/` by default. Deleting that app
  data is a separate, explicit action:
  `kirocrew app uninstall NAME --purge-data`, or unchecking **Keep app data** in
  the confirmation dialog.

**Windows NSIS uninstaller behavior.** `nsis.oneClick` is false and
`nsis.deleteAppDataOnUninstall` is left false, so the uninstaller removes only
the application install directory and its shortcuts; it never resolves or removes
the Kiro Crew home, which lives outside the install directory. Each signed Windows
installer must pass an install, create-sentinel-under-`~/.kiro/crew`,
uninstall, verify-sentinel smoke test before release. A separate Kiro-family
uninstaller could remove the parent `~/.kiro/` directory; it must exclude
`~/.kiro/crew` or prompt explicitly. That release-blocking cross-product
sign-off is tracked in
[issue #355](https://github.com/kirodotdev/KiroCrew/issues/355).

## Next steps

- [Slack setup](slack-setup.md): create and configure the Slack app.
- Other channels: [Discord](../../src/kiro_crew/docs/discord-integration.md),
  [Telegram](../../src/kiro_crew/docs/telegram-integration.md),
  [Teams](../../src/kiro_crew/docs/teams-integration.md),
  [Webex](../../src/kiro_crew/docs/webex-integration.md),
  [WeCom](../../src/kiro_crew/docs/wecom-integration.md),
  [WeChat](../../src/kiro_crew/docs/weixin-integration.md), and
  [WhatsApp](../../src/kiro_crew/docs/whatsapp-integration.md).
- [Remote and mobile access](remote-and-mobile.md): 24/7 operation on a remote
  host, and reaching the dashboard from a phone.
- [Architecture overview](../architecture/overview.md): system diagrams and the
  component map.
- [Security deep dive](../architecture/security-deep-dive.md): sandbox,
  governance, and denied commands.
- [App Kit guide](../app-kit/getting-started.md): build apps that extend
  Kiro Crew.
- [CONTRIBUTING.md](../../CONTRIBUTING.md): development setup and the PR
  workflow.
