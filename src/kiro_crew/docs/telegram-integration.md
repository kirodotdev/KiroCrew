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
- Direct messages always work. A **group** is served only when it is a supergroup
  with Topics on, `allow_forum` is `true`, AND its `chat_id` is in
  `allowed_forum_chat_ids` — then each Topic becomes its own conversation, the
  way a Slack thread does. Ordinary groups and a supergroup's **General** chat
  are always refused, because a reply there is readable by everyone in the group.
- Anyone else is quietly dropped and recorded in the audit log.

## Commands

At startup the bot publishes the menu commands from `COMMAND_SPEC` through `setMyCommands`, so typing `/` in Telegram offers autocomplete. `COMMAND_SPEC` also drives `/help`; commands that need an argument appear in the help footer instead of the menu.

- `/new` (or `/start`) — start a fresh conversation
- `/compact` — free up room when the context fills
- `/model` (or `/models`) — pick the model from an inline-button list. Button-only on purpose:
  the choices are what this account's backend actually advertised, so there is
  no model name to guess and no typo to reject mid-conversation. The pick is
  applied to the running session in place when one is idle, and is remembered
  for the conversation's later sessions (it outlives `/new`, and is held in
  memory, so a gateway restart returns to the configured default).
- `/yolo [on|off|renew]` — report or change the auto-approve grant. This is the
  SAME process-wide grant the dashboard toggle and Slack's `/kirocrew yolo`
  drive, so it expires on one clock everywhere. There is deliberately no
  `telegram.yolo` setting and no per-channel wrapper around it: an approval grant
  is global by nature, and an operator who turns auto-approve off expects it off
  everywhere rather than off in whichever surface they happened to type it in. It
  does not weaken the PreToolUse gate: sensitive-path, governance-ceiling and
  deny-list blocks still refuse a tool.
- `/link` / `/unlink` — resume or stop mirroring dashboard replies here; a
  conversation is its own mirror by default, so `/link` only withdraws an
  earlier `/unlink`
- `/stop` (or `/cancel`) — stop the current reply and clear the queue
- `/steer <msg>` — while a reply is generating, fold this message into it
  (overrides `queue_mode` for this message)
- `/queue <msg>` — while a reply is generating, hold this message and answer
  it after the current turn (overrides `queue_mode` for this message)
- `/agent` (or `/agents`) — pick the agent from an inline-button list of the specs installed on
  this machine. Button-only for the same reason `/model` is. Unlike a model, an
  agent cannot be swapped inside a running session — the spec decides which MCP
  servers and skills that process loaded at spawn — so a pick opens a fresh
  conversation. The previous one is not destroyed: switching back returns to it.
- `/status` — uptime, message counts, tool decisions, sessions
- `/ping` — answers `pong`. Answered by the gateway itself, never by the model,
  so it still works when the thing that is wedged is the model.
- `/sessions [search words]` (or `/session [search words]`) — with no words, the ten
  most recent conversations, newest first, with a mark for whichever is live. Add
  words to use the same title-and-message-content ranking as dashboard history search;
  incognito and temporary transcripts stay excluded. Results remain read-only and
  open through `/kirocrew dashboard`. **Direct message only**, like `/kirocrew
  dashboard`: either form can name conversations from across the host, and a forum
  Topic is readable by the whole supergroup, so answering there would show titles to
  members who are not on `allowed_user_ids` at all. In a Topic it refuses and points
  you to a DM. It also refuses when `allowed_user_ids` contains several people,
  because the bot cannot tell which one owns the host-wide history.
- `/title <text>` — rename this conversation, so its dashboard sidebar row reads
  as something other than the first forty characters you happened to type.
- `/cron` (or `/crons`) `list | pause <id> | resume <id> | remove <id>|all` — manage scheduled
  jobs. The same jobs the dashboard and Slack see.
- `/spawn <task>` (or `/bg`) — run a task in a background subagent.
  `/spawn list` shows what is running.
- `/task` (or `/tasks`) `run <spec> | status | cancel` — drive the unattended task runner.
- `/kirocrew dashboard [<N>h|<N>m]` — get a dashboard login link (DM only).
- `/temporary` — this conversation reads and saves no memory: no memories or
  lessons are added to the prompt, and nothing is written to the transcript. A bare
  `/temporary` just marks the conversation; `/temporary <question>` marks it and
  answers, the same as Slack's `!temporary`.
- `/incognito` — this conversation MAY read memory but saves nothing. That is the
  whole difference from `/temporary`, and it is the reason for two commands:
  incognito keeps the context you have built up and leaves no trace, temporary does
  neither. Both survive a restart, and both apply to the conversation the next
  message will actually run in rather than the one that has just rolled over.
- `/voice [on|off]` — speak this conversation's answers as well as typing them,
  using the global `voice_reply` provider settings. A bare `/voice` reports the
  current state rather than toggling, so you cannot flip it the wrong way in a
  room where you meant to turn it off. Short answers are not spoken, and the text
  reply always lands first — an answer that existed only as audio would be lost
  whenever TTS was unavailable. The conversation's choice survives `/new`;
  `telegram.voice_replies` is the default a new conversation starts from.
- `/help` — list the commands

`/steer` and `/queue` are absent from the `/` menu because the Telegram client
SENDS a menu entry the moment it is tapped, and both need a message body to act
on — a menu row for them would only ever produce the usage hint. `/spawn`,
`/title` and `/task` are absent for the same reason, and are documented in the
`/help` card's footer instead.

`/cron`, `/spawn` and `/task` reach the same code Slack's keyword commands do
(`messaging/commands.py`), so their answers are identical on both channels. Each
one reports plainly when its service is not running on this instance rather than
failing silently.

## Approving a tool

When the agent wants to run something that needs your say-so, you get three
buttons: **Approve** this one, **Deny** it, or **Trust this conversation** —
which auto-approves the rest of this conversation's tools. The prompt shows the
tool's actual arguments, so "approve bash?" becomes "approve `rm -rf
/tmp/build`?", which is the difference between a decision and a guess.

Trust is the same per-session grant Slack's Trust button gives, and it is
narrower than `/yolo`: one conversation, held in memory only, gone on restart.
Each prompt's buttons carry a one-time value, so a button left in your scrollback
cannot approve a later tool even if the agent restarts and reuses the same
internal request number: pressing an old one reports that it expired.
Neither weakens the security gate — a denied-by-policy tool is still refused.

Answer-choice buttons are tied to the conversation that created them. If you
start a new conversation, switch agents, or press an old button after that
conversation moved, the choice is refused instead of being sent somewhere else.
A choice such as `/new` is always treated as answer text for the agent, never as
the command itself; if the target conversation is busy, type the choice after it
finishes rather than queueing a button press whose origin could go stale.

## Pictures, and what else comes back

When the agent produces an image — a chart, a screenshot, a rendered diagram —
Telegram gets the **picture**, uploaded as its own message right after the
answer. Before this it printed the filesystem path as text. Only real images are
sent, decided by the file's leading bytes rather than its name, and only from
inside the session's own working directory; anything refused keeps its original
markdown so you can see what was skipped and why.

While a reply is forming you get a live typing indicator and a `🔧 tool…` footer
naming what the agent is doing. If nothing moves for a while the footer says so
(`🥱` at 15 seconds, `😨` at 45), which is the one thing the typing indicator
cannot tell you. A tool waiting on your approval is not a stall, so the mark
stays away while a prompt is open.

When a turn is worth commenting on, a quoted line under the answer reports it —
`Finished in 1m 20s · 🟠 ctx 54%`. It appears only past a threshold (10 seconds,
or 50% context): a footer under every single reply is one you learn to skip,
including on the turn where the context warning finally matters.

Scheduled jobs report back **here**. A cron you create from Telegram delivers its
result to this conversation, not only to the dashboard bell.

## Settings & reference

Everything lives in the `telegram` section of `config.json`:

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on |
| `allowed_user_ids` | `[]` | Numeric IDs allowed to chat (empty = nobody) |
| `soft_threshold_pct` | `80` | Context % where the bot suggests `/compact` |
| `show_thinking` | `false` | Post the model's reasoning after each answer as a collapsed, expandable quote. Off by default: Telegram's rate limit is per chat and shared with the streaming edits the answer already spends, so this costs one extra message per turn |
| `voice_replies` | `false` | Speak each answer as a voice/audio message alongside the text, using the global `voice_reply` provider settings. The default for a new conversation; toggle one conversation with `/voice on\|off`. Off because it costs a second message per turn and TTS may not be installed |
| `allow_forum` | `false` | Serve a supergroup's Topics as per-Topic conversations |
| `allowed_forum_chat_ids` | `[]` | Supergroup `chat_id`s allowed to do that (they are NEGATIVE, e.g. `-1001234567890`); empty = no group at all |
| `forum_activation` | `"always"` | When to answer inside an allow-listed Topic: `always`, `mention` (only when `@YourBot` is used or one of its own messages is replied to), or `off`. Slack's channel equivalent defaults to `mention`; this defaults to `always` so an existing forum keeps working after an upgrade. A value that is present but unrecognized falls back to `mention`, not `always`, so a typo cannot widen who the bot answers in a shared Topic. Never applies to a 1:1 DM, which is always served |
| `session_folder` | `""` | Sidebar folder these conversations are filed into |
| `bot_token` | `""` | Token fallback if `TELEGRAM_BOT_TOKEN` isn't set |
| `accounts` | `{}` | Deprecated compatibility map for former named bots; it is parsed but starts no channel |

Prefer the `TELEGRAM_BOT_TOKEN` env var over `bot_token` — it keeps your secret
out of `config.json`.

**If something's off:** no reply usually means your ID isn't allowed or
`enabled` is `false`; a missing `Telegram channel started` line means the token
isn't set; slow replies behind a proxy mean you should set `HTTPS_PROXY` for the
gateway. In a group, check `allow_forum` AND that the supergroup's negative
`chat_id` is allow-listed — either one missing refuses every message there. If the
bot is in the Topic and still silent, check `forum_activation`: on `mention` it
answers only when addressed, and on `off` it answers nothing.

A restart no longer replays your last few messages: the `getUpdates` cursor is kept in `~/.kiro/crew/routing/telegram_offset.json`. Delete that file only if you want a deliberate replay of whatever Telegram still holds.

Transport capabilities: streaming, edits, reactions, inbound and outbound files, rich blocks, forum-topic threads, native tables, and proactive sends are enabled. Text chunks are capped at 4,000 characters and interactive prompts at 25 buttons; excess choices become numbered text.

## Related docs

- [Slack Integration](slack-integration.md)
- [WeCom Integration](wecom-integration.md)
- [Getting Started](getting-started.md)
