# KiroCrew Desktop (Electron)

Desktop shell for the Kiro Crew web dashboard on macOS and Linux. It
automatically starts `kirocrew gateway` and connects to `localhost:5476`.

## Quick Start

```bash
cd electron
npm install
npx electron .
```

The app will:

1. Reuse an existing gateway if one is already reachable
2. Launch `kirocrew gateway` when needed
3. Show a loading screen while the backend boots
4. Load the dashboard
5. Point the user at Kiro CLI installation and sign-in on the gateway host when
   either prerequisite is missing

The Electron shell uses the same gateway-hosted setup screen as every browser;
it has no separate installer or login runner, and it performs neither step. The
screen links out to <https://kiro.dev/cli/> for the CLI, and names the commands
the user runs to sign in: `kiro-cli login` for a personal account, or
`kiro-cli login --use-device-flow --license pro` for organization SSO. Both are
shown because the portal the bare command opens offers a free Builder ID
alongside organization SSO, and picking the wrong one still succeeds — the
mismatch only surfaces later as missing models. The app observes completion
through the read-only `kiro-cli whoami` probe. Candidate selection is
fail-closed: a broken higher-priority Kiro CLI is shown as needing repair and is
not skipped in favor of a later candidate. Remote tunnel sessions check the
remote gateway host.

## Install as macOS App

Build a native `.app` bundle and install to `/Applications`:

```bash
cd electron
npm install
npx electron-builder --mac --dir
APP_DIR=$([ "$(uname -m)" = "arm64" ] && echo "dist/mac-arm64" || echo "dist/mac")
sudo rm -rf /Applications/KiroCrew.app
sudo cp -R "$APP_DIR/KiroCrew.app" /Applications/KiroCrew.app
```

Launch via Spotlight (Cmd+Space → "KiroCrew"), Dock, or `open /Applications/KiroCrew.app`.
Right-click the Dock icon → Options → Keep in Dock to pin it.

## Build `.dmg`

```bash
npm run dist
```

Output goes to `electron/dist/`.

## Updating

After pulling new code and rebuilding (`npm run build`):

```bash
# Rebuild and reinstall the desktop app
cd electron && npx electron-builder --mac --dir
APP_DIR=$([ "$(uname -m)" = "arm64" ] && echo "dist/mac-arm64" || echo "dist/mac")
sudo rm -rf /Applications/KiroCrew.app
sudo cp -R "$APP_DIR/KiroCrew.app" /Applications/KiroCrew.app

# Restart the gateway (if using Launch Agent)
launchctl stop dev.kirocrew.gateway
launchctl start dev.kirocrew.gateway
```

## Uninstall

```bash
# Remove the desktop app
sudo rm -rf /Applications/KiroCrew.app

# Remove the Launch Agent (if configured from main README)
launchctl unload ~/Library/LaunchAgents/dev.kirocrew.gateway.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/dev.kirocrew.gateway.plist
```

## Remote Tunnel Mode (Headless CDE)

If the gateway runs on a remote dev desktop (the recommended setup per
`../../docs/guides/remote-and-mobile.md`), the app can fetch tokens automatically
via SSH instead of reading the local `.local_secret`.

### Prerequisites

1. An SSH tunnel forwarding the remote gateway port to localhost:
   ```bash
   ssh -L 5476:localhost:5476 YOUR_HOST.example.com
   ```
   Or use a macOS LaunchAgent (see `../../docs/guides/assets/`).

2. `kirocrew` installed on the remote host. The default auto-discovers across common
   install layouts — no configuration needed unless you installed somewhere unusual.

### Configure

Remote host settings are **per-port** — each tab can have its own remote host
(or none, for local gateways). Focus the tab you want to configure, then use
**Tab menu → Set Remote Host…** or right-click the tab bar:

1. The modal shows which port it's configuring (e.g. "Remote host for :5476")
2. Enter your remote host's hostname or SSH config alias (e.g. `myhost.example.com` or `clouddesk`)
3. Leave the binary path at the default unless you installed kirocrew somewhere
   unusual. The default tries, in order:
   - `~/.toolbox/bin/kirocrew` (toolbox install — recommended)
   - `~/.local/bin/kirocrew` (install.sh / source install)
   - `~/.kirocrew-app/.venv/bin/kirocrew` (one-liner installer venv)
4. Optionally set a **Remote port** if the gateway port on the remote host differs
   from the local tab port (default: same as tab port)
5. Optionally set a **Remote PATH** if kirocrew needs additional directories
   (default: `~/.toolbox/bin:/usr/bin:/bin`)
6. Click Save. Leave hostname empty to clear (use local token for that port).

**Multi-instance example:**
- Tab 1 on `:5476` — local gateway, no remote host needed
- Tab 2 on `:7778` — SSH tunnel to another host, remote host configured

The app will SSH into the configured remote host and run `kirocrew token` on
each launch to get a fresh JWT — no manual paste required.

### Token flow (per tab)

```
1. Try local ~/.kiro/crew/.local_secret → /api/token/local on the tab's port
   (with a temporary ~/.kirocrew read fallback during one-time migration)
2. If remote host configured for this port:
   SSH: export PATH=<remotePath> KIROCREW_PORT=<port>; <bin> token
3. Fallback: show manual token prompt
```

### Menus

| Location | Item | Action |
|----------|------|--------|
| Tab menu / tab bar right-click | Set Remote Host… | Configure hostname for the **focused tab's** port |
| Tab menu / tab bar right-click | Refresh Token (⌘⇧T) | Fetch a fresh token for the **focused tab** |
| Tab menu / tray | Open Config File | Open `config.json` in default editor |

### Tab naming

Tabs default to `[:port]`. You can set a **default name** per port via
**Rename Tab → ☑ Set as default name**. New tabs on that port will use it
automatically. Names are stored in `remoteHosts[port].defaultName`.

### Config file

Settings are persisted via `electron-store` in
`~/Library/Application Support/KiroCrew/config.json`:

```json
{
  "remoteHosts": {
    "5476": {
      "host": "myhost.example.com",
      "binPath": "~/.toolbox/bin/kirocrew",
      "remotePort": "",
      "remotePath": "",
      "defaultName": "Cloud"
    }
  },
  "sshTimeoutMs": 20000
}
```

Open via **Tab menu → Open Config File** or tray menu.

### Troubleshooting

| Issue | Fix |
|-------|-----|
| "SSH token fetch failed" | Check `ssh YOUR_HOST` works from Terminal |
| "kirocrew binary not found in any of …" | Install kirocrew (`pip install kirocrew`), or set a custom path |
| "command not found: kiro-cli" | Set Remote PATH to include `~/.toolbox/bin` (default does this) |
| "command not found: dirname" | Remote PATH missing `/usr/bin` — reset to default or add it |
| Token fetched but 403 | Gateway may need restart — `ssh host systemctl --user restart kirocrew` |
| Wrong tab refreshed | Focus the target tab first (use Tab menu, not tray) |

## Notes

- Closing the window hides to tray — right-click the tray icon or Cmd+Q to quit
- External links open in your default browser
- Desktop leaves the child `PATH` unchanged; the gateway-side prerequisite
  service independently probes Kiro CLI's supported user-local, Homebrew,
  macOS app-bundle, and Windows MSI locations
