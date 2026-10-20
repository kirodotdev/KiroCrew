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
>
> **Desktop app: do not install the SDK into the app.** The desktop app runs
> its own bundled Python inside `backend-dist`, and the install line above does
> not apply to it. A `pip install` there writes into the code-signed bundle,
> which can break launches and updates, and every app upgrade replaces the whole
> bundle and removes the package anyway (see #14618). On the desktop app,
> Settings says this gateway cannot install the SDK and shows no command. To use
> Feishu, run the gateway yourself from a source checkout or a virtual
> environment (see [Getting Started](getting-started.md)) and install
> `lark-oapi` there. The Feishu channel then runs on that gateway, not on the
> app's bundled one, and app upgrades do not touch that environment.

## Quick start

You'll need a running gateway (`kirocrew gateway`), `lark-oapi` installed, and
access to <https://open.feishu.cn/app> (or <https://open.larksuite.com/app> for
Lark).

1. **Create a custom app** — in the developer console, create an app and note
   its **App ID** and **App Secret** from *Credentials & Basic Info*.
2. **Add the bot capability** — *Add features → Bot*.
3. **Grant permissions** — under *Permissions & Scopes*, add
   `im:message.p2p_msg:readonly` (receive DMs) and
   `im:message:send_as_bot` (reply). If you enable group chats, also add
   `im:message.group_at_msg.include_bot:readonly` (receive group messages that
   @-mention the bot). The broader `im:message` grant does not replace the
   explicit p2p event scope in the current Feishu console.
4. **Use long connection** — under *Events & Callbacks*, choose
   **Long connection** (not a request URL), then subscribe to
   `im.message.receive_v1`.
5. **Publish the app** so your tenant can install it.
6. **Find your open_id** — the console's API Explorer, or any inbound message
   in the gateway log, shows the sender's `open_id` (it starts with `ou_`).
7. **Paste it into Settings** — open **Settings → Messaging Channels → Feishu**, put the
   App ID and App Secret in their fields, add your `open_id` to the allow-list,
   turn the channel on, and save. Saving writes the two secrets to
   `~/.kiro/crew/.env` (owner-only, `0600`) and the rest to `config.json`, so
   there is nothing left to hand-edit.
8. **Restart:**
   ```bash
   kirocrew restart
   ```

DM the bot in Feishu and it answers. The panel's badge tells you where you are:
**Needs setup** until both credentials are stored, the channel is turned on, and
at least one `open_id` is on the allow-list; **Not connected** once all of that
is in place but the receiver is not running; **Connected** when it is.

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

## Access control

Deny-by-default, in both directions:

- **`allowed_open_ids` is the allow-list.** An empty list authorises **nobody** —
  the bot stays reachable on the Feishu side but rejects every message. This is
  deliberate: publishing an app must never mean an open door.
- **Group chats need two switches and an @-mention.** A group message is
  served only when `allow_group` is `true` **and** the group's `chat_id` is in
  `allowed_group_ids`. Either one alone denies. Inside an allowed group, the
  message must also @-mention **this** bot: the gateway checks the message's
  mention list for the bot's own `open_id`, which it looks up from Feishu when
  the channel starts. A plain message from an allow-listed sender is ignored,
  even when the app holds a permission that delivers every group message.
  Until the bot's `open_id` is known (the lookup failed, and the gateway logs
  one warning and keeps retrying) every group message is ignored; direct
  messages are unaffected.
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
  `kirocrew security events` rather than vanishing. A group message that does
  not mention the bot is chatter, not an access attempt, so it is ignored
  without a record.
- With only the `im:message.group_at_msg.include_bot:readonly` grant, Feishu
  delivers just the group messages that **@-mention** the bot, so a bot in a
  busy group does not see unrelated chatter. A broader grant delivers more,
  and the gateway's own mention check still ignores it.
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

## Settings reference

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
Messaging Channels → Feishu**; that one is file-only, because the panel exposes the soft
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
| `/agent [name]` | Show the agent and the ones you can pick; `/agent <name>` switches, `/agent default` goes back. |

Anything else is a prompt.

## Limits

Feishu v1 is deliberately single-shot: the agent's answer is buffered and sent
as **one reply** when the turn completes, rather than streamed. There are no
interactive buttons, so when the agent offers a choice, the `[OPTIONS: …]`
trailer arrives as a numbered list and you answer by typing one — which is an
ordinary message, so nothing extra has to be configured.

Known gaps, all follow-up work rather than defects:

- **No streaming.** Feishu supports `PATCH /im/v1/messages/{id}`, so
  edit-in-place streaming is a natural next step.
- **Text only.** Non-text messages (images, files, audio) are not read inbound.
  The bot answers with a short line saying so rather than staying silent, so a
  photo does not look like a dead bot — but only to a sender who is already on
  the allow-list, and in a group only one that is allow-listed too and that
  @-mentions the bot.
- **Replies only.** The bot answers an inbound message and cannot start a
  conversation, so it is not a proactive-notification target.
- **The panel configures the channel; it does not install it.** `lark-oapi` is an
  optional extra, and running `pip` from a dashboard action would be a new way to
  execute code in the gateway's own environment. When the SDK is missing, Settings reports whether installation is supported for this gateway interpreter and shows its exact install command only when it is; you run that command yourself.
- **The desktop app cannot run Feishu on its own gateway** until the SDK ships in the bundle (#14618); see the desktop note at the top of this page.
- **Credentials are not verified on save.** A REST tenant-token probe would have
  to pick a domain (`open.feishu.cn` or `open.larksuite.com`) and would report a
  false failure for whichever tenant it guessed wrong, so the panel stores what
  you give it. Nothing is lost: the badge tracks the receiver, and Feishu drops a
  refused app within seconds of a restart, which turns into **Not connected**
  with the reason attached.
- **No auto-reconnect beyond the SDK's own.** `lark-oapi` reconnects
  internally; if it gives up, the gateway logs that the receiver is down and you
  restart.

### Bot doesn’t receive direct messages

- Confirm `im:message.p2p_msg:readonly` is granted. A green
  `im:message.group_at_msg.include_bot:readonly` grant covers group @-mentions,
  not private messages.
- Publish a new app version after changing permissions; a draft permission does
  not affect the installed bot.
- Confirm `im.message.receive_v1` appears in the subscribed-event list, not only
  that long connection is selected.

## How it fits together

The channel is a thin transport over the shared messaging core — the same
`TurnDriver` (credential redaction, tool-approval ladder, SEL audit) every other
channel uses.

| File | Role |
| --- | --- |
| `feishu/client.py` | `lark-oapi` WebSocket receive + REST reply |
| `feishu/transport.py` | Authorisation (deny-by-default) and normalisation |
| `feishu/renderer.py` | Buffers the turn, sends one reply, plus a best-effort notice reply when the answer was redacted |
| `feishu/transport_dispatch.py` | Drives `TurnDriver`, handles `/new` `/compact` |
| `feishu/gateway.py` | `maybe_start_feishu()` boot entry point |

## Related docs

- [Channel capabilities](channel-capabilities.md): the ten-channel matrix — streaming, buttons, uploads, reply length, approval timeout
- [Getting Started](getting-started.md): install, first run, connecting a channel
- [Configuration](configuration.md): the config file and environment variables
