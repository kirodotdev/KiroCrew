# KiroCrew Design Document

> **Historical design snapshot (drift-reviewed 2026-07-13).** This is a point-in-time architectural narrative — several "Future Directions" ideas have since shipped and some numbers below are illustrative of that era. For authoritative current behavior, see `docs/system-specs/`.

## 0. Background: kiro-cli, AIM, and Why KiroCrew Exists

### The Stack

To understand KiroCrew, you need to understand the three layers beneath it:

1. **kiro-cli** is an agent runtime, not an agent itself. It provides: an LLM connection (via Amazon's backend), tool execution (bash, file read/write, grep, glob), MCP server management, session persistence, context compaction, and the ACP (Agent Client Protocol) — a JSON-RPC 2.0 stdio interface that any editor or orchestrator can drive. Think of it as "the engine."

2. **Agent configs** (JSON files in `~/.kiro/agents/`) tell kiro-cli *how* to behave: which system prompt to use, which tools to enable, which MCP servers to load, which commands to deny. Every agent — `kirocrew`, `reviewer`, `Customer360GenAIContext` — runs as `kiro-cli acp --agent <name>`. The `--agent` flag selects the config; the runtime is always kiro-cli.

3. **AIM** (Agent Install Manager) is a package manager for agents, skills, and MCP servers. `aim agent install <package>` drops a JSON config into `~/.kiro/agents/`. AIM handles distribution; it doesn't run anything.

### What KiroCrew Adds Over kiro-cli Alone

| Capability | kiro-cli alone | With KiroCrew |
|------------|---------------|---------------|
| Sessions | 1 per terminal | Unlimited concurrent (Slack threads, dashboard tabs, cron jobs, sub-agents) |
| Interfaces | Terminal only | CLI + Slack DM + Web Dashboard (React SPA) |
| Persistence | SQLite per-directory | Cross-session memory (preferences, projects, daily history, lessons) |
| Cross-session awareness | None | Session B sees what session A learned via shared memory + consolidation |
| Scheduling | None | Cron jobs with 3 schedule types, cross-process file locking |
| Autonomous tasks | None | TaskRunner: spec → decompose → parallel execute → retry → replan → checkpoint |
| Self-learning | None | Lesson extraction from corrections and failures, injected into all future sessions |
| Security enforcement | Per-agent `deniedCommands` | Tamper-resistant: bundled deny patterns injected into ALL agent configs, periodic re-enforcement |
| Context management | Manual `/compact` | Automatic compaction at configurable threshold (default 90%), budget-aware context assembly, decaying memory |
| Process resilience | Manual restart | Warm pool, circuit breaker, crash recovery, idle cleanup, orphan PID tracking |

### Why Orchestrate Agents Together?

**1. Automation without manual intervention.** Cron jobs, TaskRunner specs, and subagents all run without a human typing commands.

**2. Complicated tasks that benefit from multiple specialized agents.** Dashboard tabs let you chat with different agents simultaneously. The TaskRunner can run a full task with a specific agent. Cron jobs can use per-job agents.

**3. The system gets smarter over time.** Every conversation feeds into shared memory. Lessons from corrections and failures persist across all future sessions. A new session on Monday already knows what you worked on Friday.

### The Agent Hierarchy: kirocrew as Coordinator

`kirocrew` is itself just an agent config — same level as any other. What makes it the coordinator is that the gateway defaults to it and it has MCP tools (`spawn_run`, `cron_add`, `task_run`) that trigger the gateway to spawn sessions with other agents.

```
KiroCrew Gateway
  ├── Slack DM / CLI chat              → always kirocrew agent
  ├── Dashboard tab                    → user picks any agent from dropdown
  ├── Cron job                         → per-job agent_id (kirocrew if unset)
  ├── Subagent (spawn_run)             → kirocrew (inherits parent)
  └── TaskRunner (task_run)            → user specifies any agent at start
```

The gateway is agent-agnostic; the coordination behavior comes from the agent config, not from the gateway code.

---

## 1. Architecture at a Glance

### 1.1 Data Flow (Message Path)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  User Message (CLI / Slack DM / Dashboard)                               │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
                  ┌─────────────────────┐
                  │    HookManager      │  auto-reply / transform / inject context / deny
                  └──────────┬──────────┘
                             ▼
                  ┌─────────────────────┐
                  │   SessionManager    │  get_or_create(key, agent)
                  │   (kiro-cli pool)   │  warm pool → or cold start
                  └──────────┬──────────┘
                             ▼
                  ┌─────────────────────┐
                  │   ContextBuilder    │  Assembles prompt from:
                  │                     │  ┌─ agent prompt (prompt.md)
                  │                     │  ├─ memory (prefs, projects, history)
                  │                     │  ├─ always-on skills (full content)
                  │                     │  ├─ triggered skills (keyword match)
                  │                     │  ├─ lessons (corrections)
                  │                     │  ├─ conversation history (last 20 msgs)
                  │                     │  ├─ cross-tab context (other dashboard tabs)
                  │                     │  └─ episodic memory (vector similarity)
                  └──────────┬──────────┘
                             ▼
                  ┌─────────────────────┐
                  │  kiro-cli acp       │  JSON-RPC 2.0 over stdio
                  │  --agent <name>     │  → LLM inference
                  │                     │  → tool execution (bash, fs, MCP)
                  │                     │  yields: text_chunk, tool_call,
                  │                     │          permission_request, complete
                  └──────────┬──────────┘
                             ▼
            ┌────────────────┼────────────────┐
            ▼                ▼                 ▼
   ┌──────────────┐  ┌────────────┐  ┌───────────────────┐
   │ Response to   │  │ ConvLog    │  │ Consolidator      │
   │ User (stream) │  │ .append()  │  │ (prefs @ 30 msgs, │
   └──────────────┘  └────────────┘  │  history @ 3h idle)│
                                     └────────┬──────────┘
                                              ▼
                                     ┌──────────────────┐
                                     │  Shared Memory   │
                                     │  preferences.md  │
                                     │  projects.md     │
                                     │  history/*.md    │
                                     │  lessons.jsonl   │
                                     │  semantic/episodic│
                                     └──────────────────┘
```

### 1.2 Component Interaction Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        KiroCrew Gateway (single asyncio process)        │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                              │
│  │ Slack    │  │Dashboard │  │   CLI    │   ← User Interfaces           │
│  │ Socket   │  │ aiohttp  │  │ argparse │                               │
│  │ Mode     │  │ :5476    │  │          │                               │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                              │
│       └──────────────┼─────────────┘                                    │
│                      ▼                                                  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    SessionManager                                 │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │  │
│  │  │kiro-cli │  │kiro-cli │  │kiro-cli │  │kiro-cli │  ...       │  │
│  │  │ chat    │  │ cron    │  │ task    │  │ subagent│            │  │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │  │
│  │  warm pool ──→ idle cleanup ──→ circuit breaker ──→ compaction   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │ CronService  │  │ TaskRunner   │  │ Subagent     │  ← Orchestration │
│  │ crons.json   │  │ spec→steps   │  │ Manager      │                  │
│  │ fcntl lock   │  │ parallel exec│  │ max 3        │                  │
│  └──────────────┘  └──────────────┘  └──────────────┘                  │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │ContextBuilder│  │ MemoryStore  │  │ SkillsLoader │  ← Context       │
│  │ assembles    │  │ prefs/proj/  │  │ SKILL.md +   │                  │
│  │ prompt       │  │ history/FTS  │  │ scripts      │                  │
│  └──────────────┘  └──────────────┘  └──────────────┘                  │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │ HookManager  │  │ LessonStore  │  │ Heartbeat    │  ← Support       │
│  │ msg/tool     │  │ corrections  │  │ 60s tick     │                  │
│  │ intercept    │  │ JSONL/vector │  │ FTS/prune    │                  │
│  └──────────────┘  └──────────────┘  └──────────────┘                  │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐                                    │
│  │ Validation   │  │ SEL          │                    ← Security       │
│  │ schema/sanitize│ │ HMAC-chain  │                                    │
│  │ MCP in/out   │  │ audit trail  │                                    │
│  └──────────────┘  └──────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.3 Cross-Session Memory Flow

```
Session A (Slack)          Session B (Dashboard)         Session C (Cron)
     │                          │                             │
     │ conversation             │ conversation                │ conversation
     ▼                          ▼                             ▼
┌──────────┐             ┌──────────┐                  ┌──────────┐
│ JSONL    │             │ JSONL    │                  │ JSONL    │
│ history  │             │ history  │                  │ history  │
└────┬─────┘             └────┬─────┘                  └────┬─────┘
     │                        │                              │
     └────────────┬───────────┘──────────────────────────────┘
                  ▼
     ┌────────────────────────┐
     │  HistoryConsolidator   │  (background LLM call via _bg session)
     │  prefs path: 30 msgs  │
     │  history path: 3h idle │
     └────────────┬───────────┘
                  ▼
     ┌────────────────────────┐
     │    Shared Memory       │
     │  ┌──────────────────┐  │
     │  │ preferences.md   │──┼──→ injected into ALL new sessions
     │  │ projects.md      │  │
     │  │ history/daily.md │  │    (decaying: 3d full → 30d summary → 90d marker → pruned)
     │  │ lessons.jsonl    │  │
     │  │ semantic (vector)│  │
     │  │ episodic (vector)│──┼──→ similarity search on every message
     │  └──────────────────┘  │
     └────────────────────────┘
```

---

## 2. Session Control

### 2.1 Session Model

Every conversation thread gets its own `kiro-cli` child process, identified by a string key:

| Caller | Key Pattern | Lifetime |
|--------|-------------|----------|
| Slack thread | `{thread_ts}` | Idle timeout (60 min) |
| Dashboard tab | `dashboard:{slot_key}` | Idle timeout (60 min) |
| Cron job | `cron:{job_id}` | One-shot (reset after execution) |
| Sub-agent | `subagent:{uuid}` | Task duration |
| TaskRunner main | `taskrunner:{task_id}` | Task duration |
| TaskRunner parallel step | `taskrunner:step{N}` | Step duration |
| Background | `_bg` | Entire runtime (recycled at 70% context) |

Key behaviors:

- **Warm pool**: Pre-spawns kiro-cli processes at startup for instant first response. Custom agents skip the warm pool (cold start only).
- **Per-session semaphore**: Serializes concurrent messages on the same thread key.
- **Context compaction**: At ≥ configured threshold (`session.autocompact_pct`, default 90%, valid 5–90), fires `/compact` to kiro-cli. Context re-injected on next message.
- **Circuit breaker**: 5 consecutive failures → force-reset.
- **Idle cleanup**: 60 min inactivity → session expired. Background session never expires.

### 2.2 ACP Protocol Handshake

Each kiro-cli session goes through a 5-step init before accepting prompts:

```
initialize → session/new → set_mode (kirocrew only) → set_model (kirocrew only) → drain MCP notifications (10s) → ready
```

Custom agents skip `set_mode` and `set_model` — the `--agent` flag already configured them at spawn time. Timeout: 120s with one retry on failure.

### 2.3 Workspace Isolation

Each session gets its own working directory under `workspace_root()`:

| Session | Working Directory |
|---------|-------------------|
| CLI chat | `kirocrew-workspace/cli_chat` |
| Slack thread | `kirocrew-workspace/{thread_ts}` |
| Background | `kirocrew-workspace/_bg` |
| Cron job | `kirocrew-workspace/cron_{job_id}` |
| TaskRunner | `kirocrew-workspace/taskrunner_main` |

Platform defaults: `/Volumes/workplace/kirocrew-workspace` (macOS), `~/workplace/kirocrew-workspace` (Linux).

### 2.4 Cross-Session Communication

Sessions are isolated processes, but KiroCrew bridges them:

- **Shared memory files** (preferences.md, projects.md, daily history) — read at session start, written by consolidator
- **Conversation history JSONL** — cross-tab injection (dashboard sessions see other tabs, capped at 2K chars)
- **LLM-driven consolidation** — two paths: prefs/projects every ~30 messages, daily history + lessons after 3h idle
- **Lessons** — four write sources: `learn_add` MCP tool, TaskRunner failure extraction, consolidation, manual entry
- **Channel history** — per-channel rolling buffer (50 msgs, 5-min TTL) for Slack group channel context

### 2.5 Context Budget

| Component | Budget |
|-----------|--------|
| Preferences | 4,000 chars |
| Projects | 6,000 chars |
| Daily history (decaying) | 25,000 chars |
| Lessons | 35,000 chars |
| Conversation history | 45,000 chars |
| Cross-tab context | 6,000 chars |
| Episodic memory (vector) | 12,000 chars |
| Semantic memory (vector) | 12,000 chars |
| Per-message truncation | 1,500 chars |
| Hard total cap | 165,000 chars (~55k tokens) |

The figures above are illustrative point-in-time values from a single flat 165k-char pool. The current model (`context.py`) instead gives each section its OWN independent percentage cap and derives the global ceiling (`_MAX_CONTEXT_CHARS`) as the SUM of those caps so sections never truncate each other; the `_CONTEXT_BUDGET_BASE` of 165k is retained only as the base the percentages are taken from.

Custom agents skip KiroCrew-specific context (skills, memory, lessons) — only conversation history is injected.

---

## 3. Task Handling

### 3.1 End-to-End Lifecycle

```
User input (rough idea or spec file)
  │
  ▼
Dynamic Refine (optional — dashboard "✨ Compose" tab)
  │  LLM rewrites rough input into structured spec via multi-turn Q&A
  │
  ▼
LLM Decomposition → JSON array of steps with dependency graph
  │  Dedicated throwaway session (killed after parsing)
  │
  ▼
Plan Review (optional — dashboard "📋 Plan" view)
  │  User sees steps with dependency grouping (parallel vs sequential)
  │  Can edit titles/descriptions inline, delete steps
  │  Can send plan to Chat for iterative optimization ↔ bring back updated steps
  │
  ▼
Parallel Grouping (topological sort by depends_on)
  │
  ▼
Step Execution (groups run sequentially; steps within a group run in parallel)
  │  Each step gets its own session taskrunner:{task_id}:step{N}
  │  Full execution plan injected into each step's prompt (strict following)
  │
  ▼
Per-Step: tool approval → LLM streaming → self-review → test → working memory → checkpoint
  │
  ▼
On Failure: retry (3 logic) → crash recovery (2 process) → re-plan (2 attempts)
  │
  ▼
Completion: lesson extraction → history consolidation → notification
```

### 3.2 Decomposition and Parallel Execution

The LLM returns a structured JSON object with steps and acceptance criteria:

```json
{
  "steps": [
    {"title": "Create data model",     "depends_on": []},
    {"title": "Create API handler",    "depends_on": []},
    {"title": "Create CLI interface",  "depends_on": []},
    {"title": "Add integration tests", "depends_on": [1, 2, 3]},
    {"title": "Deploy to staging",     "depends_on": [4], "requires_approval": true}
  ],
  "acceptance_criteria": [
    "Data model supports all required fields with validation",
    "API handler returns correct status codes for all endpoints",
    "Integration tests cover happy path and error cases"
  ]
}
```

The `acceptance_criteria` are specific, testable conditions generated by the LLM from the
spec and user input. They appear as a bulleted list in the final Acceptance Check step
description, which is included in the plan JSON so users can view and edit them.
When criteria are not returned (e.g. plain JSON array), the acceptance step falls back
to a checklist derived from step titles.

`_group_parallel_steps()` produces execution groups:

```
  Step 1: depends_on=[]     ─┐
  Step 2: depends_on=[]     ─┤── Group 1 (parallel: 3 kiro-cli processes)
  Step 3: depends_on=[]     ─┘
  Step 4: depends_on=[1,2,3] ── Group 2 (sequential: reuses main session)
  Step 5: depends_on=[4]     ── Group 3 (sequential: reuses main session)
```

Parallel steps each get their own isolated kiro-cli session, dispatched via `asyncio.gather()`. Sessions cleaned up after each group.

### 3.3 How Steps Get Domain-Specific Context

kirocrew's system prompt is thin (orchestration rules only). Step-specific expertise comes from dynamic context injection:

```
  _build_step_prompt(run, step, attempt)     ← working memory + progress + step description
       │
       ▼
  build_message(step_prompt, is_new, key)    ← triggered skills + episodic memory + hooks
       │
       ▼
  LLMProvider.stream(full_prompt)            ← sent to kiro-cli with all context assembled
```

- **Triggered skills**: Step text scanned against skill trigger keywords. "Create a new project" triggers a matching skill (full content injected).
- **Episodic memory**: Vector similarity search pulls relevant past fragments.
- **Working memory**: Files changed, decisions, blockers from prior steps — survives context compaction.

### 3.4 Ad-Hoc vs Repeatable: The Spec and Plan as Contracts

The user defines *what* to achieve; the LLM decides *how*. The spec is optional — users can go directly from raw text to a plan. The **plan** (visible, editable steps with dependency graph) is the real contract.

- **Three entry points to a plan**: Raw text → decompose directly. Refined spec → decompose. File path → read → decompose. All produce the same output: a list of steps with `depends_on` relationships.
- **Plan as the editable contract**: Users see steps before execution, can edit titles/descriptions inline, delete steps, and send the plan to Chat for iterative optimization with the LLM. The plan round-trips between the Task view and Chat until the user is satisfied.
- **Spec precision drives stability**: Vague inputs produce varying plans. Precise specs with file paths, commands, and acceptance criteria produce consistent decompositions. But now users can fix the plan directly instead of re-running decomposition.
- **Cron jobs for recurring single-step tasks**: Fixed prompt, no decomposition, runs on schedule.
- **Inherent variance**: Same input can produce different step counts across runs. But Plan Mode lets users lock in a specific plan before execution — the saved plan is deterministic even if decomposition isn't.

### 3.5 From Ad-Hoc to Production: Graduating Tasks

Natural progression: prototype with TaskRunner, graduate to a deterministic script when the workflow stabilizes.

After a successful task run, you have:

| Artifact | What It Contains |
|----------|-----------------|
| `TASK_PROGRESS.md` | Step titles, status, attempt counts |
| `runs.json` | Full run metadata with step results (2K each) |
| Conversation JSONL | Step-by-step transcript |
| Lessons | Corrections extracted from failures |

The pattern:
1. Run ad-hoc with kirocrew (1-2 times) — observe which steps, tools, and skills activate
2. Extract the stable pattern — identify deterministic steps vs LLM-judgment steps
3. Build a script — mechanical steps become shell commands, judgment steps become targeted `kiro-cli chat --no-interactive --agent <name> "prompt"` calls

Example: "update documentation" graduates to a bash script where `git diff` and `pytest` are deterministic, and only "summarize changes" is an LLM call.

### 3.6 Execution Resilience

- **Logic retries** (3): Error fed back to LLM on retry.
- **Crash recoveries** (2): Separate budget from logic retries. Partial output injected as recovery context.
- **Re-planning** (2): On exhausted retries, LLM generates revised plan from completed steps + failure.
- **Watchdog**: 60 min stall → warning. 2 hours → session reset. Global timeout and token budget configurable.
- **Checkpoint resume**: `TASK_PROGRESS.md` written after every step. Restart picks up from first pending step.

### 3.7 Subagents: Parallelism Across Independent Tasks

For parallelism across tasks (not steps within a task), `SubagentManager` spawns isolated sessions:

- Default 3 concurrent, configurable via `agent.max_subagents` (0 = auto-size from host resources, ceiling 64); no recursion (subagents can't spawn subagents)
- Two modes: wait (blocks until result) and fire-and-forget
- Sessions are one-shot: released and reset in `finally`

### 3.8 Agent Selection Granularity

| Scope | Agent Selection | How |
|-------|----------------|-----|
| Dashboard tab | Any agent | User picks from dropdown |
| Cron job | Any agent | `agent_id` field per job |
| TaskRunner | One agent for entire task | Set at start time |
| Subagent | Always kirocrew | No `agent` parameter |

Per-step agent selection is a natural extension point — `get_or_create()` already accepts an `agent` parameter, it's just not wired through the step schema yet.

**Workaround**: Skills can wrap agent calls via `kiro-cli chat --no-interactive --trust-all-tools --agent <name> "task"`. No streaming visibility or warm pool, but works today.

### 3.9 Plan Mode: Visible, Editable Execution Plans

Previously, decomposition was invisible — the LLM generated steps and execution started immediately. Plan Mode adds a `"planned"` state between decomposition and execution, giving users full visibility and control over the execution plan.

**The flow:**

```
User input ──┬── [📋 Plan] ──────────────────────────→ Plan View
             │                                            │
             ├── [✨ Refine] → Spec ── [📋 Plan] ──────→ │
             │                                            │
             └── File path ── [📋 Plan] ──────────────→  │
                                                          │
                                              ┌───────────┤
                                              │           │
                                         [💬 Chat]  [▶ Execute]
                                              │           │
                                              ▼           ▼
                                         Chat slot    _execute_steps()
                                         (optimize)
                                              │
                                         [📋 Use as Plan]
                                              │
                                              └──→ Plan View (updated)
```

**Key design decisions:**

| Decision | Choice | Why |
|----------|--------|-----|
| Spec optional | Users can go text → plan directly | Spec adds value but shouldn't be mandatory. The plan is the real contract. |
| Plan ↔ Chat bidirectional | Serialize plan to chat, parse updated steps back | LLM is better at restructuring plans than a UI editor. Chat is the natural optimization interface. |
| Full plan in step prompt | All step titles, descriptions, deps, and position marker injected | Agent must follow the plan strictly, not invent new work. Original context also injected. |
| Planned runs persist | `runs.json` includes `status="planned"` | Plans survive dashboard restart. User can return to a plan later. |

**What gets injected into each step's execution prompt:**

```
## Full Execution Plan (follow strictly)
Execute ONLY what the current step describes. Do not skip ahead or combine steps.

### Group 1 (parallel)
✅ 1. Create API endpoint
   Add /health route returning uptime and version
🔄 2. Add unit tests ← YOU ARE HERE
   Test /health returns 200 with expected JSON fields

### Group 2
⬜ 3. Wire routes (depends on: 1, 2)
   Register route in app.py and update OpenAPI spec

## Original Context
<user's original input or spec that produced this plan>
```

**API surface:**

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/taskrunner/plan` | Decompose input → planned run |
| PUT | `/api/taskrunner/{id}/plan` | Update steps on planned run |
| POST | `/api/taskrunner/{id}/execute` | Execute a planned run |
| POST | `/api/taskrunner/{id}/to-chat` | Serialize plan → chat (enhanced) |
| POST | `/api/taskrunner/from-chat` | Parse steps from chat → planned run |

**Data model additions to `TaskRun`:**
- `original_input: str` — raw user text that produced the plan
- `source: str` — `"text"`, `"spec"`, `"file"`, or `"chat"`
- `status: "planned"` — new state between `"pending"` and `"running"`

**Dependency visualization in the dashboard:**
- Steps grouped by `_group_parallel_steps()` into sequential groups
- Parallel steps within a group shown with `∥` separator
- Groups connected by `→` arrows
- Example: `[Step 1 ∥ Step 2] → [Step 3] → [Step 4 ∥ Step 5]`

**Chat ↔ Task round-trip mechanism:**

The round-trip uses a hidden HTML comment `<!-- plan_task_id:xxx -->` embedded in the initial chat message as the identity link between chat and plan.

1. **Plan → Chat**: `plan_to_chat_context()` serializes the plan as markdown with a JSON code block containing the steps array, plus the hidden task ID comment. The instruction tells the agent to respond naturally but always include the complete updated steps as a `` ```json `` block at the end.
2. **Chat optimization**: The user discusses changes with the LLM. The LLM responds naturally (explaining reasoning, asking questions) and includes the updated JSON steps block at the end of its response.
3. **Chat → Plan**: The `AssistantMessage` component detects when a message contains a valid JSON steps array (parseable, array, items have `title` field) AND the conversation has a `plan_task_id` marker. When both conditions are met, an "📋 Apply to Tasks (N steps)" button appears below that specific message. Clicking it calls `POST /api/taskrunner/from-chat` to update the plan and navigates to the Tasks page. The plan is NOT executed — the user decides when to execute.

The button appears per-message, not globally. If the user asks for multiple revisions, each assistant response with valid steps gets its own button. Only the one the user clicks gets applied.

**Dependency visualization:**

Each step shows its blocking dependencies with step titles (not just indices): `⏳ blocked until done: Step 1 (Create API endpoint), Step 2 (Add tests)`. Groups are labeled with execution mode: `⚡ Group 1 — 2 steps run in parallel` or `→ Group 2 — runs sequentially after group 1`. Groups are connected by `↓ then` arrows.

### 3.10 Post-Completion Acceptance Check

After all steps pass, the task was previously marked `"completed"` unconditionally. Per-step self-reviews only verify individual step correctness, not end-to-end spec satisfaction. The acceptance check closes this gap.

**How it works:** The acceptance check is a visible step in the plan, not a hidden post-processing loop. `_decompose()` generates acceptance criteria as part of the plan, and `_append_acceptance_step()` adds an "Acceptance Check" step with those criteria as its description. When this step executes, `_run_acceptance_review()` asks an LLM reviewer to evaluate the original specification criterion by criterion:

1. **Enumerate** every acceptance criterion from the spec
2. **Check** each criterion against the executed steps and current state
3. **Identify what is missing** to meet each unsatisfied criterion — this framing directly inspires actionable remediation steps
4. If all criteria met: return `{"done": true, "evidence": [...]}` with per-criterion evidence of satisfaction
5. If gaps found: return `{"done": false, "remaining": [...]}` with fix steps tied to specific unmet criteria

The reviewer sees the full spec, all step results (✅/❌ with errors), and the current git state. Remediation steps are appended to the run along with a new acceptance step whose criteria reflect the identified gaps. The new steps execute naturally via `_execute_steps` — no hidden loop.

**User control:** The acceptance step and its criteria are included in the plan JSON shown in chat. Users can edit, refine, or remove the criteria. `update_plan()` preserves whatever the user submits; `_append_acceptance_step()` only adds a fallback if the user removed the step entirely.

**Where it runs:** All modes — `plan()`, `run()` (CLI `task_run`), and `execute_plan()`.

**Guard rails:**
- Hard limit of 3 remediation rounds (`_MAX_ACCEPTANCE_ROUNDS`)
- Respects the 50-step total cap (`_MAX_TOTAL_STEPS`)
- Exceptions in the check itself are non-fatal (treated as passed)
- `_build_step_prompt` has defensive assert against acceptance steps

**Remediation steps appear in the UI** as additional steps appended to the plan, with `acceptance_rounds` shown in the RunCard.

### 3.11 Plan Mode UX

**Planning progress:** Clicking "Plan" triggers an async decomposition call. A pulsing banner (`⏳ Generating execution plan…`) appears below the button in both Compose and File modes while planning is in progress.

**Optimize in Chat:** Instead of auto-submitting the plan to the agent (which gave the agent no instruction on what to do), "💬 Optimize in Chat" now fetches the plan context via `GET /api/taskrunner/{id}/plan-context` and pre-fills the chat input box with the plan text prefixed by `"Let's optimize this plan first without execution."` The user reviews and sends manually. This uses a `pendingInput` Redux state in `chatSlice` that `ChatPage` consumes on mount.

---

## 4. Skill and MCP Detection

### 4.1 Skills: Install → Discover → Inject → Load

Skills are directories containing a `SKILL.md` instruction file plus optional scripts and reference docs. The LLM reads the instructions and uses `execute_bash` to run scripts — skills are knowledge + tooling, the LLM is the executor.

```
my-skill/
├── SKILL.md              # Instructions (YAML frontmatter + markdown)
├── run-analysis.sh       # Script the LLM executes via bash
└── references/
    └── checklist.md      # Reference doc the LLM can cat
```

**Installation sources:**

| Source | Location |
|--------|----------|
| AIM package | `~/.aim/<package>/skills/` |
| Project-level | `$KIROCREW_PROJECT_DIR/skills/` |
| User-created (dashboard or manual) | `~/.kirocrew/skills/` |

**Context injection:**

| Type | When | How |
|------|------|-----|
| Always-on (`always: true`) | Every new session | Full content in `[Skills:]` block |
| On-demand (default) | When LLM decides | Summary in context; LLM `cat`s the file |
| Triggered (`triggers: kw1,kw2`) | When message matches keyword | Full content injected for that message |

**Two skill systems**: KiroCrew's `SkillsLoader` (injects via prompt text for kirocrew agent) and kiro-cli native (`skill://` URIs in agent config `resources` for custom agents). AIM packages bridge both.

### 4.2 MCP Lifecycle: Discover → Sync → Probe

```
Discover (config diff)  →  Sync (register)  →  Probe (verify + list tools)
   "what's new?"           "add it"             "does it actually work?"
```

**Discover** (`discover_new_servers()`): Compares `~/.kiro/settings/mcp.json` against the kirocrew agent config. Returns servers not yet registered. Config-level only — no processes spawned.

**Sync** (`sync_to_agent_config()`): Registers discovered servers via `kiro-cli mcp add` in parallel. Patches `tools`/`allowedTools`. Atomic write.

**Probe** (`probe_server()`): Spawns each MCP server, sends JSON-RPC `initialize` + `tools/list`, collects tool names, kills process. 30s timeout. Results cached 10 min.

**Dashboard workflow**: Probe All → enable/disable servers and tools → Apply & Restart Sessions.

**Per-agent MCP scoping**: `kiro-cli acp --agent <name>` loads only the `mcpServers` defined in that agent's config. The kirocrew agent loads from global `~/.kiro/settings/mcp.json` (managed by the dashboard MCP tab). Custom agents (AIM-installed) load only their own MCPs — verified experimentally with 1–8 server variance across agents.

**Skills have no probe** — just filesystem scanning. If the file exists and parses, it's valid.

### 4.3 MCP-First Rule

New LLM-facing capabilities must be MCP tools (not just CLI commands). kiro-cli reliably calls MCP tools but may refuse bash commands. CLI commands remain for human use. All MCP tool inputs are validated against declarative schemas before execution (§6.6), and all invocations are logged to the Security Event Log (§6.7).

---

## 5. Agent and Skill Authoring

### 5.1 kirocrew Is an Orchestrator, Not a Coding Specialist

kirocrew has full coding tools (`fs_write`, `execute_bash`, `fs_read`, `grep`, `glob`) but its prompt is optimized for orchestration. For serious coding, create a dedicated agent with a focused prompt, coding skills, and appropriate tool constraints.

### 5.2 Agent Lifecycle

**Installing**: AIM dashboard/CLI, `kiro-cli agent create`, or manual JSON file drop into `~/.kiro/agents/`. KiroCrew discovers agents by scanning that directory on every API request.

**Creating** — minimal config:

```json
{
  "name": "my-reviewer",
  "prompt": "You are a senior code reviewer. Focus on security and test coverage.",
  "model": "claude-sonnet-4",
  "tools": ["read", "grep", "glob"],
  "allowedTools": ["read", "grep", "glob"]
}
```

**Customizing** — extend the JSON with:
- `mcpServers`: MCP server access (note: in ACP mode, global config overrides per-agent)
- `resources`: Skills via `skill://` URIs, files via `file://` URIs
- `hooks`: Lifecycle commands (`preToolUse`, `postToolUse`, `agentSpawn`, `stop`)

**Updating**: Dashboard AIM panel (per-agent or bulk), re-install via AIM CLI, or edit JSON directly. kirocrew agent has a dashboard JSON editor with auto-restart.

### 5.3 Skill Authoring

**For kirocrew**: Create in `~/.kirocrew/skills/` (dashboard or manual). SKILL.md with optional YAML frontmatter (`name`, `description`, `triggers`, `always`). Can include scripts — LLM sees the `dir` path and uses `execute_bash` to run them.

**For custom agents**: Place in `~/.kiro/skills/` (global) or `.kiro/skills/` (workspace). Custom agents need explicit `resources` entries. Default kiro-cli agent loads both automatically.

**Sharing across both systems**: AIM packages handle this automatically. Manual: symlink or place in both locations.

### 5.4 MCP Server Authoring

Implement JSON-RPC 2.0 over stdio (`initialize`, `tools/list`, tool calls). Register in `~/.kiro/settings/mcp.json`. Probe from dashboard to verify. Available to ALL agents in ACP mode.

---

## 6. Security and Operations

### 6.1 Defense in Depth

- **Regex deny patterns** in `deniedCommands` (illustrative count at the time of writing: ~54; the bundled set has since expanded to cover protected branches and dangerous push flags like `--mirror`/`--all`) — injected into ALL agent configs at startup, periodically (~60s), and at install. Tamper-resistant: always sourced from bundled package.
- **Python-level hook deny list** — blocks credential/destructive tool patterns.
- **MCP tool input validation** — centralized schema-based validation (`validation.py`) for all MCP tool inputs before execution. Type enforcement, length limits, Unicode normalization, hidden character stripping, allow-list enums, regex patterns for IDs, numeric range checks, and unknown field rejection. See §6.6.
- **MCP response sanitization** — all tool responses pass through `build_tool_response()` which sanitizes and truncates at 100K chars to prevent resource exhaustion.
- **JSON-RPC envelope validation** — request and response envelopes validated for structure before processing/writing.
- **Security Event Log (SEL)** — immutable, HMAC-chained audit trail for every tool invocation, permission decision, and API mutation across all 8 surfaces. See §6.7.
- **Audit logging** — every bash command logged to `~/.kirocrew/audit.log` via kiro-cli `preToolUse` hook.
- **Owner lock** — Slack gateway processes only `KIROCREW_OWNER_ID` messages.
- **Dashboard auth** — HMAC-SHA256 token auth (`token_auth.py`) with IP binding. Bound to `127.0.0.1` only.

### 6.2 Heartbeat

60-second tick: reads `HEARTBEAT.md` for pending tasks, rebuilds FTS5 index (every 15 min), prunes history (daily, >365 days), prunes SEL entries (daily, >365 days), triggers idle session consolidation (>3h).

### 6.3 Real-Time Communication

Single multiplexed WebSocket at `/api/ws`: status, slots, titles, chat streaming, tool calls, notifications, logs, refine, approvals. Exponential backoff reconnect (1s→10s max). State re-fetched via Redux on reconnect — no page reload.

### 6.4 Vector Memory (Optional)

Local Ollama + FAISS (no data leaves machine):
- **Semantic**: Structured key-value facts with confidence scores, conflict resolution, injection detection, audit trail
- **Episodic**: Conversation fragments with decay scoring (`cosine_sim × importance × exp(-0.03×days)`)
- **Lessons**: Vector-aware dedup (substring + keyword overlap)

Model: Qwen3-Embedding-0.6B (Q8_0 GGUF, 610MB), internal Gitfarm package, Apache-2.0.

### 6.5 Cron Scheduling

Three schedule types: `every` (interval, min 60s), `at` (one-shot), `cron` (5-field expression). Cross-process file locking (`fcntl.flock`). Per-job agent selection. One-shot sessions from warm pool.


### 6.6 MCP Tool Input/Output Validation

All MCP tool inputs from untrusted sources (LLM, end user, other MCP tools) are validated through a centralized module (`validation.py`) before execution. This is a defense-in-depth layer — even if the LLM sends malformed or adversarial inputs, the tool never sees them.

**Architecture:**

```
LLM → JSON-RPC request → validate_jsonrpc_request()
                              ↓
                         _validate_args(tool_name, raw_args)
                              ↓
                         validate_tool_args(args, ToolSchema)
                              ↓ per field
                         validate_field(value, FieldSpec)
                              ↓
                         sanitize_string() → normalize_unicode() + strip_hidden_unicode()
                              ↓
                         _call_tool_inner(name, cleaned_args)
                              ↓
                         build_tool_response(result_text) → sanitize_response() + truncate
                              ↓
                         validate_jsonrpc_response(resp) → write to stdout
```

**Schema system:** Declarative `FieldSpec` and `ToolSchema` dataclasses define per-tool validation rules:

| Check | Implementation |
|-------|---------------|
| Type enforcement | `isinstance()` against `FieldSpec.type` |
| Required fields | Reject `None` for `required=True` |
| String length limits | `MAX_SHORT_STRING` (500), `MAX_MEDIUM_STRING` (5K), `MAX_LONG_STRING` (50K) |
| Enum allow-lists | `ALLOWED_LESSON_CATEGORIES`, `ALLOWED_SCHEDULE_KINDS` |
| Regex patterns | `_AGENT_NAME_RE` (alphanumeric + hyphens), `_JOB_ID_RE` (hex) |
| Numeric ranges | `min_val`/`max_val` (e.g. timeout 1–3600s, cron interval 60–2.6M s) |
| Unknown field rejection | Any key not in schema raises `ValidationError` |
| Unicode normalization | NFC normalization via `unicodedata.normalize("NFC", ...)` |
| Hidden character stripping | Removes zero-width, directional overrides, private use, surrogates; preserves `\n \r \t` |

**Schema registry:** 12 tools across 2 MCP servers have schemas:

| MCP Server | Tools with Schemas | Pass-through (no schema) |
|------------|-------------------|--------------------------|
| `kirocrew-core` | `spawn_run`, `learn_add`, `learn_remove`, `task_run` | `spawn_list`, `learn_list` |
| `kirocrew-cron` | `cron_add`, `cron_remove`, `cron_pause`, `cron_resume` | `cron_list`, `cron_remove_all` |

Read-only tools (`*_list`, `cron_remove_all`) pass through without schema validation — they take no meaningful user input.

**Response path:** All tool responses exit through `build_tool_response()` which sanitizes text (same Unicode pipeline) and truncates at 100K chars. The response is wrapped in the MCP `TextContent` schema (`{"content": [{"type": "text", "text": "..."}]}`). JSON-RPC response envelopes are validated before writing to stdout.

**Dashboard API:** `validate_api_body()` and `validate_string_field()` provide the same sanitization for aiohttp request bodies (max 100K body size, per-field length limits, enum allow-lists).

**Failure mode:** `ValidationError` is caught at the `_call_tool()` boundary. The tool never executes — the error is returned as a text response and logged to SEL. Zero user-facing behavioral changes for normal inputs.

### 6.7 Security Event Log (SEL)

Immutable, tamper-evident audit trail for every tool invocation, permission decision, MCP call, and dashboard API mutation. Implements transactional event logging per Amazon Security Event Logging Standard.

**Storage:** `~/.kirocrew/security_events.jsonl` — append-only JSONL with HMAC-SHA256 integrity chain.

**Event schema:**

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex |
| `timestamp` | ISO 8601 UTC |
| `event_type` | `tool_invocation` or `api_access` |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`) |
| `agent` | Agent name |
| `source` | Interface: `slack`, `dashboard`, `cli`, `cron`, `subagent`, `taskrunner`, `mcp`, `background` |
| `operation` | Tool name or `METHOD /api/path` |
| `outcome` | `invoked`, `auto_approved`, `approved`, `rejected`, `denied`, `completed`, `failed` |
| `prev_hash` / `entry_hash` | HMAC-SHA256 chain (each entry signs over previous hash) |

**Integrity chain:** Each entry's `entry_hash` is an HMAC-SHA256 over all fields (excluding `entry_hash` itself) plus the previous entry's hash. The HMAC key is a 32-byte random secret stored at `~/.kirocrew/sel_hmac.key` (mode 0600). `verify_integrity()` walks the full chain and reports any tampered or missing entries.

**Integration — 8 surfaces:**

| Surface | Events Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_call` invoked/denied, `permission_request` all outcomes | `slack/handler.py` |
| Dashboard chat | `tool_call` invoked, `permission_request` all outcomes (deny/auto/interactive) | `dashboard/chat.py` |
| TaskRunner | Decomposition auto-approvals, step execution permissions (deny/reject/approve) | `taskrunner.py` |
| Subagent | Permission requests during subagent execution | `subagent.py` |
| Background tasks | All permission outcomes via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `learn_remove`, `task_run` calls + validation failures | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, `cron_pause`, `cron_resume` calls + validation failures | `mcp_cron.py` |
| Dashboard API | All POST/PUT/DELETE operations via aiohttp middleware | `dashboard/server.py` |

**Retention:** 365 days default. Pruned daily by heartbeat service. Pruning rewrites the file (atomic rename via `.tmp`), rebuilding the chain.

**Access:**

| Interface | Endpoint |
|-----------|----------|
| Dashboard API | `GET /api/sel/events?limit=N`, `GET /api/sel/verify` |
| CLI | `kirocrew security events [-n 20]`, `kirocrew security verify` |

**Thread safety:** Singleton pattern with `threading.Lock` on all writes. Safe for concurrent access from the asyncio event loop and MCP server stdio processes.

---

## 7. Future Directions

This section captures architectural extension ideas that go beyond the current single-machine, personal-agent scope.

### 7.1 Per-Step Agent Selection

Today, agent selection is per-task (TaskRunner) or per-session (dashboard/cron). All steps in a task run with the same agent. For complex tasks that span multiple domains — coding, testing, reviewing, deploying — different steps would benefit from different specialized agents.

**What exists today:**
- `get_or_create()` already accepts an `agent` parameter
- The step schema has `title`, `description`, `depends_on`, `requires_approval`
- The workaround is skills-as-agent-wrappers via `kiro-cli chat --no-interactive`

**What would change:**
- Add `agent` field to the step schema: `{"title": "Write tests", "agent": "test-writer", "depends_on": [1]}`
- Decomposition prompt asks the LLM to assign agents per step from a list of available agents
- `_execute_single_step()` passes `agent=step.agent` to `get_or_create()` instead of `self._agent`
- Parallel steps with different agents would each spawn the correct kiro-cli process

This enables building a library of focused agents (coder, reviewer, test-writer, doc-writer) that the TaskRunner composes per task. The decomposition LLM becomes a planner that assigns both the work and the worker.

### 7.2 Shared External Artifacts for Multi-Agent Coordination

Currently, inter-step communication happens through two channels: the kiro-cli session context (LLM remembers prior turns) and WorkingMemory (text summaries injected into prompts). Both are internal to KiroCrew — they exist in the LLM's context window or in Python objects.

The limitation isn't that the state is too thin — it's that there's no shared external artifact that multiple agents can independently read from and write to. When step 1 (coder agent) writes code and step 2 (reviewer agent) needs to review it, the reviewer doesn't see the actual code — it sees a text summary of what the coder said it did. If step 1 and step 2 run on different kiro-cli processes (parallel execution or different agents), they have no shared ground truth.

**The key idea: external storage as coordination surface.** A git branch, a shared directory, a database — something outside the LLM's context that multiple agents can observe and modify. This turns the coordination model from "pass text summaries between prompts" to "agents work on the same artifacts and see each other's changes."

**Git branch as the natural coordination surface for development tasks:**
- TaskRunner creates a feature branch at task start
- Each step commits its changes to the branch
- Subsequent steps (even on different kiro-cli processes) see the accumulated work via the working tree and `git log`
- Parallel steps work on the same branch — a merge step after the parallel group reconciles conflicts
- The final step opens a CR from the branch
- The branch is the artifact, not the LLM's text summary of what it did

**Generalized: any external store works for non-coding tasks:**
- A shared document (wiki) for research tasks — agents append findings, later agents read the accumulated document
- A database table for data processing tasks — agents write intermediate results, downstream agents query them
- An S3 bucket for artifact-heavy tasks — agents upload outputs, downstream agents download inputs

This also connects to distributed coordination (7.3): if agents on different machines share a git remote or a database, they can coordinate without any direct communication between kirocrew instances. The external store becomes the message bus.

### 7.3 Distributed Coordination: Multiple Machines Running KiroCrew

Today, KiroCrew is a single-process, single-machine system. All kiro-cli processes, memory files, and the dashboard run on one host. This limits:

- **Compute**: kiro-cli processes are CPU/memory-intensive. A complex task with 3 parallel steps + warm pool + background session can saturate a laptop.
- **Availability**: If the machine sleeps or reboots, everything stops. Cron jobs don't fire. TaskRunner checkpoints but doesn't resume automatically.
- **Collaboration**: One user's kirocrew can't delegate to another user's kirocrew. There's no way to say "run this step on the Cloud Desktop that has the build environment."

**Levels of distribution (from simple to ambitious):**

**Level 1 — Remote agent execution.** The simplest extension: kirocrew on machine A dispatches a step to a kiro-cli process on machine B via SSH or an API. Machine B runs the agent and returns the result. Machine A remains the coordinator. This is essentially `ssh machine-b kiro-cli chat --no-interactive --agent coder "task"` — the same pattern as the skill-as-agent-wrapper, but remote.

**Level 2 — Shared memory across machines.** Multiple kirocrew instances share the same memory store (preferences, projects, lessons, history). This could be a shared filesystem (NFS, EFS), a database, or a sync protocol. Each instance reads/writes to the shared store. Sessions on machine A and machine B both see the same accumulated knowledge.

**Level 3 — Federated kirocrew.** Multiple kirocrew gateways coordinate as peers. A task decomposed on machine A can dispatch steps to machine B's SessionManager. Each machine has its own warm pool, dashboard, and cron service, but they share a task queue and memory store. This is a distributed TaskRunner — the planner runs on one node, workers run on any node with capacity.

**What would be needed:**
- A task queue (Redis, SQS, or a simple HTTP API) for step dispatch
- A shared memory backend (replacing local filesystem with a database or object store)
- Authentication between kirocrew instances (mutual TLS or shared tokens)
- A coordination protocol for step assignment, heartbeat, and result collection
- Conflict resolution for concurrent memory writes from different machines

The single-machine architecture is a deliberate simplicity choice — no infrastructure dependencies, no network failure modes, no auth complexity. Distribution adds value for teams and heavy workloads but fundamentally changes the operational model.

### 7.4 Pluggable Context: Let an Agent Decide What Context to Inject

Today, `ContextBuilder` is hardcoded Python logic. It assembles context in a fixed order with fixed budgets: preferences (1K) → projects (2K) → history (6K) → lessons (1K) → skills → conversation history (8K) → cross-tab (2K) → episodic (3K). Every session gets the same assembly pipeline. There's no way for a user or a task to say "for this step, I need the full git diff instead of memory" or "skip skills and give me more history budget."

**The insight**: context assembly is itself a judgment task — deciding what's relevant for a given message is exactly what LLMs are good at. Instead of hardcoded logic, a "context agent" could dynamically select and prioritize context sources.

**What this could look like:**

**Level 1 — Pluggable context providers.** Refactor `ContextBuilder` from a monolithic class into a pipeline of registered providers. Each provider (memory, skills, lessons, history, episodic, custom) implements a simple interface: given the message text and a budget, return context text. Users register their own providers — e.g., a provider that queries an internal wiki, fetches recent pipeline status, or reads a project-specific knowledge base. The assembly order and budgets become configurable per agent, not hardcoded.

```
ContextBuilder pipeline (configurable per agent):
  ├── MemoryProvider        (prefs + projects + history)
  ├── SkillsProvider        (always-on + triggered)
  ├── LessonProvider        (corrections)
  ├── ConversationProvider  (recent messages + cross-tab)
  ├── EpisodicProvider      (vector similarity search)
  ├── GitDiffProvider       (custom: recent changes in repo)  ← user-defined
  └── PipelineProvider      (custom: current pipeline status) ← user-defined
```

**Level 2 — Context agent.** Take it further: the context assembly step itself is an LLM call. A lightweight, fast agent (e.g., `kirocrew-lite` on Sonnet) receives the user's message plus a manifest of available context sources (with previews/summaries). It returns a selection: "for this message, inject the matching skill, last 5 conversation messages, and the git diff — skip memory and lessons." The main agent then runs with exactly the context the context agent selected.

This is essentially the same pattern as per-step agent selection (7.1), but applied to the context layer. The context agent is a planner that decides what the worker agent needs to know. It runs as a fast pre-step before each main LLM call — one cheap Sonnet call to select context, then one Opus call with the right context loaded.

**Why this matters:**
- Different task types need different context. A coding step needs git state and build output. A research step needs episodic memory and wiki results. A review step needs the full diff and coding standards. Today they all get the same fixed assembly.
- The 50K char budget is a real constraint. Wasting it on irrelevant context (e.g., injecting all lessons when the step is about deployment) reduces the quality of the main LLM call.
- Custom context providers would let teams integrate their own knowledge sources without modifying KiroCrew's core code — just register a provider that returns text.
