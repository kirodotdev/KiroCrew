# Slack Setup Guide

How to create a Slack app for Kiro Crew and connect it.

> **Dashboard-only mode**: If you don't need Slack, skip this entirely. Leave Slack tokens empty during `kirocrew setup` and the gateway runs the web dashboard without Slack.

Kiro Crew connects to Slack using **Socket Mode**, so it runs entirely from your own machine over an outbound WebSocket: no public URL, no inbound webhooks, and no hosting required. You just need a Slack workspace where you can install an app.

---

## Choose Your Path

There are **two independent paths** to create a Slack app. Pick one, and do not mix them.

| Path | Steps | Best for |
|------|-------|----------|
| **[Path A: Manifest](#path-a-create-via-manifest-recommended)** (recommended) | 6 steps | Most users; auto-configures all scopes, events, and permissions in one shot |
| **[Path B: Manual](#path-b-manual-setup)** | 10 steps | If you need to customize scopes or understand each setting individually |

Both paths require a Slack workspace where you can install apps (see [Prerequisites](#prerequisites)).

---

## Prerequisites

- A **Slack workspace** where you have permission to install apps. If you don't have one, create a free workspace at <https://slack.com/get-started>. You can install your own apps in a workspace you own.
- Python and Kiro Crew installed (`pip install kirocrew`), so you can run the `kirocrew` CLI.

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
kirocrew manifest -o ~/.kiro/crew/slack-manifest.yaml
```

Then:
1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**
2. Select your workspace
3. Paste the contents of `~/.kiro/crew/slack-manifest.yaml`
4. Click **Create**

</details>

### Step 3. Generate App Token

1. **Settings → Socket Mode** → Toggle **OFF** then back **ON**, which triggers the token generation dialog
2. Add scope `connections:write` → **Generate**
3. Copy the `xapp-...` token. This is your **App Token**

> **Why toggle off/on?** Slack manifests can declare `socket_mode_enabled: true`, but the App Token (`xapp-...`) must still be generated manually through the UI. There is no API or manifest field for token generation. Toggling forces the generation dialog to appear.

### Step 4. Install to Workspace

1. Go to **Features → OAuth & Permissions** → **Install to Workspace**
2. Approve the installation
3. Copy the `xoxb-...` token. This is your **Bot Token**

> **"Install App" button greyed out?** The Settings → Install App page sometimes has the button disabled due to a Slack UI bug. Use **Features → OAuth & Permissions → Install to Workspace** instead, which does the same thing.

> **Workspace admin approval?** Some workspaces restrict who can install apps. If your install needs approval, an admin of that workspace must approve it. In a workspace you own, you can self-approve.

### Step 5. Configure Kiro Crew

Run the interactive setup, which prompts for both tokens:

```bash
kirocrew setup
```

Paste your App Token (`xapp-...`), Bot Token (`xoxb-...`), and your Slack Member ID when prompted.

To find your Slack Member ID: open your workspace in Slack → click your profile picture → **Profile** → **⋮** → **Copy member ID**.

> ⚠️ **Your Member ID is per-workspace.** If you install the app in a different workspace, your Member ID changes, so use the ID from the workspace where Kiro Crew is installed.

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
3. Copy the `xapp-...` token. This is your **App Token**

### Step 3. Add Bot Scopes

Go to **Features → OAuth & Permissions → Bot Token Scopes** and add:

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Respond when @mentioned |
| `chat:write` | Send, update, and delete messages |
| `channels:history` | Read channel messages (for @mentions) |
| `channels:read` | List public channels (the channel picker) and read channel metadata |
| `groups:read` | Same, for private channels the bot is in |
| `im:history` | Read DM history |
| `im:read` | View DM metadata |
| `im:write` | Open DMs |
| `reactions:write` | Add and remove emoji reactions |
| `files:read` | Read uploaded files |
| `files:write` | Upload screenshots |
| `commands` | Slash commands |

Two scopes are deliberately **not** in the shipped manifest, so Path B should
leave them out unless you want the extra behavior:

| Scope | What adding it buys |
|-------|---------------------|
| `emoji:read` | Custom workspace emojis appear in the emoji picker |
| `users:read` | Profile lookups (`users.info`) resolve a sender's real name. Without it those calls fail and are caught: the display name falls back to the matching `slack.allowed_users` entry, then to the raw Slack member ID |

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
| Usage Hint | `dashboard [duration] \| @user \| #channel` |

The command name you choose here must match the `slack.command` value in `~/.kiro/crew/config.json` (default: `kirocrew`):

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

No Request URL needed: Socket Mode handles it. This is what makes the Block Kit
buttons work, including tool approval (approve / trust / reject), the
multiple-choice option buttons, the cron and subagent acknowledge buttons, and
the session Resume / End buttons.

### Step 7. Enable App Home

**Features → App Home**:

- Enable **Home Tab**
- Enable **Chat Tab**
- Check **"Allow users to send Slash commands and messages from the chat tab"**

### Step 8. Install to Workspace & Get Bot Token

1. **Features → OAuth & Permissions** → **Install to Workspace** → Approve
2. Copy the `xoxb-...` token. This is your **Bot Token**

> **"Install App" button greyed out?** Use **Features → OAuth & Permissions → Install to Workspace** instead (known Slack UI bug).

### Step 9. Configure Kiro Crew

Same as [Path A, Step 5](#step-5-configure-kirocrew).

### Step 10. Verify & Run

Same as [Path A, Step 6](#step-6-verify--run).

---

## After Setup

### Manual Token Configuration

If you prefer to configure tokens manually instead of using `kirocrew setup`:

```bash
mkdir -p ~/.kiro/crew
cat > ~/.kiro/crew/.env << 'EOF'
SLACK_APP_TOKEN=xapp-your-app-token-here
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
KIROCREW_OWNER_ID=your-slack-member-id
EOF
chmod 600 ~/.kiro/crew/.env
```

### Owner-Only Access

Only the owner (`KIROCREW_OWNER_ID`) can interact with Kiro Crew via Slack.
Multi-user access is disabled at the authorization predicate itself, not by
configuration: `is_allowed_user` resolves to an owner check, `is_open_channel`
always returns false, and the channel-join allowlist prompt is a no-op. A
`/<command> @user` invocation replies that multi-user access is disabled.

### Reaction Emojis

Kiro Crew adds phase-aware emoji reactions during message processing (queued → thinking → coding → done). Customize or disable:

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

Dashboard access is token-authenticated. When Slack is connected you can mint a
presigned link from Slack; the link is always sent as a DM, never posted into a
channel, so the token cannot leak to a channel's members.

### Getting a Link

Any of these work:

```
!dashboard              # DM: 1-hour session (default)
!dashboard 2h           # DM: 2-hour session
/kirocrew dashboard     # Slash command: 1-hour session
/kirocrew dashboard 30m # Slash command: 30-minute session
```

Durations are `<N>h` or `<N>m` and are capped at **20 hours**; anything longer
is silently clamped to the cap, and an unparseable value gets a usage reply
instead of a link.

### How It Works

1. Link must be clicked within **5 minutes** (after that the URL expires)
2. On first click: token is bound to your IP, and a session cookie is set
   (`mc_token_<port>`, HttpOnly, SameSite=Lax, `Secure` only over HTTPS). The
   cookie is keyed by the port your **browser** connects to, not the port the
   gateway listens on, because browsers do not isolate cookies by port and two
   tunnelled instances would otherwise overwrite each other's session
3. Subsequent visits use the cookie, so there is no need to re-click the link
4. Session cookie lasts for the requested duration (default 1h, cap 20h)
5. **Every** request needs a valid token or cookie, including requests that
   arrive on loopback. Loopback is not an exemption: a local port forwarder
   (`socat`, `ssh -R`, a helper script) makes remote traffic appear to come from
   `127.0.0.1`, so exempting it would be an auth bypass. The only loopback
   carve-out is a small set of internal API paths reserved for Kiro Crew's own
   processes (doctor, the MCP servers), and those additionally require a
   matching `X-Internal-Secret` read from `~/.kiro/crew/.local_secret`

### Dashboard URL Configuration

Set `dashboard.url` in `~/.kiro/crew/config.json` to the host and port you reach
the dashboard on:

```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

From this single URL, Kiro Crew derives:
- **Port** to bind on (8080 in this example)
- **Allowed origins** for the CSRF / WebSocket checks
- **Dashboard link hostname** for `!dashboard` and `/kirocrew dashboard`

When omitted, it defaults to port 5476 and the `localhost` hostname.

The gateway itself always binds loopback in this build: publishing it is your
reverse proxy's or tunnel's job, not the gateway's. `KIROCREW_BIND` widens the
bind address only (the container image sets `KIROCREW_BIND=0.0.0.0` so a
published `-p` port is reachable from the host) and changes nothing about token
auth, which is mounted unconditionally on both server paths.

## Dashboard to Slack Sync

A dashboard chat session can be linked to a Slack thread for two-way sync.

### Linking a Session

1. Open the session menu (the chevron next to the chat title, or right-click the
   session row in the sidebar)
2. Pick a target from the link list. The Slack DM entry is always offered; other
   entries come from your configured channel targets
3. A new thread is posted in that conversation, titled from the session title
   (falling back to a snippet of the first prompt), followed by the last five
   messages as context. Titles and message text are redacted before posting
4. The menu then shows a "Connected" row for the link, plus actions to post a
   reminder into the thread or to unlink

Linking a session that is already linked does not create a second thread: it
posts a short note into the existing thread and returns the existing link.
Unlinking leaves the session, its history, and the Slack thread intact, and
posts a courtesy note into the thread so a Slack-side watcher knows why it went
quiet.

### Two-Way Sync

- **Slack to dashboard**: a reply in the linked thread is routed into the linked
  dashboard session instead of spawning a fresh one
- **Dashboard to Slack**: dashboard turns are mirrored into the linked thread

### `sessions` Command

Type `sessions` in any Slack DM to list recent sessions. Each entry shows a
status dot, the session title, the agent name, a bulleted preview of recent
messages, and a **Resume** button. The same content backs the
`/<command> sessions` slash command and the App Home tab.

---

## Slack Commands Reference

### Slash Commands

The slash command name is configurable via `slack.command` in config (default: `kirocrew`).

| Command | Purpose |
|---------|---------|
| `/<command> dashboard` | Get a presigned dashboard link (DM'd to you) |
| `/<command> dashboard 2h` | Dashboard link with custom duration (cap 20h) |
| `/<command> yolo` | Toggle auto-approve all tool calls |
| `/<command> agent` | Show agent selector dropdown |
| `/<command> agent <name>` | Switch to a named agent |
| `/<command> voice` | Configure TTS voice settings |
| `/<command> config` | Manage users and channels (owner-only) |
| `/<command> users` | Manage allowed users |
| `/<command> channels` | Open channel management modal |
| `/<command> sessions` | List recent sessions with resume buttons |
| `/<command> status` | Show runtime stats |
| `/<command> restart` | Restart the gateway (owner-only) |
| `/<command> #channel` | Track or untrack a channel |

Any unrecognized sub-command prints the same list, generated from the live
registry, so `/<command> help` is not a special case: anything that does not
match falls through to it.

### Owner-Only Bang Commands

These `!`-prefixed commands are restricted to `KIROCREW_OWNER_ID`.

| Command | Purpose |
|---------|---------|
| `!dashboard` / `!dashboard 2h` | Get a presigned dashboard link |
| `!yolo on` / `!yolo off` | Toggle auto-approve all tool calls |
| `!agent <name>` / `!agent off` | Switch the active agent |
| `!ta <agent>` / `!ta off` | Override agent for current thread only |
| `!link-to-dashboard` | Import the current Slack thread into a dashboard session |
| `!project <path>` / `!project off` | Scope which project-local `.kiro` agents `!ta` can find (does not change the working directory) |
| `!title <text>` | Set the thread title |
| `!voice` | Configure TTS voice settings |
| `!channel` | Configure the current channel |
| `!stop` | Interrupt the running turn |
| `!restart` | Restart the gateway |

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

Dashboard tokens grant full session access, so treat them like passwords.

| ✅ Do | ❌ Don't |
|-------|----------|
| Keep the dashboard behind your own tunnel or reverse proxy | Share dashboard URLs, which carry the token in `?token=` |
| If a token is exposed, revoke at your tunnel or reverse-proxy auth layer — `kirocrew logout` alone does not end refresh sessions | Paste tokens in Slack channels, shared docs, or wikis |
| Avoid showing the browser URL bar during screen shares | Leave dashboard links in screen-share recordings |
| Leave the built-in `kirocrew token` deny rules enabled | Trust an AI agent that asks to run `kirocrew token` |

`kirocrew logout` revokes every issued **access** cookie, not just in-memory
state: it bumps a persisted revocation generation, so access cookies handed out
before the logout are rejected on their next request. It does **not** revoke
refresh chains, and neither does restarting the gateway, so a browser still
holding a valid `mc_refresh_<port>` cookie can obtain a fresh access cookie
afterwards — see
[remote-and-mobile.md](remote-and-mobile.md#session-duration). To cut off an
exposed dashboard, revoke at your tunnel or reverse-proxy auth layer, or sign out
in that browser (`POST /api/auth/logout`), which does revoke its chain.

> ⚠️ **Prompt injection risk**: an attacker can hide instructions in a webpage or
> document that trick your agent into running `kirocrew token` and exfiltrating
> the output. Kiro Crew ships built-in denied-command rules covering that mint,
> including nested shell payloads and the `kiro-crew` spelling, enforced at the
> PreToolUse gate (`hooks.py`) rather than injected into any agent config file.
> They are on by default; leave them on. See
> [../architecture/security-deep-dive.md](../architecture/security-deep-dive.md).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Install App button greyed out | Use **Features → OAuth & Permissions → Install to Workspace** instead (known Slack UI bug) |
| App created in wrong workspace | Delete the app on api.slack.com, then recreate it in the correct workspace |
| No events received | Verify Socket Mode is ON, events are subscribed, App Home Chat Tab is enabled. Reinstall app after changes |
| Home tab is blank | Add `app_home_opened` event, enable Home Tab, reinstall app |
| `missing_scope` error | Add the scope in OAuth & Permissions, reinstall app, re-run `kirocrew setup` |
| Bot doesn't respond | Check `kirocrew doctor` output. Ensure gateway is running (`kirocrew gateway`) |
| Install needs approval | Your workspace restricts app installs, so a workspace admin must approve, or use a workspace you own |
| Dashboard shows 403 | Token expired, IP changed, or the link was opened more than 5 minutes after it was issued. Run `!dashboard` for a new link |

---

## References

- [Slack API Docs](https://api.slack.com/)
- [Slack Socket Mode](https://api.slack.com/apis/socket-mode)
- [Create a free Slack workspace](https://slack.com/get-started)

