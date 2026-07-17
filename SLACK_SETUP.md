# Slack Setup Guide

How to create a Slack app for KiroCrew and connect it.

> **Dashboard-only mode**: If you don't need Slack, skip this entirely. Leave Slack tokens empty during `kirocrew setup` and the gateway runs the web dashboard without Slack.

KiroCrew connects to Slack using **Socket Mode**, so it runs entirely from your own machine over an outbound WebSocket — no public URL, no inbound webhooks, and no hosting required. You just need a Slack workspace where you can install an app.

---

## Choose Your Path

There are **two independent paths** to create a Slack app. Pick one — do not mix them.

| Path | Steps | Best for |
|------|-------|----------|
| **[Path A: Manifest](#path-a-create-via-manifest-recommended)** (recommended) | 6 steps | Most users — auto-configures all scopes, events, and permissions in one shot |
| **[Path B: Manual](#path-b-manual-setup)** | 11 steps | If you need to customize scopes or understand each setting individually |

Both paths require a Slack workspace where you can install apps (see [Prerequisites](#prerequisites)).

---

## Prerequisites

- A **Slack workspace** where you have permission to install apps. If you don't have one, create a free workspace at <https://slack.com/get-started> — you can install your own apps in a workspace you own.
- Python and KiroCrew installed (`pip install kirocrew`), so you can run the `kirocrew` CLI.

> **Tip**: Use a personal or test workspace for your first run. You can always export the app manifest and recreate the app in another workspace later (see [Reusing the App in Another Workspace](#reusing-the-app-in-another-workspace)).

---

## Path A: Create via Manifest (Recommended)

### Step 1. Generate the Manifest

```bash
kirocrew manifest --url
```

This prints a one-click URL that opens Slack's "Create New App" page with all scopes, events, and permissions pre-filled:

```
🔗 Click to create your Slack app:
https://api.slack.com/apps?new_app=1&manifest_yaml=...
```

### Step 2. Create the App from the Link

1. Click (or Cmd-click) the URL printed in Step 1
2. **Select your workspace** from the workspace dropdown
3. Click **Create**

<details>
<summary><strong>Alternative: paste manifest manually</strong></summary>

If the URL doesn't work, generate the raw YAML instead:

```bash
kirocrew manifest -o ~/.kirocrew/slack-manifest.yaml
```

Then:
1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**
2. Select your workspace
3. Paste the contents of `~/.kirocrew/slack-manifest.yaml`
4. Click **Create**

</details>

### Step 3. Generate App Token

1. **Settings → Socket Mode** → Toggle **OFF** then back **ON** — this triggers the token generation dialog
2. Add scope `connections:write` → **Generate**
3. Copy the `xapp-...` token — this is your **App Token**

> **Why toggle off/on?** Slack manifests can declare `socket_mode_enabled: true`, but the App Token (`xapp-...`) must still be generated manually through the UI. There is no API or manifest field for token generation. Toggling forces the generation dialog to appear.

### Step 4. Install to Workspace

1. Go to **Features → OAuth & Permissions** → **Install to Workspace**
2. Approve the installation
3. Copy the `xoxb-...` token — this is your **Bot Token**

> **"Install App" button greyed out?** The Settings → Install App page sometimes has the button disabled due to a Slack UI bug. Use **Features → OAuth & Permissions → Install to Workspace** instead — it does the same thing.

> **Workspace admin approval?** Some workspaces restrict who can install apps. If your install needs approval, an admin of that workspace must approve it. In a workspace you own, you can self-approve.

### Step 5. Configure KiroCrew

Run the interactive setup — it prompts for both tokens:

```bash
kirocrew setup
```

Paste your App Token (`xapp-...`), Bot Token (`xoxb-...`), and your Slack Member ID when prompted.

To find your Slack Member ID: open your workspace in Slack → click your profile picture → **Profile** → **⋮** → **Copy member ID**.

> ⚠️ **Your Member ID is per-workspace.** If you install the app in a different workspace, your Member ID changes — use the ID from the workspace where KiroCrew is installed.

### Step 6. Verify & Run

```bash
kirocrew doctor    # verify tokens and config
kirocrew gateway   # start KiroCrew
```

Open your workspace in Slack, find your app in the Apps section, and send it a DM. The app only lives in the workspace where you installed it.

**You're done!** 🎉 Skip ahead to [After Setup](#after-setup) for next steps.

---

## Path B: Manual Setup

Use this path if you want to configure each scope and event individually, or if you need to customize the app beyond what the manifest provides.

### Step 1. Create the App

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**
2. Name: something unique (e.g. `kirocrew`). Generic names may conflict with existing apps in the same workspace
3. Workspace: **select your workspace**

### Step 2. Enable Socket Mode & Get App Token

1. **Settings → Socket Mode** → Toggle **ON**
2. Click **Generate Token** → add scope `connections:write` → **Generate**
3. Copy the `xapp-...` token — this is your **App Token**

### Step 3. Add Bot Scopes

Go to **Features → OAuth & Permissions → Bot Token Scopes** and add:

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Respond when @mentioned |
| `chat:write` | Send, update, and delete messages |
| `channels:history` | Read channel messages (for @mentions) |
| `channels:read` | Receive `member_joined_channel` events for public channels |
| `groups:read` | Receive `member_joined_channel` events for private channels |
| `im:history` | Read DM history |
| `im:read` | View DM metadata |
| `im:write` | Open DMs |
| `reactions:write` | Add and remove emoji reactions |
| `emoji:read` | *(Optional)* Show custom workspace emojis in the emoji picker |
| `users:read` | Look up user profiles |
| `files:read` | Read uploaded files |
| `files:write` | Upload screenshots |
| `commands` | Slash commands |

### Step 4. Subscribe to Events

1. **Features → Event Subscriptions** → Toggle **ON**
2. Under **Subscribe to bot events**, add all of these:
   - `message.im`
   - `message.channels`
   - `app_mention`
   - `app_home_opened`
   - `file_change`
   - `member_joined_channel`
3. Click **Save Changes**

### Step 5. Add Slash Commands

**Features → Slash Commands** → **Create New Command**:

| Field | Value |
|-------|-------|
| Command | `/kirocrew` |
| Short Description | Dashboard access, allowlist, and channel tracking |
| Usage Hint | dashboard or @user or #channel |

The command name you choose here must match the `slack.command` value in `~/.kirocrew/config.json` (default: `kirocrew`):

```json
{
  "slack": {
    "command": "kirocrew"
  }
}
```

When creating the command, check **Escape channels, users, and links sent to your app** so mentions resolve to `<@U1234|user>` format.

### Step 6. Enable Interactivity

**Features → Interactivity & Shortcuts** → Toggle **ON**

No Request URL needed — Socket Mode handles it. This enables tool approval buttons, allowlist buttons, and overflow menus.

### Step 7. Enable App Home

**Features → App Home**:

- Enable **Home Tab**
- Enable **Chat Tab**
- Check **"Allow users to send Slash commands and messages from the chat tab"**

### Step 8. Install to Workspace & Get Bot Token

1. **Features → OAuth & Permissions** → **Install to Workspace** → Approve
2. Copy the `xoxb-...` token — this is your **Bot Token**

> **"Install App" button greyed out?** Use **Features → OAuth & Permissions → Install to Workspace** instead — known Slack UI bug.

### Step 9. Configure KiroCrew

Same as [Path A, Step 5](#step-5-configure-kirocrew).

### Step 10. Verify & Run

Same as [Path A, Step 6](#step-6-verify--run).

---

## After Setup

### Manual Token Configuration

If you prefer to configure tokens manually instead of using `kirocrew setup`:

```bash
mkdir -p ~/.kirocrew
cat > ~/.kirocrew/.env << 'EOF'
SLACK_APP_TOKEN=xapp-your-app-token-here
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
KIROCREW_OWNER_ID=your-slack-member-id
EOF
chmod 600 ~/.kirocrew/.env
```

### Owner-Only Access

Only the owner (`KIROCREW_OWNER_ID`) can interact with KiroCrew via Slack. Multi-user access, auto-allowlist on channel join, and open channels are all disabled for security.

### Reaction Emojis

KiroCrew adds phase-aware emoji reactions during message processing (queued → thinking → coding → done). Customize or disable:

```json
{
  "slack": {
    "reactions": {
      "done": "sparkle",
      "thinking": "brain",
      "coding": "computer"
    },
    "reactions_enabled": true
  }
}
```

Set `reactions_enabled` to `false` to disable all phase reactions. Valid keys: `queued`, `thinking`, `coding`, `browsing`, `tool`, `done`, `error`.

To suppress a single phase (keep the others at their defaults), set it to `null`. For example, to keep the working/browsing/tool reactions but hide the terminal `🦞` on every message:

```json
{
  "slack": {
    "reactions": { "done": null }
  }
}
```

A suppressed phase removes any prior reaction but adds nothing new; stall reactions (`🥱` / `😨`) are unaffected.

---

## Reusing the App in Another Workspace

To install your app in a different Slack workspace, export the manifest and recreate the app there:

1. Export your app manifest: **App Config → App Manifest → Copy to Clipboard** (YAML)
2. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**
3. Select the new workspace and paste the YAML
4. Re-generate the App Token (Socket Mode) and Bot Token, then re-run `kirocrew setup` with the new tokens and your Member ID for that workspace

---

## Dashboard Access

When Slack is connected, remote dashboard access requires a presigned token. Links are always sent via DM — never posted in channels.

### Getting a Link

Any of these work:

```
!dashboard              # DM: 1-hour session (default)
!dashboard 2h           # DM: 2-hour session
/kirocrew dashboard     # Slash command: 1-hour session
/kirocrew dashboard 30m # Slash command: 30-minute session
```

Maximum session duration is 6 hours.

### How It Works

1. Link must be clicked within **5 minutes** (after that the URL expires)
2. On first click: token is bound to your IP, a session cookie is set (`mc_token_{port}`, HttpOnly, SameSite=Strict)
3. Subsequent visits use the cookie — no need to re-click the link
4. Session cookie lasts for the requested duration (default 1h, max 6h)
5. SSH tunnel access (`localhost`) bypasses token auth entirely

### Dashboard URL Configuration

Set `dashboard.url` in `~/.kirocrew/config.json` to your host's DNS name:

```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

From this single URL, KiroCrew derives:
- **Port** to bind on (8080 in this example)
- **Local-only mode** — loopback hosts bind to `127.0.0.1`, non-loopback hosts bind to `0.0.0.0`
- **Allowed origins** for CSRF/WebSocket protection
- **Dashboard link hostname** for `!dashboard` and `/kirocrew dashboard`

When omitted, defaults to `localhost:5476` (loopback only, no token required).

## Dashboard ↔ Slack Sync

The dashboard has a 💬 button that links the current chat session to a Slack thread for bidirectional sync.

### Linking a Session

1. Click the 💬 button next to the chat title
2. Choose a channel from the dropdown (DM is always available)
3. A new thread is created in the selected channel with a summary of recent messages
4. The button turns solid green to indicate the session is linked

### Bidirectional Sync

- **Slack → Dashboard**: Messages sent in the linked Slack thread appear in the dashboard session in real time
- **Dashboard → Slack**: Messages and responses in the dashboard are mirrored to the Slack thread

### `sessions` Command

Type `sessions` in any Slack DM to list recent sessions. Each session shows a title, agent name, expandable summary, and a **▶️ Resume** button.

---

## Slack Commands Reference

### Slash Commands

The slash command name is configurable via `slack.command` in config (default: `kirocrew`).

| Command | Purpose |
|---------|---------|
| `/<command> dashboard` | Get a presigned dashboard link (DM'd to you) |
| `/<command> dashboard 2h` | Dashboard link with custom duration (max 6h) |
| `/<command> yolo` | Toggle auto-approve all tool calls |
| `/<command> agent` | Show agent selector dropdown |
| `/<command> agent <name>` | Switch to a named agent |
| `/<command> channels` | Open channel management modal |
| `/<command> sessions` | List recent sessions with resume buttons |
| `/<command> help` | List all available sub-commands |

### Owner-Only Bang Commands

These `!`-prefixed commands are restricted to `KIROCREW_OWNER_ID`.

| Command | Purpose |
|---------|---------|
| `!dashboard` / `!dashboard 2h` | Get a presigned dashboard link |
| `!yolo on` / `!yolo off` | Toggle auto-approve all tool calls |
| `!agent <name>` / `!agent off` | Switch the active agent |
| `!ta <agent>` / `!ta off` | Override agent for current thread only |

### Keyword Commands

Available in DMs or @mentions.

| Command | Purpose |
|---------|---------|
| `status` | Show runtime stats summary |
| `spawn <task>` | Run a subagent (blocking) |
| `bg <task>` | Run a subagent (fire-and-forget) |
| `spawn list` | List active subagents |
| `cron list` | List cron jobs |
| `cron remove <id>` | Remove a cron job |
| `sessions` | List recent sessions with resume buttons |

---

## Security: Protecting Your Dashboard Token

Dashboard tokens grant full session access — treat them like passwords.

| ✅ Do | ❌ Don't |
|-------|----------|
| Use SSH tunnel so the dashboard is only reachable via localhost | Share dashboard URLs — they contain the token in `?token=` |
| Run `kirocrew logout` or restart the gateway if a token is exposed | Paste tokens in Slack channels, shared docs, or wikis |
| Avoid showing the browser URL bar during screen shares | Leave dashboard links in screen-share recordings |
| Keep `kirocrew token` in your agent's denied commands list | Trust AI agents asking to run `kirocrew token` |

> ⚠️ **Prompt injection risk**: An attacker can embed hidden instructions in a webpage or document that trick your AI agent into running `kirocrew token` and exfiltrating the output. The `deniedCommands` list in `agents/defaults.json` blocks this by default — do not remove it.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Install App button greyed out | Use **Features → OAuth & Permissions → Install to Workspace** instead — known Slack UI bug |
| App created in wrong workspace | Delete the app on api.slack.com, then recreate it in the correct workspace |
| No events received | Verify Socket Mode is ON, events are subscribed, App Home Chat Tab is enabled. Reinstall app after changes |
| Home tab is blank | Add `app_home_opened` event, enable Home Tab, reinstall app |
| `missing_scope` error | Add the scope in OAuth & Permissions, reinstall app, re-run `kirocrew setup` |
| Bot doesn't respond | Check `kirocrew doctor` output. Ensure gateway is running (`kirocrew gateway`) |
| Install needs approval | Your workspace restricts app installs — a workspace admin must approve, or use a workspace you own |
| Dashboard shows 403 | Token expired, IP changed, or accessing remotely without a token. Run `!dashboard` for a new link |

---

## References

- [Slack API Docs](https://api.slack.com/)
- [Slack Socket Mode](https://api.slack.com/apis/socket-mode)
- [Create a free Slack workspace](https://slack.com/get-started)
</content>
</invoke>
