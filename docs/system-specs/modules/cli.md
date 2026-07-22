# CLI Module

Last Updated: 2026-07-09 (cloud launch resume safety)

## Overview

The CLI module (`kiro_crew/cli.py`) provides the `kirocrew` command using stdlib `argparse`.

## Project Directory Detection

At startup, `main()` auto-detects the project root and sets `KIROCREW_PROJECT_DIR`:

1. If `KIROCREW_PROJECT_DIR` env var is already set, use it
2. Walk up from CWD looking for a directory with both `skills/` and `src/kiro_crew/` (`_PROJECT_MARKERS`). The project-level `agents/` dir was removed when agent config was consolidated into `src/kiro_crew/config/` (commit bbbc1f6e), so the marker no longer references it — a stale `agents/` requirement left detection (and the dashboard changelog) silently broken.
3. Read saved path from `~/.kirocrew/project_dir` (written by `kirocrew setup`); the saved path is re-validated against the same markers

This allows `kirocrew` to find project-level agent config and skills from any directory.

## Commands

| Command | Description |
|---------|-------------|
| `kirocrew chat -m "msg"` | Send a single message, print streaming response |
| `kirocrew chat` | Interactive chat mode (readline, exit with Ctrl+D) |
| `kirocrew chat --model X` | Override model for this session |
| `kirocrew gateway` | Start the KiroCrew server (dashboard + Slack) |
| `kirocrew gateway --slack-only` | Start without dashboard or SSH tunnel instructions |
| `kirocrew gateway --no-crons` | Start without cron scheduler (use when another instance handles crons) |
| `kirocrew setup` | Install agent config, save project dir, configure credentials |
| `kirocrew setup --agent-only` | Only install agent config (skip credentials) |
| `kirocrew doctor` | Verify kiro-cli is installed and config is valid |
| `kirocrew cron add/list/remove` | Manage cron jobs |
| `kirocrew spawn run/list` | Manage background subagents |
| `kirocrew learn add/list/remove` | Manage learned corrections |
| `kirocrew run TASK.md` | Run an autonomous task from a spec file |
| `kirocrew token` | Print a dashboard access URL with auth token |
| `kirocrew logout` | Revoke all active dashboard sessions |
| `kirocrew manifest` | Generate Slack manifest with user alias auto-populated |
| `kirocrew update` | Update to latest version (git pull + rebuild) |
| `kirocrew status` | Show runtime stats from running gateway |
| `kirocrew stop` | Stop a running gateway (service-aware: stops the systemd/launchd service if active, otherwise terminates the gateway found by a cross-platform port lookup — lsof on POSIX, netstat on Windows). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew restart` | Restart a running gateway (service-aware: restarts the systemd/launchd service if active, otherwise terminates the foreground gateway and respawns it detached). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew service install` | Install gateway as a system-level systemd service (Linux, requires sudo for `tee` + `systemctl` only) or launchd LaunchAgent (macOS, no sudo). Auto-restarts on crash, auto-starts on boot. |
| `kirocrew service uninstall` | Stop and remove the systemd unit / launchd plist. |
| `kirocrew service status` | Show service status (`systemctl status` or `launchctl list`). No sudo required. |
| `kirocrew logs` | Tail gateway logs from the systemd journal, launchd stdout file, or `~/.kirocrew/gateway.log`. |
| `kirocrew logs -f` | Follow logs live (long-running tail). |
| `kirocrew cloud launch/list/status/connect/stop/start/destroy/iam-policy/doctor` | Provision, connect to, and manage a KiroCrew EC2 instance in the user's AWS account. |
| `kirocrew security events` | Show recent SEL audit events (`-n N` for count) |
| `kirocrew security verify` | Verify SEL HMAC chain integrity |
| `kirocrew snapshot` | Create a .tar.gz snapshot of all KiroCrew state |
| `kirocrew snapshot --keep N` | Auto-prune to N most recent snapshots (default 7) |
| `kirocrew snapshot --list` | List existing snapshots |
| `kirocrew restore <file>` | Restore from a snapshot (auto-detects replace vs merge) |
| `kirocrew restore <file> --mode replace\|merge` | Force restore mode |
| `kirocrew restore <file> --components X,Y` | Selective component restore |
| `kirocrew restore <file> --dry-run` | Preview restore without writing |
| `kirocrew restore --list-components` | Show available component names |
| `kirocrew config get [key]` | Print full config or a dot-path value |
| `kirocrew config set <key> <val>` | Set a config value (auto type detection) |
| `kirocrew config set --file <path>` | Replace config from a JSON file |
| `kirocrew config edit` | Open config in `$EDITOR` |
| `kirocrew memory show/edit` | Show or edit memory (preferences, projects, history) |
| `kirocrew mcp-cron` | MCP server for cron tools (spawned by kiro-cli) |
| `kirocrew mcp-core` | MCP server for spawn, learn, task tools (spawned by kiro-cli) |
| `kirocrew --version` | Print version |

## Setup Command

`kirocrew setup` performs:

1. Saves `KIROCREW_PROJECT_DIR` to `~/.kirocrew/project_dir`
2. Installs agent config to `~/.kiro/agents/kirocrew.json`
3. Prompts for Slack credentials (unless `--agent-only`)
4. Offers to set up custom domain `kirocrew.localhost` (macOS/Linux)

The saved project dir enables running `kirocrew` from any directory.

### Custom Domain

After credentials, `kirocrew setup` offers to add `127.0.0.1 kirocrew.localhost` to the system hosts file so the dashboard is accessible at `http://kirocrew.localhost:5476`:

- **macOS/Linux**: Uses `sudo tee -a /etc/hosts` for safe append

Skipped if `kirocrew.localhost` is already present or user declines.

## Cloud Command

`kirocrew cloud` is a human installer/control-plane surface for running
KiroCrew on the user's own AWS EC2 instance. Provisioning and teardown are not
LLM-facing tools. AWS credentials are resolved by the AWS CLI; KiroCrew stores
only profile, region, and the most recent instance tag in `cloud.json`.

`kirocrew cloud launch` runs a six-step wizard: check AWS reachability, explain
permissions, choose whether to keep an existing deployment or create a new one,
choose an instance size when creating a new stack, deploy or resume the
CloudFormation stack, sign in the remote `kiro-cli`, and open the dashboard
through SSM port forwarding. Launch is resume-safe by default: if `cloud.json`
contains a `last_tag` whose stack still exists in the same saved profile/region,
rerunning interactive `launch` offers to keep/resume that stack or create a new
installation. If `cloud.json` is missing or stale, launch discovers existing
`kirocrew-*` CloudFormation stacks with `cloudformation:ListStacks` and offers a
choice to resume one or create a new installation. `kirocrew cloud launch --new`
is the explicit escape hatch for creating a separate new stack. `--yes` keeps a
single or saved existing stack; if multiple unsaved stacks exist it fails closed
instead of choosing one arbitrarily. For a new launch, the generated tag is
written to `cloud.json` before the long CloudFormation deploy starts, so an
interrupted provisioning run can be found on the next launch attempt.

Launch and connect require the local AWS Session Manager plugin for
`AWS-StartPortForwardingSession`. If `session-manager-plugin` is missing,
`cloud launch` prompts to install AWS's official package for the current local
platform (macOS `.pkg`, Debian/Ubuntu `.deb`, or RPM Linux `.rpm`) before the
wizard reaches sign-in/dashboard tunneling. `--yes` accepts this installer
prompt. `cloud connect` performs the same check and installer prompt before
opening the dashboard tunnel. If installation is declined or fails, the command
exits non-zero and tells the user to retry after fixing the local prerequisite.

The instance-size picker supports arrow keys in an interactive terminal
(`↑`/`↓`, `j`/`k`, digit shortcuts, Enter to select) and falls back to the
numbered prompt for non-TTY input. Ctrl-C must interrupt prompts and long AWS
subprocesses; unhandled cloud-command interrupts return exit code 130.

Remote Kiro sign-in prefers the device-code flow over SSM. The launcher starts
`kiro-cli login --use-device-flow` as a background process on the instance,
captures the URL/code from its log, and leaves that same process alive while the
wizard polls for completion. It must not kill that process after scraping the
prompt or start a second hidden device-code flow. If device-code startup does
not produce an actionable URL, launch falls back to the Google/GitHub callback
flow automatically: it starts `kiro-cli login` on the instance with FIFO-backed
stdin, captures the printed loopback callback port, opens an
`AWS-StartPortForwardingSession` from the same local port to the remote port,
sends the Enter continuation back to the remote CLI, then opens or prints the
local browser URL. The temporary callback tunnel is closed after the sign-in
poll completes. In headless local terminals, browser auto-open is skipped and
the URL is printed for manual opening.

`kirocrew cloud connect` mints a dashboard token over SSM, opens an
`AWS-StartPortForwardingSession`, waits for the local tunnel port to accept TCP
connections, and opens or prints the local dashboard URL. If the tunnel port
does not become reachable, the command reports failure, does not present the
dashboard URL as usable, and does not keep a dead tunnel process open. If final
dashboard opening fails during `cloud launch`, the instance remains running but
launch returns non-zero and tells the user to rerun `kirocrew cloud connect`
after fixing the local SSM tunnel issue.

## Config Command

`kirocrew config` manages `~/.kirocrew/config.json`:

- **get** — prints full effective config (with defaults resolved) or a single dot-path value
- **set key value** — sets a value with auto type detection (bool/int/float/JSON/string). Rejects unknown leaf keys.
- **set --file path** — replaces entire config from a JSON file. File read routed through `hooks.safe_read_file()` (blocks sensitive paths).
- **edit** — opens config in `$EDITOR` (supports args like `code --wait` via `shlex.split`). Creates default config if missing.

All write paths emit SEL audit events (`config_get`, `config_set`, `config_set_file`, `config_edit`).

### Gateway Auto-Create

`kirocrew gateway` creates `~/.kirocrew/config.json` with defaults if the file doesn't exist. Does nothing if it already exists.

## Verbosity

| Flag | Level | What you see |
|------|-------|-------------|
| (none) | WARNING | Errors only |
| `-v` | INFO | Session lifecycle, context %, compaction |
| `-vv` | DEBUG | ACP events, message updates, full traces |

## Interactive Mode

- Prompt: `you> `
- Exit: `exit`, `quit`, `/exit`, `/quit`, `:q`, Ctrl+D
- Streaming output printed as chunks arrive

### Context Tracking

After each message, checks `provider.context_usage_pct()`:
- `>= autocompact_pct` (default 90%): compact → shutdown → restart provider, reset counter
- `>= 75%`: warning printed to stderr

CLI compaction is blocking (single-user, acceptable).

## Entry Point

`console_scripts` in `setup.cfg` maps `kirocrew` → `kiro_crew.cli:main`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `KIROCREW_HOME` | Override config/data directory (default `~/.kirocrew`) |
| `KIROCREW_PORT` | Override dashboard port (default `5476`, validated as int at CLI startup) |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory |
| `KIROCREW_WORKSPACE` | Override workspace root directory |

For local dev:
- **macOS/Linux**: `bin/kirocrew` (POSIX shell wrapper); `source setup.sh` adds `bin/` to PATH

The wrapper sets `KIROCREW_PROJECT_DIR` and routes to the right runtime based on install type:

- **One-liner install** (`install.sh` clones the repo into `~/.kirocrew-app/`): if a sibling `.venv/bin/kirocrew` exists, the wrapper execs it directly.
- **pip editable install** (`pip install -e .`): the console_scripts entry point resolves directly.

## Setup Scripts (First-Time Bootstrap)

`setup.sh` (macOS/Linux) auto-installs all dependencies from scratch using public tooling only.

> **Note:** Windows is not supported.

**Install order:**
1. Node.js (via `ensure-node.sh`)
2. Optional tools (git-lfs, ffmpeg for voice)
3. kiro-cli (`npm i -g`)
4. kiro-cli login (guided authentication)
5. Frontend build (`npm install && npm run build`)
6. Backend build (`pip install -e .`)
7. PATH setup + shell profile persistence
8. `kirocrew setup --agent-only` (install kiro-cli agent config)
9. Optional Slack credential configuration

Each step checks if the tool is already installed and skips if present.

## Doctor Checks

1. `kiro-cli` binary in PATH
2. Project directory and git repo
3. Agent config installed
4. Config values (provider, model, approval mode, dashboard port)
5. **MCP tools**: `@kirocrew-cron` and `@kirocrew-core` in `tools`, `allowedTools`, and `mcpServers` — auto-fixes missing entries
6. **Global mcp.json**: kirocrew MCP servers present with valid binary paths — auto-fixes stale paths
7. **Python environment**: checks Python 3.9+ availability and dependency installation
8. **Vector memory (in-process embeddings)**: vendored llama-cpp-python runtime importable, embedding model file present (downloads in background on gateway start; when absent, a light HTTPS-reachability probe of the resolved model URL runs); embeddings are always-on (`embeddings:  ✅ always-on`). On platforms with no vendored native libs (`_platform_libs_dirname()` returns None, e.g. darwin/x86_64 — Intel Macs or a Rosetta interpreter), the runtime line reports `⏹ unsupported platform … — memory uses keyword search` and is NOT counted as an issue (designed degradation per `embeddings.py`); only a load failure on a supported platform flags `embedding runtime`
9. Slack credentials (optional)
10. kiro-cli connectivity
11. Gateway running status

## Update Command

`kirocrew update` pulls the latest source and rebuilds:

1. `git pull` from `KIROCREW_PROJECT_DIR`
2. Rebuilds frontend via `build-frontend.sh` (non-fatal on failure)
3. Reinstalls backend via `pip install -e .`

## Stop Command

`kirocrew stop [--port PORT]` stops a running gateway:

1. If a systemd/launchd service is active **and** the caller did not pass
   `--port` explicitly (see Service Management), stop it via the service
   manager and return — without this branch, SIGTERM-by-port would be
   racing the manager's auto-restart.
2. Otherwise (no service active, or `--port` was passed explicitly to
   target a non-default dev gateway): `platform_compat.find_listening_pids(port)`
   to find PIDs — `lsof -ti TCP:{port} -sTCP:LISTEN` on POSIX, `netstat -ano`
   parsing on Windows (there is no `lsof` there; this previously made
   `kirocrew stop` a no-op on Windows). `listening_pid_tool_available()`
   distinguishes "no listener" from "lookup tool missing".
3. `platform_compat.process_command_line(pid)` to verify it's a KiroCrew process —
   `/proc/<pid>/cmdline` (Linux), `ps -o command=` (macOS), `Win32_Process.CommandLine`
   via WMI (Windows). The Windows venv `kirocrew.exe` re-execs `python.exe`, so the
   match is on the command line (`-m kiro_crew gateway` / `\Scripts\kirocrew.exe gateway`),
   not the image name.
4. Terminate each verified PID: `os.kill(SIGTERM)` on POSIX; `taskkill /T /F`
   (via `platform_compat.kill_process_tree`) on Windows so the gateway's detached
   children are reaped too. Liveness is probed with `platform_compat.pid_exists`
   (a raw `os.kill(pid, 0)` would *terminate* the process on Windows).
5. Waits up to 1s for exit.
6. SEL audit event logged.

## Restart Command

`kirocrew restart [--port PORT]` restarts a running gateway. Mirrors
`stop`'s service-aware structure:

1. If a systemd/launchd service is active **and** the caller did not
   pass `--port` explicitly, ask the platform to restart it. On Linux:
   `sudo systemctl restart kirocrew.service` (single
   atomic operation, smaller down-window than stop+start, and the
   supervisor stays in charge of the lifecycle the whole time). On
   macOS: `launchctl unload <plist>` + `launchctl load <plist>` (no
   `-w`, so persistent enable state is unchanged). The deprecated
   `launchctl restart` is avoided because under `KeepAlive` it behaves
   like `stop` (SIGTERM + immediate respawn) and never re-reads the plist.
2. Otherwise (foreground gateway, no service, or `--port` passed
   explicitly to target a non-default dev gateway):
   - `platform_compat.find_listening_pids(port)` (lsof on POSIX, netstat
     on Windows) to detect a running gateway. If found — OR if the lookup
     tool is absent (`not listening_pid_tool_available()`, so a missing
     tool is not mistaken for a dead gateway) — run the existing `_stop`
     kill-by-port path. If not (e.g. the user runs `restart` after a
     crash), skip the stop step rather than erroring — the user expects to
     end up with a running gateway either way. The `_stop` call is wrapped
     in a `try / except SystemExit` so a TOCTOU race (gateway exits between
     the listener check and `_stop`'s own lookup → `_stop` calls
     `sys.exit(1)`) does not abort the restart before the spawn.
   - Spawn a detached `kirocrew gateway` via `subprocess.Popen`, stdin set
     to `subprocess.DEVNULL`, and stdout + stderr redirected to
     `~/.kirocrew/gateway.log` (the same file the `kirocrew logs` command
     tails for foreground gateways). Detach is per-platform: POSIX uses
     `start_new_session=True`; Windows uses `creationflags=DETACHED_PROCESS
     | CREATE_NEW_PROCESS_GROUP` (there is no setsid) — both via
     `platform_compat`. The shell returns immediately and the user can
     follow logs via `kirocrew logs -f`.
3. SEL audit event logged with `via=service` or `via=fork pid=<n>` so
   the audit trail distinguishes the two paths.

## Service Management

`kirocrew service {install,uninstall,status}` registers the gateway
with the OS service manager so it survives SSH disconnects, restarts
on crash, and starts on boot. Implemented in `src/kiro_crew/service/`.

- **Linux** (`current_platform() == SYSTEMD`):
  - Unit file: `/etc/systemd/system/kirocrew.service` (root-owned).
  - Install: `sudo tee` writes the unit, then `sudo systemctl
    daemon-reload && sudo systemctl enable --now kirocrew.service`.
  - The gateway runs as `User=$USER Group=$(id -gn)` — kirocrew
    code never runs under sudo. Only `tee` and `systemctl` invocations
    are elevated.
  - Boot survival via `WantedBy=multi-user.target` (no linger needed —
    that's a user-service concept; this is system-level).
  - Crash-loop safety: `StartLimitBurst=3 StartLimitIntervalSec=300`.
  - Logs are read from the journal: `sudo journalctl -u kirocrew -f`,
    or unprivileged if the user is in `systemd-journal` / `adm`.
- **macOS** (`current_platform() == LAUNCHD`):
  - Plist: `~/Library/LaunchAgents/dev.kirocrew.gateway.plist`
  - Install: `launchctl load -w <plist>`. `RunAtLoad=true` and
    `KeepAlive` ensure auto-start and crash recovery.
  - Stdout and stderr are written to
    `~/Library/Logs/KiroCrew/gateway.{log,err}`.
- **Other platforms**: install/uninstall return exit code 2 with a
  message pointing to manual setup.

`kirocrew stop` is service-aware: if the service is active it calls
the platform's stop instead of SIGTERM, so the manager does not
immediately restart the gateway under us.

## Logs Command

`kirocrew logs [-n LINES] [-f]` tails the gateway log from whichever
source is most appropriate:

1. systemd journal if the system service is installed on Linux. Tries
   unprivileged `journalctl` first; falls back to `sudo journalctl`
   only if the unprivileged probe returns no rows.
2. launchd stdout file if a plist exists on macOS
3. `~/.kirocrew/gateway.log` for foreground gateways

Uses `os.execvp` so signals (Ctrl+C) propagate naturally to the
underlying `journalctl`/`tail` process.

## Dashboard Self-Update

On gateway startup and every 12 hours, a background task runs `git fetch`
and compares the remote `__version__` with the local version. Only triggers
when the remote version is strictly higher (commits without a version bump
are ignored).

- Topbar shows `📦 v0.1.3` badge — click to check and view changelog
- If newer version found: badge turns into "📦 Update Available"
- Clicking opens a dismissible changelog modal with rendered markdown
- "Update Now" button: `git pull` → rebuild → `os.execv()` restart
- Health indicator shows "Updating…" during the process
- SSE auto-reconnects when the new process starts

## Status Command

`kirocrew status` queries the running gateway's `/api/status` endpoint
and prints uptime, sessions, messages, tool calls, subagents, crons, lessons.
