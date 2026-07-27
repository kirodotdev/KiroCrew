# Project Architecture

This document describes how KiroCrew is structured, how its components interact,
and where external dependencies live. For the feature list, see
[features.md](features.md). For getting started, see
[getting-started.md](getting-started.md).

---

## High-Level Overview

KiroCrew is a gateway that sits between user-facing surfaces (CLI, Slack, web
dashboard) and the kiro-cli agent backend. It adds session management, memory,
scheduling, and a web UI on top of kiro-cli's raw agent capabilities.

```mermaid
graph TB
    subgraph "User Surfaces"
        CLI[CLI<br/><code>kirocrew chat</code>]
        Slack[Slack DM<br/>Socket Mode]
        Dashboard[Web Dashboard<br/>React SPA]
        Desktop[Desktop App<br/>Electron]
    end

    subgraph "KiroCrew Gateway"
        GW[Gateway Process<br/><i>Python / aiohttp</i>]
    end

    subgraph "Agent Backend"
        KC[kiro-cli<br/>ACP over stdio]
        LLM[LLM Provider<br/><i>Claude via Amazon</i>]
        MCP[MCP Servers<br/><i>tools</i>]
    end

    CLI --> GW
    Slack --> GW
    Dashboard --> GW
    Desktop --> Dashboard

    GW --> KC
    KC --> LLM
    KC --> MCP
```

## Message Flow

A user message travels through the gateway, gets enriched with context (memory,
skills, lessons), is forwarded to kiro-cli, and the streamed response flows back
to the user surface.

```mermaid
sequenceDiagram
    participant User
    participant Surface as Slack / Dashboard / CLI
    participant GW as Gateway
    participant Hooks as HookManager
    participant Session as SessionManager
    participant Context as ContextBuilder
    participant ACP as kiro-cli (ACP)
    participant LLM as LLM

    User->>Surface: sends message
    Surface->>GW: HTTP / WebSocket / Socket Mode
    GW->>Hooks: check auto-replies, transforms, deny
    Hooks-->>GW: pass / block / auto-reply
    GW->>Session: get_or_create(session_key)
    Session-->>GW: kiro-cli process (warm or cold)
    GW->>Context: assemble prompt context
    Note over Context: memory + skills + lessons<br/>+ history + cross-tab
    Context-->>GW: enriched context
    GW->>ACP: JSON-RPC prompt(message + context)
    ACP->>LLM: inference request
    LLM-->>ACP: stream tokens + tool calls
    ACP-->>GW: text_chunk / tool_call / complete events
    GW-->>Surface: stream response back
    GW->>GW: log to ConversationLog (JSONL)
    GW->>GW: trigger memory consolidation (async)
```

## Repository Layout

```mermaid
graph LR
    subgraph "Repository Root"
        direction TB
        SRC["src/kiro_crew/<br/><i>Python backend</i>"]
        WEB["website/<br/><i>React + Vite SPA</i>"]
        ELEC["website/electron/<br/><i>Desktop app shell</i>"]
        AGENTS["agents/<br/><i>Agent config JSON</i>"]
        SKILLS["skills/<br/><i>Skill markdown files</i>"]
        DOCS["docs/<br/><i>Documentation</i>"]
        TEST["test/<br/><i>pytest suite</i>"]
    end

    SRC --> |"serves"| WEB
    ELEC --> |"wraps"| WEB
    SRC --> |"reads"| AGENTS
    SRC --> |"loads"| SKILLS
```

| Directory | Purpose |
|-----------|---------|
| `src/kiro_crew/` | Python backend — gateway, sessions, memory, cron, MCP servers |
| `website/` | React + TypeScript + Tailwind dashboard (Vite 5) |
| `website/electron/` | Electron desktop app wrapper |
| `agents/` | Agent configuration (`defaults.json`, `prompt.md`) |
| `skills/` | Skill definitions (markdown, editable without rebuild) |
| `docs/` | User and developer documentation |
| `test/` | pytest test suite |
| `packages/` | Standalone SDK packages (`kirocrew-client-py`) |

## Backend Component Map

The Python backend (`src/kiro_crew/`) is organized around these core subsystems:

```mermaid
graph TB
    subgraph "Entry Points"
        CLI_MOD[cli.py<br/>argparse CLI]
        SLACK_GW[slack/gateway.py<br/>Socket Mode]
        DASH_SRV[dashboard/server.py<br/>aiohttp]
    end

    subgraph "Session Layer"
        SESS[session.py<br/>Session pool + warm pool]
        ACP_CLIENT[acp/client.py<br/>JSON-RPC 2.0 stdio]
    end

    subgraph "Context & Memory"
        CTX[context.py<br/>Prompt assembly]
        MEM[memory.py<br/>Structured memory + FTS5]
        VEC[vector_memory.py<br/>FAISS semantic search]
        HIST[history.py<br/>JSONL conversation log]
        LEARN[learn.py<br/>Lesson store]
        EMBED[embeddings.py<br/>In-process embeddings]
    end

    subgraph "Orchestration"
        CRON[cron.py<br/>Scheduled jobs]
        TASK[taskrunner.py<br/>Autonomous tasks]
        SUB[subagent.py<br/>Parallel agents]
        NUDGE[autonudge.py<br/>Self-nudge loops]
    end

    subgraph "MCP Servers"
        MCP_CORE[mcp_core.py<br/>spawn, learn, task, wait]
        MCP_CRON[mcp_cron.py<br/>cron tools]
        MCP_DISC[mcp_discovery.py<br/>Server detection]
    end

    subgraph "Security"
        HOOKS[hooks.py<br/>PreToolUse gate]
        SEC[security.py<br/>Denied commands + paths]
        PLAT[platform/<br/>Governance + CPP seam]
    end

    CLI_MOD --> SESS
    SLACK_GW --> SESS
    DASH_SRV --> SESS

    SESS --> ACP_CLIENT
    SESS --> CTX

    CTX --> MEM
    CTX --> VEC
    CTX --> HIST
    CTX --> LEARN

    MEM --> EMBED
    VEC --> EMBED

    SESS --> CRON
    SESS --> TASK
    SESS --> SUB

    HOOKS --> SEC
    HOOKS --> PLAT
    ACP_CLIENT --> HOOKS
```

## Data Flow: Memory Lifecycle

Memory is KiroCrew's persistence layer — it lets new sessions benefit from past
conversations without replaying them.

```mermaid
graph LR
    subgraph "Conversation"
        MSG[User messages]
        RESP[Agent responses]
    end

    subgraph "Immediate Storage"
        JSONL[ConversationLog<br/>JSONL per-session]
    end

    subgraph "Consolidation (async)"
        CONSOL[LLM Consolidator<br/><i>prefs @ 30 msgs<br/>history @ 3h idle</i>]
    end

    subgraph "Structured Memory"
        PREFS[preferences.md<br/><i>user preferences</i>]
        PROJ[projects.md<br/><i>active project context</i>]
        DAILY[history/YYYY-MM-DD.md<br/><i>daily summaries</i>]
        LESSONS[lessons.jsonl<br/><i>learned corrections</i>]
    end

    subgraph "Retrieval"
        FTS[FTS5 full-text]
        VSIM[Vector similarity<br/>FAISS + embeddings]
    end

    MSG --> JSONL
    RESP --> JSONL
    JSONL --> CONSOL
    CONSOL --> PREFS
    CONSOL --> PROJ
    CONSOL --> DAILY
    MSG --> LESSONS

    PREFS --> FTS
    PROJ --> FTS
    DAILY --> FTS
    PREFS --> VSIM
    DAILY --> VSIM
```

**Decay schedule:** 3 days full → 30 days summary → 90 days marker → pruned.

## Session Management

The gateway manages a pool of kiro-cli processes. Each session is an independent
ACP connection with its own context and conversation history.

```mermaid
graph TB
    subgraph "Session Sources"
        S1[Slack thread]
        S2[Dashboard tab]
        S3[Cron job]
        S4[Subagent]
        S5[Task step]
    end

    subgraph "Session Pool"
        WARM[Warm Pool<br/><i>pre-started kiro-cli</i>]
        ACTIVE[Active Sessions<br/><i>keyed by session_key</i>]
    end

    subgraph "kiro-cli Processes"
        P1[kiro-cli acp --agent kirocrew]
        P2[kiro-cli acp --agent reviewer]
        P3[kiro-cli acp --agent ...]
    end

    S1 --> ACTIVE
    S2 --> ACTIVE
    S3 --> ACTIVE
    S4 --> ACTIVE
    S5 --> ACTIVE

    WARM --> |"claim on demand"| ACTIVE
    ACTIVE --> P1
    ACTIVE --> P2
    ACTIVE --> P3
```

- **Warm pool** — pre-spawned sessions for instant response (configurable `session.pool_size`)
- **Idle timeout** — sessions reclaimed after `session.timeout_secs` (default 1800s)
- **Circuit breaker** — 5 consecutive failures → auto-reset
- **Crash recovery** — kiro-cli dies → auto-recreate with state

## Security Architecture

```mermaid
graph TB
    subgraph "Inbound"
        USER_MSG[User Message]
    end

    subgraph "Gateway Security Layers"
        OWNER[Owner Lock<br/><i>KIROCREW_OWNER_ID</i>]
        GOV[Governance<br/><i>POLICY ∩ PROFILE</i>]
        HOOKS_SEC[PreToolUse Hook<br/><i>deny patterns + governance</i>]
        SANDBOX[OS Sandbox<br/><i>namespaces / seatbelt</i>]
        REDACT[Output Redaction<br/><i>credentials scrubbed</i>]
    end

    subgraph "kiro-cli"
        DENIED[deniedCommands<br/><i>137 regex patterns</i>]
        TOOLS[Tool Execution]
    end

    USER_MSG --> OWNER
    OWNER --> GOV
    GOV --> HOOKS_SEC
    HOOKS_SEC --> SANDBOX
    SANDBOX --> TOOLS
    TOOLS --> REDACT
    DENIED --> TOOLS
```

Layers (outer to inner):
1. **Owner lock** — Slack gateway rejects non-owner messages
2. **Governance** — two-level policy ceiling (`POLICY ∩ PROFILE`, tightest-wins)
3. **PreToolUse hook** — denied commands + sensitive path blocking
4. **OS sandbox** — filesystem isolation of credential directories
5. **Denied commands** — 137 regex patterns in kiro-cli config (tamper-resistant)
6. **Output redaction** — credential patterns scrubbed before delivery

## Frontend Architecture

```mermaid
graph TB
    subgraph "Dashboard (React SPA)"
        VITE[Vite 5 build]
        REDUX[Redux Toolkit<br/>state management]
        ROUTER[React Router<br/>page routing]
        SSE_CLIENT[SSE Client<br/>live updates]
        WS_CLIENT[WebSocket<br/>STT streaming]
    end

    subgraph "Gateway API"
        REST[REST API<br/>/api/*]
        SSE_SRV[SSE endpoint<br/>/api/events]
        WS_SRV[WebSocket<br/>/api/stt]
        STATIC[Static files<br/>/dist/*]
    end

    VITE --> STATIC
    SSE_CLIENT --> SSE_SRV
    WS_CLIENT --> WS_SRV
    REDUX --> REST
```

- **Build:** Vite 5, React 18, TypeScript, Tailwind CSS
- **State:** Redux Toolkit with SSE-driven updates
- **Bundled:** production build staged into `src/kiro_crew/static/dist/` and served by the Python backend
- **Desktop:** Electron wraps the same SPA with multi-tab WebContentsView

## External Dependencies

```mermaid
graph LR
    subgraph "KiroCrew (local)"
        GW2[Gateway]
    end

    subgraph "Required"
        KIRO[kiro-cli<br/><i>agent runtime</i>]
        LLM2[LLM Provider<br/><i>via kiro-cli auth</i>]
    end

    subgraph "Optional"
        OLLAMA[Ollama<br/><i>local embeddings</i>]
        SLACK_API[Slack API<br/><i>Socket Mode</i>]
        MCP_EXT[External MCP Servers<br/><i>user-configured</i>]
        AWS[AWS<br/><i>artifact deploy, STT</i>]
    end

    GW2 --> KIRO
    KIRO --> LLM2
    GW2 -.-> OLLAMA
    GW2 -.-> SLACK_API
    KIRO -.-> MCP_EXT
    GW2 -.-> AWS
```

| Dependency | Required? | Purpose |
|-----------|-----------|---------|
| **kiro-cli** | Yes | Agent runtime — LLM inference + tool execution |
| **LLM Provider** | Yes | Claude (via kiro-cli's authenticated connection) |
| **Ollama** | No | Local embeddings for memory/knowledge search (bundled runtime available as fallback) |
| **Slack** | No | Slack bot gateway (dashboard works without it) |
| **AWS** | No | Artifact deploy, optional cloud STT |
| **External MCP servers** | No | Additional tools (user-configured) |

## Data Home

All persistent state lives under `~/.kiro/crew/` (or `KIROCREW_HOME`):

```
~/.kiro/crew/
├── config.json          # user configuration
├── .env                 # Slack tokens, owner ID
├── workspace/
│   ├── memory/          # preferences.md, projects.md, history/
│   ├── lessons.jsonl    # learned corrections
│   └── knowledge/       # ingested documents (FTS5 + vectors)
├── conversations/       # JSONL session logs
├── crons.json           # scheduled jobs
├── instances.json       # remote instance registry
├── hooks.json           # webhook workflow context
├── audit.log            # bash command audit trail
└── agents/              # generated kiro-cli agent configs
```

## How It All Fits Together

```mermaid
graph TB
    subgraph "User"
        U[You]
    end

    subgraph "Surfaces"
        DASH[Dashboard :5476]
        SL[Slack DM]
        TERM[CLI]
    end

    subgraph "Gateway (Python)"
        ENTRY[Entry Points]
        SESSION[Session Pool]
        MEMORY[Memory + Context]
        ORCH[Orchestration<br/><i>cron, tasks, subagents</i>]
        SECURITY[Security Layers]
    end

    subgraph "Agent (kiro-cli)"
        AGENT[Agent Process]
        TOOL_EXEC[Tool Execution]
        MCP_TOOLS[MCP Tools]
    end

    subgraph "Persistence"
        DISK[~/.kiro/crew/<br/><i>config, memory, logs</i>]
    end

    subgraph "Cloud"
        CLOUD_LLM[LLM Provider]
    end

    U --> DASH
    U --> SL
    U --> TERM

    DASH --> ENTRY
    SL --> ENTRY
    TERM --> ENTRY

    ENTRY --> SESSION
    SESSION --> MEMORY
    SESSION --> SECURITY
    SECURITY --> AGENT
    AGENT --> TOOL_EXEC
    TOOL_EXEC --> MCP_TOOLS
    AGENT --> CLOUD_LLM

    MEMORY --> DISK
    ORCH --> SESSION
    SESSION --> DISK
```

---

## Further Reading

- [features.md](features.md) — complete feature list
- [getting-started.md](getting-started.md) — installation and first steps
- [security-deep-dive.md](security-deep-dive.md) — security model in depth
- [memory-architecture.md](memory-architecture.md) — memory system design
- [mcp-architecture.md](mcp-architecture.md) — MCP server management
- [design-doc-kirocrew.md](design-doc-kirocrew.md) — historical design narrative
