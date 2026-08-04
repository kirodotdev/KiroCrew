<p align="center">
  <img src="assets/banner.svg" alt="Kiro Crew. Keep work moving. Runs on your hardware, remembers across sessions, keeps working unattended.">
</p>

<h1 align="center">Kiro Crew</h1>

<p align="center">
  <strong>A persistent workspace for development work that self-improves and continues beyond one session.</strong>
</p>

<p align="center">
  Kiro Crew is an open source development workspace that runs locally or remotely on
  your hardware. It is persistent, self-learning, and self-evolving. Work with it
  from the desktop app, web dashboard, and CLI, or continue the same work through
  connection tools like Slack and Discord.
  Your multi-step tasks can run unattended, recurring jobs run on your schedule,
  and heartbeats monitor systems until something needs attention. Kiro Crew Apps
  tailor that experience to a specific job, combining a purpose-built interface
  with agents, skills, schedules, integrations, and backend services.
</p>

<p align="center">
  <a href="https://github.com/kirodotdev/KiroCrew/releases"><img src="https://img.shields.io/badge/Download-macOS%20%7C%20Linux-2f6feb?style=flat-square" alt="Download Kiro Crew for macOS or Linux"></a>
  <a href="docs/README.md"><img src="https://img.shields.io/badge/Documentation-1f6feb?style=flat-square" alt="Read the documentation"></a>
  <a href="docs/install.md"><img src="https://img.shields.io/badge/Install%20guide-macOS%20%7C%20Linux%20%7C%20Windows-6e7781?style=flat-square" alt="Install guide for macOS, Linux, and Windows"></a>
  <a href="CONTRIBUTING.md"><img src="https://img.shields.io/badge/Contributing-238636?style=flat-square" alt="Contributing guide"></a>
  <a href="SECURITY.md"><img src="https://img.shields.io/badge/Security-8250df?style=flat-square" alt="Security policy"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-656d76?style=flat-square" alt="Apache 2.0 license"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#build-from-source">Build from source</a> ·
  <a href="#why-kiro-crew">Why Kiro Crew</a> ·
  <a href="#what-kiro-crew-does">Capabilities</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#security-and-control">Security</a> ·
  <a href="#install-configure-and-operate">Install</a> ·
  <a href="#anonymous-usage-telemetry">Telemetry</a> ·
  <a href="#docs-and-contributing">Docs</a>
</p>

## Quick start

You choose how to run Kiro Crew: the desktop app with automatic updates, a
one-line install on your machine or a remote host, the Docker image for
always-on servers, or a build from source. Every path runs on `kiro-cli`
underneath, so the first launch installs it if needed and guides Kiro
device-code sign-in.

### App downloads

The desktop app starts a bundled Gateway when no local Gateway is already
running, updates itself on the channel you download, and can connect to a
remote Gateway over an SSH tunnel. See the
[desktop app guide](docs/desktop-app.md).

- **macOS**: [Stable](https://download.crew.kiro.dev/desktop/stable/latest/KiroCrew.dmg) | [Insider](https://download.crew.kiro.dev/desktop/insider/latest/KiroCrew.dmg) | [Nightly](https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg)
- **Linux**: [Stable](https://download.crew.kiro.dev/desktop/stable/latest/KiroCrew-x86_64.AppImage) | [Insider](https://download.crew.kiro.dev/desktop/insider/latest/KiroCrew-x86_64.AppImage) | [Nightly](https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew-x86_64.AppImage)
- **Windows**: no desktop build yet, so run the Gateway from a [source install](#build-from-source) and open the dashboard in your browser

### One-line install

Install the prebuilt, SHA-256-verified wheel from the release CDN without
cloning the repository or running `npm` and a local build.

Stable, the default:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
```

Track a faster channel, `insider` or `nightly`:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider
```

Pin an exact version:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --version 0.1.0
```

Open `http://localhost:5476` and start a conversation. The web dashboard works
without messaging credentials. Add [Slack](docs/slack-setup.md),
[Telegram](src/kiro_crew/docs/telegram-integration.md), or
[WeCom](src/kiro_crew/docs/wecom-integration.md) when you want to continue
working with the same agent away from the dashboard. These channels connect
outbound, so you do not need to expose the dashboard port publicly.

### Docker

For always-on servers, the Gateway ships as a public multi-arch image on GHCR:

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

See the [Docker guide](docs/docker.md) for first-run login, channel tags, and
the container security model.

### Build from source

macOS and Linux require Python 3.10+, Node.js 18+, npm, and
[`kiro-cli`](https://kiro.dev/docs/cli/). The first desktop or dashboard launch
can install Kiro CLI on the Gateway host and guide device-code sign-in before
chat opens. Windows is supported through a native source install; follow the
[Windows guide](docs/windows-install.md) instead of the shell steps below.

```bash
# 1. Clone and build Kiro Crew
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew
make build
source .venv/bin/activate

# 2. Configure, verify, and start
kirocrew setup
kirocrew doctor
kirocrew gateway
```

## Why Kiro Crew

Most agent sessions end when the chat closes. Kiro Crew runs continuously on
hardware you control and keeps working between conversations.

**Persistent.** Sessions, memory, schedules, and task checkpoints survive
Gateway restarts, and scheduled or reactive work continues without someone at
the terminal.

**Self-learning.** Corrections and task failures become durable lessons.
Preferences and project context carry into new sessions.

**Self-evolving.** Repeated patterns become reusable skills. Memory, lessons,
and skills stay visible and editable, so each Kiro Crew grows more tailored to
the person and work around it.

**Runs where you choose.** Your Mac, a local container, or a remote machine
you control.

**One Gateway, many surfaces.** Work directly in the desktop app or web dashboard,
or continue the same work from the CLI and messaging surfaces like Slack and
Discord.

## What Kiro Crew does

| Capability | What it gives you |
|---|---|
| **Persistent sessions** | Run concurrent, isolated conversations, resume them after Gateway restarts, search prior sessions, and carry recent context into new work. |
| **Self-learning** | Turn corrections and task failures into durable lessons that change later behavior. Keep preferences, active-project context, and history scoped to the relevant workspace. Say *"no, always run the frontend checks before calling a change done"* and it becomes a workspace-scoped lesson applied in future sessions. |
| **Self-evolving skills** | Synthesize reusable skills from repeated patterns, then inspect, refine, or remove them as your work changes. |
| **Long-running tasks** | Give Kiro Crew a task spec and walk away. It plans steps, executes them, validates results, retries failures, and resumes from checkpoints. *"Implement this migration plan and stop if the tests fail"* runs as a checkpointed task with validation at each step. |
| **Unattended autonomy** | Run scheduled agent work or deterministic scripts and commands without a model call. Monitor work until it is done, or react to messaging events and authenticated webhooks without someone at the terminal. *"Every weekday at 9, summarize the open work I should review"* becomes a timezone-aware recurring job delivered to the surface you choose. |
| **Delegation** | Spawn isolated subagents for parallel work and bring their results back into the parent conversation. *"Research these three options in parallel and recommend one"* fans out to isolated subagents and synthesizes the tradeoffs. |
| **Work where you choose** | Work directly in the desktop app or web dashboard, or continue through the CLI and any connected messaging surface without moving the agent runtime or its state. |
| **Installable apps** | Add focused interfaces and domain workflows through dashboard pages, scoped Gateway APIs, events, and lifecycle hooks. |
| **Extensible tools** | Add MCP servers, markdown skills, and hooks without changing the core runtime. |
| **Visible execution** | Watch tool calls, subagent progress, context usage, approvals, schedules, memory, and logs from the dashboard. |
| **Defense in depth** | Combine tool approvals, OS sandboxing, sensitive-path checks, credential redaction, deny rules, audit events, and governance profiles. |

You can also paste a screenshot and ask what is causing an error. Kiro Crew sends
the image to the active Kiro model and keeps the diagnosis in the conversation
history.

The complete inventory is in [Features](docs/features.md) and
[What's New](CHANGELOG.md).

## How it works

```mermaid
flowchart TD
    S["Desktop app · Web dashboard · Slack · Telegram · WeCom · CLI"]
    G["Gateway<br/>access · sessions · memory · schedules · approvals · apps"]
    A["Agent sessions<br/>ACP runtime · kiro-cli · MCP tools · models"]
    S --> G --> A
```

The Gateway separates where the agent runs from where you work with it. In the
desktop app or web dashboard, you can work directly through parallel conversations,
files, task runs, approvals, memory, and apps. From Slack, Telegram, WeCom, or
the CLI, the Gateway routes your work to managed agent sessions under the same
memory, tool, approval, and policy services. Apps extend the dashboard and
Gateway APIs with focused workflows.

Each active conversation or background task uses an agent session. Its session
provider drives `kiro-cli` over ACP, streams model and tool events, and preserves
conversation state. Depending on the workload, a session is backed by its own
ACP process or by a session handle on a shared multiplexed ACP runtime. The
Gateway manages these sessions along with scheduling, approvals, memory,
security policy, messaging connections, and the dashboard.

The current runtime places the Gateway, agent sessions, ACP processes, and state
on the same host. Run Kiro Crew on your Mac, inside a container on your machine,
or on a remote Linux host you control. Conversation history, memory, and
knowledge indexes remain on that host. Model requests are handled by `kiro-cli`
and follow the account and model configuration you use there.

**Gateway.** The Gateway is the long-running Kiro Crew process. It routes
messages from the desktop app, web, CLI, and the messaging surfaces listed below. It persists
session state, injects memory and skills, starts scheduled work, coordinates
subagents, brokers approvals, enforces runtime policy, and exposes activity in
the dashboard.

**Agent sessions.** A dashboard conversation or Slack thread maps to an isolated
agent session. Scheduled jobs, task runs, Telegram and WeCom conversations, and
subagents also use managed sessions. These sessions preserve conversation
context and can run concurrently before returning results to a parent session or
configured surface.

**ACP runtime and turns.** Kiro Crew supports both a dedicated `kiro-cli` ACP
process for a session and a shared ACP runtime that multiplexes multiple session
handles. During each turn, the session sends a prompt, streams model and tool
events, resolves approvals, and returns the final result. An agent session is a
logical isolation boundary, not necessarily one OS process.

**Use the surface that fits the moment.**

| Surface | Best for |
|---|---|
| **Desktop app** | The simplest local experience, with a bundled Gateway plus multi-tab connections to local or remote Gateways. |
| **Web dashboard** | Parallel conversations, files, approvals, activity, memory, schedules, apps, settings, and system status at `localhost:5476`. |
| **Slack** | Work from DMs and threads with streaming replies, approvals, notifications, and session links back to the dashboard. |
| **Telegram** | Reach your agent from private DMs on your phone or laptop, with streaming replies, inline approvals, and commands. |
| **Discord** | Work from DMs with streaming replies and approvals delivered as message buttons. |
| **Teams** | Reach your agent from Microsoft Teams chats with streaming replies and approvals. |
| **Webex** | Work from Webex direct messages with streaming replies and inline approvals. |
| **WeCom** | Chat through an outbound-connected WeCom AI bot with configured user access and streaming replies. |
| **WeChat (Weixin)** | Reach your agent from WeChat with configured user access and streaming replies. |
| **CLI** | Fast interactive chat and direct automation with `kirocrew chat`, `run`, `cron`, `spawn`, and `security`. |

**Choose how work starts.**

| Mode | Use it for | Entry point |
|---|---|---|
| **Scheduled** | Briefings, audits, backups, and recurring maintenance | `kirocrew cron` or a natural-language request |
| **Proactive** | Goals that need another pass without waiting for a new user message | AutoNudge and goal-loop skills |
| **Reactive** | CI alerts, external automation, Slack activity, and other events | Authenticated agent webhooks and messaging events |
| **Task runner** | Bounded projects with explicit steps, tests, review, and checkpoint resume | `kirocrew run TASK.md` |
| **Subagents** | Independent workstreams that can run concurrently | `kirocrew spawn run "task"` |

**Memory, learning, and evolution.** Kiro Crew maintains preferences, active
project context, decaying history summaries, and durable lessons. Corrections
and task failures can change later behavior, while repeated patterns can become
reusable skills. In-process embeddings add semantic retrieval for memory and
the knowledge library. The stored state remains inspectable and editable
from the dashboard. Incognito and temporary session modes let you opt out when
a conversation should not persist.

**Skills, MCP, and apps.** Markdown skills supply reusable workflows and can be
loaded only when relevant. The built-in `kirocrew-core` and `kirocrew-cron` MCP
servers expose task, subagent, learning, messaging, and scheduling tools. You
can discover additional MCP servers from Kiro or Kiro Crew configuration. The
App Kit adds installable interfaces and domain workflows. Apps can add dashboard
pages, use scoped Gateway APIs, subscribe to events, and register lifecycle
hooks.

## Security and control

Kiro Crew gives an AI agent real tool access, so the controls are enforced at
the runtime boundary instead of relying only on prompt instructions.

- **Local by default.** The dashboard binds to loopback unless you explicitly
  configure a network URL. Remote dashboards require token authentication.
- **Interactive approvals.** Review tool requests in the dashboard, Slack, or
  Telegram. Session-scoped trust can reduce repeated prompts without changing
  the underlying deny and sensitive-path controls.
- **OS sandbox.** On Linux and macOS, `kiro-cli` can run inside namespace or
  Seatbelt isolation. Standard, strict, and off modes make the tradeoff
  explicit. Windows does not currently provide this OS-level layer.
- **Sensitive data guards.** Kiro Crew blocks direct access to protected paths,
  strips sensitive environment variables, and redacts credential patterns from
  output before it reaches a chat surface.
- **Denied operations.** 137 bundled deny patterns block destructive commands and
  common exfiltration paths even when a session has broad approval.
- **Auditability.** Security events and tool activity are recorded for review.
  Use `kirocrew security events`, `audit`, and `verify` to inspect them.
- **Governance ceiling.** Optional policy and profile files compose with a
  tightest-wins model. A running app or agent can narrow the allowed scope but
  cannot loosen the enterprise ceiling. Inspect it with `kirocrew policy show`,
  `validate`, and `explain`.

No agent security layer removes the need to protect credentials and review
high-impact actions. Avoid pasting secrets or sensitive personal data into a
chat. Read the [security architecture](docs/security-deep-dive.md) and use
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Install, configure, and operate

**Installer details.** The installer resolves the channel feed, verifies the wheel's SHA-256 against
the published manifest, installs through `pipx` when available or a managed
virtual environment at `~/.kiro/crew/venv`, and records the channel in
`~/.kiro/crew/channel`. The channels are `stable`, `insider`, and `nightly`, and
`KIROCREW_CHANNEL` sets the default.

**Pin an exact wheel.** You can also install one exact wheel directly and pin it to its published
SHA-256. Every version directory publishes a `SHA256SUMS` file next to the
wheel, so take the hash for your wheel from there and put it in the URL
fragment. `pip` verifies the hash and does not consult a package index for
Kiro Crew itself:

```bash
pip install "https://download.crew.kiro.dev/cli/stable/<version>/kirocrew-<version>-py3-none-any.whl#sha256=<sha256>"
```

**Semantic memory.** Semantic memory needs no setup. Embeddings run in-process, and the Gateway
downloads its embedding model in the background on first start, verifies it,
and stores it under `~/.kiro/crew/models`. Until the model lands, memory search
falls back to keyword search and picks up embeddings automatically without a
restart. Set `KIROCREW_EMBED_MODEL_URL` to point at a mirror for airgapped
installs.

See [Installing and Building](docs/install.md) for wheels, desktop builds,
Windows, optional voice dependencies, and manual setup.

**Choose where Kiro Crew runs.** The current deployment model keeps the Gateway,
agent session runtime, ACP processes, and state together on one host. Your apps
and chat surfaces connect to that Gateway.

| Deployment | How to run it | Where Kiro Crew and its state live |
|---|---|---|
| **Mac app, local** | Install or build the desktop app with `make desktop` | The app starts its bundled Gateway. Agent sessions, ACP processes, and `~/.kiro/crew` stay on your Mac. |
| **Native local** | `make build`, or install a wheel from `make wheel` | The Gateway and agent runtime run directly on your macOS, Linux, or Windows machine. |
| **Local container** | Run `ghcr.io/kirodotdev/kirocrew` and persist `/home/kirocrew` | The Gateway and agent runtime run inside the official multi-arch container on your machine. |
| **Remote hardware** | Follow the [remote host guide](docs/remote-desktop-setup.md) and install the service | The Gateway, agent sessions, and state run continuously on your Linux server, home lab, or cloud instance. Connect the desktop app or browser through an SSH tunnel. |
| **Windows source install** | Follow [the Windows guide](docs/windows-install.md) | The Gateway, agent sessions, chat, cron, and dashboard run natively with documented feature limits. |

For containers, mount the directory selected by `KIROCREW_HOME` so sessions,
configuration, memory, and credentials survive replacement. Keep the Gateway
port bound to loopback unless you intentionally configure authenticated remote
access. Container isolation and the Kiro Crew OS sandbox are separate layers
and depend on the host runtime configuration. See the
[Docker guide](docs/docker.md) for the published image and deployment details.

**Keep it running.** Install a systemd service on Linux or a launchd agent on
macOS:

```bash
kirocrew service install
kirocrew service status
kirocrew logs
```

The desktop app can use this local Gateway or connect to a remote one. For an
always-on VPS, home server, or cloud VM in your account, follow the
[remote host guide](docs/remote-desktop-setup.md). Kiro Crew does not require a
Kiro Crew-hosted control plane.

**Configure it.** User data lives under `~/.kiro/crew` by default. Manage the
main configuration with `kirocrew config get`, `set`, and `edit`.

```json
{
  "agent": {
    "provider": "acp",
    "approval_mode": "interactive",
    "sandbox": "auto"
  },
  "session": {
    "timeout_secs": 1800,
    "pool_size": 2
  },
  "dashboard": {
    "bot_name": "Kiro Crew"
  }
}
```

`agent.provider` is fixed to `acp`. Kiro Crew drives `kiro-cli` over the Agent
Client Protocol. Set the dashboard port with `KIROCREW_PORT` or
`kirocrew gateway --port <n>`. Slack credentials live in `~/.kiro/crew/.env`
rather than the JSON config.

**Troubleshoot quickly.** Start with `kirocrew doctor`. For an ACP timeout,
confirm `kiro-cli` is on `PATH` and logged in, then allow extra time for the
first MCP startup. For memory search, check that the embedding
model finished downloading under `~/.kiro/crew/models`. For a stale MCP configuration, run
`kirocrew setup --agent-only`, or add `--clean` to rebuild it.

## Anonymous usage telemetry

Kiro Crew sends **one anonymous heartbeat per day** so maintainers can see how
many copies are actively running, which versions are in use, and which
platforms and install channels to support. After a successful install or update
from the official app catalog, it also sends one anonymous per-app receipt.
Both signals are on by default and use the same controls below.

To turn it off, flip **Settings → Privacy → Send anonymous usage heartbeat** in
the dashboard (the same switch appears on the last step of first-run
onboarding). Or from a terminal:

```bash
kirocrew telemetry disable        # persists to config.json
export KIROCREW_TELEMETRY_DISABLED=1   # or per-shell / per-container
kirocrew telemetry status         # print exactly what would be sent
```

The toggle and `kirocrew telemetry disable` write the same setting, so either
one sticks across restarts and upgrades. `KIROCREW_TELEMETRY_DISABLED` overrides
both — when it is set, the dashboard toggle is disabled and says so.

**Exactly these five fields are sent, at most once per day, and nothing else:**

| Field | Example | Why |
|-------|---------|-----|
| Random instance id | `9c75560d…` (UUID4) | Lets us count how many copies ran on a given day. Generated once on first run and derived from nothing — not your hostname, username, MAC, IP, or any account. It identifies an installed copy, never a person. |
| App version | `0.1.2` | Which releases are still in use. **Release number only** — build stamps like `-nightly.20260731t065756` are stripped before sending, because a per-build timestamp is near-unique and would help identify a specific machine. |
| Python minor version | `3.12` | When the minimum can move up |
| Install channel | `dmg` | Which install path people actually use |
| First-run flag | `1` / `0` | New installs vs returning |

**Official-app install receipts are separate and event-based.** After a
successful official-catalog install or update, Kiro Crew sends one GET to
`/b/1/install/<app-slug>?t=<token>&k=<fresh|update>&v=<release>` on the same
telemetry host. The slug is the public catalog identifier. `t` is the first 32
hex characters of HMAC-SHA256 keyed by the local beacon install id over
`app-install:<slug>`; the raw install id is never sent, and tokens for different
apps cannot be linked to assemble an installed-app profile. `k` separates fresh
installs from updates, and `v` is the same release-only Kiro Crew version clamp
used by the heartbeat.

Receipts are emitted only for bundled or edition-provided official catalog
entries. Apps from user-configured registries, local-directory installs, and
self-registered apps emit nothing, so private app names never leave the machine.
If no persistent beacon install id exists yet, the receipt is skipped.

This list used to be nine fields. Release channel, OS, CPU architecture and
governance posture were **removed** — each was coarse on its own, but the
instance id is stable, so those attributes all describe the *same* copy and
together they narrowed the group any one install blends into far more than any
single field suggests.

We report this as **Daily Active Instances** rather than "users": with no account
system there is no way to resolve a copy to a person, so one person running
Kiro Crew on three machines counts as three.

**Never sent:** your prompts, model responses, file contents, file paths, repo
or branch names, credentials, environment variables, hostname, username, or IP
address. The receiving CDN is configured **not to log client IP addresses** — the
log delivery does not include that field, so no IP is stored at all.

**Automatically off** in CI, and whenever `KIROCREW_HOME` points somewhere other
than `~/.kiro/crew` (dev instances and pods are never counted).

**Enterprise administrators can pin it off entirely.** A `capabilities.telemetry`
entry in the security policy blocks both outbound signals regardless of the local
setting, and the dashboard toggle then says so instead of offering a change that
would not take effect:

```json
{"version": 1, "boot": {"fail_closed": true},
 "capabilities": {"telemetry": {"enabled": false}}}
```

See [docs/system-specs/modules/governance.md](docs/system-specs/modules/governance.md).

This is separate from `telemetry.enabled`, which controls **local-only**
performance metrics that never leave your machine. See
[docs/system-specs/modules/metrics.md](docs/system-specs/modules/metrics.md).

## Docs and contributing

| Topic | Start here |
|---|---|
| Install and packaging | [Getting started](docs/getting-started.md), [Installing and Building](docs/install.md), [Windows](docs/windows-install.md), [Desktop](docs/desktop-app.md), [Remote host](docs/remote-desktop-setup.md), [Release process](docs/release-process.md) |
| Product capabilities | [Features](docs/features.md), [Skills](skills/README.md) |
| Channels | [Slack](docs/slack-setup.md), [Discord](src/kiro_crew/docs/discord-integration.md), [Telegram](src/kiro_crew/docs/telegram-integration.md), [Teams](src/kiro_crew/docs/teams-integration.md), [Webex](src/kiro_crew/docs/webex-integration.md), [WeCom](src/kiro_crew/docs/wecom-integration.md), [WeChat (Weixin)](src/kiro_crew/docs/weixin-integration.md) |
| Architecture | [System architecture](docs/project-architecture.md), [Memory](docs/memory-architecture.md), [MCP](docs/mcp-architecture.md), [App Kit](docs/app-kit/getting-started.md) |
| Trust and dependencies | [Security](docs/security-deep-dive.md), [Security policy](SECURITY.md) |
| Project work | [Contributing](CONTRIBUTING.md), [Tenets](TENETS.md), [Governance](GOVERNANCE.md), [Maintainers](MAINTAINERS.md), [AI assistant rules](AGENTS.md), [Changelog](CHANGELOG.md) |

Contributions are welcome. Create a branch from `main`, keep changes focused,
and run the relevant checks before opening a pull request:

```bash
# Backend
pip install -e ".[voice]" --group dev
pytest

# Frontend
cd website
npm ci
npm run check
npm run build
```

Use [GitHub Issues](https://github.com/kirodotdev/KiroCrew/issues) for bugs and
feature requests. Do not file security vulnerabilities publicly.


## Contributors

KiroCrew was made possible by its internal community, the people who supported the
project and shipped its code, together with everyone who has since opened a pull
request in the open. This is that founding group; as KiroCrew grows in the open, we
look forward to many more contributors joining them. Thank you to everyone who helped
make this tool possible:

<a href="https://github.com/0618" title="MJ Zhang"><img src="https://github.com/0618.png?size=64" width="64" height="64" alt="MJ Zhang" /></a>
<a href="https://github.com/0V" title="G2"><img src="https://github.com/0V.png?size=64" width="64" height="64" alt="G2" /></a>
<a href="https://github.com/aahei" title="Ahei"><img src="https://github.com/aahei.png?size=64" width="64" height="64" alt="Ahei" /></a>
<a href="https://github.com/abe238" title="Abe Diaz (@abe238)"><img src="https://github.com/abe238.png?size=64" width="64" height="64" alt="Abe Diaz (@abe238)" /></a>
<a href="https://github.com/abhishekdhameja" title="Abhishek Dhameja"><img src="https://github.com/abhishekdhameja.png?size=64" width="64" height="64" alt="Abhishek Dhameja" /></a>
<a href="https://github.com/Abhishekmitra-slg" title="Abhishek Mitra"><img src="https://github.com/Abhishekmitra-slg.png?size=64" width="64" height="64" alt="Abhishek Mitra" /></a>
<a href="https://github.com/acdoussan" title="acdoussan"><img src="https://github.com/acdoussan.png?size=64" width="64" height="64" alt="acdoussan" /></a>
<a href="https://github.com/adam-dunc" title="Adam Duncan"><img src="https://github.com/adam-dunc.png?size=64" width="64" height="64" alt="Adam Duncan" /></a>
<a href="https://github.com/AddisonTustin" title="AddisonTustin"><img src="https://github.com/AddisonTustin.png?size=64" width="64" height="64" alt="AddisonTustin" /></a>
<a href="https://github.com/adunuthulan" title="Nirav Adunuthula"><img src="https://github.com/adunuthulan.png?size=64" width="64" height="64" alt="Nirav Adunuthula" /></a>
<a href="https://github.com/Aiden-Gaines" title="Aiden Gaines"><img src="https://github.com/Aiden-Gaines.png?size=64" width="64" height="64" alt="Aiden Gaines" /></a>
<a href="https://github.com/akhjones" title="Alexander Jones"><img src="https://github.com/akhjones.png?size=64" width="64" height="64" alt="Alexander Jones" /></a>
<a href="https://github.com/akshitdesai" title="Akshit Desai"><img src="https://github.com/akshitdesai.png?size=64" width="64" height="64" alt="Akshit Desai" /></a>
<a href="https://github.com/Albisourous" title="Albin Shrestha"><img src="https://github.com/Albisourous.png?size=64" width="64" height="64" alt="Albin Shrestha" /></a>
<a href="https://github.com/alecgdouglas" title="Alec Douglas"><img src="https://github.com/alecgdouglas.png?size=64" width="64" height="64" alt="Alec Douglas" /></a>
<a href="https://github.com/AlexShen101" title="Alex Shen"><img src="https://github.com/AlexShen101.png?size=64" width="64" height="64" alt="Alex Shen" /></a>
<a href="https://github.com/amadsalmon" title="Amad Salmon"><img src="https://github.com/amadsalmon.png?size=64" width="64" height="64" alt="Amad Salmon" /></a>
<a href="https://github.com/amulya349" title="Amulya Kumar Sahoo"><img src="https://github.com/amulya349.png?size=64" width="64" height="64" alt="Amulya Kumar Sahoo" /></a>
<a href="https://github.com/anant-kaushik" title="Anant Kaushik"><img src="https://github.com/anant-kaushik.png?size=64" width="64" height="64" alt="Anant Kaushik" /></a>
<a href="https://github.com/andrewtakeshi" title="Andrew Golightly"><img src="https://github.com/andrewtakeshi.png?size=64" width="64" height="64" alt="Andrew Golightly" /></a>
<a href="https://github.com/angeloyu" title="angeloyu"><img src="https://github.com/angeloyu.png?size=64" width="64" height="64" alt="angeloyu" /></a>
<a href="https://github.com/anjn98" title="anjn98"><img src="https://github.com/anjn98.png?size=64" width="64" height="64" alt="anjn98" /></a>
<a href="https://github.com/anmolsaxena10" title="Anmol Saxena"><img src="https://github.com/anmolsaxena10.png?size=64" width="64" height="64" alt="Anmol Saxena" /></a>
<a href="https://github.com/Anthony-dominianni" title="Anthony-dominianni"><img src="https://github.com/Anthony-dominianni.png?size=64" width="64" height="64" alt="Anthony-dominianni" /></a>
<a href="https://github.com/Anurag461" title="Anurag Kashyap"><img src="https://github.com/Anurag461.png?size=64" width="64" height="64" alt="Anurag Kashyap" /></a>
<a href="https://github.com/apoorv06s" title="apoorv06s"><img src="https://github.com/apoorv06s.png?size=64" width="64" height="64" alt="apoorv06s" /></a>
<a href="https://github.com/aqiaojoe08" title="aqiaojoe08"><img src="https://github.com/aqiaojoe08.png?size=64" width="64" height="64" alt="aqiaojoe08" /></a>
<a href="https://github.com/aravance" title="Alex Avance"><img src="https://github.com/aravance.png?size=64" width="64" height="64" alt="Alex Avance" /></a>
<a href="https://github.com/architect4dj" title="architect4dj"><img src="https://github.com/architect4dj.png?size=64" width="64" height="64" alt="architect4dj" /></a>
<a href="https://github.com/arjunsoota" title="Arjun Soota"><img src="https://github.com/arjunsoota.png?size=64" width="64" height="64" alt="Arjun Soota" /></a>
<a href="https://github.com/arpan98" title="Arpan Banerjee"><img src="https://github.com/arpan98.png?size=64" width="64" height="64" alt="Arpan Banerjee" /></a>
<a href="https://github.com/arvindsrinathus-tech" title="arvindsrinathus-tech"><img src="https://github.com/arvindsrinathus-tech.png?size=64" width="64" height="64" alt="arvindsrinathus-tech" /></a>
<a href="https://github.com/AryPathania" title="Ary Pathania"><img src="https://github.com/AryPathania.png?size=64" width="64" height="64" alt="Ary Pathania" /></a>
<a href="https://github.com/asaifuddin18" title="Aziz Saifuddin"><img src="https://github.com/asaifuddin18.png?size=64" width="64" height="64" alt="Aziz Saifuddin" /></a>
<a href="https://github.com/ashtnemi448" title="ashtnemi448"><img src="https://github.com/ashtnemi448.png?size=64" width="64" height="64" alt="ashtnemi448" /></a>
<a href="https://github.com/aswindjs" title="Aswin Damodar"><img src="https://github.com/aswindjs.png?size=64" width="64" height="64" alt="Aswin Damodar" /></a>
<a href="https://github.com/av-writes-code" title="av-writes-code"><img src="https://github.com/av-writes-code.png?size=64" width="64" height="64" alt="av-writes-code" /></a>
<a href="https://github.com/avmikhli1" title="avmikhli1"><img src="https://github.com/avmikhli1.png?size=64" width="64" height="64" alt="avmikhli1" /></a>
<a href="https://github.com/beau-bright" title="beau-bright"><img src="https://github.com/beau-bright.png?size=64" width="64" height="64" alt="beau-bright" /></a>
<a href="https://github.com/berylqliu1122" title="berylqliu1122"><img src="https://github.com/berylqliu1122.png?size=64" width="64" height="64" alt="berylqliu1122" /></a>
<a href="https://github.com/bgrubin-amzn" title="bgrubin-amzn"><img src="https://github.com/bgrubin-amzn.png?size=64" width="64" height="64" alt="bgrubin-amzn" /></a>
<a href="https://github.com/bhargav5000" title="bhargav5000"><img src="https://github.com/bhargav5000.png?size=64" width="64" height="64" alt="bhargav5000" /></a>
<a href="https://github.com/bigchkn" title="bigchkn"><img src="https://github.com/bigchkn.png?size=64" width="64" height="64" alt="bigchkn" /></a>
<a href="https://github.com/bkarson" title="bkarson"><img src="https://github.com/bkarson.png?size=64" width="64" height="64" alt="bkarson" /></a>
<a href="https://github.com/BlumenthalJD" title="Joel Blumenthal"><img src="https://github.com/BlumenthalJD.png?size=64" width="64" height="64" alt="Joel Blumenthal" /></a>
<a href="https://github.com/bobbyearl" title="Bobby Earl"><img src="https://github.com/bobbyearl.png?size=64" width="64" height="64" alt="Bobby Earl" /></a>
<a href="https://github.com/bolichen97" title="Bolin Chen"><img src="https://github.com/bolichen97.png?size=64" width="64" height="64" alt="Bolin Chen" /></a>
<a href="https://github.com/brantai" title="Brent Naylor"><img src="https://github.com/brantai.png?size=64" width="64" height="64" alt="Brent Naylor" /></a>
<a href="https://github.com/buluoray" title="Ray Xu"><img src="https://github.com/buluoray.png?size=64" width="64" height="64" alt="Ray Xu" /></a>
<a href="https://github.com/caribbeansteve" title="George Coll"><img src="https://github.com/caribbeansteve.png?size=64" width="64" height="64" alt="George Coll" /></a>
<a href="https://github.com/carttrp" title="carttrp"><img src="https://github.com/carttrp.png?size=64" width="64" height="64" alt="carttrp" /></a>
<a href="https://github.com/cathar" title="cathar"><img src="https://github.com/cathar.png?size=64" width="64" height="64" alt="cathar" /></a>
<a href="https://github.com/chancepants" title="Chance"><img src="https://github.com/chancepants.png?size=64" width="64" height="64" alt="Chance" /></a>
<a href="https://github.com/ChaonengQuan" title="ChaonengQuan"><img src="https://github.com/ChaonengQuan.png?size=64" width="64" height="64" alt="ChaonengQuan" /></a>
<a href="https://github.com/chenmingwei23" title="Raymond Chen"><img src="https://github.com/chenmingwei23.png?size=64" width="64" height="64" alt="Raymond Chen" /></a>
<a href="https://github.com/chenyjade" title="Yu Cheng"><img src="https://github.com/chenyjade.png?size=64" width="64" height="64" alt="Yu Cheng" /></a>
<a href="https://github.com/chrispaton" title="Chris Paton"><img src="https://github.com/chrispaton.png?size=64" width="64" height="64" alt="Chris Paton" /></a>
<a href="https://github.com/cixuuz" title="cixuuuuuuuuz"><img src="https://github.com/cixuuz.png?size=64" width="64" height="64" alt="cixuuuuuuuuz" /></a>
<a href="https://github.com/cohilla" title="Cody Hill"><img src="https://github.com/cohilla.png?size=64" width="64" height="64" alt="Cody Hill" /></a>
<a href="https://github.com/colewhitley" title="Cole Whitley"><img src="https://github.com/colewhitley.png?size=64" width="64" height="64" alt="Cole Whitley" /></a>
<a href="https://github.com/ConnorLoP" title="Connor LoPresti"><img src="https://github.com/ConnorLoP.png?size=64" width="64" height="64" alt="Connor LoPresti" /></a>
<a href="https://github.com/ConstantineWang" title="Jiacheng Wang"><img src="https://github.com/ConstantineWang.png?size=64" width="64" height="64" alt="Jiacheng Wang" /></a>
<a href="https://github.com/cruisercohen" title="Matt Cohen"><img src="https://github.com/cruisercohen.png?size=64" width="64" height="64" alt="Matt Cohen" /></a>
<a href="https://github.com/CrysisDeu" title="Zezhen Xu"><img src="https://github.com/CrysisDeu.png?size=64" width="64" height="64" alt="Zezhen Xu" /></a>
<a href="https://github.com/Csan25" title="Csan25"><img src="https://github.com/Csan25.png?size=64" width="64" height="64" alt="Csan25" /></a>
<a href="https://github.com/ctyndall" title="ctyndall"><img src="https://github.com/ctyndall.png?size=64" width="64" height="64" alt="ctyndall" /></a>
<a href="https://github.com/dagayev1" title="Dagadansbot"><img src="https://github.com/dagayev1.png?size=64" width="64" height="64" alt="Dagadansbot" /></a>
<a href="https://github.com/darko-mesaros" title="Darko Mesaros"><img src="https://github.com/darko-mesaros.png?size=64" width="64" height="64" alt="Darko Mesaros" /></a>
<a href="https://github.com/davidtlee-amzn" title="davidtlee-amzn"><img src="https://github.com/davidtlee-amzn.png?size=64" width="64" height="64" alt="davidtlee-amzn" /></a>
<a href="https://github.com/derrick0714" title="Xu Deng"><img src="https://github.com/derrick0714.png?size=64" width="64" height="64" alt="Xu Deng" /></a>
<a href="https://github.com/desaip05" title="Parikshit Desai"><img src="https://github.com/desaip05.png?size=64" width="64" height="64" alt="Parikshit Desai" /></a>
<a href="https://github.com/DFayerman" title="DFayerman"><img src="https://github.com/DFayerman.png?size=64" width="64" height="64" alt="DFayerman" /></a>
<a href="https://github.com/dgomesbr" title="Diego Magalhães"><img src="https://github.com/dgomesbr.png?size=64" width="64" height="64" alt="Diego Magalhães" /></a>
<a href="https://github.com/Dhaivat717" title="Dhaivat Patel"><img src="https://github.com/Dhaivat717.png?size=64" width="64" height="64" alt="Dhaivat Patel" /></a>
<a href="https://github.com/doc88129" title="Doc"><img src="https://github.com/doc88129.png?size=64" width="64" height="64" alt="Doc" /></a>
<a href="https://github.com/dougclauson" title="dougclauson"><img src="https://github.com/dougclauson.png?size=64" width="64" height="64" alt="dougclauson" /></a>
<a href="https://github.com/dsm0709" title="Siming Deng"><img src="https://github.com/dsm0709.png?size=64" width="64" height="64" alt="Siming Deng" /></a>
<a href="https://github.com/dwu96" title="Di Wu"><img src="https://github.com/dwu96.png?size=64" width="64" height="64" alt="Di Wu" /></a>
<a href="https://github.com/echorubisco" title="echorubisco"><img src="https://github.com/echorubisco.png?size=64" width="64" height="64" alt="echorubisco" /></a>
<a href="https://github.com/EllaRed" title="Emmanuella Dasilva-Domingos"><img src="https://github.com/EllaRed.png?size=64" width="64" height="64" alt="Emmanuella Dasilva-Domingos" /></a>
<a href="https://github.com/em-sec" title="Eric M"><img src="https://github.com/em-sec.png?size=64" width="64" height="64" alt="Eric M" /></a>
<a href="https://github.com/Eng-Ahmd" title="Ahmed Hassanin"><img src="https://github.com/Eng-Ahmd.png?size=64" width="64" height="64" alt="Ahmed Hassanin" /></a>
<a href="https://github.com/envyN" title="Naveen Adarsh"><img src="https://github.com/envyN.png?size=64" width="64" height="64" alt="Naveen Adarsh" /></a>
<a href="https://github.com/erichays" title="Eric Hays"><img src="https://github.com/erichays.png?size=64" width="64" height="64" alt="Eric Hays" /></a>
<a href="https://github.com/erikbomb" title="Erik Schweiss"><img src="https://github.com/erikbomb.png?size=64" width="64" height="64" alt="Erik Schweiss" /></a>
<a href="https://github.com/estenger" title="Evan Stenger"><img src="https://github.com/estenger.png?size=64" width="64" height="64" alt="Evan Stenger" /></a>
<a href="https://github.com/EzzatQ" title="Ezzat Qupty"><img src="https://github.com/EzzatQ.png?size=64" width="64" height="64" alt="Ezzat Qupty" /></a>
<a href="https://github.com/felipeb" title="Felipe"><img src="https://github.com/felipeb.png?size=64" width="64" height="64" alt="Felipe" /></a>
<a href="https://github.com/filipgodina" title="filipgodina"><img src="https://github.com/filipgodina.png?size=64" width="64" height="64" alt="filipgodina" /></a>
<a href="https://github.com/finnhad" title="Finn H"><img src="https://github.com/finnhad.png?size=64" width="64" height="64" alt="Finn H" /></a>
<a href="https://github.com/FlameFrost" title="FlameFrost"><img src="https://github.com/FlameFrost.png?size=64" width="64" height="64" alt="FlameFrost" /></a>
<a href="https://github.com/FlowTable0" title="FlowTable0"><img src="https://github.com/FlowTable0.png?size=64" width="64" height="64" alt="FlowTable0" /></a>
<a href="https://github.com/fo2rist" title="Dmitry Sitnikov"><img src="https://github.com/fo2rist.png?size=64" width="64" height="64" alt="Dmitry Sitnikov" /></a>
<a href="https://github.com/Garnethil" title="Gabriel Sanchez"><img src="https://github.com/Garnethil.png?size=64" width="64" height="64" alt="Gabriel Sanchez" /></a>
<a href="https://github.com/geetsawhney" title="geet sawhney"><img src="https://github.com/geetsawhney.png?size=64" width="64" height="64" alt="geet sawhney" /></a>
<a href="https://github.com/gmealy1" title="Gavin Mealy"><img src="https://github.com/gmealy1.png?size=64" width="64" height="64" alt="Gavin Mealy" /></a>
<a href="https://github.com/Godivamasterpiece" title="Vivek Teja Sayyaparaju"><img src="https://github.com/Godivamasterpiece.png?size=64" width="64" height="64" alt="Vivek Teja Sayyaparaju" /></a> <!-- wokeignore:rule=master -->
<a href="https://github.com/GouthamHM" title="Goutham"><img src="https://github.com/GouthamHM.png?size=64" width="64" height="64" alt="Goutham" /></a>
<a href="https://github.com/gragollier" title="Grant Gollier"><img src="https://github.com/gragollier.png?size=64" width="64" height="64" alt="Grant Gollier" /></a>
<a href="https://github.com/greatfighter" title="Spencer"><img src="https://github.com/greatfighter.png?size=64" width="64" height="64" alt="Spencer" /></a>
<a href="https://github.com/gregory-chapman" title="Gregory Chapman"><img src="https://github.com/gregory-chapman.png?size=64" width="64" height="64" alt="Gregory Chapman" /></a>
<a href="https://github.com/haozihong" title="haozihong"><img src="https://github.com/haozihong.png?size=64" width="64" height="64" alt="haozihong" /></a>
<a href="https://github.com/hchy0422" title="Kathy Han"><img src="https://github.com/hchy0422.png?size=64" width="64" height="64" alt="Kathy Han" /></a>
<a href="https://github.com/helenastafford" title="helenastafford"><img src="https://github.com/helenastafford.png?size=64" width="64" height="64" alt="helenastafford" /></a>
<a href="https://github.com/hhllii" title="hhllii"><img src="https://github.com/hhllii.png?size=64" width="64" height="64" alt="hhllii" /></a>
<a href="https://github.com/hoang-phan98" title="Hoang "><img src="https://github.com/hoang-phan98.png?size=64" width="64" height="64" alt="Hoang " /></a>
<a href="https://github.com/hugoncosta" title="Hugo Costa"><img src="https://github.com/hugoncosta.png?size=64" width="64" height="64" alt="Hugo Costa" /></a>
<a href="https://github.com/hungtnvu" title="Hung Vu"><img src="https://github.com/hungtnvu.png?size=64" width="64" height="64" alt="Hung Vu" /></a>
<a href="https://github.com/iamwhatever" title="Zejiang Guo (Joe)"><img src="https://github.com/iamwhatever.png?size=64" width="64" height="64" alt="Zejiang Guo (Joe)" /></a>
<a href="https://github.com/inaoy" title="inaoy"><img src="https://github.com/inaoy.png?size=64" width="64" height="64" alt="inaoy" /></a>
<a href="https://github.com/IngridMorstrad" title="IngridMorstrad"><img src="https://github.com/IngridMorstrad.png?size=64" width="64" height="64" alt="IngridMorstrad" /></a>
<a href="https://github.com/ishansmishra" title="Ishan Mishra"><img src="https://github.com/ishansmishra.png?size=64" width="64" height="64" alt="Ishan Mishra" /></a>
<a href="https://github.com/j20120307" title="j20120307"><img src="https://github.com/j20120307.png?size=64" width="64" height="64" alt="j20120307" /></a>
<a href="https://github.com/JainSid96" title="Siddhant Jain"><img src="https://github.com/JainSid96.png?size=64" width="64" height="64" alt="Siddhant Jain" /></a>
<a href="https://github.com/jakeg0615" title="jakeg0615"><img src="https://github.com/jakeg0615.png?size=64" width="64" height="64" alt="jakeg0615" /></a>
<a href="https://github.com/jakeinater" title="Jake Zhao"><img src="https://github.com/jakeinater.png?size=64" width="64" height="64" alt="Jake Zhao" /></a>
<a href="https://github.com/jakenoc" title="Jacob Nocentino"><img src="https://github.com/jakenoc.png?size=64" width="64" height="64" alt="Jacob Nocentino" /></a>
<a href="https://github.com/JasonZhang1993" title="Jason Zhang's Git"><img src="https://github.com/JasonZhang1993.png?size=64" width="64" height="64" alt="Jason Zhang's Git" /></a>
<a href="https://github.com/jayaprakashreddy007" title="jayaprakashreddy007"><img src="https://github.com/jayaprakashreddy007.png?size=64" width="64" height="64" alt="jayaprakashreddy007" /></a>
<a href="https://github.com/jbandon" title="Jack Bandon"><img src="https://github.com/jbandon.png?size=64" width="64" height="64" alt="Jack Bandon" /></a>
<a href="https://github.com/jeffn12" title="Jeff Neuberger"><img src="https://github.com/jeffn12.png?size=64" width="64" height="64" alt="Jeff Neuberger" /></a>
<a href="https://github.com/jianwenl" title="jianwenl"><img src="https://github.com/jianwenl.png?size=64" width="64" height="64" alt="jianwenl" /></a>
<a href="https://github.com/jkasiraj" title="jkasiraj"><img src="https://github.com/jkasiraj.png?size=64" width="64" height="64" alt="jkasiraj" /></a>
<a href="https://github.com/jodoyodo" title="David Qian"><img src="https://github.com/jodoyodo.png?size=64" width="64" height="64" alt="David Qian" /></a>
<a href="https://github.com/JohnEspenhahn" title="John Espenhahn"><img src="https://github.com/JohnEspenhahn.png?size=64" width="64" height="64" alt="John Espenhahn" /></a>
<a href="https://github.com/johnnymastin" title="Johnny Mastin"><img src="https://github.com/johnnymastin.png?size=64" width="64" height="64" alt="Johnny Mastin" /></a>
<a href="https://github.com/johnnynaught" title="johnnynaught"><img src="https://github.com/johnnynaught.png?size=64" width="64" height="64" alt="johnnynaught" /></a>
<a href="https://github.com/Jpontone" title="JPontone"><img src="https://github.com/Jpontone.png?size=64" width="64" height="64" alt="JPontone" /></a>
<a href="https://github.com/Jus973" title="Justin Z"><img src="https://github.com/Jus973.png?size=64" width="64" height="64" alt="Justin Z" /></a>
<a href="https://github.com/jyuros" title="Jaden Yuros"><img src="https://github.com/jyuros.png?size=64" width="64" height="64" alt="Jaden Yuros" /></a>
<a href="https://github.com/KaiqueGovani" title="Kaique Govani"><img src="https://github.com/KaiqueGovani.png?size=64" width="64" height="64" alt="Kaique Govani" /></a>
<a href="https://github.com/kaizawa97" title="Kai Mitsuzawa"><img src="https://github.com/kaizawa97.png?size=64" width="64" height="64" alt="Kai Mitsuzawa" /></a>
<a href="https://github.com/kesh97-hub" title="kesh97-hub"><img src="https://github.com/kesh97-hub.png?size=64" width="64" height="64" alt="kesh97-hub" /></a>
<a href="https://github.com/keyejia" title="Kellen Jia"><img src="https://github.com/keyejia.png?size=64" width="64" height="64" alt="Kellen Jia" /></a>
<a href="https://github.com/kiavashsamadi" title="Kiavash"><img src="https://github.com/kiavashsamadi.png?size=64" width="64" height="64" alt="Kiavash" /></a>
<a href="https://github.com/kishoreb95" title="Kishore Baskar"><img src="https://github.com/kishoreb95.png?size=64" width="64" height="64" alt="Kishore Baskar" /></a>
<a href="https://github.com/Kive1ru" title="Jiahao Guo"><img src="https://github.com/Kive1ru.png?size=64" width="64" height="64" alt="Jiahao Guo" /></a>
<a href="https://github.com/kondisettyravi" title="Ravi Teja Kondisetty"><img src="https://github.com/kondisettyravi.png?size=64" width="64" height="64" alt="Ravi Teja Kondisetty" /></a>
<a href="https://github.com/krishdhasmana" title="Krish Dhasmana"><img src="https://github.com/krishdhasmana.png?size=64" width="64" height="64" alt="Krish Dhasmana" /></a>
<a href="https://github.com/krunalpa-amzn" title="krunalpa-amzn"><img src="https://github.com/krunalpa-amzn.png?size=64" width="64" height="64" alt="krunalpa-amzn" /></a>
<a href="https://github.com/ktreharrison" title="Ken Harrison"><img src="https://github.com/ktreharrison.png?size=64" width="64" height="64" alt="Ken Harrison" /></a>
<a href="https://github.com/Kyle-Helmick" title="Kyle Helmick"><img src="https://github.com/Kyle-Helmick.png?size=64" width="64" height="64" alt="Kyle Helmick" /></a>
<a href="https://github.com/kyleseaman" title="Kyle Seaman"><img src="https://github.com/kyleseaman.png?size=64" width="64" height="64" alt="Kyle Seaman" /></a>
<a href="https://github.com/LachlanLindsay" title="Lachlan Lindsay"><img src="https://github.com/LachlanLindsay.png?size=64" width="64" height="64" alt="Lachlan Lindsay" /></a>
<a href="https://github.com/lagrider" title="Bojin Li"><img src="https://github.com/lagrider.png?size=64" width="64" height="64" alt="Bojin Li" /></a>
<a href="https://github.com/LandonCoe" title="LandonCoe"><img src="https://github.com/LandonCoe.png?size=64" width="64" height="64" alt="LandonCoe" /></a>
<a href="https://github.com/lcplcy" title="Lho Chen Yang"><img src="https://github.com/lcplcy.png?size=64" width="64" height="64" alt="Lho Chen Yang" /></a>
<a href="https://github.com/LeonardALQ" title="Leonard"><img src="https://github.com/LeonardALQ.png?size=64" width="64" height="64" alt="Leonard" /></a>
<a href="https://github.com/leozhad" title="Leo Zhadanovsky"><img src="https://github.com/leozhad.png?size=64" width="64" height="64" alt="Leo Zhadanovsky" /></a>
<a href="https://github.com/LeTeutz" title="Teodor Oprescu"><img src="https://github.com/LeTeutz.png?size=64" width="64" height="64" alt="Teodor Oprescu" /></a>
<a href="https://github.com/LiptonJumboTeaBag" title="John Li"><img src="https://github.com/LiptonJumboTeaBag.png?size=64" width="64" height="64" alt="John Li" /></a>
<a href="https://github.com/lmambr2" title="lmambr2"><img src="https://github.com/lmambr2.png?size=64" width="64" height="64" alt="lmambr2" /></a>
<a href="https://github.com/Lock128" title="Johannes Koch"><img src="https://github.com/Lock128.png?size=64" width="64" height="64" alt="Johannes Koch" /></a>
<a href="https://github.com/logesh4v" title="LOGESH S"><img src="https://github.com/logesh4v.png?size=64" width="64" height="64" alt="LOGESH S" /></a>
<a href="https://github.com/LucaButBoring" title="Luca Chang"><img src="https://github.com/LucaButBoring.png?size=64" width="64" height="64" alt="Luca Chang" /></a>
<a href="https://github.com/luisgabriel" title="Luís Gabriel Lima"><img src="https://github.com/luisgabriel.png?size=64" width="64" height="64" alt="Luís Gabriel Lima" /></a>
<a href="https://github.com/lukehjung" title="Luke Jung"><img src="https://github.com/lukehjung.png?size=64" width="64" height="64" alt="Luke Jung" /></a>
<a href="https://github.com/Lunchb0ne" title="Abhishek Aryan"><img src="https://github.com/Lunchb0ne.png?size=64" width="64" height="64" alt="Abhishek Aryan" /></a>
<a href="https://github.com/luokaiwei" title="Kaiwei Luo"><img src="https://github.com/luokaiwei.png?size=64" width="64" height="64" alt="Kaiwei Luo" /></a>
<a href="https://github.com/luudtran" title="luudtran"><img src="https://github.com/luudtran.png?size=64" width="64" height="64" alt="luudtran" /></a>
<a href="https://github.com/MacintoshPlus89" title="MacintoshPlus89"><img src="https://github.com/MacintoshPlus89.png?size=64" width="64" height="64" alt="MacintoshPlus89" /></a>
<a href="https://github.com/maitianqcc" title="maitianqcc"><img src="https://github.com/maitianqcc.png?size=64" width="64" height="64" alt="maitianqcc" /></a>
<a href="https://github.com/mamaiti" title="mamaiti"><img src="https://github.com/mamaiti.png?size=64" width="64" height="64" alt="mamaiti" /></a>
<a href="https://github.com/mannitrkl2006" title="manish.gupta"><img src="https://github.com/mannitrkl2006.png?size=64" width="64" height="64" alt="manish.gupta" /></a>
<a href="https://github.com/mariamalaidi" title="mariamalaidi"><img src="https://github.com/mariamalaidi.png?size=64" width="64" height="64" alt="mariamalaidi" /></a>
<a href="https://github.com/martchellop" title="Marcello Pagano"><img src="https://github.com/martchellop.png?size=64" width="64" height="64" alt="Marcello Pagano" /></a>
<a href="https://github.com/MarvellousCodes" title="Marvellous Adedapo"><img src="https://github.com/MarvellousCodes.png?size=64" width="64" height="64" alt="Marvellous Adedapo" /></a>
<a href="https://github.com/MattMCloudy" title="Matthew McLeod"><img src="https://github.com/MattMCloudy.png?size=64" width="64" height="64" alt="Matthew McLeod" /></a>
<a href="https://github.com/maufee" title="maufee"><img src="https://github.com/maufee.png?size=64" width="64" height="64" alt="maufee" /></a>
<a href="https://github.com/mbajaj92" title="Madhur Bajaj"><img src="https://github.com/mbajaj92.png?size=64" width="64" height="64" alt="Madhur Bajaj" /></a>
<a href="https://github.com/mchaloupka" title="Milos Chaloupka"><img src="https://github.com/mchaloupka.png?size=64" width="64" height="64" alt="Milos Chaloupka" /></a>
<a href="https://github.com/mclawben" title="mclawben"><img src="https://github.com/mclawben.png?size=64" width="64" height="64" alt="mclawben" /></a>
<a href="https://github.com/mcryan77" title="mcryan"><img src="https://github.com/mcryan77.png?size=64" width="64" height="64" alt="mcryan" /></a>
<a href="https://github.com/Mdkar" title="Mihir Dhamankar"><img src="https://github.com/Mdkar.png?size=64" width="64" height="64" alt="Mihir Dhamankar" /></a>
<a href="https://github.com/mdwyer223" title="Matthew Dwyer"><img src="https://github.com/mdwyer223.png?size=64" width="64" height="64" alt="Matthew Dwyer" /></a>
<a href="https://github.com/michellemxm" title="Michelle Ma"><img src="https://github.com/michellemxm.png?size=64" width="64" height="64" alt="Michelle Ma" /></a>
<a href="https://github.com/MikeMayer" title="Mike Mayer"><img src="https://github.com/MikeMayer.png?size=64" width="64" height="64" alt="Mike Mayer" /></a>
<a href="https://github.com/mikkuzne" title="Mikhail Kuznetsov"><img src="https://github.com/mikkuzne.png?size=64" width="64" height="64" alt="Mikhail Kuznetsov" /></a>
<a href="https://github.com/mkbarnum" title="mkbarnum"><img src="https://github.com/mkbarnum.png?size=64" width="64" height="64" alt="mkbarnum" /></a>
<a href="https://github.com/molladair" title="Molly Adair"><img src="https://github.com/molladair.png?size=64" width="64" height="64" alt="Molly Adair" /></a>
<a href="https://github.com/musaprg" title="Kotaro Inoue"><img src="https://github.com/musaprg.png?size=64" width="64" height="64" alt="Kotaro Inoue" /></a>
<a href="https://github.com/mustafaonuraydin" title="Mustafa Onur AYDIN"><img src="https://github.com/mustafaonuraydin.png?size=64" width="64" height="64" alt="Mustafa Onur AYDIN" /></a>
<a href="https://github.com/nadetastic" title="Dan Kiuna"><img src="https://github.com/nadetastic.png?size=64" width="64" height="64" alt="Dan Kiuna" /></a>
<a href="https://github.com/nagabharann" title="Nagabharan Nagendran"><img src="https://github.com/nagabharann.png?size=64" width="64" height="64" alt="Nagabharan Nagendran" /></a>
<a href="https://github.com/namrasaheba" title="Namra Saheba"><img src="https://github.com/namrasaheba.png?size=64" width="64" height="64" alt="Namra Saheba" /></a>
<a href="https://github.com/nateeklund" title="Nate Eklund"><img src="https://github.com/nateeklund.png?size=64" width="64" height="64" alt="Nate Eklund" /></a>
<a href="https://github.com/nathanyi96" title="Nathan"><img src="https://github.com/nathanyi96.png?size=64" width="64" height="64" alt="Nathan" /></a>
<a href="https://github.com/NDNey" title="David Ney"><img src="https://github.com/NDNey.png?size=64" width="64" height="64" alt="David Ney" /></a>
<a href="https://github.com/NguyenMatthew" title="Matthew Nguyen"><img src="https://github.com/NguyenMatthew.png?size=64" width="64" height="64" alt="Matthew Nguyen" /></a>
<a href="https://github.com/NicholasRBowers" title="Nicholas Bowers"><img src="https://github.com/NicholasRBowers.png?size=64" width="64" height="64" alt="Nicholas Bowers" /></a>
<a href="https://github.com/nihal111" title="Nihal Singh"><img src="https://github.com/nihal111.png?size=64" width="64" height="64" alt="Nihal Singh" /></a>
<a href="https://github.com/nikhil-m-amazon" title="Nikhil Menon"><img src="https://github.com/nikhil-m-amazon.png?size=64" width="64" height="64" alt="Nikhil Menon" /></a>
<a href="https://github.com/nikithajain888" title="nikithajain888"><img src="https://github.com/nikithajain888.png?size=64" width="64" height="64" alt="nikithajain888" /></a>
<a href="https://github.com/NikolasPpd" title="Nick Papadopoulos"><img src="https://github.com/NikolasPpd.png?size=64" width="64" height="64" alt="Nick Papadopoulos" /></a>
<a href="https://github.com/nishi7409" title="Nishant Srivastava"><img src="https://github.com/nishi7409.png?size=64" width="64" height="64" alt="Nishant Srivastava" /></a>
<a href="https://github.com/nitan2k" title="nitan2k"><img src="https://github.com/nitan2k.png?size=64" width="64" height="64" alt="nitan2k" /></a>
<a href="https://github.com/NovaPlasm" title="Beau Taylor"><img src="https://github.com/NovaPlasm.png?size=64" width="64" height="64" alt="Beau Taylor" /></a>
<a href="https://github.com/nyclord" title="Mark Lord"><img src="https://github.com/nyclord.png?size=64" width="64" height="64" alt="Mark Lord" /></a>
<a href="https://github.com/officialprosingh" title="Parwinder Singh"><img src="https://github.com/officialprosingh.png?size=64" width="64" height="64" alt="Parwinder Singh" /></a>
<a href="https://github.com/OnlyOneByte" title="Rengang (Angelo) Yang"><img src="https://github.com/OnlyOneByte.png?size=64" width="64" height="64" alt="Rengang (Angelo) Yang" /></a>
<a href="https://github.com/parimaldeshmukh" title="Parimal Deshmukh"><img src="https://github.com/parimaldeshmukh.png?size=64" width="64" height="64" alt="Parimal Deshmukh" /></a>
<a href="https://github.com/patrigao" title="patrigao"><img src="https://github.com/patrigao.png?size=64" width="64" height="64" alt="patrigao" /></a>
<a href="https://github.com/pbcoder" title="pbcoder"><img src="https://github.com/pbcoder.png?size=64" width="64" height="64" alt="pbcoder" /></a>
<a href="https://github.com/pepmach" title="Stan Tian"><img src="https://github.com/pepmach.png?size=64" width="64" height="64" alt="Stan Tian" /></a>
<a href="https://github.com/peterhieuvu" title="peterhieuvu"><img src="https://github.com/peterhieuvu.png?size=64" width="64" height="64" alt="peterhieuvu" /></a>
<a href="https://github.com/philipjk" title="philipjk"><img src="https://github.com/philipjk.png?size=64" width="64" height="64" alt="philipjk" /></a>
<a href="https://github.com/pierrms" title="pierrms"><img src="https://github.com/pierrms.png?size=64" width="64" height="64" alt="pierrms" /></a>
<a href="https://github.com/pilami" title="Sai Chaitanya Manchikatla"><img src="https://github.com/pilami.png?size=64" width="64" height="64" alt="Sai Chaitanya Manchikatla" /></a>
<a href="https://github.com/PNg-HA" title="PNg HA"><img src="https://github.com/PNg-HA.png?size=64" width="64" height="64" alt="PNg HA" /></a>
<a href="https://github.com/popematt" title="Matthew Pope"><img src="https://github.com/popematt.png?size=64" width="64" height="64" alt="Matthew Pope" /></a>
<a href="https://github.com/poyea" title="John Law"><img src="https://github.com/poyea.png?size=64" width="64" height="64" alt="John Law" /></a>
<a href="https://github.com/pramod-123" title="Pramod Dudhi"><img src="https://github.com/pramod-123.png?size=64" width="64" height="64" alt="Pramod Dudhi" /></a>
<a href="https://github.com/presidentarrow" title="presidentarrow"><img src="https://github.com/presidentarrow.png?size=64" width="64" height="64" alt="presidentarrow" /></a>
<a href="https://github.com/ptomooka" title="ptomooka"><img src="https://github.com/ptomooka.png?size=64" width="64" height="64" alt="ptomooka" /></a>
<a href="https://github.com/qh2244" title="Qifeng Huang"><img src="https://github.com/qh2244.png?size=64" width="64" height="64" alt="Qifeng Huang" /></a>
<a href="https://github.com/qinghua" title="qinghua"><img src="https://github.com/qinghua.png?size=64" width="64" height="64" alt="qinghua" /></a>
<a href="https://github.com/Qusai1201" title="Qusai Hussein"><img src="https://github.com/Qusai1201.png?size=64" width="64" height="64" alt="Qusai Hussein" /></a>
<a href="https://github.com/r331" title="Roman Ivanov"><img src="https://github.com/r331.png?size=64" width="64" height="64" alt="Roman Ivanov" /></a>
<a href="https://github.com/rabinarayanpatra" title="Rabinarayan Patra"><img src="https://github.com/rabinarayanpatra.png?size=64" width="64" height="64" alt="Rabinarayan Patra" /></a>
<a href="https://github.com/radical-beard" title="radical-beard"><img src="https://github.com/radical-beard.png?size=64" width="64" height="64" alt="radical-beard" /></a>
<a href="https://github.com/Rajnita" title="Rajnita Leichombam"><img src="https://github.com/Rajnita.png?size=64" width="64" height="64" alt="Rajnita Leichombam" /></a>
<a href="https://github.com/rajpuram09" title="Raj Puram"><img src="https://github.com/rajpuram09.png?size=64" width="64" height="64" alt="Raj Puram" /></a>
<a href="https://github.com/raleycs" title="Christopher Raley"><img src="https://github.com/raleycs.png?size=64" width="64" height="64" alt="Christopher Raley" /></a>
<a href="https://github.com/ramdavid" title="ramdavid"><img src="https://github.com/ramdavid.png?size=64" width="64" height="64" alt="ramdavid" /></a>
<a href="https://github.com/randallwc" title="William Randall"><img src="https://github.com/randallwc.png?size=64" width="64" height="64" alt="William Randall" /></a>
<a href="https://github.com/rbcommits" title="Raghav Bhardwaj"><img src="https://github.com/rbcommits.png?size=64" width="64" height="64" alt="Raghav Bhardwaj" /></a>
<a href="https://github.com/rcidadef" title="Roberto Cidade Fonseca"><img src="https://github.com/rcidadef.png?size=64" width="64" height="64" alt="Roberto Cidade Fonseca" /></a>
<a href="https://github.com/Remicks1" title="Jimmy Kilpatrick"><img src="https://github.com/Remicks1.png?size=64" width="64" height="64" alt="Jimmy Kilpatrick" /></a>
<a href="https://github.com/rishabhagrawal1" title="Rishabh Agrawal"><img src="https://github.com/rishabhagrawal1.png?size=64" width="64" height="64" alt="Rishabh Agrawal" /></a>
<a href="https://github.com/rittikg-amazon" title="rittikg-amazon"><img src="https://github.com/rittikg-amazon.png?size=64" width="64" height="64" alt="rittikg-amazon" /></a>
<a href="https://github.com/robchahin" title="robchahin"><img src="https://github.com/robchahin.png?size=64" width="64" height="64" alt="robchahin" /></a>
<a href="https://github.com/rochakgupta" title="Rochak Gupta"><img src="https://github.com/rochakgupta.png?size=64" width="64" height="64" alt="Rochak Gupta" /></a>
<a href="https://github.com/Rocketpoodle" title="Austin Goddard"><img src="https://github.com/Rocketpoodle.png?size=64" width="64" height="64" alt="Austin Goddard" /></a>
<a href="https://github.com/RohanK6" title="RohanK6"><img src="https://github.com/RohanK6.png?size=64" width="64" height="64" alt="RohanK6" /></a>
<a href="https://github.com/rohit-mehra" title="Rohit Mehra"><img src="https://github.com/rohit-mehra.png?size=64" width="64" height="64" alt="Rohit Mehra" /></a>
<a href="https://github.com/ronyjacobjohn-tech" title="ronyjacobjohn-tech"><img src="https://github.com/ronyjacobjohn-tech.png?size=64" width="64" height="64" alt="ronyjacobjohn-tech" /></a>
<a href="https://github.com/rpranshu" title="Pranshu Ranakoti"><img src="https://github.com/rpranshu.png?size=64" width="64" height="64" alt="Pranshu Ranakoti" /></a>
<a href="https://github.com/rstomasalberto" title="Tomas Rodriguez"><img src="https://github.com/rstomasalberto.png?size=64" width="64" height="64" alt="Tomas Rodriguez" /></a>
<a href="https://github.com/rvinitra" title="rvinitra"><img src="https://github.com/rvinitra.png?size=64" width="64" height="64" alt="rvinitra" /></a>
<a href="https://github.com/ryancormack" title="Ryan Cormack"><img src="https://github.com/ryancormack.png?size=64" width="64" height="64" alt="Ryan Cormack" /></a>
<a href="https://github.com/sandlerr" title="Roman Sandler"><img src="https://github.com/sandlerr.png?size=64" width="64" height="64" alt="Roman Sandler" /></a>
<a href="https://github.com/Sapientia-PT" title="João Miguel"><img src="https://github.com/Sapientia-PT.png?size=64" width="64" height="64" alt="João Miguel" /></a>
<a href="https://github.com/sarankota" title="Saran Kota"><img src="https://github.com/sarankota.png?size=64" width="64" height="64" alt="Saran Kota" /></a>
<a href="https://github.com/sauravgpt" title="Saurav Kumar Gupta"><img src="https://github.com/sauravgpt.png?size=64" width="64" height="64" alt="Saurav Kumar Gupta" /></a>
<a href="https://github.com/scuthbert" title="Sam Cuthbertson"><img src="https://github.com/scuthbert.png?size=64" width="64" height="64" alt="Sam Cuthbertson" /></a>
<a href="https://github.com/SebastianYuSun" title="Sebastian (Yu) Sun"><img src="https://github.com/SebastianYuSun.png?size=64" width="64" height="64" alt="Sebastian (Yu) Sun" /></a>
<a href="https://github.com/Setul0712" title="Setul0712"><img src="https://github.com/Setul0712.png?size=64" width="64" height="64" alt="Setul0712" /></a>
<a href="https://github.com/shaochew" title="shaochew"><img src="https://github.com/shaochew.png?size=64" width="64" height="64" alt="shaochew" /></a>
<a href="https://github.com/shawnxli" title="Shawn Li"><img src="https://github.com/shawnxli.png?size=64" width="64" height="64" alt="Shawn Li" /></a>
<a href="https://github.com/ShayanYaseen" title="Shayan"><img src="https://github.com/ShayanYaseen.png?size=64" width="64" height="64" alt="Shayan" /></a>
<a href="https://github.com/ShelbyZ" title="Shelby Hagman"><img src="https://github.com/ShelbyZ.png?size=64" width="64" height="64" alt="Shelby Hagman" /></a>
<a href="https://github.com/Ship-Loop" title="Siddartha "><img src="https://github.com/Ship-Loop.png?size=64" width="64" height="64" alt="Siddartha " /></a>
<a href="https://github.com/shortbloke" title="Martin Rowan"><img src="https://github.com/shortbloke.png?size=64" width="64" height="64" alt="Martin Rowan" /></a>
<a href="https://github.com/ShotaroKataoka" title="Shotaro Kataoka"><img src="https://github.com/ShotaroKataoka.png?size=64" width="64" height="64" alt="Shotaro Kataoka" /></a>
<a href="https://github.com/skagraw16" title="skagraw16"><img src="https://github.com/skagraw16.png?size=64" width="64" height="64" alt="skagraw16" /></a>
<a href="https://github.com/smeyffret" title="smeyffret"><img src="https://github.com/smeyffret.png?size=64" width="64" height="64" alt="smeyffret" /></a>
<a href="https://github.com/snoldak924" title="Sam Oldak"><img src="https://github.com/snoldak924.png?size=64" width="64" height="64" alt="Sam Oldak" /></a>
<a href="https://github.com/snowoody" title="snowoody"><img src="https://github.com/snowoody.png?size=64" width="64" height="64" alt="snowoody" /></a>
<a href="https://github.com/solnikhil" title="Nikhil Solanki"><img src="https://github.com/solnikhil.png?size=64" width="64" height="64" alt="Nikhil Solanki" /></a>
<a href="https://github.com/Soneji" title="Dhaval Soneji"><img src="https://github.com/Soneji.png?size=64" width="64" height="64" alt="Dhaval Soneji" /></a>
<a href="https://github.com/spandanagrawal" title="Spandan Gopal Agrawal"><img src="https://github.com/spandanagrawal.png?size=64" width="64" height="64" alt="Spandan Gopal Agrawal" /></a>
<a href="https://github.com/stifspear" title="stifspear"><img src="https://github.com/stifspear.png?size=64" width="64" height="64" alt="stifspear" /></a>
<a href="https://github.com/sudhamsu" title="Sudhamsu Manne"><img src="https://github.com/sudhamsu.png?size=64" width="64" height="64" alt="Sudhamsu Manne" /></a>
<a href="https://github.com/sugavaneshb" title="Sugavanesh B"><img src="https://github.com/sugavaneshb.png?size=64" width="64" height="64" alt="Sugavanesh B" /></a>
<a href="https://github.com/sujoydc" title="Sujoy Datta Choudhury"><img src="https://github.com/sujoydc.png?size=64" width="64" height="64" alt="Sujoy Datta Choudhury" /></a>
<a href="https://github.com/SungjinYoo" title="Sungjin Yoo"><img src="https://github.com/SungjinYoo.png?size=64" width="64" height="64" alt="Sungjin Yoo" /></a>
<a href="https://github.com/SwapDixit" title="Swapnil Dixit"><img src="https://github.com/SwapDixit.png?size=64" width="64" height="64" alt="Swapnil Dixit" /></a>
<a href="https://github.com/sxhmilyoyo" title="sxhmilyoyo"><img src="https://github.com/sxhmilyoyo.png?size=64" width="64" height="64" alt="sxhmilyoyo" /></a>
<a href="https://github.com/syedmujahedalih" title="Mujahed Syed"><img src="https://github.com/syedmujahedalih.png?size=64" width="64" height="64" alt="Mujahed Syed" /></a>
<a href="https://github.com/t-jones" title="Tim Jones"><img src="https://github.com/t-jones.png?size=64" width="64" height="64" alt="Tim Jones" /></a>
<a href="https://github.com/TakahiroIshii" title="Takahiro Ishii"><img src="https://github.com/TakahiroIshii.png?size=64" width="64" height="64" alt="Takahiro Ishii" /></a>
<a href="https://github.com/the-mann" title="Marcus Mann"><img src="https://github.com/the-mann.png?size=64" width="64" height="64" alt="Marcus Mann" /></a>
<a href="https://github.com/therohan21" title="Rohan Rajeev"><img src="https://github.com/therohan21.png?size=64" width="64" height="64" alt="Rohan Rajeev" /></a>
<a href="https://github.com/thethomaslane" title="Thomas Lane"><img src="https://github.com/thethomaslane.png?size=64" width="64" height="64" alt="Thomas Lane" /></a>
<a href="https://github.com/thiagoh" title="thiagoh"><img src="https://github.com/thiagoh.png?size=64" width="64" height="64" alt="thiagoh" /></a>
<a href="https://github.com/think-imbaig" title="think-imbaig"><img src="https://github.com/think-imbaig.png?size=64" width="64" height="64" alt="think-imbaig" /></a>
<a href="https://github.com/ThR3742" title="ThR3742"><img src="https://github.com/ThR3742.png?size=64" width="64" height="64" alt="ThR3742" /></a>
<a href="https://github.com/tlauda" title="Tomasz Lauda"><img src="https://github.com/tlauda.png?size=64" width="64" height="64" alt="Tomasz Lauda" /></a>
<a href="https://github.com/tlobinger" title="Thomas Lobinger"><img src="https://github.com/tlobinger.png?size=64" width="64" height="64" alt="Thomas Lobinger" /></a>
<a href="https://github.com/toby-wong" title="Toby Wong"><img src="https://github.com/toby-wong.png?size=64" width="64" height="64" alt="Toby Wong" /></a>
<a href="https://github.com/trekie86" title="Rob Wolinski"><img src="https://github.com/trekie86.png?size=64" width="64" height="64" alt="Rob Wolinski" /></a>
<a href="https://github.com/tudit" title="Udit Tumuluri"><img src="https://github.com/tudit.png?size=64" width="64" height="64" alt="Udit Tumuluri" /></a>
<a href="https://github.com/uatemycookie22" title="uatemycookie22"><img src="https://github.com/uatemycookie22.png?size=64" width="64" height="64" alt="uatemycookie22" /></a>
<a href="https://github.com/udayprakash" title="Uday Prakash"><img src="https://github.com/udayprakash.png?size=64" width="64" height="64" alt="Uday Prakash" /></a>
<a href="https://github.com/unstablebrainiac" title="Sajal Narang"><img src="https://github.com/unstablebrainiac.png?size=64" width="64" height="64" alt="Sajal Narang" /></a>
<a href="https://github.com/uzumakichillu" title="uzumakichillu"><img src="https://github.com/uzumakichillu.png?size=64" width="64" height="64" alt="uzumakichillu" /></a>
<a href="https://github.com/vaibhavbhatiadev" title="Vaibhav Bhatia"><img src="https://github.com/vaibhavbhatiadev.png?size=64" width="64" height="64" alt="Vaibhav Bhatia" /></a>
<a href="https://github.com/vamgan" title="Vamil Gandhi"><img src="https://github.com/vamgan.png?size=64" width="64" height="64" alt="Vamil Gandhi" /></a>
<a href="https://github.com/vdurante" title="Vitor Durante"><img src="https://github.com/vdurante.png?size=64" width="64" height="64" alt="Vitor Durante" /></a>
<a href="https://github.com/venkatvb" title="Venkatesh Babu AR"><img src="https://github.com/venkatvb.png?size=64" width="64" height="64" alt="Venkatesh Babu AR" /></a>
<a href="https://github.com/vishal-sahoo" title="Vishal Sahoo"><img src="https://github.com/vishal-sahoo.png?size=64" width="64" height="64" alt="Vishal Sahoo" /></a>
<a href="https://github.com/vishalvignesh" title="Vishal Vignesh"><img src="https://github.com/vishalvignesh.png?size=64" width="64" height="64" alt="Vishal Vignesh" /></a>
<a href="https://github.com/w-wei105" title="w-wei105"><img src="https://github.com/w-wei105.png?size=64" width="64" height="64" alt="w-wei105" /></a>
<a href="https://github.com/wang-shihao" title="Arthur, Shihao Wang"><img src="https://github.com/wang-shihao.png?size=64" width="64" height="64" alt="Arthur, Shihao Wang" /></a>
<a href="https://github.com/wannaFlyKa" title="Yao"><img src="https://github.com/wannaFlyKa.png?size=64" width="64" height="64" alt="Yao" /></a>
<a href="https://github.com/wbowditch" title="Will Bowditch"><img src="https://github.com/wbowditch.png?size=64" width="64" height="64" alt="Will Bowditch" /></a>
<a href="https://github.com/weinansi" title="weinansi"><img src="https://github.com/weinansi.png?size=64" width="64" height="64" alt="weinansi" /></a>
<a href="https://github.com/werainkhatri" title="Viren Khatri"><img src="https://github.com/werainkhatri.png?size=64" width="64" height="64" alt="Viren Khatri" /></a>
<a href="https://github.com/wu5bocheng" title="wu5bocheng"><img src="https://github.com/wu5bocheng.png?size=64" width="64" height="64" alt="wu5bocheng" /></a>
<a href="https://github.com/wundram" title="wundram"><img src="https://github.com/wundram.png?size=64" width="64" height="64" alt="wundram" /></a>
<a href="https://github.com/xiaochao17" title="xiaochao17"><img src="https://github.com/xiaochao17.png?size=64" width="64" height="64" alt="xiaochao17" /></a>
<a href="https://github.com/XTX-TXT" title="XTX-TXT"><img src="https://github.com/XTX-TXT.png?size=64" width="64" height="64" alt="XTX-TXT" /></a>
<a href="https://github.com/xuejinT" title="Serena Tan"><img src="https://github.com/xuejinT.png?size=64" width="64" height="64" alt="Serena Tan" /></a>
<a href="https://github.com/Xyand" title="Albert"><img src="https://github.com/Xyand.png?size=64" width="64" height="64" alt="Albert" /></a>
<a href="https://github.com/yashwanthkorla" title="Yashwanth Korla"><img src="https://github.com/yashwanthkorla.png?size=64" width="64" height="64" alt="Yashwanth Korla" /></a>
<a href="https://github.com/yehuizhang" title="Yehui"><img src="https://github.com/yehuizhang.png?size=64" width="64" height="64" alt="Yehui" /></a>
<a href="https://github.com/YifanL9" title="Yifan"><img src="https://github.com/YifanL9.png?size=64" width="64" height="64" alt="Yifan" /></a>
<a href="https://github.com/yohanesss" title="Yohanes Setiawan"><img src="https://github.com/yohanesss.png?size=64" width="64" height="64" alt="Yohanes Setiawan" /></a>
<a href="https://github.com/yuwesu" title="Sypher Su"><img src="https://github.com/yuwesu.png?size=64" width="64" height="64" alt="Sypher Su" /></a>
<a href="https://github.com/yytdfc" title="yytdfc"><img src="https://github.com/yytdfc.png?size=64" width="64" height="64" alt="yytdfc" /></a>
<a href="https://github.com/zach-herridge" title="Zach Herridge"><img src="https://github.com/zach-herridge.png?size=64" width="64" height="64" alt="Zach Herridge" /></a>
<a href="https://github.com/zachakin" title="Zach Akin-Amland"><img src="https://github.com/zachakin.png?size=64" width="64" height="64" alt="Zach Akin-Amland" /></a>
<a href="https://github.com/zander8807" title="zander8807"><img src="https://github.com/zander8807.png?size=64" width="64" height="64" alt="zander8807" /></a>
<a href="https://github.com/Zedmor" title="Akim Akimov"><img src="https://github.com/Zedmor.png?size=64" width="64" height="64" alt="Akim Akimov" /></a>
<a href="https://github.com/zeiadzaf" title="Zeiad"><img src="https://github.com/zeiadzaf.png?size=64" width="64" height="64" alt="Zeiad" /></a>
<a href="https://github.com/Zhang-Zhaolong" title="Zhaolong Zhang"><img src="https://github.com/Zhang-Zhaolong.png?size=64" width="64" height="64" alt="Zhaolong Zhang" /></a>
<a href="https://github.com/ZheLyu" title="Zhe Lyu"><img src="https://github.com/ZheLyu.png?size=64" width="64" height="64" alt="Zhe Lyu" /></a>
<a href="https://github.com/ZhengfeiJi" title="Ji"><img src="https://github.com/ZhengfeiJi.png?size=64" width="64" height="64" alt="Ji" /></a>
<a href="https://github.com/ZhongkaiLiu" title="Zhongkai Liu"><img src="https://github.com/ZhongkaiLiu.png?size=64" width="64" height="64" alt="Zhongkai Liu" /></a>
<a href="https://github.com/zhulinn" title="Lin Zhu"><img src="https://github.com/zhulinn.png?size=64" width="64" height="64" alt="Lin Zhu" /></a>
<a href="https://github.com/zifengxiazx" title="zifengxiazx"><img src="https://github.com/zifengxiazx.png?size=64" width="64" height="64" alt="zifengxiazx" /></a>

Listed alphabetically by GitHub username. Internal contributors appear here if they
consented to public recognition in the contributor survey; open-source contributors are
included from this repository's pull request history. If you contributed and would like
to be added, corrected, or removed, please open an issue or a pull request.

## License

Kiro Crew is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution information.
