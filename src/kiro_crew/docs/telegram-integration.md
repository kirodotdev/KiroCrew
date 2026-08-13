# Telegram Integration

Chat with your Kiro Crew agent right from Telegram — on your phone, your laptop,
anywhere. Create a bot, paste one token, and you're talking. Replies stream back
live, with tappable option buttons.

Telegram is the quickest channel to set up: just a bot token, no plugins, and it
works from behind a firewall — Kiro Crew reaches out to Telegram, so there's
nothing to expose.

## The easy way: just ask Kiro Crew

You don't have to edit anything by hand. In any Kiro Crew session — the
dashboard, Slack, or the CLI — say something like *"set up the Telegram
channel."* Kiro Crew walks you through creating the bot, then writes the token
and your user ID into `~/.kiro/crew/.env` and `config.json` and restarts the
gateway for you. You just hand it the bot token when it asks.

Prefer to wire it up yourself? The manual steps are below.

## Quick start

You'll need a running gateway (`kirocrew gateway`) and a Telegram account.

1. **Create a bot** — message **@BotFather**, send `/newbot`, and follow the
   prompts. You'll get a token like `123456789:AA…`.
2. **Find your user ID** — message **@userinfobot**; it replies with your number
   (e.g. `123456789`). That's the only account your bot will answer.
3. **Save the token** to `~/.kiro/crew/.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AA…
   ```
4. **Turn it on** in `~/.kiro/crew/config.json`:
   ```json
   "telegram": { "enabled": true, "allowed_user_ids": [123456789] }
   ```
5. **Restart, then say hi:**
   ```bash
   kirocrew restart
   ```

Send your bot a message and it answers. If it stays quiet, check that your ID is
in `allowed_user_ids` and look for `Telegram channel started` in the gateway
log.

## Who can reach it

> **Kiro Crew runs on your machine, with your files and credentials.** So it only
> talks to people you name — and only in private chats.

- Trusted numeric IDs go in `allowed_user_ids`; an empty list means nobody.
- Group messages are ignored — replies happen in DMs only.
- Anyone else is quietly dropped and recorded in the audit log.

## Commands

The bot publishes this list through `setMyCommands` at startup, so typing `/`
in Telegram offers them as autocomplete — `COMMAND_SPEC` in
`telegram/commands.py` is the single source behind both that menu and `/help`.

- `/new` (or `/start`) — start a fresh conversation
- `/compact` — free up room when the context fills
- `/model` — pick the model from an inline-button list. Button-only on purpose:
  the choices are what this account's backend actually advertised, so there is
  no model name to guess and no typo to reject mid-conversation. The pick is
  applied to the running session in place when one is idle, and is remembered
  for the conversation's later sessions (it outlives `/new`, and is held in
  memory, so a gateway restart returns to the configured default).
- `/yolo [on|off|renew]` — report or change the auto-approve grant. This is the
  SAME process-wide grant the dashboard toggle and Slack's `/kirocrew yolo`
  drive, so it expires on one clock everywhere. It does not weaken the
  PreToolUse gate: sensitive-path, governance-ceiling and deny-list blocks still
  refuse a tool.
- `/link` / `/unlink` — resume or stop mirroring dashboard replies here; a
  conversation is its own mirror by default, so `/link` only withdraws an
  earlier `/unlink`
- `/stop` (or `/cancel`) — stop the current reply and clear the queue
- `/steer <msg>` — while a reply is generating, fold this message into it
  (overrides `queue_mode` for this message)
- `/queue <msg>` — while a reply is generating, hold this message and answer
  it after the current turn (overrides `queue_mode` for this message)
- `/help` — list the commands

`/steer` and `/queue` are absent from the `/` menu because the Telegram client
SENDS a menu entry the moment it is tapped, and both need a message body to act
on — a menu row for them would only ever produce the usage hint.

## Settings & reference

Everything lives in the `telegram` section of `config.json`:

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_user_ids` | `[]` | Numeric IDs allowed to chat (empty = nobody) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `bot_token` | `""` | Token fallback if `TELEGRAM_BOT_TOKEN` isn't set |

Prefer the `TELEGRAM_BOT_TOKEN` env var over `bot_token` — it keeps your secret
out of `config.json`.

**If something's off:** no reply usually means your ID isn't allowed or
`enabled` is `false`; a missing `Telegram channel started` line means the token
isn't set; slow replies behind a proxy mean you should set `HTTPS_PROXY` for the
gateway.

## Related docs

- [Slack Integration](slack-integration.md)
- [WeCom Integration](wecom-integration.md)
- [Getting Started](getting-started.md)
