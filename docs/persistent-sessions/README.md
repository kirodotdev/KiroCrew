# KiroCrew Persistent Sessions

> **Prerequisite**: Complete [remote-desktop-setup.md](../remote-desktop-setup.md) first.

Upgrades the tmux-based setup to a fully persistent configuration:
- Gateway auto-restarts on crash and auto-starts on boot (systemd)
- SSH tunnel from your Mac auto-reconnects after laptop sleep (LaunchAgent)

## Dev Desktop Setup

### Phase 1: Enable systemd user services (one-time, requires sudo)

AL2 dev desktops don't have systemd user services enabled by default. AL2023 desktops typically have this working out of the box — skip to Phase 2 if `systemctl --user status` works.

Run `systemctl --user status` — if it returns without error, skip to Phase 2.

Otherwise, run these commands:

```bash
sudo tee /etc/systemd/system/user@$(id -u).service << 'EOF'
[Unit]
Description=User Manager for UID %i
After=systemd-user-sessions.service
After=user-runtime-dir@%i.service
Wants=user-runtime-dir@%i.service

[Service]
LimitNOFILE=infinity
LimitNPROC=infinity
User=%i
PAMName=systemd-user
Type=notify
PermissionsStartOnly=true
ExecStartPre=/bin/loginctl enable-linger %i
ExecStart=/usr/lib/systemd/systemd --user
Slice=user-%i.slice
KillMode=mixed
Delegate=yes
TasksMax=infinity
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable user@$(id -u).service
sudo systemctl start user@$(id -u).service
```

Verify: `systemctl --user status` should now return without error.

### Phase 2: Install KiroCrew service

```bash
cd docs/persistent-sessions
./setup.sh
```

Or manually:

```bash
mkdir -p ~/.config/systemd/user
cp kirocrew.service ~/.config/systemd/user/
sed -i "s/%u/$(whoami)/g" ~/.config/systemd/user/kirocrew.service
systemctl --user daemon-reload
systemctl --user enable kirocrew
systemctl --user start kirocrew
```

Verify: `systemctl --user status kirocrew` should show `active (running)`.

## Mac Setup

```bash
# Copy the plist
scp USER@HOST:~/workplace/kirocrew/docs/persistent-sessions/com.kirocrew.tunnel.plist ~/Library/LaunchAgents/

# Replace placeholder with your dev desktop
sed -i '' 's|ALIAS@DEV_DESKTOP_HOSTNAME|USER@HOST|g' ~/Library/LaunchAgents/com.kirocrew.tunnel.plist

# Load the tunnel
launchctl load ~/Library/LaunchAgents/com.kirocrew.tunnel.plist
```

Verify: `curl -s http://localhost:5476/api/status`

Dashboard: http://localhost:5476

## Gotchas

- **sudo broken?** `/etc/sudo.conf` may have wrong ownership on some dev desktops. Run sudo commands from a fresh SSH session, not from kiro-cli or KiroCrew.
- **Kill tmux first** — can't have two gateways on port 5476. Run `tmux kill-session -t kirocrew` before Phase 2.
- **D-Bus connection error?** Run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` then retry.
- **Laptop sleep** — the LaunchAgent tunnel includes `ServerAliveInterval=30` and `KeepAlive=true`. macOS auto-restarts it after sleep/network change. Reconnect takes ~30 seconds.

## Managing

| Action | Command |
|---|---|
| Gateway status | `systemctl --user status kirocrew` |
| Gateway restart | `systemctl --user restart kirocrew` |
| Gateway logs | `journalctl --user -u kirocrew -f` |
| Tunnel logs (Mac) | `cat /tmp/kirocrew-tunnel.log` |
| Tunnel restart (Mac) | `launchctl kickstart -k gui/$(id -u)/com.kirocrew.tunnel` |
| Uninstall gateway | `systemctl --user disable --now kirocrew` |
| Uninstall tunnel | `launchctl unload ~/Library/LaunchAgents/com.kirocrew.tunnel.plist` |

## Files

| File | Purpose |
|---|---|
| `kirocrew.service` | systemd unit file for dev desktop |
| `com.kirocrew.tunnel.plist` | macOS LaunchAgent for SSH tunnel |
| `setup.sh` | Automated Phase 2 setup |
