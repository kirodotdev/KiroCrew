<p align="center">
  <img src="assets/banner.svg" alt="Kiro Crew. Keep work moving. Runs on your hardware, remembers across sessions, keeps working unattended.">
</p>

<h1 align="center">Kiro Crew</h1>

<p align="center">
  <strong>A persistent, self-learning, self-evolving agent for work that continues beyond one chat.</strong>
</p>

<p align="center">
  Kiro Crew is an open-source personal AI agent that runs locally or remotely on
  your hardware. It is persistent, self-learning, and self-evolving. Work with it
  from the Mac app, web dashboard, and CLI, or continue the same work through
  connection tools like Slack and Discord.
  Your multi-step tasks can run unattended, recurring jobs run on your schedule,
  and heartbeats monitor systems until something needs attention. Kiro Crew apps
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

**Desktop app.** [Download Kiro Crew](https://github.com/kirodotdev/KiroCrew/releases):

- **macOS**: signed `.dmg`
- **Linux**: `.AppImage`
- **Windows**: no desktop build yet, so run the Gateway from a [source install](#build-from-source) and open the dashboard in your browser
- **Build it yourself**: `make desktop`

The app starts a bundled Gateway when no local Gateway is already running, and
agent sessions run on demand on the same machine. It can connect to a remote
Gateway over an SSH tunnel instead. See the
[desktop app guide](docs/desktop-app.md).

**One-line install.** Install the prebuilt, SHA-256-verified wheel from the
release CDN without cloning the repository or running `npm` and a local build:

```bash
# Stable channel (the default)
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh

# Or track a faster channel: insider or nightly
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider

# Pin an exact version
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --version 0.1.0
```

Open `http://localhost:5476` and start a conversation. The web dashboard works
without messaging credentials. Add [Slack](docs/slack-setup.md),
[Telegram](src/kiro_crew/docs/telegram-integration.md), or
[WeCom](src/kiro_crew/docs/wecom-integration.md) when you want to continue
working with the same agent away from the dashboard. These channels connect
outbound, so you do not need to expose the dashboard port publicly.

## Build from source

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

Kiro Crew is a persistent, self-learning, self-evolving agent that can run
continuously, locally or remotely on your hardware. It remembers across sessions,
turns corrections and failures into durable lessons, and synthesizes reusable
skills from repeated patterns.

**Persistent and always available.** Sessions, memory, schedules, and task
checkpoints persist beyond one chat and across Gateway restarts. Scheduled,
proactive, and reactive work can continue without someone at the terminal while
the host is running.

**Self-learning.** Corrections and task failures become durable lessons that
change later behavior. Preferences and project context carry across sessions.

**Self-evolving.** Repeated patterns can become reusable skills. Memory, lessons,
and skills remain visible and editable, so each Kiro Crew becomes more tailored
to the person and work around it over time.

**Runs where you choose.** Keep the runtime and its state on your Mac, in a local
container, or on a remote machine you control.

**One Gateway, many ways to work.** The Mac app and web dashboard let you work
directly in Kiro Crew across conversations, files, tasks, approvals, memory, and
apps. The Gateway connects that same runtime to the CLI and to every messaging
surface listed under [How it works](#how-it-works) when you want to continue from
another one.

## What Kiro Crew does

| Capability | What it gives you | Ask Kiro Crew to... |
|---|---|---|
| **Persistent sessions** | Run concurrent, isolated conversations, resume them after Gateway restarts, search prior sessions, and carry recent context into new work. |  |
| **Self-learning** | Turn corrections and task failures into durable lessons that change later behavior. Keep preferences, active-project context, and history scoped to the relevant workspace. | "No, always run the frontend checks before calling a change done." It saves the correction as a workspace-scoped lesson and applies it in future sessions for that workspace. |
| **Self-evolving skills** | Synthesize reusable skills from repeated patterns, then inspect, refine, or remove them as your work changes. |  |
| **Long-running tasks** | Give Kiro Crew a task spec and walk away. It plans steps, executes them, validates results, retries failures, and resumes from checkpoints. | "Implement this migration plan and stop if the tests fail." It runs the spec as a checkpointed task, validates each step, and reports the final review. |
| **Unattended autonomy** | Run scheduled agent work or deterministic scripts and commands without a model call. Monitor work until it is done, or react to messaging events and authenticated webhooks without someone at the terminal. | "Every weekday at 9, summarize the open work I should review." Kiro Crew creates a timezone-aware recurring job and delivers each result to the configured surface. Ask it to watch a pull request and it keeps checking across heartbeat cycles. |
| **Delegation** | Spawn isolated subagents for parallel work and bring their results back into the parent conversation. | "Research these three options in parallel and recommend one." It spawns isolated subagents and synthesizes the tradeoffs. |
| **Work where you choose** | Work directly in the Mac app or web dashboard, or continue through the CLI and any connected messaging surface without moving the agent runtime or its state. |  |
| **Installable apps** | Add focused interfaces and domain workflows through dashboard pages, scoped Gateway APIs, events, and lifecycle hooks. |  |
| **Extensible tools** | Add MCP servers, markdown skills, and hooks without changing the core runtime. |  |
| **Visible execution** | Watch tool calls, subagent progress, context usage, approvals, schedules, memory, and logs from the dashboard. |  |
| **Defense in depth** | Combine tool approvals, OS sandboxing, sensitive-path checks, credential redaction, deny rules, audit events, and governance profiles. |  |

You can also paste a screenshot and ask what is causing an error. Kiro Crew sends
the image to the active Kiro model and keeps the diagnosis in the conversation
history.

The complete inventory is in [Features](docs/features.md) and
[What's New](CHANGELOG.md).

## How it works

```text
Mac app / Web / Slack / Telegram / WeCom / CLI
                       ↓
                    Gateway
 access · sessions · memory · schedules · approvals · apps
                       ↓
                Agent sessions
          ACP runtime · kiro-cli · MCP tools · models
```

The Gateway separates where the agent runs from where you work with it. In the
Mac app or web dashboard, you can work directly through parallel conversations,
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
messages from the Mac app, web, CLI, and the messaging surfaces listed below. It persists
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
| **Mac app** | The simplest local experience, with a bundled Gateway plus multi-tab connections to local or remote Gateways. |
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

```bash
kirocrew chat                         # interactive terminal conversation
kirocrew run TASK.md                  # execute a multi-step spec
kirocrew cron add "briefing" \
  "summarize my open work" --cron "0 9 * * MON-FRI"
kirocrew spawn run --async \
  "research the migration options"
kirocrew service install              # keep the gateway running after reboot
```

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

**Docker.** For always-on servers, the Gateway ships as a public multi-arch
image on GHCR:

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

See the [Docker guide](docs/docker.md) for first-run login, channel tags, and
the container security model.

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
| **Remote hardware** | Follow the [remote host guide](docs/remote-desktop-setup.md) and install the service | The Gateway, agent sessions, and state run continuously on your Linux server, home lab, or cloud instance. Connect the Mac app or browser through an SSH tunnel. |
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

The Mac app can use this local Gateway or connect to a remote one. For an
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
platforms and install channels to support. This is on by default.

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
entry in the security policy blocks the heartbeat regardless of the local
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
| Project work | [Contributing](CONTRIBUTING.md), [AI assistant rules](AGENTS.md), [Changelog](CHANGELOG.md) |

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

KiroCrew was made possible by its internal community — **494 Amazon employees** who supported the project and shipped its code. This is that founding group; as KiroCrew grows in the open, we look forward to many more contributors joining them. Thank you to everyone who helped make this tool possible:

Bolin Chen, Zejiang Guo, Zezhen Xu, Simon Meyffret, Raymond Chen, Nick Bowers, Akim Akimov, Joe Pontone, Patrick Gao, Krish Dhasmana, Hoang Phan, Chen Tong, Yusheng Xu, Hugo Costa, Ben Grubin, Robert Noack, Rohan Khanderia, Luke Ely, Aidan Mackey, Stan Tian, Alec Douglas, James Joseph, Ethan Levine, Nick Papadopoulos, Erik Schweiss, Vitor Durante, Abhishek Mitra, Lanxiao Bai, Gabe Sanchez, Quan Nguyen, Lane Ambrose, Dan Dagayev, Tony Hardie, Nikhil Menon, Vamil Gandhi, Toby Wong, Di Wu, Aswin Damodar, Bocheng Wu, Chetan Chaku, Maksym Yachnyi, Matthew Barnum, Gavin Tse, Chen Yang Lho, Maninder Singh, Chris McMillon, David Fayerman, Naoya Ishikawa, Eduardo Vencovsky, Ezzat Qupty, Shreyas Bhise, Vishal Sreekrishnan, Swapnil Dixit, Mark Lord, Bharath Janyavula, Tyger Hugh, Jiahao Guo, Luca Chang, Yuliang Qiao, Shihao Wang, Joshua Yeung, Roman Ivanov, Bhavana Chinthalapally, Beau Taylor-Ladd, Christopher Huk, Kishore Baskar, Sugavanesh Balasubramanian, Parimal Deshmukh, Gregory Liu, Nansong Yi, Teodor Oprescu, Zhuoyu Li, Graham Roberts, Dinesh Jayapalan, Xu Deng, David Schlessman, Madhur Bajaj, David Hickox, Vishal Mawandia, Peter Vu, Angelo Yu, Uday Prakash, Yuta Tsuji, Sypher Su, Rohit Jose, Pedro Barrios, Yohanes Setiawan, Arpit Vyas, Connor LoPresti, Oscar Smith-Sieger, Sam Oldak, Kotaro Inoue, Shao-Cheng Wang, Rohan Kapadia, Robert Zhang, Arjun Soota, Himakireeti Konda, Jingjin Wei, Matt Pierringer, Jimmy Kilpatrick, Greg Rebholz, Yongbo Xiao, Kejian Wang, Gregory Chapman, Wilson Wu, Ahmed Hassanin, Chaoneng Quan, Siddartha  B V, John Law, Udit Tumuluri, Brent Naylor, Shuya Sawa, Rabinarayan Patra, Minglong Pan, Jianwen Liu, Joel Studevant, Eric Zhang, Greyson Nevins-Archer, August Vilakia, Arpan Banerjee, Rikiya Tsukidate, Yao Bian, Qusai Hussein, Zifeng Xia, Mustafa Onur AYDIN, Adam Doussan, Mikhail Kuznetsov, Tianxiang Xu, Justin Zhang, Kiavash Samadi, Adam Duncan, Rohit Mehra, Finn Haddon, Sean Iamartino, Akshit Desai, Mohammed Elansary, Matthew Nguyen, Axel Vidales, Huan He, Fei Ma, Jingchao Cao, Milos Chaloupka, Helena Stafford, John Espenhahn, Arturo Acuaviva, Hao Xu, Raghav Bhardwaj, Eric Muessel, Curtis Demerah, Dan McClain, Puneeth Nanjundaswamy, Sudhamsu Manne, Shashwat Srivastava, Eric Hays, Satheesh Prabhakaran, Nathan Beals, Krunal Patel, Yashwanth Korla, Tomas Rodriguez Sanchez, Vaibhav Bhatia, Matthieu Dufour, Mike Mayer, Sean Whipple, Dinesh Mathan, Luca Bruera, Marvellous Adedapo, Aryaman Pathania, Ravi Teja Kondisetty, Shayan Yaseen, Reece Bailey, Kyle Seaman, Koushik Ginjupally, Matt McLeod, Arnaldo Garcia, Thomas Lane, Mihir Dhamankar, Sam Cuthbertson, Nirav Adunuthula, David Lee, Thiago Andrade, Tian ZHANG, Vineeth Chinthala, Saif Rahman, Cole Whitley, Emmanuella Dasilva-Domingos, Nihal Singh, Kenneth Harrison, Ashwin Menon, Alex Truong, Ben Bloschock, Selena Wang, Amit Menon, Caillin Bathern, Naveen Adarsh Petla, Joel Blumenthal, Joshua Chang, Chris Boomhower, Matthew Pope, Takahiro Ishii, Yu Zhang, Swapnil Gaikwad, Chris Wundram, Emmanuel Okonkwo, Dhaval Soneji, Mohammed Madni Vaid, Sungjin Yoo, Carter Trpik, Shubham Gupta, David Qian, lili liu, Keshav Kumar Prabhakharan, Vasanth Subramanian, Yehui Zhang, yagna gurjala, Omar Abu Mukh, William Randall, Luis Gabriel Lima, Bobby Earl, Dallin Kooyman, Kevin Goldberg, Nitan Singh, Chen Qiu, Faizan Ali, Rishabh Agrawal, Lysander Hernandez, Emma Zhou, Barrett Karson, Ariana Morgan, Namra Alkeshbhai Saheba, Jason Sirota, Lipeng Yang, Rony Jacob John, Yifan Liu, Nick Gonzales, Maxwell Schroder, Mark Asp, David Ney Abarca, Alex Avance, Chengxi Li, Jaden Yuros, Anthony Orozco, Goutham Manjunatha, Alex Jones, Giovanni Viviani, Luu Tran, Saurav Gupta, Petter Nilsson, Rohan Rajeev, Beau Bright, Lin Zhu, Parikshit Desai, Anirudh Narayanan, Roberto Matarrita Arce, Xinyu Zhao, Tyst Marin, Nate Eklund, Marc Shelton, Pranshu Ranakoti, Dayong Li, Anchit Thakur, William Bowditch, Trevor Liberty, Matthew Muncy, Zach Akin-Amland, Abe Diaz, George Coll, Sebastian Sun, Nishant Srivastava, John Li, Ryan Reich, Zach Herridge, Kushal Jain, Jake Gordon, Tyler Barkley, Marcus Mann, Nathan L. Burns, Shailesh Agrawal, Himanish Kaul, Mariam Alaidi, Imran Baig, Giridhar Shyam Sankararaman, Jake Nocentino, Stephane Robin, Angelo Yang, Vishal Sahoo, Jack Bandon, Aiden Gaines, Leonard Al-Qaseer, Ian Auger-Juul, Juan Segura, Saran Kota, Johnny Mastin, Paul Davis, Vasudeva H, Evan Stenger, Leo Zhadanovsky, Setul Patel, Jiacheng Wang, Michael Viscardi, Hung Vu, Addison Tustin, Filippo Galli, Andrew Janzen, Rittik Gautam, Landon Coe, Khaled Sarieddine, Doruk, Yueyang Mi, Amit Chowdhary, Amr Saleh, Chenying Han, Dhaivat Patel, Avi Mikhli, Jaya Kasiraj, Mathieu Pelletan, Abhishek Sharma, Filip Godina, Viren Khatri, Qiong Liu, Parwinder Singh, Nagabharan Nagendran, Dima Sitnikov, Geet Sawhney, Abhishek Dhameja, Chanon Sinitskul, Brian Thomas, Edward Riede, Shawn Li, Alexander Yuan, SIMING DENG, Marcello Silva, Nani, Jin Cheng, Martin Rowan, Rob Chahin, David Van Winkle, Gavin Mealy, Zhe Lv, Srihari Attuluri, Xuecong Zang, Anmol Saxena, Shubhranshu Kumar, Rohan Kumar, Paxton Tomooka, Tao Jiang, Felipe Barajas, Shuli He, Sandip Dutta, Shuolei Jin, Mujahed Syed, Apoorv Srivastava, Kunal Raut, Raghu Burukunte, Shubham Agrawal, FuChen, Projjol Banerji, Jeff Neuberger, Kyjauna Marshall, Noufal Edappanoli, Wei Wei, Tomasz Lauda, Yu Cheng, Kan Zhu, Anant Kaushik, Spencer Zhang, Balaji S, Spandan Agrawal, Kyle Helmick, Pramod Dudhi, Nitin Kanigicharla, Phillip Gong, Atharwa Adawadkar, Chris Paton, Ishan Mishra, Piyush Galphat, Di Wu, Francesco Falcone, Alexander Shen, Joao Miguel, Adi Sridharan, Derek Wilson, Will Maillard, Roman Sandler, Weinan Si, Austin Goddard, Gilhong Min, Sivan Cooperman, Grant Gollier, Jim Hill, Kevin Zuern, Amir Naghibi, connor marr, Louay Morsi, Kellen Jia, Nolan Clayton, Rob Stevens, Sai Chaitanya Manchikatla, Christopher Tyndall, Nischal Kumar, Warren Bui, Chad Bailey, Manish Kumar Gupta, Jamie Gao, Lachlan Lindsay, Matthew Dwyer, Jake Zhao, Jatin Dewani, Roberto Cidade, Bhargav Mistry, Zhongkai Liu, Akash Shrestha, Alexander Blom, Chris Raley, Serena Tan, Artem Pliasunov, Chance Rebholz, Liam Wirth, Sergey Chebotarev, Zeiad Zaf, David Ramos, Ayan Das, Shameem PK, Weibin Zeng, Rahul Dabas, Indika Pathirage, Moshe Yakovson, Anjan Agarwala, Jiayi Zhang, Zihong Hao, Abhishek Aryan, Jacob Morgan, Manuel Chavez, Wenli Yan, Johnny Xue, Albert Huang, Kaiwei Luo, Alex Yelle, Hugo Wen, Jaya Prakash Reddy Gade, Lakshman sai Donavan, Mert Hizli, Anthony Dominianni, Chris Mendis, Purlaksh, Qinghua Gao, Rochak Gupta, Jonathan Cox, Qifeng Huang, Sujoy Datta, Nikitha Tejpal, Prutha Shouche, Tim Lee, Vinitra Ramasubramaniam, Vivek Sayyaparaju, Albin Shrestha, Bojin Li, Gautam Mishra, Kai Mitsuzawa, Kaique Govani, Nagarajesh Lakshmanan, ShotaroKataoka, Thomas Ricatte, Zhaolong Zhang, Albert Achtenberg, Isaac Weaver, Amulya Sahoo, MJ, Sajal Narang, Rohit Ingle, Shelby Hagman, Venkatesh Babu Ayyallu Rajan, Matt Cohen, Paul McKissock, Zhengfei Ji, Abhishek Shasthry, Amad Salmon, Artem Krivonos, Arvind Srinath Kumar, Aziz Saifuddin, Casey Huggins, Jackie Ly, Justin Treece, Lester Lee, Luke Jung, Manish Patel, Rob Wolinski, Siddhant Jain, Sugan Kumar, Wenyu Yang, Andrew Golightly, Arshdeep Takkar, Daisy Dazhen, Justin Bess, Stif Spear Subba.

## License

Kiro Crew is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution information.
