# WeChat Integration

Talk to your KiroCrew agent from WeChat — through a WeCom (企业微信) AI bot. Create
the bot in your WeCom console, drop in two values, and you're chatting. Replies
stream back live.

> **WeChat vs. WeCom.** KiroCrew connects through **WeCom (企业微信)**, the work
> edition, using its AI-bot API. It does **not** sign in to a personal WeChat
> account — people message the bot from inside WeCom.

Like Telegram, the connection is outbound-only: KiroCrew opens a secure
WebSocket to WeCom, so there's no callback URL or open port to manage.

## The easy way: just ask KiroCrew

You don't have to edit anything by hand. In any KiroCrew session — the
dashboard, Slack, or the CLI — say something like *"set up the WeChat channel."*
KiroCrew tells you where to create the WeCom AI bot, then writes your Bot ID and
Secret into `~/.kirocrew/.env` and `config.json` and restarts the gateway for
you. You just paste the two values when it asks.

Prefer to wire it up yourself? The manual steps are below.

## Quick start

You'll need a running gateway (`kirocrew gateway`) and admin access to your
WeCom console.

1. **Create an AI bot** — in the WeCom admin console, open **应用管理 → AI 智能体**
   and create a bot. Its settings page shows a **Bot ID** and a **Secret**.
2. **Note the userids** — every WeCom member has a `userid` (账号). Collect the
   ones you want to let in.
3. **Save the credentials** to `~/.kirocrew/.env`:
   ```
   WECOM_BOT_ID=your-bot-id
   WECOM_SECRET=your-bot-secret
   ```
4. **Turn it on** in `~/.kirocrew/config.json`:
   ```json
   "wechat": {
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

Those two values — **Bot ID** and **Secret** — are all KiroCrew needs. There's
no corp ID, agent ID, callback URL, or AES key to wire up. Good to know: the
WeCom bot replies to messages you send it — it can't start a conversation on its
own, and it has no buttons (so `OPTIONS` arrive as plain text).

## Who can reach it

> **KiroCrew runs on your machine, with your files and credentials.** So it only
> talks to the owner and the userids you name.

- Authorized senders: the **owner** (`KIROCREW_OWNER_ID`) plus anyone listed in
  `allowed_users`. With no owner and an empty list, nobody gets in.
- Whole-company access: set `"allow_all_users": true` (or flip **Allow all
  organization members** in Settings → WeCom) to skip listing each userid.
  This is an explicit opt-in — an empty list never means "everyone" — and it
  works because a WeCom AI bot is only reachable inside your own org tenant.
  Messages without a userid are still dropped.
- The WeCom AI bot carries direct messages only — one conversation per userid.
- Anyone else is quietly dropped and recorded in the audit log.

## Commands

- `/new` (or `新对话`, `清空`) — start a fresh conversation
- `/compact` — free up room when the context fills

## Settings & reference

Everything lives in the `wechat` section of `config.json`:

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_users` | `[]` | `{ "userid", "name" }` entries allowed to chat (empty = owner only) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `hard_threshold_pct` | `95` | Context % hard cutoff |
| `ws_url` | `wss://openws.work.weixin.qq.com` | WeCom AI-bot endpoint |

Credentials go in `~/.kirocrew/.env`: `WECOM_BOT_ID` and `WECOM_SECRET`.

**If something's off:** no reply usually means the sender's userid isn't allowed
or `enabled` is `false`; a missing `channel started` line means a credential is
unset; if it connects and then goes quiet, the bot may have been removed from
the WeCom console — re-add it and restart.

## Related docs

- [Slack Integration](slack-integration.md)
- [Telegram Integration](telegram-integration.md)
- [Getting Started](getting-started.md)
