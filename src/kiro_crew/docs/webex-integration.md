# Webex Integration

Chat with your Kiro Crew agent from Cisco Webex — on your phone, your laptop,
anywhere. Create a bot on the Webex developer portal, paste one token, and
you're talking.

Webex needs no public URL and no webhooks: Kiro Crew registers a device with
Webex and receives messages over an outbound WebSocket, so it works from
behind a firewall or NAT. Replies land as one message per turn, with a live
status placeholder ("🤔 Thinking…" → "🔧 Running: …") while the agent works.

## The easy way: just ask Kiro Crew

You don't have to edit anything by hand. In any Kiro Crew session — the
dashboard, Slack, or the CLI — say something like *"set up the Webex
channel."* Kiro Crew walks you through creating the bot, then writes the token
and your email into `~/.kiro/crew/.env` and `config.json` and restarts the
gateway for you. You just hand it the bot token when it asks.

Prefer to wire it up yourself? The manual steps are below.

## Quick start

You'll need a running gateway (`kirocrew gateway`) and a Webex account.

1. **Create a bot** — log in at [developer.webex.com](https://developer.webex.com),
   open **My Webex Apps** under your avatar, click **Create a New App** →
   **Create a Bot**, and fill in the name/username/icon. Copy the **Bot access
   token** shown on the confirmation page (it's displayed only once; you can
   regenerate it later from the app's edit page).
2. **Save the token** to `~/.kiro/crew/.env`:
   ```
   WEBEX_BOT_TOKEN=YmFzZTY0…
   ```
3. **Turn it on** in `~/.kiro/crew/config.json` — your own Webex account email
   is the allow-list:
   ```json
   "webex": { "enabled": true, "allowed_emails": ["you@example.com"] }
   ```
4. **Restart, then say hi:**
   ```bash
   kirocrew restart
   ```
   Search for your bot's username in Webex and send it a direct message.

## Commands

| Command | What it does |
|---|---|
| `/new` | Start a fresh conversation (new session) |
| `/compact` | Compress the conversation context |
| `/help` | Show available commands |

Messages sent while a reply is in progress are folded into the running turn.

## Security model

- **Deny-by-default** — an empty `allowed_emails` list rejects everyone.
  Anyone in an org can message a Webex bot, so add only your own email(s).
- **Direct messages only** — the bot ignores group spaces, even from allowed
  users, so tool output can never leak into a room.
- **Shared turn pipeline** — Webex turns run on the same TurnDriver as Slack:
  credential/exfiltration redaction, the tool-approval ladder, and security
  event logging all apply. With interactive approval mode, tools that need
  approval are denied by default (Webex has no approve/deny buttons); use the
  dashboard for sessions that need interactive tool approval.

## Configuration reference

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_emails` | `[]` | Webex account emails allowed to chat (empty = nobody) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `hard_threshold_pct` | `95` | Context % where the bot force-compacts |
| `bot_token` | `""` | Token fallback if `WEBEX_BOT_TOKEN` isn't set |

Prefer the `WEBEX_BOT_TOKEN` env var over `bot_token` — it keeps your secret
out of `config.json`.

**If something's off:** no reply usually means your email isn't in
`allowed_emails` or `enabled` is `false`; a missing `Webex channel started`
line in the logs means the token isn't set or is invalid; group-space messages
are ignored by design — DM the bot directly.

## Related docs

- [Slack Integration](slack-integration.md)
- [Telegram Integration](telegram-integration.md)
- [WeCom Integration](wecom-integration.md)
- [Getting Started](getting-started.md)
