# Slack Integration

KiroCrew connects to Slack via Socket Mode. You interact with it through DMs
or in channels where the bot is present.

## Activation Modes

> **Security note:** Slack interaction is restricted to the owner only
> (`KIROCREW_OWNER_ID`). Multi-user access is disabled for security —
> allowed users would act under the owner's system identity.

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
| `!yolo on/off/status` | Toggle auto-approve for all tool calls |
| `!agent <name>` | Switch to a different agent globally |
| `!agent off` | Switch back to default kirocrew agent |
| `!ta set <name>` | Set agent for this thread only |
| `!ta off` | Remove thread agent override |
| `!ta status` | Show current thread agent |
| `!allowlist @user` | ~~Add/remove a user from the allowlist~~ (disabled) |
| `!allowlist #channel` | ~~Track/untrack a channel for auto-allowlist~~ (disabled) |
| `!channel` | Show current channel activation mode |
| `!channel always/mention/observe/review/off` | Set channel activation mode |
| `!channel agent <name/off>` | Set per-channel agent override |

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
| `/kirocrew @user` | ~~Same as `!allowlist @user`~~ (disabled) |
| `/kirocrew #channel` | ~~Same as `!allowlist #channel`~~ (disabled) |

## Tool Approval Flow

When KiroCrew needs to run a tool (file write, bash command, etc.):

1. **Auto mode** (`!yolo on`): silently approves everything
2. **Interactive mode** (default): posts Approve / Trust session / Reject buttons
3. 120-second timeout — auto-rejects if no click
4. "Trust session" approves all remaining tools for that session

Approval buttons appear in both Slack and the dashboard. Approving in either
place resolves both.

## Emergency Stop

When KiroCrew is executing and you need to halt it immediately, type `!stop`
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

## OPTIONS Buttons

When KiroCrew presents choices, they render as interactive Block Kit buttons.
Click a button to send that choice back to the conversation. You can select
multiple options before submitting.

## Sharing Access

> ⚠️ **Multi-user Slack access is currently disabled for security.** KiroCrew
> is restricted to the bot owner only. The `!allowlist` command and
> `/kirocrew @user` are disabled. Allowed users in config have no effect.
>
> Rationale: allowed users act under the owner's system identity (file
> permissions, AWS credentials) with no scope limits or expiry.

The dashboard can still be accessed via `!dashboard` presigned links by the
owner only.

## Channel Monitoring

When `slack.tracking_channels` is configured, KiroCrew watches for new members
joining those channels and prompts the owner to allowlist them.

### Channel Activation Modes

Each tracked channel has an activation mode:

| Mode | Behavior |
|------|----------|
| `always` | Respond to every message in the channel |
| `mention` | Respond only when @mentioned or in active threads |
| `observe` | Monitor only — no responses, used for analytics |
| `review` | Responses sent as ephemeral drafts with Approve/Edit/Cancel buttons |
| `off` | Disabled |

Review mode is useful for channels where you want human approval before the
bot posts publicly.

## Setting Up Your Slack App

KiroCrew connects to Slack as a Socket Mode app that you create and install in
your own workspace.

1. **Create a Slack app** at https://api.slack.com/apps and enable **Socket
   Mode**. Generate an app-level token (`xapp-`) with the `connections:write`
   scope.
2. **Add a bot user** and OAuth scopes for the features you use (e.g.
   `chat:write`, `im:history`, `app_mentions:read`, `reactions:write`). Install
   the app to your workspace to get the bot token (`xoxb-`).
3. **Set credentials** in `~/.kirocrew/.env` (`SLACK_APP_TOKEN`,
   `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`).
4. **Slash command** (optional) — the command name is configurable via
   `slack.command` in config.json (default: `kirocrew`). Each app instance
   should use a unique name.

> **Access scoping** — Multi-user access is disabled for security. KiroCrew is
> restricted to the bot owner only (`KIROCREW_OWNER_ID`).

## Settings API (dashboard)

The Slack tab at `/settings?tab=slack` is backed by three dashboard-only
endpoints (registered behind token auth, never on the API-only server):

- `GET /api/slack/config` — masked token previews (`xoxb-••••wxyz`), presence
  booleans, owner ID, slash command, enterprise-org allowlist, behavior
  toggles, plus live status: `connected` (real socket outcome recorded at
  startup), `connect_error` (e.g. `invalid_auth`), and `read_only` (true for
  any request that is not direct-local).
- `PUT /api/slack/config` — direct-local only (loopback peer AND no proxy
  forwarding headers; remote gets 403). Tokens are write-only and verified
  against Slack before storage (`auth.test` / `apps.connections.open`);
  rejected tokens return 400 and are never written. Offline saves succeed
  with `verify_warning`. Clearing a token requires a strict boolean
  `<field>_clear: true`. Response `restart_required` is true for secret/owner
  changes and boot-read config (`command`, `allowed_enterprise_ids`);
  `reactions_enabled` / `show_thinking` apply live.
- `GET /api/slack/manifest` — renders the bundled app manifest (alias from
  `?alias=`, defaulting to `kirocrew`, never `$USER`) and Slack's one-click
  create deep link. Public template only.

Secrets land in `config_dir/.env` via atomic 0600 writes; `os.environ` is
synced after saves so status reads stay truthful. `allowed_users` /
`open_channels` are intentionally not exposed while the runtime enforces
owner-only access.
