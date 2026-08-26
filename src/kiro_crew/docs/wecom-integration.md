# WeCom Integration

Talk to your Kiro Crew agent from WeCom — through a WeCom (企业微信) AI bot. Create
the bot in your WeCom console, drop in two values, and you're chatting. Replies
stream back live.

> **WeChat vs. WeCom.** Kiro Crew connects through **WeCom (企业微信)**, the work
> edition, using its AI-bot API. It does **not** sign in to a personal WeChat
> account — people message the bot from inside WeCom.

Like Telegram, the connection is outbound-only: Kiro Crew opens a secure
WebSocket to WeCom, so there's no callback URL or open port to manage.

## The easy way: just ask Kiro Crew

You don't have to edit anything by hand. In any Kiro Crew session — the
dashboard, Slack, or the CLI — say something like *"set up the WeCom channel."*
Kiro Crew tells you where to create the WeCom AI bot, then writes your Bot ID and
Secret into `~/.kiro/crew/.env` and `config.json` and restarts the gateway for
you. You just paste the two values when it asks.

Prefer to wire it up yourself? The manual steps are below.

## Quick start

You'll need a running gateway (`kirocrew gateway`) and admin access to your
WeCom console.

1. **Create an AI bot** — in the WeCom admin console, open **应用管理 → AI 智能体**
   and create a bot. Its settings page shows a **Bot ID** and a **Secret**.
2. **Note the userids** — every WeCom member has a `userid` (账号). Collect the
   ones you want to let in.
3. **Save the credentials** to `~/.kiro/crew/.env`:
   ```
   WECOM_BOT_ID=your-bot-id
   WECOM_SECRET=your-bot-secret
   ```
4. **Turn it on** in `~/.kiro/crew/config.json`:
   ```json
   "wecom": {
     "enabled": true,
     "allowed_users": [{ "userid": "zhangsan", "name": "Zhang San" }]
   }
   ```
5. **Restart, then say hi:**
   ```bash
   kirocrew restart
   ```

Message the bot in WeCom and it answers. If it stays quiet, look for
`WeCom WS connected and subscribed` in the gateway log and confirm your userid
is allowed.

Those two values — **Bot ID** and **Secret** — are all Kiro Crew needs. There's
no corp ID, agent ID, callback URL, or AES key to wire up. Good to know about
today's WeCom channel: it renders no tappable buttons, so a list of choices
arrives as a numbered list you answer by typing. Tappable cards do exist in the
AI-bot API; they are not wired up yet.

## Who can reach it

> **Kiro Crew runs on your machine, with your files and credentials.** So it only
> talks to the owner and the userids you name.

- Authorized senders: the **owner** (`KIROCREW_OWNER_ID`) plus anyone listed in
  `allowed_users`. With no owner and an empty list, nobody gets in.
- Whole-company access: set `"allow_all_users": true` (or flip **Allow all
  organization members** in Settings → WeCom) to skip listing each userid.
  This is an explicit opt-in — an empty list never means "everyone" — and it
  works because a WeCom AI bot is only reachable inside your own org tenant.
  Messages without a userid are still dropped.
- **Direct messages only — one conversation per userid.** A message sent to the
  bot in a WeCom **group** is refused and recorded in the audit log, even from an
  allowed sender. Your session is keyed to your userid, so answering in a group
  would replay that private conversation's history and tool output to everyone in
  the room. Group support needs its own per-group sessions and group allow-list.
- Anyone else is quietly dropped and recorded in the audit log.

## Sending files and screenshots

Send an image, a file, or a voice note and the agent sees it. Images and files are
downloaded and decrypted from WeCom's CDN; a voice note uses WeCom's own
transcript, so nothing extra has to be installed. A screenshot with a caption
works too — the caption comes through with the picture, and a picture with no
caption at all is still delivered rather than ignored. A caption that happens to
look like a command is treated as text: attach a photo captioned `/new` and you get
an answer about the photo, not a reset conversation and a lost picture. Send the
command in its own message to run it.

Per-item ceilings follow WeCom's own: 10 MB per image, 20 MB per file (including an
audio file you attach, which is transcribed locally), 10 items per message. A voice
note has no size ceiling here because its bytes are never fetched — WeCom sends the
transcript. Anything refused is reported rather than silently dropped. Sending a
file *to* you is not wired up yet, so if the agent produces an image you'll get its
path rather than the picture.

## Commands

- `/new` (or `新对话`, `清空`) — start a fresh conversation
- `/compact` (or `压缩`) — free up room when the context fills
- `/stop` (or `/cancel`, `停止`) — stop the reply that's running
- `/link` / `/unlink` — mirror the dashboard's replies for this conversation here
- `/help` (or `帮助`) — show the command list

While a reply is running, prefix a message to control it:

- `/steer <message>` — fold it into the running reply now
- `/queue <message>` — not supported here; you'll be asked to resend instead

**Approving tools.** WeCom has no approve/deny buttons, so a tool the agent wants to
run is declined unless auto-approve is already on. Turn it on from the dashboard (or
Slack) — it is one grant for the whole machine, it expires on its own, and tools your
policy denies stay blocked either way. There is deliberately no WeCom command for it:
one switch with one answer beats a per-channel copy.

Send commands in your direct chat with the bot. A leading `@mention` is tolerated
and stripped before the command is matched, so `@Kiro /new` also works — but see
"Who can reach it": messages sent in a WeCom group are refused, so commands only
take effect in a direct chat.

## Settings & reference

Everything lives in the `wecom` section of `config.json`:

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_users` | `[]` | `{ "userid", "name" }` entries allowed to chat (empty = owner only) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `hard_threshold_pct` | `95` | Context % hard cutoff |
| `ws_url` | `wss://openws.work.weixin.qq.com` | WeCom AI-bot endpoint |

Credentials go in `~/.kiro/crew/.env`: `WECOM_BOT_ID` and `WECOM_SECRET`.

**If something's off:** no reply usually means the sender's userid isn't allowed
or `enabled` is `false`; a missing `channel started` line means a credential is
unset; if it connects and then goes quiet, the bot may have been removed from
the WeCom console — re-add it and restart.

## Related docs

- [Slack Integration](slack-integration.md)
- [Telegram Integration](telegram-integration.md)
- [Getting Started](getting-started.md)
