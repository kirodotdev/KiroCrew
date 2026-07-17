---
name: kirocrew-commands
description: Complete CLI reference for KiroCrew commands. Use for help, commands, setup, how to, what can you do, getting started, onboarding.
always: false
triggers: help, commands, setup, gateway, how to, what can you do, getting started, onboard, browse, auth, doctor, cron, artifact, memory, snapshot, eval, security
---
# KiroCrew CLI Reference

## Setup & System

| Command | Description |
|---------|-------------|
| `kirocrew setup` | Interactive wizard — install agent config and configure credentials |
| `kirocrew setup --agent-only` | Only install kiro-cli agent config, skip credential prompts |
| `kirocrew setup --clean` | Fresh install — don't merge from existing config |
| `kirocrew doctor` | Verify KiroCrew setup (checks all dependencies) |
| `kirocrew update` | Update KiroCrew to the latest version |
| `kirocrew --version` | Print installed version |

## Gateway (Server)

| Command | Description |
|---------|-------------|
| `kirocrew gateway` | Start dashboard + Slack gateway |
| `kirocrew gateway --slack-only` | Slack only — skip dashboard web server |
| `kirocrew gateway --no-crons` | Skip cron scheduler |
| `kirocrew gateway --port 9999` | Override dashboard port |
| `kirocrew gateway --port auto` | OS-assigned ephemeral port |
| `kirocrew gateway --no-open` | Don't auto-open dashboard URL in browser |
| `kirocrew gateway --approval reads` | Auto-approve read-only tools |
| `kirocrew gateway --approval yolo` | Auto-approve all tools (requires isolated KIROCREW_HOME) |
| `kirocrew gateway --approval interactive` | Prompt for every tool (default) |
| `kirocrew gateway --seed FIXTURE` | Seed $KIROCREW_HOME from fixture before starting (dev) |
| `kirocrew gateway --test-mode` | Alias for `--port auto --no-open --json-ready --approval reads` |
| `kirocrew stop` | Stop a running gateway |
| `kirocrew stop --port 9999` | Stop gateway on specific port |
| `kirocrew restart` | Restart gateway (service-aware) |
| `kirocrew status` | Show runtime stats (uptime, sessions, crons, lessons) |

## Service Management

| Command | Description |
|---------|-------------|
| `kirocrew service install` | Install and start as system service (sudo on Linux) |
| `kirocrew service uninstall` | Stop and remove system service |
| `kirocrew service status` | Show service status (systemctl/launchctl) |
| `kirocrew logs` | Show gateway logs (last 100 lines) |
| `kirocrew logs -f` | Follow (tail) live log output |
| `kirocrew logs -n 50` | Show last N lines |

## Dashboard Access

| Command | Description |
|---------|-------------|
| `kirocrew token` | Print a dashboard URL with auth token (TTL: 20h) |
| `kirocrew token --ttl 1h` | Token with custom TTL (e.g. 1h, 30m) |
| `kirocrew logout` | Revoke all active dashboard sessions |
| `kirocrew manifest` | Generate Slack app manifest with your alias |
| `kirocrew manifest --url` | Print one-click Slack app creation URL |

## Chat

| Command | Description |
|---------|-------------|
| `kirocrew chat` | Interactive chat (REPL mode) |
| `kirocrew chat -m "message"` | Single message (non-interactive) |
| `kirocrew chat --model claude-opus` | Use specific model |
| `kirocrew chat --tui` | Launch TUI instead of REPL |
| `kirocrew tui` | Launch Terminal UI |
| `kirocrew tui --yolo` | TUI with auto-approve all tools |
| `kirocrew tui --session SESSION_KEY` | Resume a specific session |
| `kirocrew tui --workspace NAME` | Start with a specific workspace |
| `kirocrew tui --agent NAME` | Start with a specific agent |

## Browsing (Playwright MCP)

Browsing uses **Playwright MCP tools**, not kirocrew CLI. The `kirocrew browse` subcommands manage auth only.

| Command | Description |
|---------|-------------|
| `kirocrew browse setup` | Install Playwright MCP + browsers via AIM |
| `kirocrew browse auth health` | Check Midway/Kerberos/MCS auth status (prints JSON) |
| `kirocrew browse auth inject` | Get cookies for Playwright injection (prints JSON) |
| `kirocrew browse auth federate <url>` | Complete federate SSO for a URL, print final URL |

**Browsing workflow:** Load the `browser-auth` skill (or follow it directly):
1. `kirocrew browse auth health` — check auth; if unhealthy, tell user to run `kinit -f` / `mwinit -o` / etc.
2. `kirocrew browse auth refresh` — write Playwright storage state from midway cookies (pre-loads auth into browser context)
3. Use Playwright MCP tools: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill_form`, `browser_take_screenshot`, `browser_evaluate`
4. If you hit `idp.federate.amazon.com`, run `kirocrew browse auth federate <url>` and navigate to the returned `final_url`

**Note:** Playwright auto-installs during `kirocrew setup`. On ARM AL2 fallback to `ReadInternalWebsites` MCP.

## Autonomous Task Runner

| Command | Description |
|---------|-------------|
| `kirocrew run TASK.md` | Run a task spec file (auto-resumes from checkpoint) |
| `kirocrew run TASK.md --fresh` | Start from scratch, ignore checkpoint |
| `kirocrew run TASK.md --no-test` | Skip build/test verification after each step |
| `kirocrew run TASK.md --timeout 3600` | Set global timeout in seconds |
| `kirocrew run TASK.md --name "My Task"` | Override human-readable task name |

## Subagents

| Command | Description |
|---------|-------------|
| `kirocrew spawn run "task"` | Spawn a background subagent (wait for result) |
| `kirocrew spawn run --async "task"` | Fire-and-forget subagent |
| `kirocrew spawn list` | List active subagents |

## Cron Jobs

| Command | Description |
|---------|-------------|
| `kirocrew cron list` | List all cron jobs |
| `kirocrew cron add NAME MESSAGE --every 3600` | Add job with interval (seconds) |
| `kirocrew cron add NAME MESSAGE --cron "0 9 * * MON-FRI"` | Add job with cron expression |
| `kirocrew cron add NAME MESSAGE --agent myagent` | Add job for specific agent |
| `kirocrew cron add NAME MESSAGE --approval-mode auto` | Add job with auto tool approval |
| `kirocrew cron add NAME MESSAGE --channel C123456` | Post results to Slack channel |
| `kirocrew cron update JOB_ID --message "new msg"` | Update job message |
| `kirocrew cron update JOB_ID --agent myagent` | Update job agent |
| `kirocrew cron update JOB_ID --approval-mode auto` | Set auto-approval |
| `kirocrew cron update JOB_ID --approval-mode default` | Reset approval to default |
| `kirocrew cron remove JOB_ID` | Remove a cron job |
| `kirocrew cron pause JOB_ID` | Pause a cron job |
| `kirocrew cron resume JOB_ID` | Resume a paused job |
| `kirocrew cron trigger JOB_ID` | Trigger a job immediately |

## Learning & Memory

| Command | Description |
|---------|-------------|
| `kirocrew learn list` | List all saved lessons |
| `kirocrew learn add "rule text"` | Save a lesson (category: knowledge) |
| `kirocrew learn add "rule text" --category tool` | Save with category (tool/preference/knowledge) |
| `kirocrew learn add "rule text" --negative "avoid X"` | Save with negative example |
| `kirocrew learn remove "query"` | Remove lessons matching substring |
| `kirocrew memory list` | Show semantic memory entries |
| `kirocrew memory search "query"` | Search episodic memories |
| `kirocrew memory stats` | Show memory statistics |
| `kirocrew memory audit` | Scan memory for suspicious content |
| `kirocrew memory export` | Export all memory to JSON (stdout) |
| `kirocrew memory export -o file.json` | Export to file |
| `kirocrew memory import file.json` | Import memory from JSON |
| `kirocrew memory migrate` | Migrate legacy markdown memory to vector store |
| `kirocrew consolidate` | List sessions with unconsolidated messages |
| `kirocrew consolidate SESSION_KEY` | Force consolidate a session (triggers auto-skill extraction) |
| `kirocrew consolidate --all` | Consolidate all pending sessions |

## Artifacts

LLM-generated UI components (widgets, HTML, markdown, SVG, JSON, text).

| Command | Description |
|---------|-------------|
| `kirocrew artifact list` | List all artifacts |
| `kirocrew artifact list --tag ops --kind widget` | Filter by tag and kind |
| `kirocrew artifact list -q "CR"` | Substring filter on name |
| `kirocrew artifact show SLUG` | Print artifact content |
| `kirocrew artifact show SLUG --version 2` | Show specific version |
| `kirocrew artifact show SLUG --meta` | Show metadata as JSON |
| `kirocrew artifact save --name "My Widget" --content-file widget.html` | Save new artifact |
| `kirocrew artifact save --name "X" --content "..." --tags ops,cr` | Save with inline content |
| `kirocrew artifact update SLUG --content-file widget.html` | Update artifact content |
| `kirocrew artifact update SLUG --name "New Name" --tags ops` | Rename/retag |
| `kirocrew artifact versions SLUG` | List version numbers |
| `kirocrew artifact delete SLUG` | Delete artifact and all versions |

## Agents & Workspaces

| Command | Description |
|---------|-------------|
| `kirocrew agent list` | List KiroCrew agents |
| `kirocrew agent create --name NAME` | Create a new agent |
| `kirocrew agent create --name NAME --kiro-agent kirocrew --workspace default` | Full options |
| `kirocrew agent update NAME --kiro-agent new-agent` | Update agent settings |
| `kirocrew agent delete NAME` | Delete an agent |
| `kirocrew workspace list` | List workspaces |
| `kirocrew workspace create --name NAME --dir /path/to/dir` | Create workspace |
| `kirocrew workspace create --name NAME --copy-from existing` | Copy from existing |
| `kirocrew workspace update NAME --dir /new/path` | Update workspace dir |
| `kirocrew workspace delete NAME` | Delete workspace |

## Apps

| Command | Description |
|---------|-------------|
| `kirocrew app list` | List installed apps |
| `kirocrew app install /path/to/app-dir` | Install app from local directory (needs app.json) |
| `kirocrew app enable NAME` | Enable an installed app |
| `kirocrew app disable NAME` | Disable an installed app |
| `kirocrew app uninstall NAME` | Uninstall an app |
| `kirocrew app uninstall NAME --keep-data` | Uninstall but preserve data directory |
| `kirocrew app info NAME` | Show app details |
| `kirocrew app init NAME` | Scaffold a new app (kebab-case name) |
| `kirocrew app init NAME --backend --ui --cron` | Scaffold with backend, UI, and sample cron |

## Configuration

| Command | Description |
|---------|-------------|
| `kirocrew config get` | Show all config |
| `kirocrew config get agent.provider` | Get specific value (dot-separated key) |
| `kirocrew config set dashboard.url http://localhost:5476` | Set a config value (port is the KIROCREW_PORT env var, not a config key) |
| `kirocrew config set --file config.json` | Load full config from JSON file |
| `kirocrew config edit` | Open config in $EDITOR |

## Security & Eval

| Command | Description |
|---------|-------------|
| `kirocrew security audit` | Scan conversation history for suspicious tool usage |
| `kirocrew security deny-list` | Show active deny patterns |
| `kirocrew security events` | Show recent security event log entries (last 20) |
| `kirocrew security events -n 50` | Show N entries |
| `kirocrew security verify` | Verify security event log HMAC integrity |
| `kirocrew eval` | Run smoke test evaluation (~30s) |
| `kirocrew eval memory_recall_basic` | Run specific scenario by name |
| `kirocrew eval --all` | Run all scenarios (slow) |
| `kirocrew eval --judge` | Enable LLM judge scoring |

## Snapshot & Restore

| Command | Description |
|---------|-------------|
| `kirocrew snapshot` | Create a portable backup of KiroCrew state |
| `kirocrew snapshot /path/to/dir` | Snapshot to specific output directory |
| `kirocrew snapshot --keep 7` | Keep N most recent snapshots (default: 7) |
| `kirocrew snapshot --list` | List existing snapshots |
| `kirocrew restore` | Restore from most recent snapshot |
| `kirocrew restore /path/to/snap.tar.gz` | Restore from specific snapshot |
| `kirocrew restore --mode replace` | Replace mode (default) |
| `kirocrew restore --mode merge` | Merge mode |
| `kirocrew restore --dry-run` | Preview without applying |
| `kirocrew restore --components memory,crons` | Restore specific components only |
| `kirocrew restore --list-components` | List restorable components |
| `kirocrew restore --force` | Restore even if gateway is running |

## Slack Commands

### All Allowed Users
| Command | Description |
|---------|-------------|
| `!dashboard` | Get a presigned dashboard link (DM'd to you). Link expires in 5 min; session lasts 1h |
| `!dashboard 2h` | Dashboard link with custom duration (accepts `<N>h` or `<N>m`, max 6h) |
| `/kirocrew dashboard` | Same via slash command |
| `/kirocrew help` | List available slash sub-commands |
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
| `/kirocrew sessions` | List recent sessions with resume/end buttons |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override config/data directory | `~/.kirocrew` |
| `KIROCREW_PORT` | Override dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
