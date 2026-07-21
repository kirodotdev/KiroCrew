# Discord Integration

Talk to your agent from Discord DMs. The channel connects outbound over
Discord's Gateway WebSocket, so it works behind NAT and firewalls with no
webhook or public address.

## Setup

1. **Create the app.** Open the [Discord Developer Portal](https://discord.com/developers/applications),
   click **New Application**, and name it.
2. **Get the bot token.** On the app's **Bot** page, click **Reset Token** and
   copy it. No privileged intents are needed: the bot is DM-only and DM
   content is always delivered.
3. **Install the bot.** On **Installation**, copy the install link (or use
   OAuth2 → URL Generator with the `bot` scope) and open it to add the bot to
   any server you share. You can then DM it directly.
4. **Find your user ID.** In Discord, enable **Settings → Advanced →
   Developer Mode**, then right-click your name and choose **Copy User ID**.
5. **Configure KiroCrew.** In the dashboard, open **Settings → Discord**:
   enable the channel, paste the bot token, and add your user ID to the
   allowed list. Or edit config directly:

   ```bash
   # ~/.kirocrew/.env
   DISCORD_BOT_TOKEN=<your bot token>
   ```

   ```json
   // ~/.kirocrew/config.json
   {
     "discord": {
       "enabled": true,
       "allowed_user_ids": ["123456789012345678"]
     }
   }
   ```

6. **Restart the gateway** (`kirocrew restart`). The Settings page shows a
   Connected badge once the channel is up.

## Security model

- **Deny-by-default allow-list.** Anyone sharing a server with the bot can DM
  it, so an empty `allowed_user_ids` rejects every message (fail closed).
  Every denial is recorded in the security event log.
- **DM-only.** Messages in server channels are ignored even from allowed
  users, so tool output can never land in a shared channel.
- **Token handling.** The token lives in `~/.kirocrew/.env` (mode 0600), is
  masked in the settings UI, is excluded from agent subprocess environments,
  and config writes are accepted only from the machine running the gateway.

## Usage

Send a DM to chat. Replies stream in and long answers split across messages.

| Command | Effect |
|---------|--------|
| `!new` | Start a fresh conversation |
| `!compact` | Compress context when it gets long |
| `!link` / `!unlink` | Mirror this conversation's dashboard tab here |
| `!stop` | Stop the current reply and clear the queue |
| `!help` | Show commands |

While a reply is running, prefix a message with `!steer` to fold it into the
running turn, or `!queue` to answer it after. A plain mid-turn message follows
the global `messaging.queue_mode` setting. Telegram-style `/` prefixes are
also accepted, though Discord's own client may intercept bare `/` as a
slash-command.

`[OPTIONS:]` choices render as buttons. Tool approvals appear as
Approve/Deny buttons when the approval mode is interactive.
