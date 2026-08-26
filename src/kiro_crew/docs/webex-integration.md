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

## Using it in a group space

Spaces are off by default and are a separate, deliberate decision: a reply in a
space is readable by every member, including people your `allowed_emails` list
excludes. Four things have to line up.

1. **Add the bot to the space** — from the space's *People* menu in Webex. A bot
   that is not a member never sees the space at all.
2. **@mention the bot in every message.** Webex delivers a space message to a bot
   ONLY when the message mentions it. An unmentioned message produces no reply
   **and no log line** — the gateway never receives it, so silence there is Webex
   filtering, not a fault. Commands need the mention too, before the slash:
   `@YourBot /new`.
3. **Get the space's ID.** It is an opaque string with no UI that shows it:
   ```bash
   curl -H "Authorization: Bearer $WEBEX_BOT_TOKEN" https://webexapis.com/v1/rooms
   ```
   The `id` of the space you want is what you need. (An @mention in a denied space
   also records the id in the security event log, so a single failed attempt
   surfaces it.)
4. **Turn it on and name the space** — the switch alone grants nothing:
   ```json
   "webex": {
     "enabled": true,
     "allowed_emails": ["you@example.com"],
     "allow_group_rooms": true,
     "allowed_room_ids": ["Y2lzY29zcGFyazovL3VzL1JPT00v…"]
   }
   ```

A space is its OWN conversation, shared by everyone in it — not a branch of your
DM. `/new` there resets the space's conversation and leaves your DM untouched,
and `/sessions` there lists the space's history rather than yours.

## Commands

| Command | What it does |
|---|---|
| `/new` | Start a fresh conversation (new session) |
| `/compact` | Compress the conversation context |
| `/model` | List the models this account can use, and pick one |
| `/sessions` | List this conversation's earlier sessions |
| `/yolo on \| off \| renew` | Auto-approve every tool for a while |
| `/link` | Resume mirroring dashboard replies here (on by default) |
| `/unlink` | Stop mirroring dashboard replies here |
| `/stop` (or `/cancel`) | Stop the current reply and clear the queue |
| `/kirocrew dashboard [2h]` | Get a dashboard login link (**DM only**) |
| `/help` | Show available commands |

`/help` is generated from the same table the parser uses, so it cannot go stale.

In a group space every command needs the bot's @mention in front of it
(`@YourBot /new`), because Webex only delivers mentioned messages. A dashboard
link is a credential every member of a space could read, so that one command is
refused outside a direct message.

### While a reply is running

A message sent mid-reply is folded into the running turn by default. Prefix it to
choose:

| Prefix | What it does |
|---|---|
| `/queue <message>` | Answer it after the current reply finishes |
| `/steer <message>` | Fold it into the running reply now |

Set `messaging.queue_mode` to `queue` to make queueing the default. A queued
message gets a `⏳ Queued` receipt that updates in place, and a burst is answered
as one reply rather than several.

### Approving a tool

When a tool needs your approval the bot posts Approve / Deny buttons on an
Adaptive Card, and the same question as text:

```
🔐 Approve `fs_write`?

Reply 1 to approve or 2 to deny.
```

Press a button, or reply `1` / `2` — either resolves it. The text always ships
alongside the card, so the prompt is answerable even where the card does not
render. Anything that is not an answer is treated as an ordinary mid-turn
message, so you can redirect the agent instead of answering. An unanswered prompt
is **denied** after five minutes.

## Security model

- **Deny-by-default** — an empty `allowed_emails` list rejects everyone.
  Anyone in an org can message a Webex bot, so add only your own email(s).
- **`allowed_emails` IS the trust boundary — every entry is an operator.** There
  is no second, narrower "owner" tier, here or on any other channel. Anyone on
  that list can run every command, which includes `/yolo` (auto-approves tools
  for the whole process, not just their own conversation, and the dashboard
  toggle and CLI drive the same grant) and `/kirocrew dashboard` (mints a
  presigned dashboard login for YOUR Kiro Crew, DM-only). So add a second address
  only for a person you would hand your own dashboard to. Both commands write a
  SEL record naming the sender, so who took a grant or a link is always
  reviewable in the audit log.
- **Direct messages by default** — group spaces are off until you turn them on
  AND name the spaces, because a reply in a space is readable by every member,
  including people your allowed-emails list excludes. Turning the switch on alone
  answers nothing.
- **Files are scanned before the agent sees them** — Webex scans attachments for
  malware, and a file that is still scanning, infected, or unscannable is refused
  rather than handed over.
- **A group turn will not upload a file or mint a dashboard link** — both would
  disclose more than the reply itself, so they stay DM-only. A reply that
  references a local file keeps printing the path in a space instead of shipping
  the bytes.
- **Shared turn pipeline** — Webex turns run on the same TurnDriver as Slack:
  credential/exfiltration redaction, the tool-approval ladder, and security
  event logging all apply.
- **Approvals fail closed** — a prompt nobody
  answers is denied, and a policy that denies this channel blocks an approve
  while still letting a deny through, so a refused tool never hangs. Turning
  `/yolo` on does not weaken the security gate: sensitive paths, the governance
  ceiling and the deny-list all run ahead of auto-approval, so a hard deny still
  wins.

## Configuration reference

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_emails` | `[]` | Webex account emails allowed to chat (empty = nobody) |
| `allow_group_rooms` | `false` | Answer in group spaces, not just DMs |
| `allowed_room_ids` | `[]` | Spaces the bot may answer in (empty = none) |
| `reply_in_thread` | `true` | Reply under the message's own thread |
| `wdm_base` | `""` | Pin the Device Manager host, which must be an https Webex host (empty = discover it) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `hard_threshold_pct` | `95` | Context % where the bot force-compacts |
| `bot_token` | `""` | Token fallback if `WEBEX_BOT_TOKEN` isn't set |
| `session_folder` | `""` | Folder that Webex sessions are filed under |

Prefer the `WEBEX_BOT_TOKEN` env var over `bot_token` — it keeps your secret
out of `config.json`.

**If something's off:** no reply usually means your email isn't in
`allowed_emails` or `enabled` is `false`; a missing `Webex channel started`
line in the logs means the token isn't set or is invalid. In a group space, no
reply and no log line means the message did not @mention the bot (Webex never
delivered it); a reply refusal with nothing in the log but a
`denied_room_not_permitted` security event means the space is not in
`allowed_room_ids`.

## Related docs

- [Slack Integration](slack-integration.md)
- [Telegram Integration](telegram-integration.md)
- [WeCom Integration](wecom-integration.md)
- [Getting Started](getting-started.md)
