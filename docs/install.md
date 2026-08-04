# Installing & Building KiroCrew

This guide covers how to build, install, and run KiroCrew. There are three ways
to do it, from lightest (a developer checkout) to heaviest (a double-clickable
desktop app). All builds are driven by the repo-root [`Makefile`](../Makefile)
and use plain `pip` + `npm`/Vite + `pytest` — there is no proprietary build
tooling.

> **Platforms: macOS, Linux, and Windows.** macOS/Linux use the `Makefile` +
> `setup.sh` path below; Windows runs natively from a Python source install
> (`pip install -e . tzdata`, launched via `python -m kiro_crew gateway`). All
> POSIX-only process/signal/file-lock/metrics calls are routed through
> `kiro_crew.platform_compat`. See
> [windows-install.md](windows-install.md) for the Windows setup steps.

## Prerequisites

| Requirement | Needed for | Notes |
|-------------|------------|-------|
| **Python 3** | Backend | `pip` install; `make build` creates a `.venv` |
| **Node.js + npm** | Frontend (dashboard) | Builds the React/Vite SPA; also for the desktop app |
| **An agent backend** | Driving the LLM | `kiro-cli` — see below |
| **Ollama** (optional) | Memory / knowledge embeddings | Graceful degradation if absent |

### Agent backend (required)

KiroCrew drives an LLM through the **`kiro-cli`** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP). It is the only provider (`agent.provider = acp`).

Install `kiro-cli` per its own docs, make sure it is on your `PATH`, and log in:

```bash
kiro-cli login
```

`kirocrew doctor` reports whether `kiro-cli` is found and logged in.

### Ollama (optional — for memory / knowledge embeddings)

Memory and the knowledge library use a local [Ollama](https://ollama.com) server
for embeddings. If Ollama is absent, KiroCrew degrades gracefully (embedding
search is disabled) rather than crashing.

```bash
# Install Ollama from https://ollama.com, then pull the embedding model:
ollama pull qwen3-embedding:0.6b      # default
# or the documented fallback:
ollama pull nomic-embed-text
```

Ollama runs at `http://localhost:11434` by default.

## The three ways to run

### a. From source (development)

Build the dashboard, install the backend into a local virtualenv (`.venv`), and
run the gateway directly from `src/`:

```bash
make build                                   # npm build + editable backend install into .venv
PYTHONPATH=src python -m kiro_crew gateway   # → http://localhost:5476
```

`make build` runs two steps:

1. **`frontend`** — `npm ci` (or `npm install`) + `npm run build` in `website/`,
   then copies `website/dist` into `src/kiro_crew/static/dist` so the backend
   serves the SPA.
2. **`backend`** — creates `.venv` and runs an editable install (`pip install -e .`).

You can also invoke any CLI subcommand the same way, e.g.
`PYTHONPATH=src python -m kiro_crew setup` or
`PYTHONPATH=src python -m kiro_crew doctor`.

### b. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
that has Python:

```bash
make wheel                # builds the frontend, then python -m build --wheel → dist/
pip install dist/*.whl    # → installs the kirocrew / kirocrew-browse commands onto PATH
kirocrew gateway          # → http://localhost:5476
```

The wheel is `dist/kirocrew-0.1.0-*.whl`. The dashboard is bundled into the
package via the custom `BuildWithFrontend` build step in
[`setup.py`](../setup.py); the pip install name is **`kirocrew`** (the import
package is `kiro_crew`).

Installed console scripts:

| Command | Entry point |
|---------|-------------|
| `kirocrew` | `kiro_crew.cli:main` |
| `kirocrew-browse` | `kiro_crew.browser.cli:main` |

Optional extras (install with e.g. `pip install kirocrew[voice]`):

| Extra | Adds |
|-------|------|
| `voice` | `boto3`, `amazon-transcribe` for speech-to-text |
| `aws` | `boto3` for AWS integrations |
| `desktop` | `pyinstaller` for building the frozen backend (REMOVED — desktop builds use python-build-standalone + uv) |

### c. Bundled desktop app

Build a double-clickable desktop app that embeds a python-build-standalone
interpreter + uv-installed deps inside an Electron shell. End users need **no**
Python, pip, npm, or node:

```bash
make desktop              # → website/electron/dist/KiroCrew-*.dmg (macOS)
                          #   or website/electron/dist/KiroCrew-*.AppImage (Linux)
```

See [desktop-app.md](desktop-app.md) for the full build pipeline (frontend →
python-build-standalone → pip install → electron-builder) and how the app
locates and launches the bundled backend.

## Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Build the frontend (npm/Vite) + install the backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled → `dist/` |
| `make desktop` | Full desktop app — DMG (macOS) / AppImage (Linux) |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, and caches |

Override the Python interpreter with `make PY=python3.12 build`.

## Configure and run

After installing (any of the three methods), set up and verify:

```bash
kirocrew setup            # interactive wizard: data dir, agent, credentials
kirocrew doctor           # verify everything is wired up
kirocrew gateway          # start the server → open http://localhost:5476
```

From a source checkout, prefix with `PYTHONPATH=src python -m kiro_crew` instead
of `kirocrew`.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KIROCREW_HOME` | `~/.kiro/crew` | Data directory (config, credentials, databases) |
| `KIROCREW_PORT` | `5476` | Port the gateway / dashboard listens on |

- Config file: `~/.kiro/crew/config.json` (manage via `kirocrew config get/set/edit`).
- Credentials: `~/.kiro/crew/.env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`).

> **Data home moved under `~/.kiro/`.** KiroCrew now stores its data in
> `~/.kiro/crew` (was the top-level `~/.kirocrew`), sharing the `~/.kiro/` base
> with other Kiro-family apps. An existing `~/.kirocrew` install migrates
> automatically on first launch — its data (config, credentials, session
> history, databases) is copied into `~/.kiro/crew`, OVERWRITING any file
> already there, and the old `~/.kirocrew` directory is then **deleted
> outright**. There is no rollback copy. Re-downloadable bulk content (the
> embedding `models/` and rebuildable `cache/`) is **not** copied — the new home
> regenerates it on first start. Set `KIROCREW_HOME` to relocate the data home
> (e.g. outside `~/.kiro/` entirely) BEFORE upgrading if you want to keep the
> two homes separate.
>
> **No rollback.** Because `~/.kirocrew` is deleted (not archived) once the move
> completes, there is no supported way to go back to a release older than this
> move — that release knows nothing of `~/.kiro/crew` and would find no
> `~/.kirocrew` to read, starting empty. If you need to preserve your pre-move
> data, back it up yourself BEFORE upgrading, e.g.:
>
> ```bash
> cp -a ~/.kirocrew ~/.kirocrew.manual-backup
> ```

> **Note:** `KIROCREW_PORT` is an environment variable (validated at CLI entry),
> not a config key; it sets the port the gateway / dashboard binds to. You can
> also pass `--port` on the CLI to override it. The `dashboard.url` config key is
> only for advertising a remote URL.

## Uninstall and data retention

Uninstalling KiroCrew preserves `$KIROCREW_HOME` (`~/.kiro/crew` by default).
That directory contains configuration, credentials, memory, sessions, apps, and
the audit chain; none of the repository-controlled uninstall paths remove it:

- `kirocrew service uninstall` removes only the systemd unit or launchd plist.
- Python/npm package removal has no `preuninstall` or `postuninstall` cleanup hook.
- The macOS DMG/zip and Linux AppImage have no repository cleanup hook. Removing
  the application bundle/image therefore leaves the data home intact.
- App Kit uninstall preserves `apps/<name>/data/` by default. Deleting that app
  data is a separate, explicit action: `kirocrew app uninstall NAME --purge-data`
  or uncheck **Keep app data** in the confirmation dialog.

There is intentionally no implicit whole-home purge. Back up with `kirocrew
snapshot` before manually removing a data home you no longer need.

**External certification dependency.** Windows desktop releases use an NSIS
installer, whose uninstaller electron-builder generates. `nsis.oneClick` is
false and `nsis.deleteAppDataOnUninstall` is left false, so the uninstaller
removes only the application install directory and its shortcuts; it never
resolves or removes the KiroCrew home, which lives outside the install
directory. Each signed Windows installer
must nevertheless pass an install → create sentinel under
`~/.kiro/crew` → uninstall → verify sentinel smoke test before release. A
separate Kiro-family uninstaller could remove the parent `~/.kiro/` directory;
it must exclude `~/.kiro/crew` or prompt explicitly. That release-blocking
cross-product sign-off is tracked in
[issue #355](https://github.com/kirodotdev/KiroCrew/issues/355).

## Linux: the agent sandbox and unprivileged user namespaces

On Linux, KiroCrew isolates the agent by entering a **user namespace** and then a
**mount namespace**, over-mounting credential paths such as `~/.aws` and `~/.ssh`
so the agent cannot read them. If that sandbox cannot be built, KiroCrew
**refuses to run the agent** rather than running it without isolation — spawns
fail closed. This is deliberate and is not something to work around casually.

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

Where — and only where — this mechanism is the one in play, the installer also
writes `/etc/apparmor.d/kirocrew-userns` and loads it. The profile grants exactly
one permission (`userns`) and is applied by systemd to the kirocrew service only,
via `AppArmorProfile=-kirocrew-userns` in the unit. It is the same approach stock
Ubuntu already uses for `chrome`, `brave`, `bwrap-userns-restrict`, `buildah` and
others in the same position.

This uses the sudo prompt `service install` already needs for the unit file — no
additional privilege — and it **cannot fail your install**: if the profile cannot
be written, loaded, or verified, you get a warning and the install continues.
`kirocrew service uninstall` unloads and removes it.

On a host where the mechanism is absent (Debian, Arch, RHEL, Amazon Linux) or the
sysctl is already `0`, the step is skipped silently and nothing changes.

**Running the gateway outside systemd** (e.g. `kirocrew gateway` in a terminal)
does not pick up the profile, because systemd is what applies it. Use:

```bash
aa-exec -p kirocrew-userns -- kirocrew gateway
```

**Please do not "fix" this by setting the sysctl to 0.** That disables a
kernel-wide protection for every application on the machine to satisfy one
app-scoped need. The per-application profile exists precisely so you don't have to.

### Other reasons user namespaces can be denied

The AppArmor profile addresses only the Ubuntu restriction. These are different
mechanisms with different remedies, and they report different errnos — the
sandbox probe names the failing step so you can tell them apart:

| Symptom | Mechanism | Remedy |
|---|---|---|
| `unshare(CLONE_NEWNS)` fails `EPERM`, sysctl is `1` | Ubuntu ≥ 23.10 AppArmor userns restriction | `kirocrew service install` (this page) |
| `unshare(CLONE_NEWUSER)` fails `ENOSPC` / `EUSERS` | `user.max_user_namespaces=0` (CIS-hardened host) | raise that sysctl |
| `unshare` fails and `kernel.unprivileged_userns_clone=0` | Debian-family legacy knob (defaults to 1 since Debian 11) | set it to 1 |
| `unshare` fails `EINVAL` / `ENOSYS` | kernel built without `CONFIG_USER_NS` | none short of a different kernel |
| Fails inside Docker/Podman | the container's seccomp filter denies `unshare` | container run flags, **not** host config |
| RHEL/Fedora/Rocky/AL2023 | SELinux, not AppArmor | userns has been enabled since RHEL 8; the profile is inert here |

To see which step is failing on your host:

```bash
python3 -c "
import kiro_crew.sandbox as sb
sb.reset_backend(); print(sb.detect_backend(), sb._last_unshare_failure)"
```

## Troubleshooting

For runtime issues (ACP handshake timeouts, embedding/memory search, Slack,
MCP server cleanup), see the **Troubleshooting** section of the
[README](../README.md#troubleshooting). A quick health check is always:

```bash
kirocrew doctor
```
