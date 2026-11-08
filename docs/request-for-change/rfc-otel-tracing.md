---
title: Native OpenTelemetry traces for every turn, step and session, with credits and correlation IDs
status: draft
author: kyleseaman, with devmate
created: 2026-11-05
last-audited: 2026-11-06
audited-at: 9996c57111
doc-pr: null
implementation-prs: []
tracking-issues: [1643]
supersedes: []
superseded-by: []
---

# RFC: Native OpenTelemetry traces

- Status: draft. Nothing is built. Code facts are measured at `9996c57111`.
- Tracking issue: [#1643](https://github.com/kirodotdev/KiroCrew/issues/1643) (`tier:T3`: a maintainer's design
  sign-off is needed before any change merges).
- Related: [#24884](https://github.com/kirodotdev/KiroCrew/pull/24884) (execution correlation identities in hooks;
  open). This design uses its field names but does not depend on it.
  [rfc-crew-log-telemetry-export.md](rfc-crew-log-telemetry-export.md) (crew-log folds for metrics; draft).
  [metrics.md](../system-specs/modules/metrics.md) (the existing metrics pipeline this reuses).

## 1. Summary

Kiro Crew exports OpenTelemetry **metrics** only. The issue asks for **traces**: point Langfuse, or any OTLP
backend, at Kiro Crew with an endpoint and an auth header, and see per-session and per-agent traces with model,
usage, latency and cost.

This RFC proposes:

1. **Every model turn on every surface is traced, with one tree shape.** Spans open where every model turn already
   passes: the two ACP stream paths. A turn is its own trace, unless it belongs to a run root: a sub-agent's parent
   turn, a workflow, an Ultra Mode round, or a cron run. Surface spans add the trigger on top.
2. **GenAI convention names** (`invoke_agent`, `chat`, `execute_tool`) plus `workflow`, `phase` and markers, matching
   a live CloudWatch trace built from #24884 and an external ACP tap. Model and spend sit on every agent and chat span.
3. **Credits as a first-class usage measure.** Kiro and Kiro Agent report credits and no tokens, so credits go on
   every turn's `chat` span, which Langfuse counts as usage.
4. **A fixed set of correlation IDs on every span**, named after #24884's hook contract, and **one deterministic id
   derivation** (§5.8), so core, hook consumers and an external ACP tap compute the same span ids.
5. **Off by default, with no new network egress until an endpoint is set.** Export reuses the metrics OTLP path's
   host policy, audit and enterprise override, adds native SigV4 for CloudWatch. Phases 1a to 8 edit no file under
   Sensitive Change Review.
6. **Content is opt-in per category** (§9.1): prompts, completions, tool arguments, tool output and file paths are
   each off by default. Secrets are never recorded. This ships last, as its own SCR-reviewed phase.

## 2. Motivation

### 2.1 What exists today

- The OTel API and SDK are core dependencies (`setup.cfg`). The `otlp` extra pins
  `opentelemetry-exporter-otlp-proto-http`, which also ships the span exporter.
- `src/kiro_crew/metrics/` provides a MeterProvider, a local JSONL sink, and an attribute guard
  (`metrics/schema.py::validate_attrs`). Its OTLP egress is gated per host by `metrics/otlp_governance.py`
  (`capabilities.telemetry` → `otlp_hosts`; fails closed; SEL-audited).
- Metrics OTLP headers come from environment variables named in `headers_env`
  (`_destination_headers` in `src/kiro_crew/metrics/provider.py`). A literal header value in config is refused.
- There is no TracerProvider and no span anywhere in `src/`.
  [telemetry-otlp-export.md](../guides/telemetry-otlp-export.md) says so under "Traces are not exported".

### 2.2 The customer ask (#1643)

- An endpoint plus an auth header, also settable through `OTEL_EXPORTER_OTLP_ENDPOINT` / `_HEADERS` /
  `OTEL_SERVICE_NAME`.
- A Settings section.
- `gen_ai.*` semantic conventions.
- Spans for agent and sub-agent turns, LLM generations, and tool and MCP calls, nested to mirror the orchestration.
- A true no-op when off.

### 2.3 What the backends actually report

Measured from the parsers in `src/kiro_crew/acp/_dispatch.py` and the live captures under
`test/fixtures/acp_frames/`:

| Harness | Tokens per turn | Cost | Per-model-call boundaries |
|---|---|---|---|
| Kiro (kiro-cli) | none: the prompt response carries only `stopReason` (`parse_prompt_token_usage`) | credits, per model call, summed per turn (`parse_metadata`) | an end marker per call, no start |
| Kiro Agent | none | credits, a whole-turn total (`_apply_kas_turn_completion` in `src/kiro_crew/acp/session_handle_kas.py`) | intervals only up to the first displayed chunk |
| Claude Code | input, output and cache counts | USD, session-cumulative, diffed to per turn | none |
| codex, opencode, copilot, goose | present in captures | opencode only: USD | none |

So per-generation spans cannot be built honestly, and tokens are missing for the default harness.

## 3. Goals

- G1. Every model turn from every surface produces a trace, from the first implementation PR.
- G2. A Langfuse user gets useful traces (turns, tools, credits, sessions) by the second PR.
- G3. The orchestration tree, including sub-agents, workflows and Ultra Mode, can be rebuilt from parent spans,
  links and shared correlation attributes.
- G4. Credits are a usage measure; tokens and USD are carried where a harness reports them.
- G5. Off by default, with zero cost when off. Nothing leaves the host without an endpoint the operator set.
- G6. By default no span carries prompt text, completion text, tool arguments, tool output or file paths. Each of
  these can be turned on as its own category (§9.1). Secrets are never recorded, whatever is turned on.

## 4. Non-goals

- Inventing per-model-call (`chat`) spans, token counts or USD the backend does not report.
- Tracing inside kiro-cli, MCP servers or hook scripts.
- gRPC export (it would add a dependency; Langfuse accepts HTTP).
- A Langfuse-specific SDK. Three Langfuse attribute keys are proposed (§7), plus two more for opt-in content (§9.1),
  all as plain span attributes.
- Changing the metrics signal or its defaults.

## 5. Design

### 5.1 Trace model

- **The tree shape is the same for every run kind.** Each `invoke_agent` has `user.prompt`, `chat` and
  `execute_tool` children, and model and spend sit on `invoke_agent` and `chat`. Only the root differs:
  - **R1 (root kinds).** A turn nothing else caused is its own root, with `trace_id = turn_id`. Otherwise it joins a
    **run root**: the parent turn for a sub-agent, a workflow run, an Ultra Mode round, or a cron run.
  - **R2 (anchors).** A run root that can outlive a process is a short anchor span. Children are parented to it by
    deterministic id, so nothing is held open and a restart loses nothing. Each run kind ends with an
    `<kind> outcome <status>` marker.
  - **R3 (roll-ups).** `kirocrew.usage.*_total` goes on spans that end after all of their children (a blocking
    spawn, workflow, phase, Ultra Mode item, outcome markers), never as `gen_ai.usage.*`.
  - **R4 (single-cause join).** A turn whose only cause is one finished async run joins that run's trace: a
    workflow's completion turn, a sub-agent's completion-event turn, or a wake answering one report. A turn with
    several causes roots itself and links to each.
  - **R5 (budgets).** X-Ray allows trace data to be modified for 7 days and limits a trace document to 100–500 KB
    ([X-Ray quotas](https://docs.aws.amazon.com/general/latest/gr/xray.html)). A run root therefore rolls over to a
    linked continuation trace after 6 days or 2,000 spans.
  - **R6 (sessions).** Langfuse treats the session id as trace-level and groups "all observations with the same
    sessionId … including their enclosing traces"
    ([sessions](https://langfuse.com/docs/observability/features/sessions)). So every span carries its **root's**
    `session.id` (a keyed hash of the Crew session key, stable across ACP recycles), and its own session goes in
    `kirocrew.session_key_hash`.
- **No session-level trace or anchor.** Chats last days and hold hundreds of turns. `session.id` already groups them:
  in Langfuse's Sessions view, and in CloudWatch by filtering or grouping `attributes.session.id` in Transaction
  Search or Logs Insights (the key CloudWatch's own sessions view reads is verified in phase 2).

Example trees (the same style as the live trace in §5.6):

```
plain session turn / Bot DM (phase 1a)    sub-agents (phase 1b)
invoke_agent <agent>                      invoke_agent <agent>
  user.prompt                               chat
  chat [<model>]                            execute_tool spawn_sub_agents      blocking
  execute_tool <tool> ...                     invoke_agent <child> ; chat ; execute_tool ...
  turn.complete                             execute_tool spawn_run             async, ~100 ms
                                              invoke_agent <child> ...         runs on after the parent ends
                                            turn.complete
                                          invoke_agent <agent>               completion-event turn, same trace (R4)
```

An async child stays a child of its short, already-ended spawn span. Both Langfuse and X-Ray draw it past the
parent's end in the same waterfall. Cron runs root at a `cron <job>` anchor. Channel turns are plain session turns
with `kirocrew.channel`.

### 5.2 Two layers

**Layer 1: the ACP turn (universal).**
- `invoke_agent` opens in `AcpSessionProvider.stream` (`src/kiro_crew/acp/session_provider.py`) for kiro and
  Kiro Agent, and in the `AcpClient` stream path for Claude and direct clients.
- Both hold the Crew session key as `_session_key` (`src/kiro_crew/acp/session_provider.py`,
  `src/kiro_crew/acp/client_class.py`).
- `agent.provider` is `enum=["acp"]`, so no turn bypasses this layer.
- With no surface span open, the turn becomes a root with a minted id.

**Layer 2: surface roots.** Found by tracing callers to layer 1:

| Engine | Entry | Surfaces |
|---|---|---|
| Dashboard runner | `_run_chat` in `src/kiro_crew/dashboard/chat_runner.py` | chat, Bot DMs, worker sessions, queue drain, regenerate and rewind, forks, monitor wakes on dashboard slots, linked Slack threads, Issue Radar, Spec Builder |
| Channel driver | `TurnDriver.run` in `src/kiro_crew/messaging/driver.py` | Slack, Telegram, Discord, Teams, Webex, WeCom, Weixin, WhatsApp, iMessage, Feishu; monitor wakes on channel slots |
| Collect helper | `stream_and_collect` in `src/kiro_crew/llm_helpers_parts/turn_stream.py` | cron agent jobs, Bot heartbeat and check-ins, workflow agent calls, side chat, threads, apps |
| Background helper | `background_turn` / `run_bg_oneliner` in `src/kiro_crew/llm_helpers_parts/background_turns.py` | titles, summaries, consolidation |
| Sub-agent runner | `_run_inner_impl` in `src/kiro_crew/subagent_manager/run_turn.py` | `spawn_run`, `spawn_sub_agents`, continue, retry |
| Webhook runner | `_run_hook_inner` in `src/kiro_crew/dashboard/handlers/hooks.py` | inbound webhooks, `register_hook` callbacks |
| Task runner | the step loop's `client.stream` in `src/kiro_crew/task_executor.py` | task-runner steps |

Turns with no model call get their own spans: script and command cron runs, standalone compaction, and reset.

### 5.3 Steps inside a turn

Each step is a child span of the turn:

- **`chat`**: the turn's model work, from prompt sent to stopReason. This is a per-turn aggregate (§2.3). It carries
  model and spend (§7).
- **`user.prompt` and `turn.complete`**: zero-length markers at the prompt send and at the prompt result.
- **Tool calls**, built-in and MCP, from the two existing tool-timing hooks in `metrics/tool_calls.py`.
- **Approvals and permission prompts**, with `wait_ms` and who decided.
- **Queue wait.**
- **Compaction.**
- **Retries**, one `invoke_agent` per attempt.
- **Derived model-call steps**, at the boundary the dashboard already derives
  (the crew-log step boundary in `src/kiro_crew/dashboard/chat_turn/stream_chunks.py`), marked `derived` and never
  carrying usage.
- **Links** (not parents) for asynchronous causes: `session_send` and broadcast, steer, workflow launch, and the
  queued sender.
- **Errors** are recorded as span status plus `error.type`, never message text.

Boundaries no harness exposes are listed in §11.

### 5.4 Sub-agents

- A sub-agent's turns are **children of the spawn tool span, in the parent turn's trace**. They use the origin the
  crew log already records (`remember_child_origin` in `src/kiro_crew/crew_log/emit_runtime.py`) and
  `SubagentInfo.parent_session_key` (`src/kiro_crew/subagent_parts/run_record.py`).
- The spawn tool span exists at admission, so an async child can be parented to it even after the parent turn has
  ended. `spawn_continue` and retries are new children of the same origin.
- The completion-event turn joins the parent's trace (R4). Native child agents of kiro-cli and Claude stay inside the
  parent's `chat`, because the harness gives them no turn.

### 5.5 Ultra Mode and Bot conductors

Ultra Mode runs a conductor (`kirocrew-conductor`) that dispatches worker sessions (`kirocrew-worker`) and tracks
them on the work ledger. A Bot (crewmate) conductor uses the same machinery with the `mate_root` stamps
(`WORKER_FIELD` and `MATE_ROOT_FIELD` in `src/kiro_crew/crew_mode.py`). Both are covered, distinguished by
`kirocrew.crew.root_kind`.

**Decision proposed: one trace per round** (the default; `traces_ultra_mode: "turn"` keeps per-turn traces and links).
A whole round renders as one waterfall, the same experience as a workflow:

```
ultra round <N>                         anchor; trace = (conductor, goal, round)
  invoke_agent kirocrew-conductor       person, patrol or wake turns
    chat ; execute_tool work_dispatch   -> kirocrew.work.create / bind spans
  work_item <item_id>                   anchor per item
    invoke_agent kirocrew-worker        chat ; execute_tool ... ; kirocrew.work.report
    work_item outcome <state>           verdict, fails, roll-up totals
  ultra round outcome <state>
```

- **Why a round, not a turn or a goal.** Neither backend draws links as a waterfall, which rules out a trace per
  turn. A goal can run for weeks, past X-Ray's 7-day window. A round is the conductor's own dispatch unit, and R2
  and R5 handle a long round. Rounds of one goal share `kirocrew.work.goal_id` and link in order.
- **IDs.** The round trace is `H(trace.ultra, conductor key, goal_id, round, continuation)`, the item anchor is
  `H(work.item, item_id, generation)`, and each outcome is keyed by the ledger's own `work_event_id`.
  - `goal_id` is a new random id the work ledger mints when a new goal is set. Until it exists, the board
    generation plus the goal's item-count base stand in.
- **No goal text** in names or attributes. The round is named `ultra round N`.
- **Sessions:** every span carries the conductor's `session.id` (R6). Worker sessions are found by
  `kirocrew.session_key_hash` and `kirocrew.crew.root`.

Producers:

- **Every ledger write goes through one function**, `on_work_recorded`
  (`src/kiro_crew/crew_log/emit_surfaces.py`). It handles all eleven actions and the worker's `work_report`.
  Each write becomes a `kirocrew.work.<action>` span in the acting turn, with attributes only: `item_id`, round,
  generation, state, verdict, fail count, closed-vocabulary status and reason, and the PR number. No title,
  acceptance, decision, summary or artifact text.
- **Worker creation** (`session_create`, `work_dispatch`, `session_fork`) is a span in the conductor's turn. The
  worker's turns then sit under their item's anchor in the same round trace.
- **Report → wake:** a report advances the `work` fold, and `conductor_wake.py` fires the conductor's loop. Reports
  that land together share one wake, so the wake turn carries one link per report it answers, inside the round
  trace. Undelivered reports get an event and a link.
- **Handoff, adopt, release and hand-back on close** are manager-change spans: old and new manager (hashed), plus a
  link.
- **Patrol health** (stale, orphaned, pending-overdue counts) goes on the patrol turn.
- **Filtering:** `kirocrew.crew.root` selects a whole run; `kirocrew.work.item_id` selects one item across the
  conductor and its worker.

### 5.6 Span names

Names follow the OTel GenAI conventions and match a live CloudWatch trace produced from #24884 plus an external ACP
tap:

| Span | `gen_ai.operation.name` | Notes |
|---|---|---|
| `workflow <name>` | — | run root; roll-up totals |
| `phase <title>` | — | from `ctx.phase(title)`; ends at the next phase or the run's end, because there is no phase-end event |
| `workflow outcome <status>` | — | zero-length marker: `finished`, `failed` or `cancelled` |
| `invoke_agent <agent>` | `invoke_agent` | an ACP turn, or a workflow agent call (`<phase>:<label>`) |
| `chat` / `chat <model>` | `chat` | the per-turn model aggregate; `chat <model>` once a resolved model is known |
| `execute_tool <tool>` | `execute_tool` | child of `invoke_agent`, a sibling of `chat`; MCP calls show as `execute_tool mcp` (Q2) |
| `user.prompt`, `turn.complete` | — | markers; `turn.complete` carries the stop reason |

### 5.7 Workflows

```
workflow <name>
  phase <title>
    invoke_agent <phase>:<label>
      chat
      execute_tool <tool> ...
  workflow outcome <status>
  invoke_agent <chat agent>          the completion turn, same trace
    user.prompt / chat / turn.complete
```

- **Workflow-only:** phases and `<phase>:<label>` names, because only the workflow DSL has them. An Ultra Mode round
  and its `work_item` anchors play the same role there. Outcome markers, roll-ups, the completion join and rollover
  are generic (§5.1).
- **Phases.** The workflow DSL has phases (`ctx.phase(title)`, run event `phase_started`). It has no phase-end event
  and no phase id, so the span ends at the next phase or at the run's end, and its identity is the 1-based phase
  ordinal. An optional `phase_index` key on `phase_started` and `agent_started` lets external producers skip
  recounting. That changes the workflow event contract, so the workflows spec is updated in the same commit.
- **The completion turn joins the run's trace.** `inject_workflow_result` (`src/kiro_crew/dashboard/workflow_inject.py`)
  starts a turn to report the finished run. That turn's only cause is the run, so it takes the run's `trace_id`
  with the `workflow` root as its parent, keeps its own `turn_id` as an attribute, and links back to the launching
  turn.
  - This is R4 (§5.1) applied to workflows.
  - This needs `workflow_run_id` on the injected turn, which is one of #24884's listed remaining items. It is added
    in core, with no hook edit.

### 5.8 Deterministic IDs

One documented derivation, so core, a #24884 hook consumer and an ACP tap all compute the same ids for the same
thing. It ships as one module with a conformance fixture (inputs → ids).

```
H(kind, p1..pn) = sha256("kirocrew.v1|" + ns + "|" + kind + "|" + esc(p1) + "|" + ... + "|" + esc(pn))
span_id  = first 16 hex of H        (if all zero, hex 16..31)
trace_id = first 32 hex of H(trace.<kind>, ...), except a turn trace, which is turn_id itself
esc(p)   = "%" -> "%25", "|" -> "%7C"
```

- `ns` is a random per-install trace namespace, exported raw as the resource attribute `kirocrew.trace_ns`.
- It exists because several inputs are unique only within one install: workflow ids are sequential (`wf_000123`),
  work-item and cron run ids are 8 hex.

| Span | Inputs | Repeat behaviour |
|---|---|---|
| `invoke_agent` (turn), `chat`, `user.prompt`, `turn.complete` | `turn_id`, `attempt` | `attempt` = 1-based count of `session/prompt` requests carrying that `turn_id`, which core and a tap both see; a regenerate is the next attempt |
| `execute_tool`, approval | `acp_session_id`, `tool_call_id` | updates refine the open span; a start for an id already ended is a session-load replay and is dropped |
| `workflow`, `workflow outcome` | `run_id` | a rerun has a new `run_id`, so a new trace with a link to the old one |
| `phase` | `run_id`, phase ordinal | after a restart the same script order gives the same id; spans already exported are not re-emitted |
| workflow `invoke_agent` | `run_id`, `agent_id` (`a{call_index}`) | cached results on resume are not re-emitted |
| sub-agent turns | parent's trace; `turn_id`, `attempt` under the spawn tool span | continue and retry are new children; the completion-event turn joins (R4) |
| `ultra round` anchor | `conductor key`, `goal_id`, `round`, continuation | a round bump starts a new trace; rollover bumps the continuation |
| `work_item` anchor | `item_id`, `generation` | a re-plan keeps the id |
| `<kind> outcome` | the run key; for items, the ledger's `work_event_id` | one per run end |
| cron run | `job_id`, `run_id`, fire time | a catch-up fire is a new run |
| session lifecycle | Crew session key, `acp_session_id`, event, seq | one per ACP session per event |
| Ultra Mode ledger write | `item_id`, the ledger's own `work_event_id` | `item_id` + generation + action is not unique (one item has many reports); an undone write emits nothing |

### 5.9 Option B (no core spans) and the hybrid

**Option B** produces traces without core spans. It is what the live trace above came from.
- **An ACP tap**: a stdio proxy configured in place of the harness binary. For kiro-cli that is the existing
  `KIROCREW_KIRO_BIN` override, and launch trust is "the CLI runs". It reads `session/prompt` (with #24884's
  `_meta.kirocrew`), tool-call updates, permission requests, metering, cost and stop reasons, and emits spans with
  the §5.8 ids. It drops all content.
- **#24884's hooks**: `AgentSpawn`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `Stop`, carrying Crew's
  turn, session, tool and workflow identities.

**What it costs:**
- No hook covers workflow phases, session lifecycle, cron or channel triggers, the queue or the work ledger. A
  consumer must read Crew's internal workflow journal and crew-log files to get them.
- The tap runs as the harness process, inside the agent sandbox, so its egress bypasses `otlp_hosts` and SEL audit.
  A safer shape is a local file or socket feeding an external collector.
- A tap crash breaks the ACP session.
- Core still needs #24884 merged, and its `hook_runtime/*` half is under Sensitive Change Review.
- Other harnesses need their own launch overrides, adapted per harness at their resolvers and never in
  `sandbox_spawn.py`.
- It cannot ship as a builtin app (`no-new-builtin-apps`).

| | A: native core spans | B: tap + hooks | Hybrid |
|---|---|---|---|
| Coverage | every surface, step and session | ACP turns, tools, approvals; workflows and Ultra Mode only by reading internal files | A's |
| Spend accuracy | Crew's per-turn usage, attempt sums included | raw frames; must redo cumulative-cost diffing and replay guards | A's |
| Overhead | in-process; one check per hook point when off | a process per session, a hop per frame, a subprocess per hook | A's, plus B where installed |
| Failure isolation | tracer faults are swallowed | a tap crash kills the session | A's |
| Maintenance | in-repo, replay-tested per harness | tracks wire formats outside CI | split |
| SCR exposure | none in phases 1a to 8; phase 9's sink registration (§9.1) | #24884's hook half | #24884's, on its own schedule |
| Egress governance | `otlp_hosts`, SEL, enterprise override | bypassed or delegated to a collector | A governed |
| Time to Langfuse / CloudWatch | phase 2 | now, externally, kiro-cli only | now for B users; phase 2 for all |

**Decision proposed: the hybrid, with A as the shipped path.**
- Publish §5.8 as the shared contract. External B producers stay outside the repo and conform to it.
- When both run on one install, core owns every span kind it emits. B adds only what core lacks (for example a hook
  run as a child of core's tool span, by id) and never re-emits a core span id.

## 6. Correlation IDs

Every span carries whichever of these exist for it. Names are #24884's
(`docs/guides/hook-correlation-contract.md` on that branch), with a `kirocrew.` prefix and the same meaning.
#24884 notes `agent_id` is a workflow agent call, distinct from `subagent_id`, and this design keeps that split.

- **Raw** means a random value that reveals nothing.
- **Hashed** means HMAC-SHA256, truncated, keyed from the install's vault (`secrets/vault.py`'s `derive_subkey`;
  imported, not edited), and stable per install.
- **The local JSONL sink keeps both forms.** Raw values under `kirocrew.local.*` sit next to the hashed ones. The OTLP
  exporter wrapper strips `kirocrew.local.*` before export. So a user with no backend, including a #24884 hook
  consumer holding raw session keys, can join locally, and raw keys never leave the host.

| Attribute | Minted by | #24884 emits it | Exported as |
|---|---|---|---|
| `kirocrew.turn_id` | dashboard turn entry; else the tracer | yes | raw |
| `kirocrew.event_id` | #24884's hook firing | yes | raw |
| `kirocrew.session_key_hash` (this span's own session); `session.id` = the trace root's (R6) | slot key | yes (raw) | hashed |
| `kirocrew.parent_session_key_hash` | creator, sub-agent parent, fork source | yes (workflows only) | hashed |
| `kirocrew.acp_session_id` | the backend | yes | raw |
| `kirocrew.tool_call_id` | the backend | yes | raw |
| `kirocrew.subagent_id` | sub-agent admission | named, not produced | raw |
| `kirocrew.workflow_run_id`, `kirocrew.agent_id`, `kirocrew.launched_workflow_run_id` | workflow runner | yes | raw |
| `kirocrew.work.item_id`, `.round`, `.generation` | work ledger (`it_` + 8 random hex, `src/kiro_crew/work_ledger_store.py`) | no | raw |
| `kirocrew.work.conductor_hash`, `kirocrew.crew.root`, `kirocrew.crew.dispatcher`, `kirocrew.crew.root_kind` | slot stamps | no | hashed (kind raw) |
| `kirocrew.cron.job_id`, `kirocrew.cron.run_id` | cron service | no | raw |
| `kirocrew.channel`, `kirocrew.channel_thread_hash` | channel adapters | no | kind raw; thread hashed |

## 7. Credits, usage and Langfuse

### 7.1 Where the numbers come from

- **Kiro:** credits from `meteringUsage`, summed per turn; the last frame arrives before the prompt result.
- **Kiro Agent:** a whole-turn total, assigned rather than added, so replays at session load are ignored.
- **Claude and opencode:** session-cumulative USD, diffed per turn.
- **`TurnUsage`** (`src/kiro_crew/acp/types.py`) holds all of these. The metrics side already records
  `kirocrew.turn.credits` (`TURN_CREDITS_METRIC` in `src/kiro_crew/metrics/turns.py`).
- **Sub-agent and workflow-agent spend** is recorded once, on their own turns, never in the parent's turn.
- **Built-in child agents** of kiro-cli and Claude run inside the parent turn, and their spend cannot be split.
- **Cancelled turns** may have partial credits and are marked `kirocrew.usage.complete=false`.

### 7.2 Attributes

Every `invoke_agent` and `chat` span carries model and spend. That fills the Input Tokens, Output Tokens and Model
fields that were empty in the live CloudWatch trace, wherever the harness reports them:

- `gen_ai.request.model`, `gen_ai.response.model` (only a resolved id, never `"auto"`), and
  `gen_ai.usage.input_tokens` / `output_tokens` when reported. The GenAI conventions define these on both agent and
  inference spans.
- `kirocrew.usage.credits`, `kirocrew.usage.cost_usd` (when reported), `kirocrew.usage.model_calls`,
  `kirocrew.usage.complete`.
- The Langfuse keys below, on `chat` only.

`workflow` and `phase` spans end after their children, so they carry exact roll-ups: `kirocrew.usage.credits_total`,
`*_tokens_total`, `cost_usd_total` and `kirocrew.models`. They deliberately carry no `gen_ai.usage.*`, so no backend
counts the same spend twice. A sub-agent run carries its own credits the same way.

The same spend appears on `invoke_agent` and on its `chat`. Langfuse counts usage only on generations, so it is
counted once there. A query that sums every span must filter on `chat`.

**CloudWatch.**
- AWS documents span classification by `gen_ai.operation.name` and tool names by `gen_ai.tool.name`
  ([AgentCore generic framework support](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-generic.html)).
- I found no AWS page that maps Transaction Search's token and model fields to attribute keys. They correspond to
  the conventions' `gen_ai.usage.*` and `gen_ai.request.model`. Phase 2 verifies this against a live Transaction
  Search.
- Credits are a `kirocrew.*` attribute. Filter on it in Transaction Search, or sum it with Logs Insights over the
  `aws/spans` log group, filtered to `chat` spans. Phase 2's guide ships that query.

### 7.3 Langfuse mapping

From [Langfuse's OpenTelemetry mapping](https://langfuse.com/docs/opentelemetry/get-started) and
[token and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking):

- **Usage and cost are kept only on `generation` observations.** Langfuse infers the type from other conventions
  (such as `gen_ai.operation.name`) before falling back to "has a model ⇒ generation". That is why usage sits on its
  `chat` span, which also carries `langfuse.observation.type=generation`, and is counted there rather than on
  `invoke_agent`.
- **Usage goes in `langfuse.observation.usage_details` as flat JSON.** Flat keys are stored unchanged, so
  `{"credits": 0.96}` appears as a usage type named `credits`. Buckets are mutually exclusive and summed into a
  total, so credits are never mixed with token buckets on one span.
- **Cost goes in `langfuse.observation.cost_details`**, only when the harness reports USD.
- **Credits → USD is a Langfuse model definition** with a price for the `credits` usage type, owned by the user.
- **Crew ships no credits-to-USD rate.** Credit prices differ by plan ([pricing](https://kiro.dev/pricing/),
  [add-on credits](https://kiro.dev/docs/billing/add-on-credits/)), so one rate would read as authoritative and be
  wrong for most users.
- **Vendor-neutral:** `gen_ai.*`, `session.id` and `kirocrew.*`. **Langfuse-only:** the three `langfuse.observation.*`
  keys, on the `chat` span only, which other backends ignore. Opt-in content adds `langfuse.observation.input` and
  `.output` on `chat` (§9.1).

## 8. Export, auth and configuration

- **Settings**, all off or empty by default: `telemetry.traces_enabled`, `telemetry.traces_otlp_endpoint` (the full
  `/v1/traces` URL), and `telemetry.traces_otlp_headers_secret` (header name → vault entry name, read at exporter
  build). `headers_env` works as it does for metrics.
  - A literal header value in config is refused, and no header value is logged.
  - A separate switch from `telemetry.enabled`, so local metrics turning on never turns tracing on.
- **Standard env vars** are honoured only while tracing is switched on, so a variable set for another tool cannot
  start egress:
  - endpoint: `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, then `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`;
  - headers: `OTEL_EXPORTER_OTLP_TRACES_HEADERS`, then `OTEL_EXPORTER_OTLP_HEADERS`;
  - service name: `OTEL_SERVICE_NAME` on the trace resource only.

  Config wins over env.
- **Protocol:** http/protobuf through the existing `otlp` extra.
- **Egress:** every destination goes through `otlp_governance`'s host policy, its per-export re-check, SEL audit and
  the enterprise override. No new governance scope.
- **Settings UI:** a Traces block in Settings → Privacy, behind a Feature Preview, off by default. The auth header is
  entered through a secret card, so its value never enters config.
- **No-op when off:** no trace SDK import, no provider, no thread; one boolean check per hook point.

### 8.1 AWS CloudWatch

X-Ray's OTLP traces endpoint (`https://xray.<region>.amazonaws.com/v1/traces`) is HTTP only and needs Transaction
Search enabled ([CloudWatch OTLP endpoints](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)).
It takes SigV4, which AWS recommends, or, since 2026-11, an X-Ray API key sent as a bearer token
([X-Ray OTLP endpoint](https://docs.aws.amazon.com/xray/latest/devguide/xray-opentelemetry.html),
[bearer tokens](https://docs.aws.amazon.com/xray/latest/devguide/xray-opentelemetry-bearer-token.html)).

| Route | Credentials | Phase |
|---|---|---|
| API key through the static-header path above | stored in the vault; AWS advises an expiry and rotation | 2 (no extra code) |
| ADOT / OTel Collector with `sigv4authextension`; Crew exports to it over loopback | the collector's credential chain | 2 (documented) |
| **Native SigV4 in Crew's exporter (proposed default for AWS)** | the user's AWS profile through botocore, resolved per export, short-term | 2b: an optional `otlp-aws` extra and a per-request signer |

- Config holds only `traces_otlp_auth: sigv4`, a profile **name** and a region. No key or token is ever stored.
- Signed headers are never logged.
- The host passes the same `otlp_hosts` gate as any other.
- Credentials are resolved by the gateway process, not the sandboxed agent.
- Langfuse stays on the static-header path.

## 9. Security and privacy

- **Attributes.** One fixed key allowlist. Every string goes through `validate_attrs` (length cap, credential and
  exfiltration-URL scrub, high-entropy check). No span events with free text, and no `record_exception`.
- **Never recorded by default:** prompt or completion text, tool arguments or results, file paths, MCP server or
  tool names, work-item titles, summaries or decisions. The first five are opt-in per category (§9.1); MCP names
  are their own opt-in (Q2); work-item text is never recorded (Q3).
- **Never recorded at all:** secrets. No setting turns the credential redactor off.
- **Session, root and thread keys** leave the host only hashed (§6).
- **Redaction ratchet.** `_REDACTOR_CALL_RE` in `test/test_security_posture.py` fails a new module containing a redactor call unless it
  is registered in `security_posture*`. The tracing modules of phases 1a to 8 call `validate_attrs` and contain no such call, as the
  metrics OTLP path already does, so no registration and no Sensitive Change Review are needed.
- **No file under Sensitive Change Review is edited** by phases 1a to 8. `secrets/vault.py` is only imported. Hooks
  and their timing are left alone, because `hook_runtime/*` is SCR-watched. Opt-in content capture (§9.1) is the
  one exception: it ships alone as phase 9, with an SCR-reviewed sink registration.
- **Fork safety.** The span batch processor starts a thread and re-creates it in fork children, the failure class
  of [#4219](https://github.com/kirodotdev/KiroCrew/issues/4219) (a multithreaded child breaks the sandbox probe).
  The existing exporter-thread test guard is extended, and a test asserts the probe passes with tracing on.

### 9.1 Opt-in content capture

Content capture is designed here and built in phase 9. Until a switch is on, nothing in this section runs.

**Switches.** Five booleans, each `false` by default and each independent. They are flat `traces_capture_*` keys
on `telemetry`, matching how `TelemetryConfig` (`src/kiro_crew/config/sections_telemetry.py`) already names its
sub-features (`otlp_endpoint`, `beacon_enabled`) and the `traces_*` keys in §8:

| Setting | Attribute | Span |
|---|---|---|
| `telemetry.traces_capture_prompts` | `gen_ai.input.messages` | `chat` |
| `telemetry.traces_capture_completions` | `gen_ai.output.messages` | `chat` |
| `telemetry.traces_capture_tool_arguments` | `gen_ai.tool.call.arguments` | `execute_tool` |
| `telemetry.traces_capture_tool_output` | `gen_ai.tool.call.result` | `execute_tool` |
| `telemetry.traces_capture_file_paths` | no attribute of its own; controls path masking inside the four above | — |

- **The names are the GenAI conventions'.** All four attributes are in the
  [GenAI attribute registry](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/registry/attributes/gen-ai.md),
  status Development, requirement level Opt-In, and are listed in its
  [content capture configuration](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md#content-capture-configuration),
  which requires an explicit user opt-in. The conventions moved to that repository from the main semantic-conventions
  repository. Phase 9 re-checks the names, because Development attributes can still change.
- **Values follow the conventions' message schema** (`role` plus `parts`), serialized as a JSON string where the SDK
  cannot set a structured value.
- **`gen_ai.system_instructions` is not captured.** Kiro Crew's system prompt carries memory and lessons, which no
  category above covers.
- **The switches add no names.** Tool and MCP names still follow Q2, so with MCP names off an MCP call's captured
  arguments sit on a span still named `execute_tool mcp`.

**Secrets are never recorded.** Every captured string goes through this pipeline, in this order, and no setting
skips a step:

1. **Credential pass:** `redact_via_context` (`src/kiro_crew/platform/context.py`), the companion-aware egress
   redactor. It replaces credentials and exfiltration URLs in place, so the rest of the text survives. The
   whole-value `[REDACTED]` of `validate_attrs` would blank almost every prompt, so it is not used for content.
2. **Path pass,** only while `traces_capture_file_paths` is off: `redact_local_paths`
   (`src/kiro_crew/security/redaction.py`) replaces absolute host paths with `[redacted-path]`.
3. **Size caps:** 8,192 characters per attribute and 32,768 per span, as module constants. A cut value ends in
   `…[truncated <n> chars]`, and the span lists the cut keys in `kirocrew.capture.truncated`. Redaction runs before
   the cut, so a cap can never split a secret and leave half of it unredacted. Once a span's budget is spent, later
   attributes are dropped and listed the same way.

A test pins that no config key, environment variable or switch disables step 1.

**File paths, honestly.** With `traces_capture_file_paths` off, the path pass masks absolute paths under the roots
`redact_local_paths` knows (`/home`, `/Users`, `/tmp`, a drive letter and the rest of its list), wherever they sit:
prompt text, a shell command line, an MCP argument. It does not mask relative paths (`src/app.py`), because nothing
tells them apart from ordinary words. A user who turns on tool arguments should expect relative paths in them. With
the switch on, paths are kept as written.

**Both sinks alike.** The switches act where the span is built, so the local JSONL sink and OTLP export receive the
same captured content. The local sink stores it on disk under the telemetry directory, and with an endpoint set it
leaves the host. Each switch's Settings description says so: "Sends this content to your traces endpoint and writes
it to the local trace files." The switches sit in the Traces block in Settings → Privacy, behind phase 7's Feature
Preview. On enterprise builds, which already ignore local `otlp_*` config, they are forced off.

**Langfuse.** Langfuse's [OpenTelemetry mapping](https://langfuse.com/docs/opentelemetry/get-started) fills an
observation's input and output from `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` on tool spans, so
captured tool content appears without extra keys. For model input and output its mapping page names
`langfuse.observation.input` / `.output` and the older `gen_ai.prompt` / `gen_ai.completion`, but not
`gen_ai.input.messages` / `gen_ai.output.messages`. So the `chat` span also carries `langfuse.observation.input`
and `.output`, holding the same redacted, capped JSON, under the Langfuse-key default of Q4. Other backends read the
`gen_ai.*` keys.

**Review.** The capture module calls both redactors, so the `_REDACTOR_CALL_RE` ratchet in
`test/test_security_posture.py` requires it to be registered in `_REDACTION_SINKS`. That registration lives in a
`security_posture_sinks_*.py` file, which Sensitive Change Review watches. Phase 9 is therefore its own PR, and
phases 1a to 8 stay SCR-free.

## 10. Migration plan

Each phase is default off and independently shippable. Phase 2 is where Langfuse and CloudWatch become usable.

| Phase | Scope | Exit criteria (testable) | Size |
|---|---|---|---|
| 1a | Tracing package, local JSONL sink, correlation IDs, the §5.8 id module and its conformance fixture, layer-1 `invoke_agent` with `chat` / `user.prompt` / `turn.complete` / `execute_tool`, spend on agent and chat spans, error status, dashboard root, `session.id` per R6: **a complete plain session turn** | Off: no trace SDK import and no thread. A replayed turn from each engine yields an `invoke_agent` with `session.id`. A Kiro replay yields `usage_details={"credits":…}` and no token keys. A Claude replay yields per-turn USD on both spans. Every id matches the fixture; a session-load tool replay emits nothing. `kirocrew.local.*` is in the file. A planted prompt or credential appears in no attribute. A dashboard turn and a Bot DM each reproduce the §5.1 session tree, and two sessions with the same tool call id stay apart. | ~1,100 lines |
| 1b | Sub-agent nesting (blocking and async); the completion-event join; the generic run-root helpers (R2–R5); Ultra Mode and Bot root/item attributes | A blocking child sits under `spawn_sub_agents`. An async child sits under its ended `spawn_run` span. Its completion turn has the parent's `trace_id`, and two batched completions give a root with two links. Every span in the trace carries the parent's `session.id`. A span-budget overflow starts a linked continuation. A worker turn carries its root and item. | ~700 |
| 2 | OTLP export, vault and env headers, `OTEL_*` vars, governance gate; guide with Langfuse and CloudWatch (API key, collector) walkthroughs and the credits query | Loopback wire test receives spans. A denied host receives nothing. An env endpoint without the switch sends nothing. No header value is logged. `kirocrew.local.*` is absent on the wire. One manual run against Transaction Search shows model and tokens on a Claude turn. | ~750 |
| 2b | Native SigV4 for CloudWatch (`otlp-aws` extra) | A known-answer signature over a fixed body and clock. Signed headers appear in no log record. An unresolvable profile stops export with one warning and keeps the local sink. | ~300 |
| 3 | Surface roots for channels, cron (including script runs), heartbeat, background, webhooks, task runner; `workflow` / `phase` / `workflow outcome` spans and roll-ups; optional `phase_index`; the completion turn in the run's trace; retry attempts | Each engine gets a root with its trigger. A script cron gets a run span and no `chat` span. A three-phase workflow replay reproduces the live trace's tree. The completion turn has the run's `trace_id` and a link to its launcher. A resumed run re-emits no exported span. | ~950 |
| 4 | Steps: approvals with wait time, derived model steps, queue wait, steer, send, broadcast and workflow-launch links, in-turn compaction | An approval's `wait_ms` equals the injected delay. A queued turn links to its sender. No step span carries usage. | ~600 |
| 5 | Ultra Mode round traces (§5.5): round and item anchors and outcomes, `goal_id` on the ledger header, ledger-write and report spans, worker creation, report → wake links, manager changes, patrol health; needs only phase 1b | A two-round replay gives two linked round traces sharing `goal_id`. A worker turn's parent is its item anchor. A restart mid-round re-derives the same ids and re-emits nothing. A batched wake carries one link per report. Ledger spans carry none of the planted free-text sentinels. A Bot root gives `root_kind=mate`. | ~950 |
| 6 | Session lifecycle: create, adopt, close, reset, compaction, model/harness/conductor switch; parent-session links | A session's spans share one `session.id` across an ACP session recycle. | ~500 |
| 7 | Settings UI behind a Feature Preview, secret card | The preview off hides the block. Saving stores a vault reference, never the value. | ~400 |
| 8 | Adopt #24884's `telemetry_context` turn id and `event_id` when it merges; `traceparent` in prompt `_meta` | With #24884 merged, a dashboard span's `kirocrew.turn_id` equals the hook payload's `turn_id`. | ~150 |
| 9 | Opt-in content capture (§9.1): the five `traces_capture_*` switches, the redaction pipeline and caps, the `langfuse.observation.input` / `.output` mirror, Settings rows in phase 7's Traces block, and the `_REDACTION_SINKS` registration; needs phases 1a, 2 and 7, its own PR under Sensitive Change Review | All switches off: no content attribute in the file or on the wire. Each switch on adds only its own attribute. With every switch on, a planted credential and exfiltration URL appear in no attribute. With file paths off, a planted `/home/<user>/…` path reads `[redacted-path]`; with it on, the path is kept. An over-cap value ends in the truncation marker and is listed in `kirocrew.capture.truncated`. The local file and a loopback OTLP receiver hold the same content. No config key or env var skips the credential pass. | ~500 + SCR |

Phases 2b, 3, 4, 5 and 6 can all run in parallel after phase 2. A plain session turn and sub-agent nesting are
complete in phase 1, and the Ultra Mode round waterfall needs only phase 1b's helpers. Phase 9 needs phases 1a, 2
and 7, and is the only phase that edits a file under Sensitive Change Review.

## 11. Coverage and known gaps

Covered (detail in the implementation plan that follows acceptance):
- every model turn on every surface listed in §5.2;
- every step in §5.3;
- sub-agents, workflows, cron (agent and script), webhooks and the task runner;
- the Ultra Mode and Bot tree in §5.5;
- session lifecycle.

Every turn carries credits where the backend reports them.

Not traced, and why:

- **Individual model calls:** no harness reports their start and end, so steps are derived.
- **Tokens and USD for Kiro and Kiro Agent:** only credits are reported. USD comes only from a backend-side price.
- **Built-in child agents of kiro-cli and Claude:** they run inside the parent turn, with spend not split out.
- **MCP server internals:** stdio servers are started by kiro-cli, so only the round trip is visible.
- **Approvals of tools kiro-cli auto-approves:** these never reach Crew.
- **`kirocrew chat` CLI turns:** a separate short-lived process with no tracer.
- **Hook script execution time:** `hook_runtime/*` is SCR-watched.
- **Ultra Mode across Crew instances:** joins only on the raw `item_id`, since hashed keys differ per install.
- **A worker that crashes without reporting:** appears only in the patrol's stale count.
- **Temperature and other request parameters:** Crew never sets or sees them.
- **Token fields on `phase` and `workflow`:** empty by design; totals are `kirocrew.*_total`.
- **gRPC export:** X-Ray's endpoint is HTTP-only anyway.

## 12. Backward compatibility

- Everything is new and off by default.
- The metrics signal, its resource attributes and its `service.name` are unchanged.
- The existing warning when `telemetry.otlp_endpoint` points at a traces route stays. Its text gains a pointer to
  the traces setting.

## 13. Alternatives considered

- **Build spans from crew-log folds** ([rfc-crew-log-telemetry-export.md](rfc-crew-log-telemetry-export.md)). The
  crew log records dashboard turns, steps, tools, approvals and the work ledger, but not channel or background
  turns, and its writer can drop entries at a ceiling. Spans also need live start and end times. Rejected as the
  primary source. The crew log's ids (turn ordinal, child origin) are reused instead.
- **Wait for #24884 and assign ids externally.** That would leave Langfuse users without traces until it merges and
  an adapter exists. Rejected. The names are shared, so both can coexist.
- **One trace per session.** Never exported while a session is open. Rejected.
- **Usage only on `invoke_agent`.** Langfuse types it `agent` and drops the usage, so the usage it counts sits on
  `chat`. `invoke_agent` repeats it for viewers such as CloudWatch. Rejected as the only location.
- **Option B alone (an ACP tap plus hooks, no core spans).** Covered in §5.9: it misses most surfaces, bypasses
  egress governance, and a tap fault breaks the session. Kept as a compatible external producer, not the product.
- **A sidecar collector as the only AWS route.** It is an extra process for a desktop user. Kept as a documented
  route beside native SigV4.
- **A credits-to-USD rate in Crew.** Misleading across plans. Rejected in favour of backend-side pricing.

## 14. Open questions

Each open one has a proposed safe default that applies until a maintainer decides otherwise. Q1 is resolved.

- **Q1. Content capture. Resolved:** build it as opt-in per category (prompts, completions, tool arguments, tool
  output, file paths), every category off by default, secrets never recorded, in phase 9 as its own SCR-reviewed PR
  (§9.1).
- **Q2. Tool names on spans.** Built-in names come from a closed set; MCP server and tool names are user-defined and
  can reveal what someone works on. *Default: built-in names on; MCP calls named only `mcp`; MCP names behind a
  separate opt-in.* Agent names follow the built-in rule.
- **Q3. Work-item title.** A keyed hash of a short free-text title adds little and can be guessed. *Default: no title
  attribute at all;* `item_id` joins everything.
- **Q4. Langfuse-only keys.** *Default: on, on the `chat` span only.* Without them, Kiro credits do not reach
  Langfuse's usage view.
- **Q6. Tracer scope name.** `opentelemetry.instrumentation.kirocrew` would let AgentCore Evaluations' generic
  framework support classify the spans. *Default: `kiro_crew`;* switch only if Evaluations is wanted.
- **Q7. Workflow names in span names** (`workflow <name>`, `phase <title>`, `<phase>:<label>`). These are
  script-authored structure, but free text. *Default: on, capped at 64 characters and passed through
  `validate_attrs`, with a switch to use ordinals instead.*
- **Q9. Ultra Mode trace shape.** *Default: one trace per round (§5.5);* `traces_ultra_mode: "turn"` keeps one
  trace per turn plus links.
- **Q8. Option B packaging.** *Default: external, conforming to §5.8.* A builtin app is blocked by
  `no-new-builtin-apps`.
- **Q5. Sequencing with #24884.** Its split (identity propagation; hook fields under SCR) and its remaining items run
  in parallel. Phase 8 adopts its ids when it merges.
