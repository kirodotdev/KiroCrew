# Slack Integration

Kiro Crew connects to Slack via Socket Mode. You interact with it through DMs
or in channels where the bot is present.

## Activation Modes

Slack interaction is restricted to the owner — see [Access control](#access-control).

Each channel can have a different activation mode:

| Mode | Behavior | Default for |
|------|----------|-------------|
| `always` | Process every message from allowed users | DMs |
| `mention` | Only respond when @mentioned; continue in thread replies | Group channels |
| `observe` | Passively record messages; respond only when @mentioned (full context) | — |
| `off` | Ignore all messages | — |
| `review` | Require explicit activation before replying to each message | — |

Set per-channel: `!channel always` / `!channel mention` / `!channel observe` / `!channel review` / `!channel off`

## Owner Commands

Only the owner (set via `KIROCREW_OWNER_ID`) can use these:

| Command | Description |
|---------|-------------|
| `!yolo` / `!yolo on/off/renew` | Show, toggle, or renew auto-approve for all tool calls |
| `!agent <name>` | Switch to a different agent globally |
| `!agent off` | Switch back to default kirocrew agent |
| `!ta <name>` | Set agent for this thread only |
| `!ta off` | Remove thread agent override |
| `!ta` | Show the current thread agent |
| `!channel` | Show current channel activation mode |
| `!channel always/mention/observe/review/off` | Set channel activation mode |
| `!channel agent <name/off>` | Set per-channel agent override |

`!allowlist` and `/kirocrew @user` are not accepted while access is owner-only, and
`slack.allowed_users` in config has no effect.

## Commands for All Allowed Users

| Command | Description |
|---------|-------------|
| `!dashboard` | Get a presigned dashboard link (DM'd to you) |
| `!dashboard 2h` | Dashboard link with custom duration (max 6h) |
| `!stop` | Force-halt the current agent turn in this thread. Bypasses the per-session semaphore and cancels the active task. See "Emergency Stop" below |

## Keyword Commands

Available to all allowed users (no `!` prefix needed):

| Command | Description |
|---------|-------------|
| `status` | Show runtime stats (uptime, sessions, crons, lessons) |
| `ping` | Auto-reply with `pong 🦞` |
| `cron list` | List all scheduled cron jobs |
| `cron remove <id>` | Remove a cron job |
| `cron pause <id>` | Pause a cron job |
| `cron resume <id>` | Resume a paused cron job |
| `spawn run "task"` | Spawn a background subagent |
| `spawn list` | List running subagents |
| `run <path>` | Run an autonomous task from a spec file |
| `run status` | Check task runner status |
| `run cancel` | Cancel the running task |
| `sessions` | List recent dashboard sessions with resume buttons |
| `!compact` | Manually trigger context compaction |
| `!incognito <msg>` | Send message in incognito mode (reads memory, blocks writes) |
| `!temporary <msg>` | Send message in temporary mode (blocks both reads and writes) |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/kirocrew dashboard` | Same as `!dashboard` |

## Tool Approval Flow

When Kiro Crew needs to run a tool (file write, bash command, etc.):

1. **Auto mode** (`!yolo on`): silently approves everything
2. **Interactive mode** (default): posts Approve / Trust session / Reject buttons
3. 120-second timeout — auto-rejects if no click. Slack has its own figure; the
   other five channels that prompt at all wait five minutes, and four channels do
   not prompt. See [Channel capabilities](channel-capabilities.md).
4. "Trust session" approves all remaining tools for that session

Approval buttons appear in both Slack and the dashboard. Approving in either
place resolves both.

## Emergency Stop

When Kiro Crew is executing and you need to halt it immediately, type `!stop`
in the thread where the agent is running.

`!stop` is intercepted before the per-session semaphore in the Slack event
handler, so it acts even when the agent is mid-tool-call or mid-stream.
The active asyncio task is cancelled, the message queue for that session is
cleared, the pending queue is dropped, and the session is reset. You will
see "⛔ Execution stopped." in the thread when the stop completes.

Authorization: owner and allowed users. Unauthorized callers get
"⛔ Not authorized." and an audit log entry under `slack.stop_command`.

## Streaming

Responses stream in real-time via progressive Slack message edits. A cursor
(▍) shows during streaming. Tool calls appear as 🔧 _tool name_ inline.

When the response finishes, the 👀 reaction swaps to 🦞.

## File attachments

Slack ingests attachments on incoming messages and can upload local image references from completed replies. Outbound uploads are limited to 10 files, 10 MiB per file, and 25 MiB total per reply.

## OPTIONS Buttons

When Kiro Crew presents choices, they render as interactive Block Kit buttons.
Click a button to send that choice back to the conversation. You can select
multiple options before submitting.

## Access control

Kiro Crew on Slack answers the bot owner (`KIROCREW_OWNER_ID`) and nobody else.
Multi-user access is disabled because an allowed user would act under the owner's
system identity — the owner's file permissions and cloud credentials — with no scope
limit and no expiry. `!allowlist`, `/kirocrew @user` and `slack.allowed_users` are all
inert as a result, and stale allowlist entries are pruned at startup.

`!dashboard` presigned links go to the owner only.

## Channel Monitoring

When `slack.tracking_channels` is configured, Kiro Crew watches for new members
joining those channels and prompts the owner to allowlist them.

### Channel Activation Modes

Each tracked channel has an activation mode:

| Mode | Behavior |
|------|----------|
| `always` | Respond to every message in the channel |
| `mention` | Respond only when @mentioned or in active threads |
| `observe` | Record authorized messages; respond only when @mentioned or in active threads, with channel context |
| `review` | Generate an owner approval draft instead of posting a public response |
| `off` | Disabled |

Review mode is useful for channels where you want human approval before the
bot posts publicly.

## Setting Up Your Slack App

Kiro Crew connects to Slack as a Socket Mode app that you create and install in
your own workspace.

1. **Create a Slack app** at https://api.slack.com/apps and enable **Socket Mode**. Generate an app-level token (`xapp-`) with the `connections:write` scope.
2. **Add a bot user** with these Bot Token Scopes:
   `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`,
   `commands`, `files:read`, `files:write`, `groups:history`, `groups:read`,
   `im:history`, `im:read`, `im:write`, `reactions:write`, and `users:read`.
3. **Add User Token Scopes** if the same app supplies a user token to a
   separately configured Slack MCP/search integration: `channels:history`,
   `channels:read`, `groups:history`, `groups:read`, `im:history`, `im:read`,
   `mpim:history`, `mpim:read`, `search:read`, and `users:read`. The gateway
   does not consume this `xoxp-...` token.
4. **Subscribe to bot events**: `message.im`, `message.channels`,
   `message.groups`, `app_mention`, `app_home_opened`, `file_change`, and
   `member_joined_channel`. Install or reinstall the app to grant the scopes and
   get the bot token (`xoxb-`).
5. **Set credentials** in `~/.kiro/crew/.env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`). Slack has no `enabled` setting: both Slack tokens opt the channel in.
6. **Slash command** (optional) — the command name is configurable via
   `slack.command` in config.json (default: `kirocrew`). Each app instance
   should use a unique name.

## Settings page

The Slack channel view at `/settings/channels/slack` shows the connection state, the
masked token previews, the owner ID, the slash command and the behaviour toggles. Three
things about it are worth knowing before you use it:

- **Editing is loopback-only.** A request reaching the dashboard through a proxy or from
  another machine is read-only, so a remote browser sees the state and cannot change it.
- **A token is verified against Slack before it is stored.** A rejected token is not
  written. If the host is offline the save succeeds with a warning instead, and clearing
  a token takes an explicit clear action rather than an empty field.
- **Some changes need a restart** — the tokens, the owner ID, the slash command and the
  enterprise-org allowlist are read at boot. Reactions and thinking-indicator toggles
  apply immediately.

Secrets are written to `.env` in the config directory with owner-only permissions, never
to `config.json`.

## Related docs

- [Channel capabilities](channel-capabilities.md): the ten-channel matrix — streaming, buttons, uploads, reply length, approval timeout
- [Getting Started](getting-started.md): install, first run, connecting a channel
- [Configuration](configuration.md): the config file and environment variables
