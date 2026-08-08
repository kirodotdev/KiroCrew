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

## Prerequisites

| Requirement | Needed for | Floor |
|-------------|------------|-------|
| **Python** | Backend | `>= 3.10` (`requires-python` in `pyproject.toml`; `make build` provisions a 3.12 `.venv` by default) |
| **Node.js + npm** | Building the dashboard | `20 \|\| >= 22` (`website/package.json` `engines`); `ensure-node.sh` targets 20, and drops to 16 on Amazon Linux 2 where newer official builds need a glibc that host does not have |
| **`kiro-cli`** | Driving the LLM | Required; see below |

Node is only needed to *build* the dashboard. The prebuilt wheel, the DMG, and
the AppImage all ship the dashboard already bundled, so end users of those
artifacts need neither Node nor a compiler.

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

If the host has no Python 3.10+, the installer installs one from your distro:
`apt` on Debian/Ubuntu (including the split `python3-venv` package), `dnf` on
Amazon Linux / RHEL / CentOS Stream, and `yum` on CentOS 7. Where no base-repo
package supplies 3.10+ (CentOS 7 ships 3.6, older Ubuntu 3.8) it uses an
**already-installed** [mise](https://mise.jdx.dev/) python-build-standalone
interpreter if you have one (it runs on the older glibc those releases carry);
otherwise it prints how to get a newer Python and stops. The signed installer
never pipes an unsigned third-party script into a shell — to use the mise path,
install mise yourself first (`curl https://mise.run | sh`). When it finishes it
prints the next step: `kirocrew gateway` to start now, or `kirocrew service
install` to run it as a service.

### b. From source (development)

Build the dashboard, install the backend into a local virtualenv (`.venv`), and
run the gateway straight out of `src/`:

```bash
make build                                   # npm build + editable backend install into .venv
PYTHONPATH=src python -m kiro_crew gateway   # -> http://localhost:5476
```

`make build` runs two steps:

1. **`frontend`**: `npm ci` (or `npm install`) + `npm run build` in `website/`,
   then copies `website/dist` into `src/kiro_crew/static/dist` so the backend
   serves the SPA.
2. **`backend`**: creates `.venv` and runs an editable install with the `dev`
   extra (`pip install -e ".[dev]"`).

Both targets bootstrap their toolchain first (`ensure-node.sh`,
`ensure-python.sh`) and fall back to whatever is on `PATH` if that fails. The
backend target refuses to build a venv from an interpreter older than 3.10
rather than letting the install backtrack forever.

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

Optional extras (install with e.g. `pip install "kirocrew[voice]"`):

| Extra | Adds | For |
|-------|------|-----|
| `voice` | `boto3`, `amazon-transcribe` | Speech-to-text transcription |
| `otlp` | `opentelemetry-exporter-otlp-proto-http` | OTLP/HTTP metrics export. Installing it does not enable egress; that still needs an explicit `telemetry.otlp_endpoint` |
| `perf` | `py-spy` | Out-of-process profiling (`kirocrew perf sample --pid`). The in-process sampler needs nothing extra |
| `teams` | `PyJWT[crypto]` | Microsoft Teams channel (validates the inbound Bot Framework RS256 JWT) |
| `desktop` | `pyinstaller` | Building a frozen backend from `packaging/kirocrew-backend.spec` |
| `dev` | pytest, black, isort, flake8, mypy, ... | Contributor tooling; what `make build` installs |

The `desktop` extra exists, but neither `make desktop` nor `make backend-bin`
needs it: both run `packaging/build-desktop.sh`, which embeds a
python-build-standalone interpreter instead of freezing one. PyInstaller is only
required if you invoke `packaging/kirocrew-backend.spec` directly.

### d. Bundled desktop app

A double-clickable app that embeds a python-build-standalone (PBS) interpreter
plus uv-installed deps inside an Electron shell. End users need no Python, pip,
npm, or Node:

```bash
make desktop
```

Output is a DMG (plus a zip) on macOS and an AppImage on Linux, under
`website/electron/dist/`. On macOS the default is ONE universal DMG: the
Electron shell is lipo-merged, and the backend, which cannot be lipo-merged,
ships as two complete PBS trees selected at launch by `process.arch`. The
x86_64 backend is built under Rosetta 2, so a universal build needs an
Apple-Silicon host; `UNIVERSAL=0` forces a faster host-arch-only build. Linux is
always host-arch.

Prebuilt downloads for the release channels are linked from the
[README](../../README.md#app-downloads). Windows has no desktop build yet:
run the gateway from a source install and open the dashboard in a browser.

See [desktop-app.md](../build/desktop-app.md) for the full pipeline (frontend,
PBS provisioning, pip install, pruning, electron-builder) and how the app
locates and launches the bundled backend.

### e. Docker

For always-on servers the gateway also ships as a public multi-arch image on
GHCR. See [docker.md](docker.md).

## Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Frontend (npm/Vite) + backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled, into `dist/` |
| `make backend-bin` | Frozen standalone backend binary (host arch only) |
| `make desktop` | Full desktop app: DMG on macOS, AppImage on Linux |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, and caches |

Override the Python interpreter with `make PY=python3.12 build`.

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
directory, Slack credentials, the slash-command name, timezone, dashboard URL,
and (on macOS) the desktop app. It does NOT install `@playwright/mcp` or register
the browser proxy: Browser Mode is a durable toggle you turn on later in
**Settings → Browser**, and enabling it there is what downloads `@playwright/mcp`
plus the selected engine's browser binary and registers the compression proxy.

**Answering "n" to "Configure Slack tokens?" leaves Slack disabled and gives you
dashboard-only mode.** The web dashboard is fully functional without any
messaging credentials; add Slack later when you want to reach the same agent
away from your desk.

Two flags narrow the wizard:

| Flag | Effect |
|------|--------|
| `--agent-only` | Install the agent config and stop, skipping the workspace and every credential prompt |
| `--clean` | Fresh agent config: ignore the existing `kirocrew.json` and regenerate from defaults instead of merging your MCP servers and tools forward |
| `--electron-only` | Install only the macOS desktop app |

The two combine: `kirocrew setup --agent-only --clean` rebuilds the agent config
from scratch and touches nothing else. That is the fix for a broken or stale MCP
configuration, because without `--clean` the existing file is used as the base
so all user customizations survive.

## Configuration

- Config file: `~/.kiro/crew/config.json`, managed with
  `kirocrew config get/set/edit`.
- Credentials: `~/.kiro/crew/.env` holding `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`,
  and `KIROCREW_OWNER_ID`. See [slack-setup.md](slack-setup.md) for creating the
  Slack app.

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

For always-on operation (Slack bot, cron jobs, background tasks):

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
exactly one permission (`userns`) and is applied by systemd to the kirocrew
service only, via `AppArmorProfile=-kirocrew-userns` in the unit. It is a
**named** profile with no attachment path, so it cannot apply to any other
process, and it is the same approach stock Ubuntu already uses for `chrome` and
`brave`.

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
terminal) does not pick up the profile, because systemd is what applies it —
and there is no unprivileged way to enter it yourself. `aa_change_onexec()` into
a named profile is not permitted for an ordinary unconfined user, and `aa-exec`
does **not** fail when it cannot transition: it execs the command unconfined, so
`aa-exec -p kirocrew-userns -- kirocrew gateway` appears to work and changes
nothing. Run the gateway as the service instead.

### The AppImage (desktop app) needs its own profile

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

The `kiro-cli` backend did not answer in time. Four common causes:

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

## Uninstall and data retention

Uninstalling Kiro Crew preserves `$KIROCREW_HOME` (`~/.kiro/crew` by default).
That directory holds configuration, credentials, memory, sessions, apps, and the
audit chain, and none of the repository-controlled uninstall paths remove it:

- `kirocrew service uninstall` removes only the systemd unit (plus the AppArmor
  profile it installed) or the launchd plist.
- Python and npm package removal has no `preuninstall` or `postuninstall`
  cleanup hook.
- The macOS DMG/zip and the Linux AppImage have no cleanup hook, so removing the
  application bundle or image leaves the data home intact.
- App Kit uninstall preserves `apps/<name>/data/` by default. Deleting that app
  data is a separate, explicit action:
  `kirocrew app uninstall NAME --purge-data`, or unchecking **Keep app data** in
  the confirmation dialog.

There is intentionally no implicit whole-home purge. Back up with `kirocrew
snapshot` before manually removing a data home you no longer need.

**External certification dependency.** Windows desktop releases use an NSIS
installer, whose uninstaller electron-builder generates. `nsis.oneClick` is false
and `nsis.deleteAppDataOnUninstall` is left false, so the uninstaller removes only
the application install directory and its shortcuts; it never resolves or removes
the Kiro Crew home, which lives outside the install directory. Each signed Windows
installer must nevertheless pass an install, create-sentinel-under-`~/.kiro/crew`,
uninstall, verify-sentinel smoke test before release. A separate Kiro-family
uninstaller could remove the parent `~/.kiro/` directory; it must exclude
`~/.kiro/crew` or prompt explicitly. That release-blocking cross-product
sign-off is tracked in
[issue #355](https://github.com/kirodotdev/KiroCrew/issues/355).

## Next steps

- [Slack setup](slack-setup.md): create and configure the Slack app.
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
