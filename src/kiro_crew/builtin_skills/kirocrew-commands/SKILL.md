---
name: kirocrew-commands
description: Complete CLI reference for Kiro Crew commands. Use for help, commands, setup, how to, what can you do, getting started, onboarding.
always: false
triggers: help, commands, setup, gateway, how to, what can you do, getting started, onboard, browse, auth, doctor, cron, artifact, memory, snapshot, eval, security, kirocrew pod, pod up, pod down, pod ls, pod status, pod logs, pod provision, pod install, pod token
inject_on_trigger: false
---
# Kiro Crew CLI Reference

Detail lives in `references/` next to this file. A section below that names a
file there holds only a pointer: read that file before you answer about it.

## Setup & System

| Command | Description |
|---------|-------------|
| `kirocrew setup` | Interactive wizard — install agent config (messaging channels connect later) |
| `kirocrew setup --slack` | Also run the guided Slack credential setup (opt-in; ignored with `--agent-only`) |
| `kirocrew setup --agent-only` | Only install kiro-cli agent config, skip the other wizard steps |
| `kirocrew setup --clean` | Fresh install — don't merge from existing config |
| `kirocrew doctor` | Verify Kiro Crew setup (checks all dependencies) |
| `kirocrew doctor --bundle` | Collect logs + crash reports into a redacted diagnostics zip |
| `kirocrew ledger-sweep` | List finished session/work ledgers older than 30 days (dry run, deletes nothing) |
| `kirocrew update` | Update Kiro Crew to the latest version |
| `kirocrew update approve` | Approve a pending in-app update armed from the dashboard |
| `kirocrew update --force` | Discard local commits when a git checkout has diverged from upstream (git installs only) |
| `kirocrew --version` | Print installed version |
| `kirocrew sandbox status` | Report whether this launch is covered by the userns AppArmor profile |
| `kirocrew sandbox install-profile` | Attach the profile to this app (sudo; `--path P` for an explicit executable) |
| `kirocrew sandbox remove-profile` | Unload and remove the profile (sudo) |

`update --force` is destructive on a git install: the hard reset discards local
commits, recoverable only from `git reflog`. The `sandbox` verbs matter only on
hosts with `kernel.apparmor_restrict_unprivileged_userns=1` (Ubuntu 23.10+ and
derivatives) and are no-ops everywhere else; `--path` is refused for
world-writable locations and for shared interpreters such as `/usr/bin/python3`,
which would over-grant.

## Kiro Crew's Own Engine (KAS)

Signs in and runs without kiro-cli: `kas` installs Kiro Crew's own engine bundle and holds its own Kiro identity in the vault.

| Command | Description |
|---------|-------------|
| `kirocrew kas install` | Download this release's pinned KAS engine and Node runtime (`--force` replaces an installed copy; `--from DIR` installs offline; `--version`/`--url` need `--unpinned` to leave the pin) |
| `kirocrew kas status` | Show the pinned engine, what is installed, and the route |
| `kirocrew kas remove` | Delete installed engine versions (`--version V` for one, `--yes` skips the confirmation) |
| `kirocrew kas login` | Sign the vault in with a device code, no browser needed on this host (`--provider builder_id\|idc\|google\|github`, default `builder_id`; `idc` needs `--start-url`, and takes `--region`) |
| `kirocrew kas whoami` | Show which Kiro identity the vault is signed in as (`--format text\|json`) |
| `kirocrew kas logout` | Remove every Kiro identity from the vault |
| `kirocrew kas route` | Print which program owns the Kiro sign-in here: `kas-bundle` or `kiro-cli` |

## Gateway (Server)

| Command | Description |
|---------|-------------|
| `kirocrew gateway` | Start dashboard + Slack gateway |
| `kirocrew gateway --slack-only` | Slack only — skip dashboard web server |
| `kirocrew gateway --no-crons` | Skip cron scheduler |
| `kirocrew gateway --port 9999` | Override dashboard port |
| `kirocrew gateway --port auto` | OS-assigned ephemeral port |
| `kirocrew gateway --no-open` | Don't auto-open dashboard URL in browser |
| `kirocrew gateway --approval reads` | Auto-approve read-only tools on background turns (cron, task runner, subagents); a channel turn still asks about every tool |
| `kirocrew gateway --approval yolo` | Auto-approve all tools (requires isolated KIROCREW_HOME) |
| `kirocrew gateway --approval interactive` | Prompt for every tool. Without `--approval`, `agent.approval_mode` decides (default `auto`: no prompt) |
| `kirocrew gateway --seed FIXTURE` | Seed $KIROCREW_HOME from fixture before starting (dev) |
| `kirocrew gateway --seed-replace` | Wipe a non-empty target home and re-seed it (paired with `--seed`) |
| `kirocrew gateway --no-tunnel` | Never publish a tunnel for this process's whole life, whatever `tunnel.enabled` says. Scoped to TUNNELS: it does not change where the dashboard binds |
| `kirocrew gateway --json-ready` | Print one `KIROCREW_READY:{...}` line (port, token, pid, KIROCREW_HOME) once the dashboard is bound |
| `kirocrew gateway --test-mode` | Alias for `--port auto --no-open --json-ready --approval reads` |
| `kirocrew stop` | Stop a running gateway |
| `kirocrew stop --port 9999` | Stop gateway on specific port |
| `kirocrew restart` | Restart gateway (service-aware) |
| `kirocrew status` | Show runtime stats (uptime, sessions, crons, lessons) |
| `kirocrew usage` | Recent credit spend per day; `--by model\|agent\|channel\|session`, `--days N` / `--since YYYY-MM-DD`, `--json` |
| `kirocrew trace show KEY` | Print one traced turn as a waterfall of its model calls, tool calls and sub-agents, read from the local span files (written while `telemetry.traces_enabled` is on); `KEY` is a turn id, session key or id, ACP session id or trace id |
| `kirocrew gateway-pid` | Print the PID holding this home's gateway lock (JSON) |
| `kirocrew open SESSION` | Open the desktop app on a session through a `kirocrew://` deep link (`SESSION` is the `?sid=` value in the dashboard URL); `--print` shows the URL and opens nothing |

The token in a `--json-ready` line grants dashboard access for up to 20 hours, so
captured stdout from a test harness is a credential: keep it out of logs and
transcripts.

## Service Management

| Command | Description |
|---------|-------------|
| `kirocrew service install` | Install and start as system service (sudo on Linux) |
| `kirocrew service uninstall` | Stop and remove system service (Linux: both system and per-user unit, each scope reported) |
| `kirocrew service status` | Show service status (systemctl/launchctl; Linux: system and per-user unit, each named) |
| `kirocrew logs` | Show gateway logs (last 100 lines) |
| `kirocrew logs -f` | Follow (tail) live log output |
| `kirocrew logs -n 50` | Show last N lines |
| `kirocrew tmp reclaim PATH…` | Remove each named /tmp or /var/tmp entry of yours once it is idle and unused (`--dry-run` removes nothing) |

## Pods (Isolated Worktree Test Instances)

Read `references/pods-and-browsing.md` for every `kirocrew pod` command (up,
down, ls, status, logs, provision, install, token, restart, exec, api, prune).

## Dashboard Access

| Command | Description |
|---------|-------------|
| `kirocrew token` | Print a dashboard URL with auth token (TTL: 20h) |
| `kirocrew token --ttl 1h` | Token with custom TTL (e.g. 1h, 30m) |
| `kirocrew logout` | Revoke all active dashboard sessions |
| `kirocrew desktop tunnel --host HOST` | Keep the desktop app's SSH forward to a remote crew alive (client-only mode); `--local-port` is the loopback port the app connects to, `--remote-port` the one the crew listens on over there |
| `kirocrew manifest` | Generate Slack app manifest with your alias |
| `kirocrew manifest --url` | Print one-click Slack app creation URL |
| `kirocrew remote-control status` | Show whether personal Remote Control is on (`--json`); a personal sign-in is on by default |
| `kirocrew remote-control on` | Turn personal Remote Control back on after an `off`; it connects after Kiro Crew restarts |
| `kirocrew remote-control off` | Turn personal Remote Control off; it stays off across restarts |

## Chat

| Command | Description |
|---------|-------------|
| `kirocrew chat` | Interactive chat (REPL mode) |
| `kirocrew chat -m "message"` | Single message (non-interactive); `-m -` reads it from a piped stdin (up to 1 MiB) |
| `kirocrew chat --model MODEL_ID` | Use a specific model (default from config; `kirocrew models` lists the names) |
| `kirocrew chat --headless -m "task"` | Run one unattended task on this data home's running gateway and exit with a result code; never reads the terminal. `--message-file PATH` reads the seed from a file, `--mode conductor`, `--approval-mode trust`, `--until-done` (with `--idle-grace SECS`, default 900), `--cwd DIR`, `--timeout SECS` (exit 4), `--budget CREDITS` (exit 5), `--json`. A request for a person ends the run (exit 3). Refused (exit 2) inside an agent session: run it from a normal terminal, or use `spawn_run` |

When `kirocrew chat` asks to run a tool, answer `y` to allow that call once or
`n` to deny it, the same keys `kiro-cli chat` uses. `o` denies it and lets you
type a reply to the agent instead. Anything else denies.

## Browsing (`browser` MCP tool)

Read `references/pods-and-browsing.md` for the `browser` tool's actions and the
`kirocrew browser` commands.

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
| `kirocrew spawn run --crew NAME "task"` | Run as a Bot (its memory and template); an unknown name is refused |
| `kirocrew spawn run --agent NAME "task"` | Run under a named agent template; an unknown name is refused |
| `kirocrew spawn run --model ID "task"` | Run on a model id; it outranks the member's and the template's pin |
| `kirocrew spawn list` | List subagents that no session started (CLI and app runs) |

## Cron Jobs

| Command | Description |
|---------|-------------|
| `kirocrew cron list` | List all cron jobs |
| `kirocrew cron add NAME MESSAGE --every 3600` | Add job with interval (seconds) |
| `kirocrew cron add NAME MESSAGE --cron "0 9 * * MON-FRI"` | Add job with cron expression |
| `kirocrew cron add NAME MESSAGE --agent myagent` | Add job for specific agent |
| `kirocrew cron add NAME MESSAGE --approval-mode auto` | Add job with auto tool approval |
| `kirocrew cron add NAME MESSAGE --channel C123456` | Post results to Slack channel |
| `kirocrew cron add NAME MESSAGE --at "tomorrow 9am"` | One-shot job: fires once, then deletes itself (Unix timestamp or time string) |
| `kirocrew cron add NAME MESSAGE --cron "0 9 * * *" --timezone Europe/Paris` | Evaluate the cron expression in an IANA timezone |
| `kirocrew cron add NAME "" --every 3600 --script ~/.kiro/crew/crons/f.py:run --no-persistent-session --minimal-context` | Zero-token job: run a Python function (file must already be under `crons/`; registers, does not copy) |
| `kirocrew cron add NAME "" --every 600 --command "df -h /" --no-persistent-session --minimal-context` | Zero-token job: run a vetted shell command |
| `kirocrew cron add ... --timeout 30 --timeout-secs 60` | Subprocess timeout (script/command) and whole-wake budget |
| `kirocrew cron add ... --[no-]persistent-session --[no-]minimal-context --hide-in-chat --model ID` | Session shape and model; defaults are persistent session on, minimal context off, so pass `--no-persistent-session --minimal-context` for a script/command job |
| `kirocrew cron add ... --project-path /abs/repo` | Run the agent session in that repo, so its `.kiro/steering/` and `AGENTS.md` load (agent jobs only; `cron update JOB_ID --project-path ''` clears it) |
| `kirocrew cron update JOB_ID --message "new msg"` | Update job message |
| `kirocrew cron update JOB_ID --agent myagent` | Update job agent |
| `kirocrew cron update JOB_ID --approval-mode auto` | Set auto-approval |
| `kirocrew cron update JOB_ID --approval-mode default` | Reset approval to default |
| `kirocrew cron remove JOB_ID` | Remove a cron job |
| `kirocrew cron pause JOB_ID` | Pause a cron job |
| `kirocrew cron resume JOB_ID` | Resume a paused job |
| `kirocrew cron adopt JOB_ID --session-of SESSION` | Give the job an owning chat session, so that session manages it and receives its results |
| `kirocrew cron adopt JOB_ID --release` | Clear the owning session, returning the job to CLI/dashboard-only management |
| `kirocrew cron trigger JOB_ID` | Trigger a job immediately |
| `kirocrew cron history JOB_ID` | Show a job's past runs, newest first (read-only); `-n N` (default 20), `--status error` keeps every run that did not succeed, `--run RUN_ID` prints one full record with its trace, `--json` |
| `kirocrew cron history --all` | Read every job's runs, newest first |
| `kirocrew cron preview SCRIPT` | Run a script cron locally with real MCP tools; notifications and session and crew-log calls are printed, not made |
| `kirocrew cron preview SCRIPT -m "msg" -e K=V` | Preview with an input message / extra env vars |

## MCP Resource Subscriptions

A session subscribes with the `resource_subscribe` MCP tool once `mcp_gateway.resource_subscriptions.enabled` is on (off by default; needs a restart). These commands are the operator's view.

| Command | Description |
|---------|-------------|
| `kirocrew resource-subs list` | List every session's MCP resource subscriptions |
| `kirocrew resource-subs remove ID` | Remove one subscription by its id (the `rsub_...` id `list` shows) |

## Learning & Memory

| Command | Description |
|---------|-------------|
| `kirocrew learn list` | List all saved lessons |
| `kirocrew learn add "rule text"` | Save a lesson (category: knowledge) |
| `kirocrew learn add "rule text" --category tool` | Save with category (tool/preference/knowledge) |
| `kirocrew learn add "rule text" --negative "avoid X"` | Save with negative example |
| `kirocrew learn add "rule text" --applies on_topic` | Save as a past finding loaded only when relevant (`always`: a standing rule for every session) |
| `kirocrew learn remove "query"` | Remove lessons matching substring (add `--repo-scope FRAG` to remove only that scope, `--repo-scope ""` for global only) |
| `kirocrew memory list` | Show semantic memory entries |
| `kirocrew memory search "query"` | Search episodic memories |
| `kirocrew memory stats` | Show memory statistics |
| `kirocrew memory audit` | Scan memory for suspicious content |
| `kirocrew memory create-store <name>` | Create the directory of a named V1 store declared in `memory_stores` (`use` never creates it; refuses the default store, an undeclared name and a V2 member store) |
| `kirocrew memory export` | Export the default store's rows to JSON (stdout) |
| `kirocrew memory export -o file.json` | Export to file |
| `kirocrew memory export --store <name>` | Export a named store's rows instead |
| `kirocrew memory import file.json` | Import memory from JSON |
| `kirocrew memory import --store <name> file.json` | Import into a named store instead |
| `kirocrew memory import-text notes.md` | Offer the facts in a text or Markdown file and save the ones you confirm |
| `kirocrew memory migrate` | Migrate legacy markdown memory to vector store |
| `kirocrew memory backup [--keep N]` | Back up active memory stores now |
| `kirocrew memory backups [--store <name>]` | List memory backups, newest first |
| `kirocrew memory restore [--store <name>] [--from FILE \| --cancel-pending]` | Restore a store from a backup (default: its newest) |
| `kirocrew memory carve --store <name>` | Filter or count a crew store's memory by carve facets (`--scope`, `--surface`, `--crew`, `--session-key`) |
| `kirocrew memory retired [--restore ID]` | List episodes a semantic write superseded, and restore one |
| `kirocrew memory forget KEY` | Delete one semantic memory key after you confirm it (owner only; needs a terminal and the running gateway; not on native Windows). `--superseded-by KEY` records the live key that replaces it |
| `kirocrew memory show [preferences\|projects\|history]` | Show the markdown memory layer (default: all three; `--format md\|json`, `--since YYYY-MM-DD` for history) |
| `kirocrew memory dream list` | Print the Dreaming approval list of what a run proposed, read-only (`--store`, `--json`) |
| `kirocrew memory dream run` | Ask the gateway for a Dreaming run now; it still waits for idle |
| `kirocrew memory shadow report` | Per shadow memory stage, show its flag, window, days, sessions and would-change rate, read-only (`--stage`, `--since YYYY-MM-DD` or `<n>d`, `--store`, `--json`) |
| `kirocrew knowledge stats` | Count sources, documents and items in the knowledge library (read-only; `--json` for JSON) |
| `kirocrew knowledge dedup` | Preview cross-source duplicate knowledge documents (dry-run) |
| `kirocrew knowledge dedup --apply` | Actually collapse the duplicates |
| `kirocrew consolidate` | List sessions with unconsolidated messages |
| `kirocrew consolidate SESSION_KEY` | Force consolidate a session (triggers auto-skill extraction) |
| `kirocrew consolidate --all` | Consolidate all pending sessions |

## Artifacts

Read `references/apps-and-artifacts.md` for the `kirocrew artifact` commands.

## Agents & Workspaces

| Command | Description |
|---------|-------------|
| `kirocrew agent list` | List Kiro Crew agents |
| `kirocrew agent create --name NAME` | Create a new agent |
| `kirocrew agent create --name NAME --kiro-agent kirocrew --workspace default` | Full options |
| `kirocrew agent update NAME --kiro-agent new-agent` | Update agent settings |
| `kirocrew agent update NAME --display-name "Label"` | Change the name the dashboard shows (`""` clears it); the agent name stays |
| `kirocrew agent delete NAME` | Delete an agent |
| `kirocrew agent reset-model [--agent kirocrew]` | Clear a pinned model so the agent tracks the shipped default |
| `kirocrew agent pin [NAME FILE]` | Choose which spec file an agent name runs when several declare it (`agent.spec_pins`); with no arguments, list the pins. `--clear` removes the pin for `NAME` |
| `kirocrew agent validate [PATH]` | Check agent specs offline for unknown fields, wrong value shapes and malformed tools entries; exits 1 on a problem (`--allow-unknown-fields`, `--print-schema`) |
| `kirocrew models [NAME]` | List the model names `--model` accepts, or check one name (`--json`, `--port`) |
| `kirocrew workspace list` | List workspaces |
| `kirocrew workspace create --name NAME --dir DIRNAME` | Create workspace (`--dir` is a **name under the data home**, not an absolute path) |
| `kirocrew workspace create --name NAME --copy-from existing` | Copy from existing |
| `kirocrew workspace update NAME --dir DIRNAME` | Update workspace dir (same containment rule) |
| `kirocrew workspace delete NAME` | Delete workspace |

**On the CLI**, `--dir` must resolve to a **strict descendant** of
`$KIROCREW_HOME` (default `~/.kiro/crew`): anything landing outside — `/tmp/x`,
`../x`, `~/x` — is refused with a SEL `denied` audit event, and so is the data
home **root itself** (in any spelling: absolute, `~/.kiro/crew`, `.`, or empty),
since a workspace there would put agent-writable memory on top of `config.json`
and `.env`. The test is containment, not "is it absolute": an absolute path
landing *under* the home is accepted, since it resolves where the relative form
would. Pass `workspace-myproject`, not `/path/to/dir`.

Note the surface difference: the **dashboard** `POST /api/workspaces` DOES accept
an absolute `dir` (screened by `is_sensitive_path`, so `~/.ssh` / `~/.aws` /
keystone paths are still refused). The CLI is the stricter of the two.

## Apps

Read `references/apps-and-artifacts.md` for the `kirocrew app` commands.

## Configuration

| Command | Description |
|---------|-------------|
| `kirocrew config get` | Show all config |
| `kirocrew config get agent.provider` | Get specific value (dot-separated key) |
| `kirocrew config set dashboard.url http://localhost:5476` | Set a config value (port is the KIROCREW_PORT env var, not a config key) |
| `kirocrew config set --file config.json` | Load full config from JSON file |
| `kirocrew config pending list` | Show each settings file changed outside Kiro Crew and held for review, and what changed (the default of `kirocrew config pending`; needs no dashboard) |
| `kirocrew config pending apply FILE SIGNATURE` | Keep the outside change (`FILE` is the file name, e.g. `config.json`; `SIGNATURE` the version `kirocrew config pending` printed) |
| `kirocrew config pending discard FILE SIGNATURE` | Put the saved version back |
| `kirocrew config edit` | Open config in $EDITOR |
| `kirocrew config defaults [KEYS…]` | Review stored values that still hold a superseded default |
| `kirocrew config defaults [KEYS…] --adopt` | Remove those stored keys so the current defaults apply |
| `kirocrew config defaults [KEYS…] --keep` | Record the stored values as intentional and stop reporting them |

## Profiling (debug-only)

Read `references/security-and-governance.md` for the profiling commands.

## Security & Eval

Read `references/security-and-governance.md` for the security, audit, secrets
and eval commands.

## Governance Policy

Read `references/security-and-governance.md` for the governance policy commands.

## Cloud (Bring-Your-Own AWS)

Read `references/cloud-network-desktop.md` for the `kirocrew cloud` commands.

## Tailnet (Tailscale)

Read `references/cloud-network-desktop.md` for the tailnet commands.

## Computer Use (Desktop Automation)

Read `references/cloud-network-desktop.md` for the computer-use commands.

## Snapshot & Restore

Read `references/snapshot-slack-env.md` for the snapshot and restore commands.

## Slack Commands

Read `references/snapshot-slack-env.md` for the Slack slash and channel commands.

## Environment Variables

Read `references/snapshot-slack-env.md` for the environment variables Kiro Crew reads.
