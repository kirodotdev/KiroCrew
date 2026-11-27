---
title: Crew background jobs — run a long step off the chat and wake the same chat when it ends
status: draft
author: iamwhatever
created: 2026-11-22
last-audited: 2026-11-26
audited-at: 2bc1117160
doc-pr: null
implementation-prs: []
tracking-issues: [40712]
supersedes: []
superseded-by: []
---

# RFC: Crew background jobs — run a long step off the chat and wake the same chat when it ends

- Status: draft. Nothing built. Reviewed in a worker grill on 2026-11-22 (Gateway, Launcher and Skeptic participants); its edits are applied below. Revised on 2026-11-25 after review on [#40717](https://github.com/kirodotdev/KiroCrew/pull/40717): the owner's decision on the approval path and a maintainer's answers to the open questions are applied. Revised again on 2026-11-26 with the owner's decision to borrow from KAS's jobs layer (§8). All of these choices are listed under [Decisions](#decisions).
- Author: iamwhatever
- Related: [`rfc-agent-started-turns`](rfc-agent-started-turns.md) (the claude-only path this generalises), [`rfc-app-sdk-durable-jobs-and-view-state`](rfc-app-sdk-durable-jobs-and-view-state.md) (the app-scoped job record this borrows from), [`rfc-crew-log-wake`](rfc-crew-log-wake.md).

Code facts were first measured at `c11cc04317` (kirodotdev/KiroCrew main, 2026-11-22) and read again at `6c870d9a41` (main, 2026-11-25) and `2bc1117160` (main, 2026-11-26). They cite symbols, not line numbers. Paths are under `src/kiro_crew/` unless they say otherwise. KAS facts are read in the KAS source, kiro-team/kiro-agent `main` at `92c8574f19` (mainly `packages/acp-type-covenant/JOBS.md` and `packages/kiro-agent/src/jobs/`), and checked at tag `agent-v-0.66.117`, the engine Crew pins (`KAS_PIN_VERSION` in `acp/kas_pin.py`).

## Summary

An agent often has a long step whose outcome it cannot change while it runs: a build, a test suite, a data export, a render, a long download. Today the agent either blocks its turn on that step (the chat is busy for the whole run) or backgrounds it with no way to learn it ended.

This RFC adds **Crew background jobs**. The agent hands Crew one command. Crew checks it with the shell tool's policy plus cron's command vet. When no policy tier decided, Crew asks the person the way the shell tool does: the chat's approval mode applies, and the job card offers **Allow always** for that exact command, a rule only the person's click can write. Crew runs the job in a sandbox at least as strict as the cron floor. The agent's turn ends and the chat is free. When the command exits, Crew posts the result into the **same chat** and starts a turn there through the queue every gateway-started turn already uses. It works on every backend, because the wake is an ordinary prompt turn, not a backend feature.

## Motivation

### What exists today

| Mechanism | What it does | Where it falls short for this ask |
|---|---|---|
| Agent-started turns (claude) | When a `run_in_background` Bash exits, Claude Code starts a turn itself; Crew's idle reader sees it and runs it as its own assistant message (`dashboard/chat_turn/agent_started.py`, `_on_agent_turn`). Config `agent.agent_started_turns`, default `true` (`config/sections_agent.py`). | Claude only: `ACP_BACKENDS_AGENT_STARTED_TURNS = frozenset({ACP_BACKEND_CLAUDE})` (`agent_sdk/backends_turns.py`); any other backend registers nothing (`watch_agent_turns` in `providers/acp_turns.py`). The job lives in the adapter's process tree, so it dies with that process. Phase 3 for other backends is blocked until an adapter marks the end of a turn it starts (`rfc-agent-started-turns.md`, decision 4). |
| `background/completed` crew-log entry | Records a model call the gateway made on the session's behalf (`on_background_completed` in `crew_log/emit_turn.py`). | Not a job record. Its `kind` enum is `title`, `summary`, `memory_consolidation`, `dynamic_card` (`crew_log/entry_types_delegated.py`). Nothing in it says a command ran or ended. |
| `background_wake_pending` / `background_wake_absent` | Answer "will something start another turn in this session?" from pending synthesis, sub-agents, workflow runs and an active monitor loop; `absent` adds queue entries, resource subscriptions and cron jobs (`dashboard/chat_utils_completion.py`). Used to decide the completion sound and the deferred-follow-up notice (`_turn_ending_chain` in `dashboard/chat_turn/turn_ending.py`). | They only read wake owners; they start nothing. A backend-native background command is in neither, so a turn that says "I'll report when the build ends" after a KAS `&` launch gets the "nothing will wake this chat" notice, which is correct today and is the gap. |
| Workflow result delivery | When a workflow run ends, its redacted summary is appended to the originating chat (`inject_workflow_result` in `dashboard/workflow_inject.py`) and a turn is started or queued with `slot.enqueue_or_run_prompt(..., gateway_prompt_kind=WORKFLOW_FOLLOWUP_KIND)` (`_wf_on_done` in `dashboard/server_runtime/workflow_startup.py`). | The right wake shape, backend-neutral. But a workflow run is agent work, not "run this command". |
| `monitor_start` loops | Re-inject a message into the same session every interval; survive gateway restarts; one automation per session (the `monitor_start` schema in `mcp_tools/control_parts/schemas.py`). The gate screens only a named GitHub PR. | Polling a file for "done" costs one model turn per tick, and the loop occupies the chat's only automation slot. |
| Work-ledger wake | A worker's report wakes its conductor through the `work-ledger` probe on the monitor loop (`ledger_wake.py` docstring), the `irq` cheap-probe/expensive-wake pattern (`irq.py` docstring). | Conductor-only, and deliberately cannot carry an instruction: nothing on that server reaches `enqueue_or_run_prompt` (`mcp_work.py` docstring). The probe-then-wake split is reusable. |
| `register_hook` | Registers a webhook that injects a message into a **dedicated** `hook:<id>` session (`register_hook` in `mcp_tools/control.py`). | Wakes a different session, not the same chat. The caller must hold a webhook bearer token and HMAC secret, so a script that calls back must carry a secret. |
| `task_run` | Starts the autonomous task runner from a spec (`task_run` in `mcp_tools/control.py`). | An LLM agent run, not a command; its result opens a task-review slot (`_open_result_slot` in `dashboard/handlers/taskrunner.py`), not the same chat. |
| One-shot cron with `command` | Runs a model-supplied shell command in a sandbox (`mode="cc"`), with a timeout, a cgroup ceiling and a 64 KB output cap (`run_command_sandboxed` and `_MAX_COMMAND_OUTPUT` in `cron_script.py`). | Closest runner. But it is schedule-shaped, it has its own lexical gate (`mcp_cron_vet.py`) instead of the shell tool's policy, and `run_command_sandboxed` itself says it "executes outside the kiro-cli ACP permission/hook flow". |

### KAS

- KAS has a jobs layer, on per session by the `universalBackgroundTool` setting; it is what kiro-cli turns on for its background runs. With it on, an `execute_bash` or `execute_pwsh` call still running after 60 s moves to a job (`BACKGROUND_TOOL_DELAYS_MS` in `packages/kiro-agent/src/jobs/background-tools.ts`). When the job ends on an idle session, KAS starts a turn itself with reason `job-end-wake` (`job-end-notifications.ts`). The engine Crew pins, 0.66.117, already has this code.
- The setting is off unless a client sends it, the session saved it, or the feature registry turns it on; the registry default is off (`UNIVERSAL_BACKGROUND_TOOL` in `packages/kiro-agent/src/feature-config/features.ts`). Crew never sends it: no `universalBackgroundTool`, `_kiro/job` or `job-end-wake` appears under `src/` or `website/src/`.
- If it were on, Crew would drop the wake turn: Crew drains and drops frames that arrive between turns on KAS, kiro and codex (`_run_turn` in `acp/session_handle_turn.py`). Claude's own launch marker says the same for claude: nothing reports the end of that work to Crew (`parse_background_launch` in `acp/_dispatch_parts/adapter_signals.py`).

So on KAS as Crew runs it today there is no path from "command exited" to "this chat gets a turn", and no other backend has one. The wake has to come from Crew. §8 borrows the parts of KAS's design that are not KAS-specific.

## Who uses it

- An agent in a dashboard chat with a long deterministic step: build and test, a dataset export, a video render, a large download.
- A conductor's worker that must wait on its own build without holding a chat slot.
- Non-coding work: convert a folder of documents, run a report script, transcode media.

Not the target: work that needs the model mid-run (use a sub-agent), recurring checks (use cron or `monitor_watch`), and external callbacks (use `register_hook`).

## Goals

1. Works on every backend, KAS included, with no backend change.
2. The wake reaches the same chat, as a normal turn, with the same context, tools, redaction and persistence as a workflow follow-up.
3. A job is never a way around the shell policy, the approval path or the sandbox.
4. Bounded: jobs per chat, runtime, output and disk.
5. Honest at the edges: restart, chat close and a busy chat each have one defined outcome.

## Non-goals

- Changing agent-started turns on claude. A claude agent may keep using native `run_in_background`.
- Re-attaching to a job's process after a gateway restart (see §6).
- Interactive jobs (stdin, TTY), streaming output into the chat while the job runs, or job chains.
- Channel sessions (Slack, Discord, Webex) in v1; see decision 7.
- Reading kiro-cli's own allow rules. Crew cannot read them (§2), so a job's standing allow is a Crew-side rule.

## Design

### 1. Tool surface

Four tools on `kirocrew-core`. None carries `autoApprove`, for the reason the `mcp_work.py` docstring states: an auto-approved MCP tool skips the PreToolUse gate.

| Tool | Input | Returns |
|---|---|---|
| `job_start` | `command` (string), `cwd` (optional, absolute), `timeout_secs` (optional), `label` (optional, short) | `job_id`, the limits applied, and one line telling the agent to end its turn |
| `job_status` | `job_id` (optional; omitted lists the jobs this caller may see) | state (`queued`, `running`, `completed`, `failed`, `timed_out`, `cancelled`, `outcome_unknown`), start time, run time, exit code |
| `job_cancel` | `job_id` | whether a stop was sent and the final state once known |
| `job_output` | `job_id`, `lines` (optional, default 100, at most 1000) | the newest `lines` lines of the job's log, oldest first, redacted, from at most the last 128 KB |

The `job_start` description tells the agent to use it for a step likely to run longer than about a minute. Crew cannot move a shell call that is already running (§8).

**Caller identity.** All four resolve the caller with `require_strict_session_key` (`mcp_core_parts/identity.py`), as `register_hook` does, and accept a session key only from the gateway caller context or the signed per-session token (`_session_key_from_token`). They refuse the plain `KIROCREW_SESSION_KEY` env tier of `_resolve_session_key_strict` outright. That tier is process-keyed and names one tenant of a shared runtime, and its `host_pid_is_shared` guard (`mcp_caller.py`) returns False when the mapping is missing or unreadable, so it can fail open onto another chat's jobs.

**Who may see a job.** A caller may see or touch only jobs its own chat started, with one widening for sub-agents. A sub-agent's job belongs to the sub-agent's key, which its token resolves to. The job record copies `parent_session_key` and `root_session_key` from the `SubagentInfo` record (`subagent_parts/run_record.py`) found by the gate-resolved key, never from an argument or env; an empty `root_session_key` reads as the parent's own key. The parent and the root may list it, read its status and output, and cancel it.

`job_start` is added to `WAKE_ARMING_TOOLS` (`dashboard/chat_utils_turn_text.py`), so the deferred-follow-up notice stays quiet after it.

### 2. Check before run

`job_start` judges the command at least as strictly as a shell tool call from that chat:

1. Run cron's command vet, `_vet_shell_command` (`mcp_cron_vet.py`), on the command. A vet refusal refuses the job. This is the control `cc` mode relies on for `~/.ssh` (see §3).
2. Build a `ToolCall` with `command=<command>`, `is_shell=True` and the chat's own spec grants, and run `HookManager.judge` (`hook_runtime/hook_manager.py`). One shared helper builds this, so the job path and the shell path cannot read different grants.
3. `deny` refuses the job with the same deny notice the shell tool would show. Deny parity holds, because command denial lives in Crew's PreToolUse gate (`build_agent_config` in `agent_parts/spec_build.py` injects no `deniedCommands`).
4. An `allow` that a tier stamped runs the job. An `allow` that no tier stamped means "fall through to the surface's own approval path" (`HookManager.judge` docstring). The job's approval path has three rungs, in this order:
   1. **A stored Allow always rule for this exact command** runs the job with no card (see below).
   2. **The chat's approval mode applies the same way it does for the shell tool.** One shared helper reads the chat's mode for both paths, so the job path and the shell path cannot read different modes. Today that logic sits in the shell tool's path after the gate (`_decide_permission_request` in `dashboard/chat_turn/tool_approval.py`). Only the mode itself counts here (YOLO, the chat's Trust, a scoped Trust grant); the helper skips the shell path's per-command, per-program, trusted-pattern and trust-reads grants, which do not start jobs (decision 14). In a mode where the shell tool would run this command in this chat without asking, the job starts without a card. In a mode where the shell tool would ask, the job card is raised. A mode the installed policy denies for the shell tool is denied for jobs too (`approval_mode_policy.py`).
   3. **The job card.** Its answers are Allow once, Deny and **Allow always** for that exact command. Denying it starts nothing.
5. The vet result, the verdict, what answered an unstamped `allow` (a stored rule, the approval mode or a card answer), the command (redacted) and the job id are written to the SEL audit.

Steps 1 to 3 run on every `job_start`, before step 4. So a stored rule and an approval mode can only answer an `allow` that no tier stamped. They never turn a vet refusal or a `deny` into a start, and they do not change the sandbox floor in §3.

The MCP call to `job_start` is itself judged first, as any MCP tool is. So a job passes every gate a shell call passes, plus the cron vet.

**Allow always.** This is the owner's decision on the approval path (decision 4).

- **What the click stores.** Allow always starts this job and stores one Crew-side rule for the exact command text. The match is string equality on the whole command, never a pattern, as the channel agents' command grants are (`_trusted_commands` in `channel.py`).
- **Who can write it.** Only the person's click. The approval route writes the rule from the command the pending card carries, never from text the client or the agent sent. The shell card's exact-command grant works the same way (`trust_command` in `dashboard/chat_api/slot_approval.py` checks the request against the pending card's `full_command`). The agent cannot write it. No job tool and no other tool takes a rule as input. The gate refuses the agent's file-edit tool on `config.json` (`_tier_write_protected` in `hook_runtime/gate_tiers.py`). For a shell, the OS sandbox is the control: `config.json` is read-only inside every sandbox wrap (the `operator_readonly_layer` docstring in `sandbox_plan.py` calls it a READONLY crew leaf), and a job always runs inside one (§3).
- **When it is offered.** Only when the shell card would offer its own exact-command grant for the same text (`trust_command_grantable` in `dashboard/chat_turn/permission_prompt.py`). A command the card had to redact, or one the exfiltration scan refuses, gets Allow once and Deny only.
- **The next time.** The gateway honors the rule at rung 1. The command text must be equal; one changed character asks again.
- **kiro-cli grants.** Crew still cannot read kiro-cli's own `allowedCommands` (`_doctor_unreadable_allowed_commands`, called from `_doctor` in `cli_doctor.py`), so they are not honored. A command the person allowed there asks once on the job card; Allow always then covers it.
- **Where the rule lives.** In its own list in Crew's config, beside the per-tool grants a person makes with "Always allow this tool" (`hooks.auto_approve_tool_identities`, `dashboard/always_allow_tool.py`), written through the same locked config writer. Settings lists the rules and removes one on request. It holds for every chat of the owner until removed (decision 14).
- **Shell-card grants do not count.** A grant the person made on a shell tool card for one command or one program (`trust_command`, `trust_base`) does not start a job. Only a rule written from a job card does (decision 14).

### 3. Run

Crew owns the process; the backend does not. The runner reuses the parts of `run_command_sandboxed` (`cron_script.py`): shell resolve, `wrap_argv`, `cgroup_scope_argv`, `popen_limited` with `start_new_session=True`, a scrubbed env (`_clean_cron_env` in `cron_script_child_env.py`) and a per-run scratch dir (`_allocate_cron_scratch`).

- **Sandbox.** The job's mode is the stricter of the chat's `agent.sandbox` mode and the cron floor `cc` (`run_command_sandboxed`). The agent's shell runs inside the backend process, which Crew wraps at spawn (`wrap_argv` passed in `acp/client_spawn_env.py`); on kiro-cli that shell may also sit inside kiro-cli's own sandbox. A gateway-spawned job does not inherit kiro-cli's layer, so it must not be looser than `cc`. `cc` leaves `~/.ssh` readable on purpose, and `run_command_sandboxed` names `_vet_shell_command` as the primary control for it, which is why §2 runs that vet.
- **No sandbox backend.** A job refuses to start when the sandbox backend is `none`, whatever `agent.sandbox_allow_unsandboxed_exec` says, and writes the refusal to SEL. `wrap_argv` (`sandbox_wrap.py`) alone is not enough: it raises only in its refusal branch, and runs unconfined once the owner sets that key. The refusal is not a dead end: its text tells the agent the way forward, which is to run the step in its own turn with the shell tool.
- **Env and policy files.** The env scrub drops `_CRON_ENV_DENY`, which includes the same `_AGENT_DENIED_ENV_KEYS` agent children lose (`cron_script_child_env.py`, `sandbox_argv.py`). A job cannot rewrite Crew's policy files: those paths are sealed read-only in every wrap (`_namespace_plan` in `sandbox_plan.py`, `_live_plan_host` in `sandbox_argv.py`).
- **Identity.** Runs as the gateway's OS user, which is the owner. A job may only start from a chat whose execution identity is the owner's own Global store (`capture_session_execution` in `execution_context.py`). A mate member, an app-owned slot or a relayed/remote caller is refused. Incognito and Temporary chats are refused, as `register_hook` refuses them.
- **Dev Fleet pods.** A pod chat's job runs on the pod's own gateway (`_apply_pod_home_remap` in `acp/client_spawn_env.py`).
- **Output.** stdout and stderr go to one file in the job's directory, capped (§5). Nothing is parsed while the job runs.

### 4. Wake the same chat

On exit, Crew writes the final record, then delivers like a workflow result, with a different prompt:

1. Build a short body for the person: label, state, exit code, run time, and the last part of the output (§5), redacted with `redact_exfiltration_urls` then `redact_credentials`, as `_redact` in `dashboard/workflow_inject.py` does. Redact the full tail before cutting it, as `run_command_sandboxed` does for stderr.
2. Append it to the chat as a gateway row with `meta.kind = "job_result"`, durable via `append_if_absent_off_loop`, as `inject_workflow_result` does.
3. Call `slot.enqueue_or_run_prompt(prompt, partial(_run_chat, _synthetic_payload=True), state, gateway_prompt_kind=JOB_FOLLOWUP_KIND)` (`dashboard/state_parts/chat_slot.py`), with a new kind beside `WORKFLOW_FOLLOWUP_KIND` in `GATEWAY_PROMPT_INJECT_KINDS` (`dashboard/chat_utils_injected_rows.py`). The prompt is fixed gateway text with the id and state of each job that ended (§6), never the label. It says the output is not in this conversation and tells the agent to call `job_output(job_id=<id>)`. It does not copy the workflow text "Its result was just posted above" (`_wf_on_done`), which is false on a warm session, and it does not carry a model-chosen name as the workflow prompt does.
4. **Owner gone.** If a sub-agent's session is released before its job exits, the wake goes to `root_session_key or parent_session_key`.

So job output reaches the model only as a `job_output` tool result, which is data. A command cannot write the agent's next instruction.

This is the only new wake. It is backend-neutral because it is an ordinary prompt turn.

`background_wake_pending` gains one owner: a running job of this chat. `background_wake_absent` inherits it.

### 5. Limits

Proposed defaults; each is a config key under `agent.jobs` with a hard ceiling.

| Limit | Default | Ceiling | On breach |
|---|---|---|---|
| Running jobs per chat | 2 | 4 | `job_start` refuses and names the running ids |
| Running jobs per gateway | 8 | 16 | queued as `queued`, starts when a slot frees |
| Run time | 60 min | 6 h | killed (process group); state `timed_out` |
| Run time under a time-limited Trust grant | what is left on the grant | the row above | killed when the grant expires; state `cancelled`, cause "Trust grant expired" |
| Output file per job | 10 MB | 50 MB | the log keeps its start and, once the job ends, its newest end; one gap line marks what was dropped between them |
| Output in the result row | last 4 KB | 16 KB | rest reachable with `job_output` |
| Job records kept | 7 days | — | record, output and scratch dir removed by the sweep |
| Disk for all job dirs | 1 GB | — | `job_start` refuses until the sweep frees space |

The 60-minute default fits the builds named under [Who uses it](#who-uses-it); the agent can still pass a shorter `timeout_secs` (decision 11).

**Approval lifetime.** A job outlives the turn that approved it. When a job starts with no card because a time-limited Trust grant answered it (today the scoped grant `slot._trust_scope`, which `_slot_is_trusted` in `dashboard/chat_turn/tool_approval.py` checks again on every approval), its run time is no longer than what is left on that grant. When the grant expires, Crew cancels the job and records why. This fails closed: a grant that has ended approves nothing that is still running (decision 12). A card answer, a stored Allow always rule and a Trust the person clicked for the chat have no end time, so they set no such limit.

### 6. Edge cases

| Event | Outcome |
|---|---|
| Chat busy when the job ends | The notice goes in as a steer, read at the agent's next model call, where the backend takes steers (`supports_steer` on the ACP client). A re-wake is owed: if the turn ends without the agent calling `job_output` for that job, the follow-up is queued with `enqueue_or_run_prompt`. A backend with no steer gets the queued follow-up only. A conductor "Pause all" also queues it (`crew_pause.holds_slot` in `chat_slot.py`). |
| Several jobs end together | Each appends its row. One queued follow-up per chat is enough: a second finish while one is queued adds its job id and state to that queued prompt instead of queuing a second one, so the prompt names every job that ended since the last follow-up. |
| Session recycled or swept while the job runs | No effect on the job: Crew owns the process, not the backend. The follow-up turn starts or resumes the session as any queued turn does. |
| Sub-agent released while its job runs | The job keeps running; the wake goes to `root_session_key or parent_session_key` (§4). |
| Person closes the chat | Its running jobs, and its sub-agents' jobs whose `parent_session_key` or `root_session_key` is this chat, are cancelled and recorded `cancelled` with cause "chat closed". No fallback slot is created (decision 9). The person cannot tell from the chat title that jobs are running, so the close action says it at that moment, in one line: closing stops N running jobs. A chat with no running job closes as it does today: no added line, and no dialog on every close (decision 5). |
| A time-limited Trust grant ends while its job runs | The job is cancelled and recorded `cancelled` with cause "Trust grant expired" (§5). One wake tells the chat. |
| Person presses Stop in the chat | Stops the current turn only. Jobs keep running; `job_cancel` or a cancel control on the job row stops them. |
| Gateway restart | v1 does not re-attach. On boot, reconciliation marks every job still `running` as `outcome_unknown`, as KAS does for a job whose engine exited, and as the App SDK job record marks its runs interrupted (`_INTERRUPT_MESSAGE` in `apps/job_sdk.py`). It kills any leftover process group it can prove it owns, and delivers one wake per affected chat saying the outcome is unknown. The agent decides whether to start it again. |
| Job's chat is a cron tab or a `hook:` session | Same as any chat, so a cron-run agent can hand off a long step too. |

### 7. What the agent sees

The `job_start` result tells the agent to end its turn and not to poll. The follow-up turn says which job ended and in what state, and tells the agent to read the output with `job_output`. The `job_result` row is for the person reading the chat. The agent must not rely on it: a warm session replays no history, since `_session_replay_history` (`dashboard/chat_turn/prompt_assembly.py`) replays only `if _context_is_new and not _provider_has_history`. On claude, the tool description says native `run_in_background` also works there; on other backends `job_start` is the only way to be woken.

### 8. Borrowed from KAS jobs

KAS's jobs layer (`packages/acp-type-covenant/JOBS.md`) has the same job; Crew copies the parts that do not depend on KAS. All of them are Crew-owned and work on every backend.

| Part | KAS | Crew |
|---|---|---|
| Job id | the tool-use id of the call (JOBS.md) | the ACP `toolCallId` of the `job_start` call, from `CallerContext.tool_call_id` (`mcp_caller.py`); a new id when that is empty |
| Status | pushed as `_kiro/job/update`; clients never poll it | pushed to the chat and the job row on start and end; the agent is told not to poll (§7) |
| Output | `.log` capped at 256 MB, keeping head and tail; `_kiro/job/output` returns the newest lines, default 100, at most 1000, from the last 128 KiB | the same shape, at Crew's caps (§5) and redacted (`job_output`, §1) |
| End states | `completed`, `failed`, `cancelled`, `timed_out`, `outcome_unknown`, `detached` | the same, less `detached` (Crew has no forks) |
| Idle chat at exit | KAS starts a `job-end-wake` turn | the existing queue (§4) |
| Busy chat at exit | notice on the steering buffer, read at the next model call; a re-wake is owed (`deferWake` in `job-end-notifications.ts`) | a steer where the backend takes steers, with a re-wake owed (§6) |
| Check-ins | at most 2 for a running job on an idle session: after 5 min of silence or 30 min after the last turn, 10 min apart (`JOB_CHECK_IN_POLICY` in `job-check-in.ts`) | the same limits; each check-in is a fixed prompt with the job id and state, through the same queue |
| Cancel | stop, then force; wait up to 31 s (`JOB_STOP_WAIT_MS` in `job-runtime.ts`) | stop the process group, then kill it; the same bounded wait; `job_cancel` does not block on it |

**Not borrowed: auto-move.** KAS moves a shell call to a job when it is still running after 60 s (`BACKGROUND_TOOL_DELAYS_MS`). Crew cannot. The backend runs `execute_bash` in its own process tree, and Crew's gate only allows or denies the call; it cannot take over a running process, which also dies when the session recycles. So v1 has no auto-move. The agent starts a job with `job_start`, and its description says to do so for a step likely to run long (§1). Auto-move belongs to the KAS-native path under Alternatives.

**What Crew owns that KAS does not.** KAS jobs serve one KAS session. Crew adds what a gateway runner needs:

- every backend, KAS included, through an ordinary prompt turn (§4);
- caps per chat and per gateway, a run-time default and ceiling, and disk caps (§5); no run-time cap was found in KAS;
- a caller identity the agent cannot forge, owner-only starts, and sub-agent ownership (§1, §3);
- the shell-parity gate, the cron vet and the sandbox floor on every `job_start` (§2, §3), because a gateway-spawned job does not pass through the backend's own permission flow.

## Decisions

Each decision below says who made it. Only decisions 4 to 6 and 15 are the owner's own.

### Picked by the grill

The grill picked these three where participants offered more than one fix. Each was picked by the grill, not by the owner, and stays open to the owner's override.

1. **`~/.ssh` exposure: keep `cc` and run `_vet_shell_command` in `job_start`, rather than defaulting jobs to `strict`.** `strict` hides `~/.ssh` and breaks git and scp over SSH, which long build steps often need (`run_command_sandboxed` says so). The vet is the control cron already relies on, so jobs and cron stay one policy. Picked by the grill; open to the owner's override.
2. **The wake prompt points to `job_output` instead of relying on the row.** The other offer was to fence the row or give it a non-assistant role. Pointing to `job_output` fixes the warm-session gap (§7) and delivers output as a tool result, which is data. The row stays for the person, and a test pins that it is never replayed as assistant text. Picked by the grill; open to the owner's override.
3. **Sub-agent jobs are owned by the sub-agent and visible to its parent and root.** Both keys come from the `SubagentInfo` record found by the gate-resolved key, never from an argument or env. If the sub-agent session is gone at exit, the wake goes to `root_session_key or parent_session_key`. Picked by the grill; open to the owner's override.

### Decided by the owner in review

From buluoray's [review of 2026-11-22](https://github.com/kirodotdev/KiroCrew/pull/40717#pullrequestreview-6014135617) on this document's pull request. That review also says that, with decision 4 in place, the rest of the design looks good.

4. **Approval path: the job card offers Allow always, and the approval mode applies as it does for the shell tool.** The first draft raised the card for every unstamped `allow`, in every approval mode, with no Allow always. A repeated `npm test` then asked every time, even in a mode where the person chose not to be asked. Now the job card offers **Allow always** for that exact command. Clicking it stores a Crew-side rule; only the person's click can write that rule, and the agent cannot. The next time, the gateway honors that rule, and the person's approval mode applies the same way it does for the shell tool. Deny verdicts, the cron vet and the `cc` floor stay as written (§2).
5. **Closing a chat says how many jobs it stops.** One line at that moment, not a dialog on every close (§6). A non-blocking note in the same review.
6. **The no-sandbox refusal gives the way forward.** `job_start` tells the agent to run the step in its own turn (§3). A non-blocking note in the same review.

### Answered by a maintainer in review

From bolichen97's [review comment of 2026-11-22](https://github.com/kirodotdev/KiroCrew/pull/40717#issuecomment-7083658301). These were the open questions of the first draft. This revision applies each answer; each stays open to the owner's override.

7. **Channel sessions are out of v1.** Slack, Discord and Webex sessions queue differently.
8. **`cc` keeps `~/.aws` hidden.** A job that needs cloud credentials, such as `cdk deploy`, runs in the agent's own turn, where the shell tool's approval card sees it.
9. **Closing the chat cancels its jobs.** No fallback `job-<id>` slot: it would be one more place for output nobody reads.
10. **UI: v1 reuses the workflow result row and adds a Cancel action.** A new row kind can wait until people use the feature (Phase 3).
11. **Run time: 60-minute default, 6-hour ceiling.** 30 minutes was short for `cargo build` plus tests (§5). The Design Review on this pull request raised the same point.
12. **Approval lifetime fails closed.** A job started under a time-limited Trust grant runs no longer than what is left on that grant, and is cancelled with a recorded cause when the grant expires (§5).
13. **No re-attach in v1.** `outcome_unknown` plus one wake is enough (§6).

### Picked in this revision

The owner's words for decision 4 leave two details open. This revision picks the narrower answer for each; both stay open to the owner's override.

14. **An Allow always rule is its own stored rule, for every chat of the owner, until removed. Shell-card grants do not start jobs.** The rule lives in its own list in Crew's config, as the "Always allow this tool" grants do (§2). The other offer was to keep it in the chat's own grant store, where the shell card's grants live; that store ends with the chat, so a repeated `npm test` would ask again in each new chat. Reading shell-card grants for jobs was rejected because a person who trusted a command for one shell call did not agree to a run of up to 6 hours outside the turn.

### Decided by the owner on 2026-11-26

15. **Crew keeps running jobs itself, on every backend, and borrows from KAS's jobs layer.** The borrowed parts and the one left out, auto-move, are in §8. Turning on KAS's own jobs is a possible later step for KAS chats (Alternatives).

## Migration plan

### Phase 0 — probe (no product code)

Answer, in this document:

- On KAS 0.66.117 with `universalBackgroundTool` on, do the `_kiro/job/update` frames and the `job-end-wake` turn reach Crew when a moved shell call ends between turns, and what does Crew do with them today? Exit: a recorded answer from a stdio capture. It sizes the KAS-native path under Alternatives; it does not change the gateway runner, which every other backend needs.
- Do common long steps pass under `cc` plus the cron vet: `cargo build`, `npm test`, `cdk synth`? Exit: one table of pass/fail per step and host.

The sample is small on purpose: three steps, one repository each, on two hosts, so six cells.

| Step | Repository |
|---|---|
| `cargo build` | this repository, `native/kc_native` |
| `npm test` | this repository, `website/` |
| `cdk synth` | a new app made by `cdk init app --language typescript` |

Hosts: one Linux host and one macOS host, each with an OS sandbox backend (`detect_backend` in `sandbox_wrap.py` does not return `none`). No Windows host is sampled, because a job refuses to start there (§3). A pass on this sample shows these three steps can run; it does not show that every build can.

### Phase 1 — runner and record (no wake)

Job store (one file per job, one writer, as `apps/job_sdk.py` does), runner, limits, `job_start` / `job_status` / `job_cancel` / `job_output`, the shared shell-judge helper. Exit criteria:

- a command the shell tool denies is refused by `job_start` with the same tier name;
- a command `_vet_shell_command` refuses is refused by `job_start`;
- with no stored rule, an `allow` no tier stamped raises the job card in each approval mode where the shell tool asks for the same command, and starts the job without a card in each mode where the shell tool does not ask; denying the card starts nothing;
- the job card offers Allow always; clicking it stores one rule for that exact command, and the next `job_start` with the same command starts without a card, while a command that differs by one character asks;
- an approve request whose command differs from the pending card's command stores no rule;
- no tool call creates or changes a rule, and a write to the rule store from a job's shell fails;
- a stored rule does not start a command that `_vet_shell_command` or a deny tier refuses;
- a grant made on a shell tool card for the same command does not start a job;
- removing the rule in Settings makes the next `job_start` ask again;
- a job started under a time-limited Trust grant is cancelled when that grant expires, with the cause recorded;
- a parity test runs one table of commands through both paths and asserts equal deny verdicts;
- a job from a member, app-owned, Incognito or Temporary chat is refused;
- a call carrying only a forged `KIROCREW_SESSION_KEY` env value is refused by all four tools;
- a sub-agent's job is listed, readable with `job_status` and `job_output`, and cancellable from its parent and its root, and from no other chat;
- a chat whose approval mode asks, with a shell-card grant or trusted pattern for the command, still raises the job card;
- closing a root chat cancels its sub-agents' running jobs too;
- with a `none` sandbox backend and `agent.sandbox_allow_unsandboxed_exec = true`, `job_start` refuses, writes an SEL record, and its refusal text tells the agent to run the step in its own turn;
- run-time, output and per-chat caps each trip in a test; a capped log keeps its start and its newest end, and `job_output` returns at most 1000 lines;
- a credential-shaped string in output is redacted in `job_output`.

### Phase 2 — wake

`JOB_FOLLOWUP_KIND`, the result row, `enqueue_or_run_prompt`, `background_wake_pending` owner, `WAKE_ARMING_TOOLS` entry, chat-close cancel, restart reconciliation. Exit criteria:

- on KAS and on claude: start `sleep 60; echo done` as a job, end the turn, and a follow-up turn appears in the same chat within seconds of exit, and the agent reads `done` through `job_output`;
- a job that ends during a running turn queues one follow-up that runs after it;
- two jobs that end during one running turn yield one follow-up that names both job ids;
- closing the chat cancels its job and starts no turn;
- closing a chat with two running jobs shows one line saying it stops 2 running jobs, and closing a chat with no running job shows nothing new;
- a gateway restart mid-job yields `outcome_unknown` and one wake per chat;
- a job that ends during a running turn on a backend that takes steers reaches the agent at its next model call, and a follow-up is queued only if the turn ends without `job_output` for that job;
- a silent job on an idle chat gets at most 2 check-in wakes, at least 10 min apart;
- the job id equals the `job_start` call's `toolCallId` when the caller context carries one;
- `job_cancel` on a job that ignores the stop signal kills it within the bounded wait;
- a sub-agent released before its job exits wakes `root_session_key or parent_session_key`;
- job output that contains "ignore previous instructions" never appears in the prompt text;
- the label never appears in the prompt text;
- the `job_result` row is never replayed to the model as assistant text on a cold start.

### Phase 3 — UI

v1 reuses the workflow result row for a job and adds a Cancel action to it (decision 10). A dedicated job row kind with live state waits until people use the feature. Exit criteria:

- a running job shows in its chat as a workflow-style row with a Cancel action, and Cancel ends the job with state `cancelled`;
- a job that has ended shows no Cancel action.

## Backward compatibility

New tools, a new queue kind and a new config section. No stored-data migration. With `agent.jobs.enabled = false`, the tools are not listed and nothing else changes.

## Security considerations

- **At least the shell tool's policy.** One helper builds the shell `ToolCall` for both paths, and a parity test pins equal deny verdicts (§2). The agent can never add an allow rule, and no job tool carries `autoApprove`.
- **Unstamped allow follows the shell tool's approval path.** An `allow` no tier stamped is answered by a stored Allow always rule, else by the chat's approval mode read the same way the shell tool reads it, else by the job card. kiro-cli `allowedCommands` grants are not honored, because Crew cannot read them (§2).
- **Only the person's click writes an Allow always rule.** The approval route writes it from the command on the pending card. The match is the exact command text. No tool takes a rule as input, and the store is read-only inside every sandbox wrap. A rule answers only an unstamped `allow`: the cron vet and every deny tier run first on every start (§2).
- **Cron vet for `~/.ssh`.** `_vet_shell_command` runs on every job, because `cc` leaves `~/.ssh` readable and relies on that vet (§3).
- **Same or stricter sandbox, truly fail closed.** At least `cc`. A `none` sandbox backend refuses the job even when the owner set `agent.sandbox_allow_unsandboxed_exec`, and the refusal is in SEL (§3). Env scrubbed with the cron scrub.
- **Owner only, unforgeable caller.** Caller key from the gateway caller context or the signed token only; the env tier is refused. Refused for members, apps, relayed callers, Incognito and Temporary. Jobs are visible and cancellable only from the chat that started them, plus a sub-agent's parent and root.
- **Output is data, not instruction.** Redacted before it is stored in the row or served by `job_output`. The wake prompt is fixed text with the job id and state only; neither output nor label enters it.
- **Audit.** SEL records on start (with vet result and verdict), refusal, cancel and end.

### Residual risks

- **Approval lifetime.** A job outlives the turn that approved it, so one approval covers its whole run time, up to the 6 h ceiling. A time-limited Trust grant shortens that to what is left on the grant (§5). A card answer, a stored rule and a Trust the person clicked for the chat do not. Not grilled.
- **Script body, over a long run.** `_vet_shell_command` and the policy read only the words of the command line, not the body of a script it calls. A job runs much longer than a shell call, so `./build.sh` can change what it does after it was checked, for up to 6 hours.
- **A stored rule names words, not content.** An Allow always rule for `npm test` or `./build.sh` covers that exact text in any folder of any chat of the owner, whatever the script holds on that day. The sandbox floor, the env scrub, the cron vet and every deny tier still apply to each start, and the person can remove the rule in Settings.
- **A shell with no OS sandbox can write the rule store.** The rules live in `config.json`. Where the agent's own shell runs with no OS sandbox, that shell can already rewrite `config.json`, as it can today for every grant stored there (`_tier_write_protected` says the OS sandbox is the shell-side control). Jobs add no new way in: a job itself always runs sandboxed, and on a host with no sandbox backend `job_start` refuses.
- **One more prompt on kiro-cli.** A command the person already allowed in kiro-cli's own rules still asks on the job card, because Crew cannot read those rules. One Allow always click there ends it.
- **No jobs without an OS sandbox.** Refusing a `none` backend means no jobs on a host without an OS sandbox (every Windows host, per `run_command_sandboxed`), even when the owner opted into unsandboxed exec. The refusal tells the agent to run the step in its own turn (§3).
- **Phase 0 is open.** It is not yet known whether `cargo build`, `npm test` and `cdk synth` pass under `cc` plus the vet. The sample is six cells (Phase 0), so a pass there does not cover every build.

## Alternatives considered

- **KAS-native jobs: turn on `universalBackgroundTool` and accept KAS's own wake turn.** A possible later optimisation for KAS chats, not v1. It would give KAS chats auto-move, and the job would be the same shell call Crew already gates. It needs Crew to send the setting on `session/new` and `session/load`, and to accept a turn it did not start, which means adding KAS to agent-started turns (`rfc-agent-started-turns.md`; KAS now marks such a turn with `turn_start` / `turn_end`). Risks: Crew's idle expiry, runtime recycle and its `session/close` on release would end running jobs; a wake turn's approvals arrive with no Crew turn open and the pre-turn drain in `_run_turn` would reject them; JOBS.md calls the wire experimental; KAS has no per-chat or run-time cap; on the kiro-cli relay route the job runs under kiro-cli's sandbox, not Crew's. It helps no other backend, so the gateway runner stays.
- **One-shot cron with `command`, then `cron_trigger`.** The runner is right, but its only gate is cron's own lexical vet, and it runs outside the permission flow. Using it as is would make a job a way around the shell policy. This RFC reuses its runner and its vet, and adds the shell policy and the approval card.
- **A launcher the agent runs through its own shell** (`kirocrew job run -- <cmd>`). Rejected. No unforgeable chat identity reaches a shell on the shared runtime: KAS runs on the shared `AcpRuntime` (`providers/acp_runtime_start.py`), whose process env names whichever session claimed the process (`_apply_session_identity_env` in `acp/client_spawn_env.py`), and KAS MCP entries keep only `KIROCREW_HOME` (`_MANAGED_ENV_KEYS_KEPT` in `acp/kas_env_withhold.py`). A shell can also forge a "finished" report for a job that never ran. And "the real shell judges first" cannot be built or verified as a pre-step: ACP gives a verdict only as `session/request_permission` while the tool runs (`_handle_permission` in `acp/client_permissions.py`), and Crew cannot read kiro-cli's `allowedCommands` to replay it. Reviving it would first need an identity carrier the agent cannot forge.
- **`monitor_start` polling a done-file.** One model turn per tick, and it takes the chat's only automation slot (the `monitor_start` schema).
- **`register_hook` and a script that POSTs on exit.** Wakes a `hook:` session, not this chat, and the script must hold a webhook secret.
- **`task_run`.** An LLM agent, and its result lands in a task-review slot (`_open_result_slot`).

## Open questions

The seven questions of the first draft were answered in review and are now decisions 7 to 13. What is left:

1. **Phase 0.** Both probes are still to run. Their answers are recorded in this document (Phase 0).
2. **Owner sign-off on what changed after the review.** Decisions 7 to 14 were written into this document after the owner's review. Each stays open to the owner's override.
3. **A dedicated job row.** Whether a later phase adds a job row kind with live state, once people use the feature (decision 10).
