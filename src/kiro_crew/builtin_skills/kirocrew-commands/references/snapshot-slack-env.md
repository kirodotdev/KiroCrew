# Snapshot and restore, Slack commands, environment variables

Moved verbatim from the kirocrew-commands `SKILL.md` core.

## Snapshot & Restore

| Command | Description |
|---------|-------------|
| `kirocrew snapshot` | Create a portable backup of Kiro Crew state |
| `kirocrew snapshot /path/to/dir` | Snapshot to specific output directory |
| `kirocrew snapshot --keep 7` | Keep N most recent snapshots (default: 7) |
| `kirocrew snapshot --strict` | Exit 3 instead of 0 when the bundle omits entries it was asked to carry |
| `kirocrew snapshot --list` | List existing snapshots |
| `kirocrew restore /path/to/snap.tar.gz --mode merge` | Restore a snapshot, keeping existing state and adding to it. There is no default mode: without `--mode` a terminal is asked and a non-terminal is refused |
| `kirocrew restore /path/to/snap.tar.gz --mode replace` | Replace mode (overwrite existing state; keeps this install's `config.json`, `config.local.json` and `hooks.json`) |
| `kirocrew restore /path/to/snap.tar.gz --mode replace --include-config` | Replace mode that also overwrites `config.json`, `config.local.json` and `hooks.json` with the snapshot's |
| `kirocrew restore /path/to/snap.tar.gz --mode merge --dry-run` | Preview without applying |
| `kirocrew restore --components memory,crons` | Restore specific components only |
| `kirocrew restore --list-components` | List restorable components |
| `kirocrew restore --force` | Restore even if gateway is running |

## Slack Commands

### All Allowed Users
| Command | Description |
|---------|-------------|
| `!dashboard` | Get a presigned dashboard link (DM'd to you). Link expires in 5 min; session lasts 1h |
| `!dashboard 2h` | Dashboard link with custom duration (accepts `<N>h` or `<N>m`, max 20h) |
| `/kirocrew dashboard` | Same via slash command |
| `/kirocrew help` | List available slash sub-commands |
| `/kirocrew status` | Show runtime stats |
| `/kirocrew sessions` | List recent sessions with resume/end buttons |
| `!stop` | Force-halt the current agent turn (bypasses semaphore, cancels active task) |
| `status` | Show runtime stats |
| `ping` | Auto-reply `pong` |
| `cron list` | List cron jobs |
| `run <path>` | Run an autonomous task from a spec file |

### Owner-Only Slash Commands
| Command | Description |
|---------|-------------|
| `/kirocrew yolo` | Toggle YOLO mode (auto-approve all tool calls) |
| `/kirocrew agent` | Show agent selector dropdown |
| `/kirocrew agent <name>` | Switch to named agent |
| `/kirocrew voice` | Open TTS voice settings modal |
| `/kirocrew config` | Open config modal |
| `/kirocrew users` | Open allowed users management modal |
| `/kirocrew channels` | Open tracked channels modal |
| `/kirocrew restart` | Restart the gateway (needs a systemd supervisor) |

### Slack Code Channel Commands
Answered in a code channel while `slack.code_channels` is on; elsewhere `agent` falls back to the app-wide selector above.

| Command | Description |
|---------|-------------|
| `/kirocrew agent [<name>]` | List the channel session's agents, or switch it to one |
| `/kirocrew approval [normal\|reads\|trust]` | Show or set the channel session's approval mode |
| `/summarize`, `/wrap-up`, `/create-pr` | The channel's runtime commands |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
| `KIROCREW_POD_REPO` | Repo to resolve worktree names from | invoking cwd |
| `KIROCREW_POD_ROOT` | Isolated pod HOMEs (nuked on stop) | `~/.kirocrew-pods` |
| `KIROCREW_POD_BASE_PORT` | Port derivation base | `7810` |
| `KIROCREW_POD_LIVE_PORT` | Port a pod must never bind | `5476` |
