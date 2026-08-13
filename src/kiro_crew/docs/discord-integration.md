# Discord Integration

Talk to your agent from Discord DMs and explicitly approved server threads.
The channel connects outbound over Discord's Gateway WebSocket, so it works
behind NAT and firewalls with no webhook, public address, or inbound port.

## How the bot behaves

| Context | Behavior |
|---------|----------|
| **DMs** | Responds to messages from an allow-listed user. No `@mention` needed. |
| **Approved server threads** | Responds when both the sender's user ID and the exact thread ID are allow-listed. Everyone who can view the thread can read replies and tool output. |
| **Normal server channels** | Always ignored, even if someone accidentally enters the channel ID in the thread allow-list. Kiro Crew verifies the Discord channel type before running a turn. |
| **Unknown users or threads** | Denied. Security-relevant attempts are audited; unrelated guild chatter is discarded silently. Empty user allow-list denies everything; empty thread allow-list means DMs only. |

Discord represents threads as specialized guild channels with their own channel
IDs. Forum posts are threads too. Group DMs are different and are not supported
by Discord's bot API.

## Setup

### 1. Create the app

Open the [Discord Developer Portal](https://discord.com/developers/applications),
click **New Application**, and name it.

### 2. Get the bot token and choose intents

On the app's **Bot** page, click **Reset Token** and copy it. Discord shows the
token only once; treat it like a password.

- **DMs only:** leave Presence Intent, Server Members Intent, and Message Content
  Intent **OFF**. DM content is always delivered.
- **Server threads:** enable **Message Content Intent**. Discord requires this
  privileged intent to deliver guild/thread message text. Presence and Server
  Members intents remain unnecessary.

Kiro Crew requests the guild/message-content Gateway intents only when at least
one `allowed_thread_ids` entry is configured, so DM-only installs keep the
narrower intent set.

### 3. Install the bot

On **Installation**, enable **Guild Install** and select the **`bot`** OAuth
scope. `applications.commands` is not needed because Kiro Crew uses text
commands such as `!help`, not Discord slash commands.

For DMs only, no guild permissions are required. For server threads, grant:

- **View Channel**
- **Read Message History**
- **Send Messages in Threads**
- **Add Reactions** (used for mid-turn steer receipts)

A manual thread-capable install URL is:

```text
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=274877973568
```

This permission set does not grant **Send Messages** in normal server channels.
For a private thread, explicitly add the bot to that thread if Discord does not
already show it as a member.

### 4. Find the user and thread IDs

Enable **Discord Settings → Advanced → Developer Mode**.

- Right-click your own name → **Copy User ID**.
- Right-click the thread → **Copy Channel ID**. Use the thread's own ID, not its
  parent channel ID.

Both values are long numeric snowflakes, for example `284102345871466496`.

### 5. Configure Kiro Crew

In **Settings → Discord**, enable the channel, paste the bot token, add every
user who may run the agent, and optionally add approved server thread IDs.

Or edit the local configuration directly:

```bash
# ~/.kiro/crew/.env
DISCORD_BOT_TOKEN=<your bot token>
```

```json
// ~/.kiro/crew/config.json
{
  "discord": {
    "enabled": true,
    "allowed_user_ids": ["123456789012345678"],
    "allowed_thread_ids": ["234567890123456789"]
  }
}
```

Omit `allowed_thread_ids` or leave it empty for DMs only. An optional
`soft_threshold_pct` (default `80`, clamped to 1–100) sets the context % at
which the bot suggests `!compact`.

### 6. Restart the gateway

```bash
kirocrew restart
```

Discord settings and Gateway intents are read at startup. The Settings page
shows **Connected** once Discord accepts the IDENTIFY handshake and sends READY.
If Discord closes with code 4014, enable Message Content Intent in the Developer
Portal or clear the thread allow-list and restart in DM-only mode.

### 7. Test both paths

- **DM:** click the bot's name → **Message**, then send `!help`.
- **Thread:** open an approved thread and send `!help`. No `@mention` is needed.
- **Negative check:** send `!help` in the parent channel; the bot must remain
  silent.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Enabled, but no Discord activity | Token missing or gateway not restarted | Set `DISCORD_BOT_TOKEN`, then `kirocrew restart` |
| Gateway closes with 4004 | Bad or reset token | Reset the token, update `.env`, restart |
| Gateway closes with 4014 after adding a thread | Message Content Intent is disabled | Enable it on the Bot page, then restart |
| DMs are ignored | User ID is missing/wrong | Add the numeric user ID; inspect `kirocrew security events` |
| Thread is ignored but DMs work | Thread ID missing/wrong, bot cannot view it, or Message Content Intent is off | Copy the thread's Channel ID, check permissions/membership, enable the intent, restart |
| Parent channel is ignored | Expected behavior | Use an approved thread; normal channels are always disabled |
| Bot can read but cannot reply in a thread | Missing guild permission or private-thread membership | Grant View Channel, Read Message History, Send Messages in Threads; add the bot to private threads |
| Logs are silent on successful connection | `agent.log_level` is `WARNING` | Trust the Connected badge or lower the log level |

## Security model

- **Two allow-lists for threads.** A server-thread turn runs only when both the
  sender and exact thread are approved. An empty user list denies all traffic;
  an empty thread list preserves DM-only behavior.
- **Channel-type verification.** Kiro Crew resolves an approved guild channel
  through Discord and accepts only announcement, public, or private thread
  types. Accidentally entering a normal channel ID does not enable it.
- **Global intent scope.** Enabling any server thread turns on Discord's global
  Message Content intent. Discord then delivers content from every server
  channel the bot can see; Kiro Crew immediately discards traffic outside
  approved threads and does not audit routine background chatter.
- **Shared output warning.** Every member who can view an approved thread can
  read agent replies, tool output, and interactive approvals. Approve only
  threads whose membership and history are appropriate for that disclosure.
- **Conversation scope.** Approved participants in one thread share that
  thread's agent session and context. DMs remain isolated per user.
- **Token handling.** The token lives in `~/.kiro/crew/.env` (mode 0600), is
  masked in Settings, excluded from agent subprocess environments, and can be
  changed only from the machine running the gateway.

## Usage

Send a DM or message an approved thread. Replies stream in place and long
answers split across messages.

| Command | Effect |
|---------|--------|
| `!new` | Start a fresh conversation (shared for the current thread) |
| `!compact` | Compress the current conversation context |
| `!sessions` | Pick a recent dashboard session and continue it here (owner only) |
| `!link` / `!unlink` | Resume or stop mirroring dashboard replies here (on by default) |
| `!stop` | Stop the current reply and clear its queue |
| `!help` | Show commands |

### Continuing a Discord conversation from the dashboard

A Discord conversation is its own mirror: every turn of that session is
delivered back to the Discord channel it is read in, including the turns you
later take from the session's dashboard tab. Nothing to switch on — the binding
is (re-)asserted on each message you send, so it also survives a gateway
restart or a rival claim that took it away.

`!unlink` turns it off, and the refusal is remembered: without that, "off" would
last exactly until your next message, since a conversation with no binding is
indistinguishable from one that was never linked. `!link` withdraws it. Neither
touches a binding you set explicitly from the dashboard to some other target.

### Continuing a dashboard session from Discord

`!sessions` lists your 10 most recent dashboard conversations as buttons. Tap
one and that session continues in this Discord conversation: the last five
messages are replayed for context, and everything you send afterwards goes to
that session instead of your own Discord conversation. `!unlink` releases it and
returns you to your Discord conversation; `!new` releases it and starts a fresh
Discord conversation.

While a session is resumed, `!compact` compresses **that** session's context and
`!stop` cancels **its** running turn. Replies from the dashboard for a resumed
session also appear in Discord, so you can hand work back and forth.

`!session` (singular) is accepted as a typo-safe alias, including with a trailing
phrase — without it the message reaches the agent as ordinary chat text, which
reads as "the feature isn't installed" rather than "you typed it wrong".

#### Seeing and releasing it from the dashboard

A resumed session is **two-way**: what you type in the dashboard is also
delivered to Discord, and Discord messages arrive in that session. Because that
is otherwise invisible from the dashboard side, the resumed session shows a chip
in its chat header — `Driven from Discord DM` — with a **Release** action, and its
session menu lists `Connected: Discord DM` with a **Two-way** badge.

Release is the dashboard-side equivalent of `!unlink`, which used to be the only
way out of a resumed session. It confirms first, and re-attaching is done with
`!sessions` from the channel. A one-way `!link` mirror is labelled **Mirror** and
offers **Stop mirroring** instead, so the two are not confused.

The binding is stored per session and survives a gateway restart. A session can
only be active in one place at a time — if it is already attached to Slack or
another channel, Kiro Crew refuses and tells you where it lives, rather than
moving it silently.

`!sessions` is **owner-only and requires exactly one entry in
`discord.allowed_user_ids`**. Session listing and resume are global operations —
they can reach any dashboard conversation, not just Discord ones — so with two
or more allowed users Kiro Crew cannot tell which one owns the workspace and
refuses the command instead of guessing. Incognito and temporary sessions are
never listed, and session titles plus replayed messages are scrubbed of
credentials and suspicious URLs before they reach Discord.

While a reply is running, prefix a message with `!steer` to fold it into the
running turn or `!queue` to answer it afterward. `[OPTIONS:]` choices render as
buttons, and interactive tool approvals render as Approve/Deny buttons.
