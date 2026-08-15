---
name: kirocrew-commands
description: Complete CLI reference for KiroCrew commands. Use for help, commands, setup, how to, what can you do, getting started, onboarding.
always: false
triggers: help, commands, setup, gateway, how to, what can you do, getting started, onboard, browse, auth, doctor, cron, artifact, memory, snapshot, eval, security, kirocrew pod, pod up, pod down, pod ls, pod status, pod logs, pod provision, pod install, pod token
---
# KiroCrew CLI Reference

## Setup & System

| Command | Description |
|---------|-------------|
| `kirocrew setup` | Interactive wizard — install agent config (messaging channels connect later) |
| `kirocrew setup --slack` | Also run the guided Slack credential setup (opt-in; ignored with `--agent-only`) |
| `kirocrew setup --agent-only` | Only install kiro-cli agent config, skip the other wizard steps |
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

## Pods (Isolated Worktree Test Instances)

Ephemeral, full-stack KiroCrew gateways — one per feature worktree — that run on
their own port + isolated `KIROCREW_HOME` and never touch the live `:5476`
gateway or shared data. Linux `systemd --user` only. `<wt>` is a worktree name
(resolved by directory basename or `feat/<name>` branch convention).

| Command | Description |
|---------|-------------|
| `kirocrew pod install` | Lay down the systemd --user template unit (once per machine) |
| `kirocrew pod provision <wt>` | Build the worktree's venv + SPA dist (the on-ramp) |
| `kirocrew pod up <wt>` | Bring up an isolated pod (auto-builds venv; fails if dist missing) |
| `kirocrew pod up <wt> --provision` | Provision (venv + dist build) then bring up |
| `kirocrew pod up <wt> --json` | Bring up and print `{base_url, token, port}` as JSON |
| `kirocrew pod ls` | List running pods |
| `kirocrew pod status <wt>` | Up/down + health for one pod |
| `kirocrew pod token <wt>` | (Re)mint a dashboard token for a running pod |
| `kirocrew pod url <wt>` | Print the pod's base URL |
| `kirocrew pod logs <wt> -n N` | Tail the pod's journal |
| `kirocrew pod down <wt>` | Evict the pod and delete its isolated HOME |
| `kirocrew pod exec <wt> -- <args>` | Run a kirocrew command against a pod, using the pod's own binary and data |

**Platform:** Linux only. On macOS/Windows every systemd-touching verb refuses
with a one-line message pointing at `./dev-backend.sh` — it does not crash, and
`pod install` writes no unit file. `pod url` works anywhere (pure computation).

Port derivation: `base + (cksum(name) % 199) + 1` (base `7810` → `7811..8009`).
Override with `PORT=` in `~/.kiro/crew/pods/<name>.env`.

See `src/kiro_crew/pod/README.md` for the full reference.

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

## Browsing (`playwright-cli`)

Browsing is not a `kirocrew` subcommand and not an MCP tool. You drive a browser by
running `playwright-cli` shell commands. It is available when the binary is on
`PATH`. **Settings → Browser** installs it with one click (and holds the optional
attach token); the equivalent by hand is `npm install -g @playwright/cli@latest`
(Node.js 20 or newer).

| Command | Description |
|---------|-------------|
| `playwright-cli open <url>` | Open a page (prints URL, title, and a snapshot path) |
| `playwright-cli snapshot` | Write the accessibility tree to a YAML file, print its path |
| `playwright-cli click <ref>` / `fill <ref> <text>` | Act on an element from a snapshot |
| `playwright-cli screenshot [ref]` | Write a PNG, print its path. `[ref]` is an ELEMENT, not a path; do not pass `--filename` (it resolves against the CWD and is not auto-approved) |
| `playwright-cli state-save` / `state-load <file>` | Save or restore a logged-in session. Bare `state-save` writes into the service's own directory; both a name and `state-load` prompt for approval, because each names a local path |
| `playwright-cli attach --extension` | Drive the user's own running Chrome, with their logins |
| `playwright-cli show --port <n> --host 127.0.0.1` | Serve the CLI's dashboard for the Browser panel |

**Browsing workflow:** load the `web-browse`, `web-verify`, or `browser-auth`
skill for the shape of the task, then:
1. `command -v playwright-cli`. Absent means browsing is unavailable: read the page
   with `web_fetch` and tell the user the install command.
2. `playwright-cli open <url>`. The printed URL and title usually confirm the page
   without reading anything else.
3. Read the snapshot YAML at the printed path only when you need the tree, for
   example before clicking. Refs like `[ref=e5]` belong to that snapshot, so
   re-snapshot after any page change.
4. On a login redirect, the session is absent or expired: `state-load` a saved
   session, or ask the user to sign in in the Browser panel and `state-save` it.

**No npm access (internal registry, air-gapped host):** detection is **PATH-based**
-- `playwright-cli` on `PATH` is all that matters, so ANY install route works and the
Settings button is a convenience, not the only one. In order of likelihood:

1. Most internal registries proxy npmjs, so the plain install already works.
2. Force the public registry for this one package:
   `npm install -g @playwright/cli --registry=https://registry.npmjs.org`.
3. **An internal registry that requires a login the user does not have** (the
   common Amazon-internal / corporate case). Install into a user-owned prefix
   against the public registry, ignoring the corporate `.npmrc` for this one
   command, then put the binary on `PATH`:

   ```bash
   NPM_CONFIG_USERCONFIG=/dev/null \
     npm install --prefix ~/.local/share/playwright-cli \
     --registry=https://registry.npmjs.org @playwright/cli@0.1.18
   mkdir -p ~/.local/bin
   ln -sf ~/.local/share/playwright-cli/node_modules/.bin/playwright-cli \
     ~/.local/bin/playwright-cli
   ```

   Two caveats worth stating to the user rather than burying: `~/.local/bin` has
   to be **on `PATH`** or Kiro Crew still reports "not installed" (detection is
   `PATH` + the Node bin dirs, nothing else); and `NPM_CONFIG_USERCONFIG=/dev/null`
   deliberately ignores their employer's registry configuration, which is their
   call to make, not ours to assume.
4. Air-gapped: `npm pack @playwright/cli` on a connected machine, copy the
   `.tgz` over, then `npm install -g ./playwright-cli-<version>.tgz`. Note the
   tarball alone is not runnable -- it needs its `playwright` /
   `playwright-core` dependencies resolved too.

What does **not** substitute for it: `pip install playwright` and
`dotnet tool install Microsoft.Playwright.CLI` install a DIFFERENT tool -- the
`playwright` browser-installer/codegen CLI, not `@playwright/cli` (binary
`playwright-cli`, its own 0.x line, which depends on `playwright@1.63.0-alpha`).
Switching to yarn, pnpm or bun hits the same registry, so it only helps when the
`npm` client itself is missing. And there is **no standalone binary**: the
upstream GitHub release carries no build assets and `playwright-cli.js` starts
with `#!/usr/bin/env node`, so Node.js 18+ is required no matter how it is
fetched.

**Approval:** page-scoped verbs run without prompting the user, because installing
the CLI is itself the consent. Verbs that reach the local machine still prompt on
purpose -- `eval`, `run-code`, `upload`, `state-load`, a named `state-save`, and the
installers. Let the user approve those rather than rewriting the command to dodge
the prompt.

The full verb list is in the skill `playwright-cli install --skills agents --global`
writes.

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
| `kirocrew cron preview SCRIPT` | Run a script cron locally with real MCP tools; notifications are printed, not delivered |
| `kirocrew cron preview SCRIPT -m "msg" -e K=V` | Preview with an input message / extra env vars |

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
| `kirocrew knowledge dedup` | Preview cross-source duplicate knowledge documents (dry-run) |
| `kirocrew knowledge dedup --apply` | Actually collapse the duplicates |
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

| Command | Description |
|---------|-------------|
| `kirocrew app list` | List installed apps |
| `kirocrew app install /path/to/app-dir` | Install app from local directory (needs app.json) |
| `kirocrew app enable NAME` | Enable an installed app |
| `kirocrew app disable NAME` | Disable an installed app |
| `kirocrew app uninstall NAME` | Uninstall an app and preserve its data directory |
| `kirocrew app uninstall NAME --purge-data` | Uninstall and explicitly delete the app data directory |
| `kirocrew app info NAME` | Show app details |
| `kirocrew app init NAME` | Scaffold a new app (kebab-case name) |
| `kirocrew app init NAME --backend --ui --cron` | Scaffold with backend, UI, and sample cron |
| `kirocrew app dev NAME` | Toggle an app into dev mode (no-store UI serving + live reload on file change) |
| `kirocrew app dev NAME --off` | Leave dev mode |

## Configuration

| Command | Description |
|---------|-------------|
| `kirocrew config get` | Show all config |
| `kirocrew config get agent.provider` | Get specific value (dot-separated key) |
| `kirocrew config set dashboard.url http://localhost:5476` | Set a config value (port is the KIROCREW_PORT env var, not a config key) |
| `kirocrew config set --file config.json` | Load full config from JSON file |
| `kirocrew config edit` | Open config in $EDITOR |

## Profiling (debug-only)

Off unless `KIROCREW_DEBUG=1` is set; the CLI is the only entry point. Emits folded
stacks (open in speedscope / flamegraph.pl). See `docs/architecture/design-notes/profiling.md`.

| Command | Description |
|---------|-------------|
| `KIROCREW_DEBUG=1 kirocrew perf sample --call mod:fn` | Profile that callable in-process (no extra dependency) |
| `KIROCREW_DEBUG=1 kirocrew perf sample` | Attach to the running gateway (needs `pip install "kirocrew[perf]"`) |
| `KIROCREW_DEBUG=1 kirocrew perf sample --pid 1234 --seconds 30` | Attach to a specific PID for N seconds (1-300) |
| `... --interval 0.002` | Seconds between samples (0.001-1.0, default 0.005) |
| `... --output /tmp/p.folded` | Where to write the profile (default `./kirocrew-profile.folded`) |
| `KIROCREW_DEBUG=1 kirocrew desktop metrics` | Per-process CPU/memory of the **Electron** app (`--json`, `--top N`, `--path`) |

On macOS the attach path additionally needs elevated privileges (the OS denies
`task_for_pid`), so it may require sudo; `--call` needs neither py-spy nor sudo.

`desktop metrics` reads a recording rather than querying the app: `getAppMetrics()`
is Electron-main-only, so the app samples itself into an artifact when **started**
with `KIROCREW_DEBUG` set. Setting the variable only for the CLI does not make an
already-running app record -- restart it.

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

## Governance Policy (read-only)

Inspects the two-level security model (`effective = POLICY ∩ PROFILE`,
tightest-wins). All four verbs are read-only — the enterprise ceiling is never
edited through the CLI (its files are keystone-fenced so the agent cannot read or
write them).

| Command | Description |
|---------|-------------|
| `kirocrew policy show` | Show the effective enterprise security policy |
| `kirocrew policy validate` | Load-check the policy + all profiles |
| `kirocrew policy explain SCOPE ITEM` | Explain one tool/scope decision for a surface |
| `kirocrew policy explain SCOPE ITEM --session-key K --agent A --app APP` | Scope the explanation to a surface |
| `kirocrew policy profile NAME` | Show a profile by name |

## Cloud (Bring-Your-Own AWS)

Runs KiroCrew on an EC2 instance in **your own** AWS account; credentials are
resolved by the `aws` CLI and never stored by KiroCrew. All verbs accept
`--profile` / `--region`; the single-instance verbs also accept `--tag`
(defaults to the last launched instance).

| Command | Description |
|---------|-------------|
| `kirocrew cloud doctor` | Check cloud prerequisites + AWS reachability |
| `kirocrew cloud launch` | Provision + configure an instance (interactive) |
| `kirocrew cloud launch --size TIER -y` | Non-interactive launch at a size tier |
| `kirocrew cloud launch --new` | Create a separate new instance instead of resuming the saved one |
| `kirocrew cloud launch --keep-on-failure` | On bootstrap failure keep the instance for inspection |
| `kirocrew cloud list` | List your KiroCrew cloud instances |
| `kirocrew cloud status` | Show one instance's state |
| `kirocrew cloud connect` | Open the dashboard over an SSM tunnel |
| `kirocrew cloud tunnel` | Open the dashboard SSM tunnel (standalone alias of connect) |
| `kirocrew cloud connect --local-port N --no-browser` | Forward to a specific local port, no browser |
| `kirocrew cloud login` | Sign kiro-cli in on the instance (fixes "not logged in" chat errors) |
| `kirocrew cloud stop` | Stop the instance (pause billing) |
| `kirocrew cloud start` | Start a stopped instance |
| `kirocrew cloud destroy` | Remove the instance and ALL its AWS resources |
| `kirocrew cloud destroy --dry-run` | Show the delete command without running it |
| `kirocrew cloud iam-policy` | Print the least-privilege IAM policy to apply |
| `kirocrew cloud iam-boundary` | Pre-create the immutable permissions boundary (admin, one-time) |

## Computer Use (Desktop Automation)

Default-OFF behind a keystone enable (`~/.kiro/crew/computer_use.json`, **not**
`config.json`). macOS only. These are human debug/diagnostic twins of the
`computer_*` MCP tools — the agent uses the MCP tools, not these.

| Command | Description |
|---------|-------------|
| `kirocrew computer doctor` | Report platform support, keystone enable state, and the advisory Accessibility / Screen Recording probe |
| `kirocrew computer doctor --json` | Same as JSON |
| `kirocrew computer apps` | List on-screen applications the accessibility layer can address |
| `kirocrew computer call TOOL k=v …` | Run ONE computer-use tool through the same gated chokepoint the agent uses |
| `kirocrew computer call --calls '[…]'` | Run a JSON array of calls in a SINGLE process, so `element_index` values stay resolvable |

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
| `KIROCREW_HOME` | Override config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
| `KIROCREW_POD_REPO` | Repo to resolve worktree names from | invoking cwd |
| `KIROCREW_POD_ROOT` | Isolated pod HOMEs (nuked on stop) | `~/.kirocrew-pods` |
| `KIROCREW_POD_BASE_PORT` | Port derivation base | `7810` |
| `KIROCREW_POD_LIVE_PORT` | Port a pod must never bind | `5476` |
