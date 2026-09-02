# Feishu Integration

Talk to your Kiro Crew agent from Feishu (Lark / 飞书) — through a Feishu custom
app bot. Create the app in the Feishu developer console, drop in two values, and
you're chatting.

Like Telegram and WeCom, the connection is outbound-only: Kiro Crew opens a
long-connection WebSocket to Feishu, so there's no callback URL, public
hostname, or open port to manage.

> **Optional dependency.** The Feishu long connection is the one channel that is
> only specified through a vendor SDK (handshake, tenant-token refresh, and
> frame envelope), so it needs `lark-oapi`. It is **not** part of core, so install
> it directly into the environment that runs the gateway:
> `pip install 'lark-oapi>=1.4,<2'`. Without it the gateway logs a line and skips
> the channel — nothing else is affected.

## Quick start

You'll need a running gateway (`kirocrew gateway`), `lark-oapi` installed, and
access to <https://open.feishu.cn/app> (or <https://open.larksuite.com/app> for
Lark).

1. **Create a custom app** — in the developer console, create an app and note
   its **App ID** and **App Secret** from *Credentials & Basic Info*.
2. **Add the bot capability** — *Add features → Bot*.
3. **Grant permissions** — under *Permissions & Scopes*, add
   `im:message` (receive) and `im:message:send_as_bot` (reply).
4. **Use long connection** — under *Events & Callbacks*, choose
   **Long connection** (not a request URL), then subscribe to
   `im.message.receive_v1`.
5. **Publish the app** so your tenant can install it.
6. **Find your open_id** — the console's API Explorer, or any inbound message
   in the gateway log, shows the sender's `open_id` (it starts with `ou_`).
7. **Paste it into Settings** — open **Settings → Channels → Feishu**, put the
   App ID and App Secret in their fields, add your `open_id` to the allow-list,
   turn the channel on, and save. Saving writes the two secrets to
   `~/.kiro/crew/.env` (owner-only, `0600`) and the rest to `config.json`, so
   there is nothing left to hand-edit.
8. **Restart:**
   ```bash
   kirocrew restart
   ```

DM the bot in Feishu and it answers. The panel's badge tells you where you are:
**Needs setup** until both credentials are stored, **Not connected** once they
are but the receiver is not running, **Connected** when it is.

<details>
<summary>Prefer to edit the files directly?</summary>

The panel is a front end over the same two files, so this is equivalent:

```
# ~/.kiro/crew/.env
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=your-app-secret
```

```json
// ~/.kiro/crew/config.json
"feishu": {
  "enabled": true,
  "allowed_open_ids": ["ou_xxxxxxxxxxxxxxxx"]
}
```

Config writes from the panel are **loopback-only** — a remote or tunnelled
dashboard session gets a read-only view, because widening who may reach the
agent is not something a forwarded request should be able to do. On such a
session, editing the files on the gateway's own machine is the way in.

</details>

## Who can reach it

Deny-by-default, in both directions:

- **`allowed_open_ids` is the allow-list.** An empty list authorises **nobody** —
  the bot stays reachable on the Feishu side but rejects every message. This is
  deliberate: publishing an app must never mean an open door.
- **Group chats need two switches.** A group message is served only when
  `allow_group` is `true` **and** the group's `chat_id` is in
  `allowed_group_ids`. Either one alone denies.
- **A group has its own conversation.** A group turn is keyed by the group's
  `chat_id` under a non-direct chat type, so an allow-listed user writing in a
  group never resumes their private DM session — their DM history cannot become
  context for a reply the whole room reads. This holds under
  `messaging.dm_scope: unified` too, where a DM key deliberately collapses into
  a cross-channel bucket and a group key deliberately does not.
- **Only two chat types are served.** A DM (`p2p`) and a group (`group`) are
  named explicitly; a message whose chat type is absent or unrecognised is
  denied rather than assumed to be a DM, so a context whose authorisation was
  never evaluated can never run a turn.
- **Every denial is audited.** Rejected inbound messages write a SEL audit
  record with `source: feishu`, so an unexpected sender shows up in
  `kirocrew security posture` rather than vanishing.
- Feishu itself only delivers a group message to the bot when it is
  **@-mentioned**, so a bot in a busy group does not see unrelated chatter.
- **Mentions reach the agent as names.** Feishu puts opaque placeholders
  (`@_user_1`) in the message text and the display names in a separate
  `mentions` list, so the two are rejoined before the agent sees the prompt:
  "ask @Alice to review" stays intact rather than becoming "ask to review".
  `@_all` becomes `@all`. A message that is nothing but mentions carries no
  instruction and is ignored, so a bare `@bot` does not start an empty turn.
- **Slash commands work in a group too.** A group message has to mention the
  bot, so it arrives as `@FeishuBot /new`. That single leading mention is
  removed before matching, so `/new`, `/reset` and `/compact` still intercept
  there instead of being sent to the agent as a prompt.
  Only a bare command intercepts, and **a message naming anyone else is never a
  command**: `@FeishuBot please run /new later` and `@FeishuBot /new @Alice` are
  both ordinary prompts. That second case is deliberate — reading it as `/new`
  would reset the conversation on a message you addressed to a colleague, and
  that is not recoverable, so anything ambiguous is treated as a prompt.

## Settings

| Key | Default | What it does |
| --- | --- | --- |
| `feishu.enabled` | `false` | Top-level switch. Also needs both env vars. |
| `feishu.allowed_open_ids` | `[]` | Who may DM the bot. Empty = nobody. |
| `feishu.allow_group` | `false` | Serve group chats at all. |
| `feishu.allowed_group_ids` | `[]` | Which groups, when `allow_group` is on. |
| `feishu.soft_threshold_pct` | `80` | Context % that prompts you to `/compact` or `/new`. |
| `feishu.hard_threshold_pct` | `95` | Context % that forces a compaction. |
| `feishu.session_folder` | `""` | Sidebar folder for sessions that start here. Empty = unfiled. |

Everything above except `hard_threshold_pct` is editable in **Settings →
Channels → Feishu**; that one is file-only, because the panel exposes the soft
threshold as the single number worth tuning and keeps the hard ceiling as a
safety net.

Credentials come from the environment only (`FEISHU_APP_ID`,
`FEISHU_APP_SECRET`), matching the console's own naming — they are never written
into `config.json`. The panel stores them in `.env` and afterwards shows only a
masked preview: a saved secret can be **replaced or cleared, never read back**.

## Commands

| Command | Effect |
| --- | --- |
| `/new` (or `/reset`) | Start a fresh session; the old context is dropped. |
| `/compact` | Compact the current context in place. |

Anything else is a prompt.

## What this channel can and cannot do

Feishu v1 is deliberately single-shot: the agent's answer is buffered and sent
as **one reply** when the turn completes, rather than streamed. There are no
interactive buttons, so when the agent offers a choice, the `[OPTIONS: …]`
trailer arrives as a numbered list and you answer by typing one — which is an
ordinary message, so nothing extra has to be configured.

Known gaps, all follow-up work rather than defects:

- **No streaming.** Feishu supports `PATCH /im/v1/messages/{id}`, so
  edit-in-place streaming is a natural next step.
- **Text only.** Non-text messages (images, files, audio) are ignored inbound.
- **Replies only.** The bot answers an inbound message and cannot start a
  conversation, so it is not a proactive-notification target.
- **The panel configures the channel; it does not install it.** `lark-oapi` is an
  optional extra, and running `pip` from a dashboard action would be a new way to
  execute code in the gateway's own environment. When the SDK is missing, Settings reports whether installation is supported for this gateway interpreter and shows its exact install command only when it is; you run that command yourself.
- **Credentials are not verified on save.** A REST tenant-token probe would have
  to pick a domain (`open.feishu.cn` or `open.larksuite.com`) and would report a
  false failure for whichever tenant it guessed wrong, so the panel stores what
  you give it. Nothing is lost: the badge tracks the receiver, and Feishu drops a
  refused app within seconds of a restart, which turns into **Not connected**
  with the reason attached.
- **No auto-reconnect beyond the SDK's own.** `lark-oapi` reconnects
  internally; if it gives up, the gateway logs that the receiver is down and you
  restart.

## How it fits together

The channel is a thin transport over the shared messaging core — the same
`TurnDriver` (credential redaction, tool-approval ladder, SEL audit) every other
channel uses. See [messaging-transport.md](messaging-transport.md).

| File | Role |
| --- | --- |
| `feishu/client.py` | `lark-oapi` WebSocket receive + REST reply |
| `feishu/transport.py` | Authorisation (deny-by-default) and normalisation |
| `feishu/renderer.py` | Buffers the turn, sends one reply |
| `feishu/transport_dispatch.py` | Drives `TurnDriver`, handles `/new` `/compact` |
| `feishu/gateway.py` | `maybe_start_feishu()` boot entry point |
