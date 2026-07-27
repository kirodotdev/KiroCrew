# Running KiroCrew on a Remote Host (24/7)

Run KiroCrew on an always-on remote Linux host — a VPS, a cloud VM (EC2,
GCE, DigitalOcean, Hetzner, etc.), or any spare Linux box — so the Slack
bot, cron jobs, and task runner keep working while your laptop sleeps.

## Host Requirements

- **OS**: Any modern Linux distribution (Ubuntu 22.04+, Debian 12+,
  Fedora, Amazon Linux 2023, etc.). Node 20+ is needed for `slack-mcp`,
  so prefer a distro that ships or can install a recent Node.
- **RAM**: A Linux host with ~10GB+ RAM. KiroCrew itself uses ~10GB, but
  MCP cold starts and heavy tool calls can cause memory spikes beyond
  that, so give yourself headroom (16GB is comfortable).
- **CPU**: A couple of vCPUs is fine for a single user; extra cores help
  with CPU-intensive tool calls and parallel subagent execution.
- **Architecture**: x86_64 or arm64.

## Pre-Setup (SSH into the new host)

### 1. Install basics

You need Python 3 (3.11+), `pip`, `git`, and Node.js 20+ to install
KiroCrew and build the dashboard. `tmux` is handy for long-running
sessions. Install with your distro's package manager, for example:

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y git tmux python3 python3-pip python3-venv

# Fedora / Amazon Linux 2023
sudo dnf install -y git tmux python3 python3-pip
```

Install Node.js 20+ from your distro, [nodejs.org](https://nodejs.org/),
or a version manager such as [nvm](https://github.com/nvm-sh/nvm).

Optionally set your git identity if you plan to develop on this host:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

### 2. Install the agent backend

KiroCrew drives the `kiro-cli` agent over ACP. Install `kiro-cli` per its own
docs, make sure it is on your `PATH`, and log in:

```bash
kiro-cli login
```

See the [README](../README.md) for backend options and credentials.
Configure your provider credentials (e.g. an Anthropic API key) in
`~/.kirocrew/.env` as described in the README.

## Install KiroCrew

Install the Python backend (`pip`) and build the React dashboard (`npm`),
exactly as on a local machine — see the [Getting Started guide](getting-started.md)
for the full walkthrough:

```bash
# 1. Clone and install the backend
git clone https://github.com/kirodotdev/KiroCrew.git
cd kirocrew

# 2. Build the frontend bundle
cd website && npm install && npm run build && cd ..
cp -r website/dist src/kiro_crew/static/dist

# 3. Install the backend (bundles the dashboard)
pip install .

# 4. Configure
kirocrew setup
```

This installs the `kirocrew` command onto your `PATH`. See the
[README](../README.md) for the agent backend and Ollama setup.

After setup:
```bash
kirocrew doctor            # verify everything
kirocrew setup             # configure Slack tokens (optional)
```

## Run 24/7 (`kirocrew service install`)

The simplest path is the built-in installer. It registers a
system-level systemd unit (Linux) or launchd LaunchAgent (macOS), so
the gateway survives SSH disconnects, auto-restarts on crash, and
auto-starts on boot (Linux) or user login (macOS). On Linux the
install step prompts for sudo once to write the unit file and run
`systemctl`; the gateway itself runs as your user, not root:

```bash
kirocrew service install
```

Manage:

```bash
kirocrew service status      # check status
kirocrew logs -f             # tail live logs
kirocrew stop                # stop the service
kirocrew restart             # restart the service (atomic on systemd; unload+load on launchd)
kirocrew service uninstall   # remove the unit / plist
```

**Boot survival** is handled by the unit's `WantedBy=multi-user.target`
plus `enable --now`. Nothing extra needed. The gateway will start on
host reboot.

**Sudo scope:** `kirocrew service install` only runs `sudo tee`
(to write the unit file under `/etc/systemd/system/`) and `sudo
systemctl ...` (daemon-reload, enable, restart). No kirocrew / MCP /
LLM code path is invoked under sudo. Once started, the gateway runs
as `User=$USER Group=$(id -gn)` — not root.

**Alternative — manual recipe** (e.g. if you want to customize the
unit, or for hosts where the wrapped install isn't appropriate):

```bash
# Resolve the actual binary path at install time
KIROCREW_BIN=$(command -v kirocrew 2>/dev/null || echo "$HOME/.local/bin/kirocrew")

sudo tee /etc/systemd/system/kirocrew.service << EOF
[Unit]
Description=KiroCrew AI Agent Gateway
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
User=$(whoami)
ExecStart=$KIROCREW_BIN gateway
Restart=on-failure
RestartSec=10
WorkingDirectory=$HOME
Environment=HOME=$HOME
Environment=PATH=$(dirname $KIROCREW_BIN):$HOME/.local/bin:$HOME/.nvm/versions/node/$(node -v 2>/dev/null || echo v20.0.0)/bin:/usr/local/bin:/usr/bin
Environment=KIROCREW_PROJECT_DIR=$(git -C "$(dirname "$(readlink -f "$KIROCREW_BIN")")" rev-parse --show-toplevel 2>/dev/null || echo "")

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kirocrew
sudo systemctl start kirocrew
```

The system-level unit lets you tail logs with
`sudo journalctl -u kirocrew -f` or restart with `sudo systemctl restart kirocrew`.

**Alternative — tmux** (if you don't want a service at all):

```bash
tmux new -s kirocrew
kirocrew gateway
# Ctrl+B, D to detach
# Reconnect: tmux attach -t kirocrew
```

tmux survives SSH disconnect but does **not** auto-restart on crash or
auto-start on reboot. Use the service install path unless you have a
specific reason not to.

## Sync KiroCrew to a New Host

When setting up a new remote host (or replacing a dead one), sync your
local KiroCrew state so the remote instance has your memories,
preferences, lessons, and agent configs from day one.

### What to Sync

| Category | Path | Why |
|---|---|---|
| **Memory** | `~/.kirocrew/workspace/memory/` | Preferences, projects, history — the agent's long-term knowledge of you |
| **Databases** | `~/.kirocrew/memory.db`, `memory.db-wal`, `memory.db-shm`, `memory_index.db` | Episodic & semantic memory (SQLite + WAL for complete state) |
| **Config** | `~/.kirocrew/config.json` | KiroCrew settings (Slack tokens, model prefs) |
| **Lessons** | Stored in `memory.db` | Learned corrections that override default behavior |
| **Task specs** | `~/.kirocrew/tasks/` | Saved task runner specs |
| **Skills** | `~/.kirocrew/skills/` | Custom skill definitions |
| **Hooks** | `~/.kirocrew/hooks/` | Webhook listener configs |
| **Cron jobs** | `~/.kirocrew/crons.json` | Scheduled recurring jobs |
| **Dotfiles** | `~/.gitconfig`, `~/.bashrc`, `~/.zshrc` | Shell & git config |

> **Why sync WAL files?** SQLite uses Write-Ahead Logging — recent writes go to `memory.db-wal` before being checkpointed into `memory.db`. Without the WAL, the remote gets a stale snapshot missing your latest memories and lessons.

### What NOT to Sync

- `~/.kirocrew/sessions/` — optional; sync if you want chat history on remote (sync-to-remote.sh includes this)
- `~/.kirocrew/session_pid_*.txt` — process tracking files, host-specific
- `~/.kirocrew/audit.log`, `security_events.jsonl` — large logs, not needed on new host
- `~/.kirocrew/.env`, `.local_secret`, `sel_hmac.key` — secrets, regenerated on first run

### Quick Sync Script

A standalone sync script is available at [`scripts/sync-to-remote.sh`](../scripts/sync-to-remote.sh):
- Custom dashboard port support (for multi-host setups)
- Atomic SQLite backup for complete memory state
- Session sync for dashboard chat history restore
- Patches `config.json` with remote-specific settings (`auto_open_browser=false`)
- `--dry-run` flag to preview without transferring

Run from your **local machine** (replace `user@host` with your remote
host's SSH target — set it as `DEFAULT_HOST` in the script or pass it as
the first argument):

```bash
# Basic usage (edit DEFAULT_HOST in the script, or pass as arg)
scripts/sync-to-remote.sh user@your-host.example.com

# Custom port (if running multiple remote hosts)
scripts/sync-to-remote.sh user@your-host.example.com 7779

# Preview what would sync
scripts/sync-to-remote.sh --dry-run

# Help
scripts/sync-to-remote.sh --help
```

> **Note on sessions**: The script syncs `~/.kirocrew/sessions/` by default so your chat history appears on the remote dashboard. If you prefer a clean slate, remove the sessions step from the script.

### After Syncing

On the new host:
```bash
# 1. Install KiroCrew (if not already) — see "Install KiroCrew" above

# 2. Verify
kirocrew doctor

# 3. Re-enter any host-specific credentials
kirocrew setup          # re-enter Slack tokens / API keys if needed

# 4. Start the gateway
sudo systemctl start kirocrew   # or: tmux new -s kirocrew && kirocrew gateway
```

### Ongoing Sync (Optional)

If you develop on both local and remote, keep memories in sync with a
shell alias (replace `user@your-host.example.com` with your SSH target):

```bash
alias sync-claw='rsync -avz ~/.kirocrew/workspace/memory/ user@your-host.example.com:~/.kirocrew/workspace/memory/ && rsync -avz ~/.kirocrew/memory.db ~/.kirocrew/memory_index.db user@your-host.example.com:~/.kirocrew/'
```

## Access Dashboard via SSH Tunnel

The dashboard binds to `localhost:5476` on the remote host. Don't expose
that port publicly — instead, forward it to your local machine over an
SSH tunnel and open it in your local browser:

```bash
ssh -L 5476:localhost:5476 user@your-host.example.com
```

Then open `http://localhost:5476` in your local browser.

**Custom port** — if you configured a non-default port (set via the
`KIROCREW_PORT` environment variable on the remote host, e.g. when you
ran `sync-to-remote.sh user@host 7779`), match the tunnel:

```bash
# Tunnel-only (no shell, stays in foreground)
ssh -N -L 7779:localhost:7779 user@your-host.example.com

# Background tunnel (returns immediately)
ssh -fN -L 7779:localhost:7779 user@your-host.example.com
```

To make this automatic on every SSH connection, add to `~/.ssh/config` on your local machine:

```
Host your-host.example.com
    LocalForward 5476 localhost:5476
```

Now a plain `ssh your-host.example.com` will always set up the tunnel.

This works on macOS, Linux, and Windows (OpenSSH is built into Windows 10+ — the SSH config file lives at `%USERPROFILE%\.ssh\config`).

### Persistent Tunnel (macOS LaunchAgent)

The above approaches require an active terminal session — if the terminal closes, the tunnel dies. To keep the tunnel running permanently (survives reboots, auto-reconnects on network drops):

```bash
REMOTE_HOST="user@your-host.example.com"

cat > ~/Library/LaunchAgents/com.kirocrew.tunnel.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kirocrew.tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>while true; do echo "\$(date): Connecting..." >> /tmp/kirocrew-tunnel.log; ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -L 5476:localhost:5476 $REMOTE_HOST 2>> /tmp/kirocrew-tunnel.log; echo "\$(date): Disconnected (exit \$?)" >> /tmp/kirocrew-tunnel.log; sleep 15; done</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardErrorPath</key>
    <string>/tmp/kirocrew-tunnel.err</string>
</dict>
</plist>
EOF

launchctl bootout gui/$(id -u)/com.kirocrew.tunnel 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kirocrew.tunnel.plist
```

Manage:

```bash
tail -f /tmp/kirocrew-tunnel.log                                    # check status
launchctl bootout gui/$(id -u)/com.kirocrew.tunnel                  # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kirocrew.tunnel.plist  # start
```

> **Note**: The tunnel auto-reconnects when SSH drops (network change, sleep/wake, etc.) within 15 seconds. For passwordless reconnection, use SSH key-based authentication to the remote host.

### Raycast Script (macOS - zsh)
If you use Raycast scripts, you can use the below snippet to start an SSH tunnel and open the dashboard with the KiroCrew token directly from a Raycast Script. Replace `"user@your-host.example.com"` with your remote host's SSH target.

```zsh
#!/bin/zsh -e
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open Kirocrew
# @raycast.mode compact
# Optional parameters:
# @raycast.icon 🐉
# @raycast.packageName KiroCrew Utils
# Documentation:
# @raycast.description Get token and start KiroCrew
REMOTE_HOST="user@your-host.example.com"
REMOTE_CMD='source ~/.zshrc; kirocrew token'
DEBUG_LOG="/tmp/kirocrew_debug.log"

if lsof -i :5476 -sTCP:LISTEN > /dev/null 2>&1; then
  echo "Tunnel already opened, accessing KiroCrew"
else
  ssh -fNT -L 5476:localhost:5476 "${REMOTE_HOST}"
  echo "Tunnel opened, accessing KiroCrew!"
fi

SECONDS=0
# `kirocrew token` prints two URLs: a localhost one and a direct host one.
# Keep only the localhost URL — that's what routes through the SSH tunnel opened
# above.
URL="$(ssh -o ConnectTimeout=10 "${REMOTE_HOST}" "${REMOTE_CMD}" 2>"${DEBUG_LOG}" | grep -m1 'localhost:5476')"
echo "Token fetch took ${SECONDS}s (debug: ${DEBUG_LOG})"

if [[ -z "$URL" ]]; then
  echo "ERROR: empty token. Check ${DEBUG_LOG}"
  exit 1
fi

open "$URL"
```

## Troubleshooting

| Issue | Fix |
|---|---|
| `kirocrew: command not found` after install | Ensure pip's script dir is on `PATH` (often `~/.local/bin`); `source ~/.bashrc` or re-login |
| Agent backend errors / timeouts | Confirm `kiro-cli` is on your `PATH` and you are logged in (`kiro-cli login`); `kirocrew doctor` reports its status |
| Service won't start | `sudo journalctl -u kirocrew -n 50` to check logs |
| Service keeps restarting | Check `systemctl status kirocrew` for exit code, then check logs |
| SSH tunnel refuses connection | Confirm the gateway is running on the remote host and listening on the expected port (`ss -ltnp \| grep 5476`) |
| Dashboard shows embeddings not ready | The embedding model (~610MB) downloads in the background over HTTPS from the KiroCrew CDN on gateway startup — run `kirocrew doctor` (it probes the resolved model URL) and check network reachability; a mirror can be set via `KIROCREW_EMBED_MODEL_URL`. Memory falls back to keyword search until the model lands |

> **Note**: Embeddings are always-on and run in-process (bundled
> llama-cpp-python) — no Ollama install needed. Until the model file lands,
> KiroCrew degrades gracefully to keyword search and the agent still runs.
