# Subagents & Parallel Work

Kiro Crew can spawn background subagents to handle tasks in parallel. This is
useful for fan-out work like reviewing multiple packages, running parallel
searches, or delegating independent tasks.

## How to Use

### Via Chat

Ask naturally:
- "Review these 3 packages in parallel"
- "Search for X, Y, and Z at the same time"
- "Run this task in the background"

Kiro Crew uses the `spawn_run` MCP tool to create subagents.

### Via Slack

```
spawn review the latest CR for MyPackage
spawn list
```

The grammar is `spawn <task>` (or `bg <task>`) — there is no `run` subcommand;
`spawn list` / `spawn status` render the running subagents.

### Via MCP Tool

The `spawn_run` tool accepts:
- `task` — single task description
- `tasks` — array of tasks for parallel execution
- `agent` / `agents` — optional agent name(s) for each task
- `include_memory` / `include_lessons` / `include_project` — booleans (default `true`) switching off a context group the sub-agent would otherwise inherit
- `max_turns` — per-spawn tool-call budget override (0 = unset, max 1000)
- `model` — model override for this spawn (e.g. `deepseek-3.2`)
- `reasoning_effort` — `low` / `medium` / `high` / `xhigh` / `max`, batch-wide
- `backend` — the agent harness the run starts on instead of the gateway default (`kiro`, `claude`, `codex`, or any other backend installed and allowed on this gateway), batch-wide. A `tasks` entry can set its own `backend` (and `model`) so one wave can send each task to a different harness. A backend that is not allowed here, not installed on this machine, or installed only after the gateway started (restart the gateway to use it), is refused at spawn time with the list of backends that can start (and, for an allowed one that is missing, the command that installs it) rather than replaced by the default. Pass `model` as an id from that backend's own list. A `spawn_continue` follow-up, and a retry, ask for the same backend as the original run
- `light` — mark a helper job (search, lookup, run-and-report, summarize) as light, batch-wide; a `tasks` entry's own `true` or `false` wins for that task. See [Light helpers](#light-helpers)
- `keep` — make the run a continuable conversation with guaranteed resumability and longer retention
- `cwd` — absolute launch directory, which must be under a configured `subagent_cwd_allowed_roots` entry
- `isolate` — `"worktree"` gives each task its own git worktree of the repository `cwd` is in, detached at that checkout's HEAD (uncommitted changes there are not carried over) and placed beside the repository as `<repo>-wt-<run id>`, which must also be under `subagent_cwd_allowed_roots`; parallel writers on one repository then never share a working tree. The completion event names the worktree and its commits. A worktree with commits, changes or ignored files is kept; a clean one is removed when the run's record expires. Needs `cwd`

Setting `model`, `reasoning_effort` or `backend`, or `keep: true`, forces the
dedicated-process path instead of session sharing.

#### Light helpers

A helper that only searches, looks something up, runs a command and reports, or
summarizes rarely needs the chat's model at full reasoning effort. With
`agent.subagent_light_tier` on, a spawn marked `light` runs on the model in
`agent.role_models.subagent_light` and the effort in
`agent.role_efforts.subagent_light`. Both pins can be written through the
configuration API like the plain sub-agent pins; the Settings page does not list
them yet:

```json
{ "agent": { "subagent_light_tier": true,
             "role_models": { "subagent_light": "claude-haiku-4.5" },
             "role_efforts": { "subagent_light": "low" } } }
```

- A `model` or `reasoning_effort` the call names always wins; the light pin fills
  only the one it left unset.
- A light spawn beats the plain `agent.role_models.subagent` pin; any other spawn
  keeps it.
- A spawn that names an `agent`, a `crew` or a `backend` is not changed.
- A pinned model or effort starts the sub-agent in its own process, like any
  per-spawn pin (see above).
- With the setting off, which is the default, every spawn picks the model and
  effort it always has, and a `light` flag is ignored without a word. With the
  setting on, a `light` flag that cannot apply (no pin is set for what the call
  left unset, the pin is not a usable model id) is dropped and the spawn receipt
  says why, naming the setting it found empty.
- The model pin applies only where the run starts on the gateway default backend
  (`agent.acp_backend`), including a backend inherited from a chat created on that
  same backend. Where the run starts is worked out first, the way the run itself
  will be started: a helper started by a Bot's worker runs on that Bot's backend.
  On any other backend, or one that cannot be worked out, only the effort pin
  applies and the receipt's `warning` says the model pin was dropped.
- The receipt's `light` line reports what the tier filled in. The effort verdicts
  (`effort_applied`, `effort_dropped`, `effort_pending`) still report only an
  effort the call named itself, so a light spawn that took its effort from the
  pin shows the `light` line and no effort verdict.
- `agent.subagent_light_lean_context` (off by default) also starts a `light`
  spawn without the lessons and memory blocks, while the tier is on. A helper
  that searches or summarizes seldom needs them, and each block adds tokens to
  every run. A call that sets `include_lessons` or `include_memory` to `true`
  keeps that block, `include_project` is not changed, and a spawn that names an
  `agent`, a `crew` or a `backend` is not narrowed. The receipt's `light` line
  names what was withheld, and the sub-agent is told by name what it lacks. With
  the setting off every spawn starts with the context it always has.

#### When to delegate

Do focused work directly: one task is faster done in the parent, even a
multi-step one. Spawn when the work splits into two or more independent tasks,
when a step would flood the parent's context with bulk output and only the
distilled result is needed, when you need an independent review or reproduction
whose result would be wrong if it saw this session's context, or when the user
asks for delegation or the task needs a different agent, model or crew. A long task, empty slots or a different
model name alone is not a reason to delegate. Guidance lives in the prompt; no
runtime gate refuses a one-task call.

The spawn receipt says whether bounded parent work is supported (a
dashboard-owned parent turn). When supported, the parent may finish at most one
minute of ready non-overlapping work, then end the turn so queued results can
arrive. Otherwise it yields immediately. `spawn_continue` always yields.

Wait through completion events when no useful independent work remains. Do not
repeat a child's task to stay busy. Collect all outcomes in the batch, including
failures, then check artifacts and actual test results before reporting success.
Cancellation and late results must not restart an old task. Inspect side effects
before retrying. Parallel writers need separate ownership; a worktree does not
isolate shared databases, ports or external services.

The other spawn tools:
- `spawn_sub_agents` — same fan-out as `spawn_run`, but BLOCKS and returns the collected results (a member the spawn gate deferred is reported with why it waits, and its result arrives later as a completion event); takes `agents` (array of `{agent_or_mode, prompt}`), `cwd`, and the same `include_*` switches
- `spawn_continue` — dispatch a follow-up turn into a completed run's conversation (`conversation`, `task`, optional `agent` / `max_turns` / `model`); context scope is inherited, so the `include_*` flags are not accepted
- `spawn_steer` — inject a message into a RUNNING subagent's in-flight turn (`agent_id`, `message`, `mode`: `interrupt` default or `follow_up`). A spawn that was accepted but has not started yet answers `queued — not started` (HTTP 409 `queued_not_started`)
- `spawn_release` — end a continuable conversation (`conversation`) so it can no longer be continued. A backend that keeps its own session store answers `Not disposed` instead, since its session was not deleted and may still resume. On a run of yours that is still queued (accepted, not started) it cancels the run instead, so it never starts
- `spawn_list` — list running, queued (accepted, not yet started) and completed subagents
- `spawn_status` — read a run's transcript: the live partial view while it runs, the retained full transcript once complete (see below). A spawn that was accepted but has not started yet answers `QUEUED` with the reason it waits; it starts on its own, so do not spawn it again
- `resource_status` — advisory host headroom (available memory, CPU load, posture, the current concurrent sub-agent cap, and the agent slice's task count against its `pids.max` ceiling). In a nested container whose memory limit sits above the visible cgroup, an ample host reading shows the posture as `unknown` and says the container's limit cannot be read; tight and critical readings still show, and admission keeps using the host figure

## How It Works

1. Kiro Crew spawns one or more subagent processes
2. Each subagent gets its own agent session with its own agent's tools. On the
   default backend it is also refused what the agent that started it is refused
   (a denied command, path or URL in that agent's file), so a restricted agent
   cannot hand a restricted action to a helper. Where that agent's own call
   would ask you (a chained or redirected command its denied list cannot be
   checked against), the helper's asks too. With the "KAS sub-agents"
   Feature Preview on, a subagent on another agent also runs without asking only
   what the chat that started it runs without asking: the pre-approvals in its
   own agent file are not applied, and an `ask` or `deny` in either file still
   holds. An MCP server the chat's agent pre-approves by name is pre-approved
   for the helper's own server of that name too, so that server answers
   without a card even when the helper's file sets it up differently. A `*` in
   the chat agent's `allowedTools`, or an `mcp` allow rule with no `match`,
   names no server and pre-approves none of the helper's MCP tools. To let a
   helper run something unasked, pre-approve it in the chat's
   agent or trust the chat; to keep a helper asking, give its file an `ask`
   rule. An `ask` rule also holds for every helper started below its agent,
   one on the chat's own agent included. A helper already running keeps what
   it started with
3. Results are automatically injected back as `[Subagent completion event]`
4. Kiro Crew synthesizes the results into a final response

After a gateway restart the dashboard's Subagents panel is rebuilt with recent
finished runs: the newest 50 from the last day. Dismissing a card is recorded
durably, so it stays dismissed after a restart. A dismissal that answers HTTP 503
`dismissal_unwritable` could not be saved, and the card may come back.

## Limits

- **Max concurrent**: free memory bounds how many start (each start must leave `agent.spawn_min_memory_gb` free); the count is a high ceiling, `agent.subagent_auto_max` = 32 by default (`agent.max_subagents = 0`); set a positive integer (3 or more) to pin a fixed cap. See [dynamic-subagent-sizing.md](dynamic-subagent-sizing.md)
- **Timeout**: 3 hours per subagent task (`agent.subagent_timeout_secs`, clamped to 60s..86400s at load; 0 means "use the default"; a running subagent keeps the deadline it started with, so a changed value applies to the next run), 20 minutes delivery (semaphore wait + injection), 15 minutes per injection attempt (`KIROCREW_INJECTION_TIMEOUT`, clamped to the delivery cap). A **blocking** `spawn_sub_agents` call collects for at most 2 hours regardless of that setting (`KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT`, itself capped at 7200s), so use `spawn_run` for work longer than 2 hours and read the results from its completion events
- **Turn limit**: 1000 tool calls per subagent by default (configurable via `agent.subagent_max_turns`, maximum 1000). A stored value, including 100, is preserved on upgrade; an unset key automatically uses the current default. Run `kirocrew config defaults` to inspect an older stored default, then use `kirocrew config defaults --adopt agent.subagent_max_turns` only if you want to replace that pin with the current default.
- **Memory guard**: a start is admitted only if at least 2 GB of memory stays available after it, counting what starts still warming up will take. A dedicated-process start (a `model`/`reasoning_effort` override, `keep`, `bare`, `allowed_tools`, or a role model pin) is priced at what such a runtime settles at: about 1 GB until runs of that agent have been measured, then their learned size capped at 2 GB, and never less than `agent.subagent_cost_gb` (0.5 GB by default). A start that shares its parent's runtime is priced about 0.35 GB lower, since it still starts the agent's MCP servers. Each owes that price until two reaper sweeps have measured it (or, where they cannot, two sweep intervals after it first answered), then nothing. If a shared start has to fall back to its own process, the guard re-checks before that process starts. When headroom is insufficient the start waits in the queue rather than failing (an incognito or temporary spawn, or one with the task queue off, waits in memory) and is re-checked as soon as memory can have moved (a subagent finishing, or a free-memory reading taken every few seconds while something waits, which also notices a warming one settling), at the latest when `agent.admit_wait_secs` passes, for at most `agent.subagent_queue_max_wait_secs` (30 minutes by default) of being held back; past that the spawn ends without starting and its parent gets the result `never started: waiting for memory (not enough memory freed up in time); retry once memory frees up`. While it waits, the chip names that limit as the time after which the spawn gives up. Only this floor holds a spawn: the "critically low" memory posture (`agent.resource_critical_gb`) defers cron firings, not subagents. While another chat's subagents wait for memory, a chat that already runs its share of dedicated subagents waits for its next top-level start too, so the memory goes to the waiting chat first (shares follow `agent.lane_weights`); a nested subagent that shares its parent's runtime starts at the floor itself, since its parent already holds its memory. Configure the floor with `agent.spawn_min_memory_gb`; set it to 0 to disable this guard. An install still carrying the old 4.0 default moves to 2.0 once; a value set back afterwards is kept.
- **Nesting**: a subagent can spawn its own subagents. A nested spawn is tracked apart from the parent's wave, so its children are not counted against that wave's completion total
- **macOS memory pressure**: on macOS the memory guard also reads the kernel's memory-pressure level (the one Activity Monitor graphs). While it is WARN or CRITICAL and one of this gateway's dedicated subagents is running, a new start waits even when the free-memory figure is above the floor; the chip says "macOS reports memory pressure". Temporary and incognito spawns wait the same way. A held start is re-checked every 15 seconds and whenever a subagent finishes; if it has been held for `agent.subagent_queue_max_wait_secs` (30 minutes by default) with no other top-level start from the same chat beginning in that time (the starts ahead of it in that chat's queue, from any `spawn_run` batch; scheduled and background jobs share one queue, so a start from any of them counts for all of them), it is ended without starting ("never started: waiting for memory"), and the wave's results count it among those that never started, to resubmit once memory eases; and while an episode lasts longer than that, new starts it would hold end at once. With no dedicated subagent of ours running, the start is judged on the figure alone, and a subagent's own nested subagents are never held. `agent.spawn_min_memory_gb = 0` turns this off with the rest of the guard
- **Redaction**: task strings in SubagentInfo are redacted (credentials, inside URLs too) before surfacing to Slack/dashboard

## KAS sub-agents (a feature preview)

With the "KAS sub-agents" feature preview on (Settings > Developer > Feature
previews, off by default), a KAS dashboard chat on the default agent hands short, independent tasks whose
results it needs in the same reply to the KAS engine's own sub-agent tool
(`invoke_sub_agent`), with the built-in `kirocrew-helper` agent. A KAS sub-agent
runs inside the chat's turn and process: it starts no process and no MCP server of
its own, so the memory guard, the queue and the sub-agent count settings above do
not apply to it, and its cost is part of the chat's turn. It holds the chat agent's
built-in tools and the MCP servers that agent uses, on the same running servers,
but none of Kiro Crew's own tools (memory, skills, artifacts, spawn, cron,
messages). The chat agent's rules decide its calls as they decide the agent's own:
what the agent runs without asking, it runs without asking, and what the agent is
asked about or denied, it is too. Each dispatch is one approval card unless the
chat is trusted or YOLO is on.
The same tool can run your own agents (the ones in your agents folder and the
project's `.kiro/agents`; the first 8 in name order are tried). Run this way, your agent keeps
its own tools, MCP servers and deny rules, runs on the model its own file names
(the chat's model when it names none, or one that is not available to you) and holds no Kiro
Crew tool (memory, skills). As in kiro-cli, the chat agent's rules decide its calls:
what the chat's agent runs without asking, it runs without asking, what the chat's
agent is asked about, it is asked about, and what its own file allows makes no
difference; a tool its own file says to ask about is always asked for. A tool of an MCP server follows the chat agent's rule only when both
agents' files set that server up the same way (the same command, arguments and
every other setting); the two then use one running server. A tool of any other
server is asked for, and so is a tool of a server that has its own `env` values
or `headers`.
This needs an engine that names which sub-agent is asking (kiro-cli 3.5.0 does);
on one that does not, every call of your agent is asked for. One of your agents
can start another (or the helper) when its own tools include the sub-agent tool;
the one it starts follows the chat agent's rules too, is asked for every MCP tool
call, and is asked about a tool whenever its own file, or the file of an agent
that started it, says to ask about it. When Kiro Crew cannot tell which agent
started it (a start you were not asked about), it is asked about a tool whenever
any of your registered agents' files says to ask. An agent whose shell or file-write limits,
blocked web addresses or `preToolUse` hooks Kiro Crew applies only in a chat of its own is not run this
way; `spawn_run` starts it with those limits.
The tool's own list of agents comes from the engine and names more than this
chat runs: Kiro Crew's own agents, your agents past the eighth and an agent left
out for the reasons above are listed and refused when named. The refusal of an
agent past the eighth reads `deny subagent matching "*"`; it refuses that one
agent, not every sub-agent. Why an agent was left out is written to the gateway
log, not to the chat.
`agent.subagent_max_turns`, `agent.subagent_timeout_secs` and a spawn's
`max_turns` do not apply to a KAS sub-agent: the engine ends one after 300 model
calls, or after an hour without progress (KAS engine 0.66.119).
It ends with the turn and cannot be stopped on its own; Stop the turn instead
(a chat whose only sub-agents run in the engine shows no Stop-all control; when
Kiro Crew's own sub-agents run beside them, Stop all stops the turn as well, and
with it the chat's answer). That
chat's agent no longer lists `spawn_sub_agents`; `spawn_run` and everything in this
page stay as they are, for that chat and every other.

While they run, the bar above the message box lists one row per KAS sub-agent with
its task, the tool it is running and how long it has run; click a row to open its
card in the Subagents panel, which shows the task, each tool call and its answer.
A KAS sub-agent has no Stop or Cancel of its own and no "Retry failed". Its card
shows no credit or model figure, because the engine reports neither for one
sub-agent: its credits are part of the answer's total.

After the answer, the reply keeps a closed "Sub-agents in this turn" line, also after
a reload or when you reopen the chat later. Open it to see each KAS sub-agent's task,
how it ended and the names of its tool calls, as the engine reported them.

Turn it on or off under Settings > Developer > Feature previews; the change applies from the next chat, not to one already
running. Off, Kiro Crew starts its own sub-agents for that chat, as before. Either
way an agent you wrote keeps what its file says, and every backend but KAS ignores
the preview. One reading follows kiro-cli while it is on: a dashboard chat on your
own agent whose `tools` is `["*"]` and whose `excludedTools` name `use_subagent`
and `invoke_sub_agent` keeps its `subagent_<name>` tools, and one that excludes
`subagent` keeps `invoke_sub_agent`, so the agent can still start a sub-agent. The
chat says so when it starts. Off, such an agent starts no KAS sub-agent.
An agent whose shell settings set `denyByDefault` and whose `allowedTools` also
names the shell keeps its `allowedCommands` list while the preview is on: a
command outside the list is refused, as on kiro-cli. Off, naming the shell in
`allowedTools` lifts the list when every `deniedCommands` entry can be passed
on as one rule.
Three `deniedCommands` entries also follow kiro-cli while it is on, and refuse more
than they name: `rm \| .*` refuses every shell command (the agent may then have
no shell tool at all), `rm [|] .*` every command
matching `rm * *`, and `rm .*|shred +x` every command matching `rm *` or `shred *x`.
The chat's notice says what each refuses.

## Named Agents

You can specify which agent a subagent should use:

```
spawn_run(tasks=["review code", "check tests"], agents=["code-reviewer", "test-analyzer"])
```

Named agents use their own system prompt and skills.

## Results

Subagent results are posted to:
- The dashboard (via WebSocket notification)
- Slack DM (with an ack button)
- The parent conversation (as completion events)

Long results are split into multiple Slack messages (3900 chars per chunk).

## Completion Event Truncation

The completion event injected back into the parent conversation is a bounded
copy of the subagent's streamed transcript. When the cap drops content, the
event carries a **short preview + the transcript's file path** (not a bare
truncated blob), and the parent reads the rest on demand — the `read` tool
(offset/limit), `grep`, or the `spawn_status` MCP tool — instead of re-running
the subagent.

The full transcript lives at `~/.kiro/crew/subagents/<id>/result.txt` and is
**retained for a grace window after delivery** (default 1 hour) so those reads
succeed; the reaper then prunes it.

Three `agent.*` config knobs control what the parent session sees:

| Key | Values | Default | Effect |
|-----|--------|---------|--------|
| `agent.completion_keep` | `"head"` / `"tail"` / `"both"` | `"head"` | Which end of the transcript to keep when it exceeds the cap |
| `agent.completion_keep_chars` | int (`0` disables) | `3000` | Character cap applied after `completion_keep` |
| `agent.subagent_result_ttl_secs` | int (seconds) | `3600` | How long the delivered `result.txt` is kept before the reaper prunes it. The window starts when the completion reaches the parent, so a completion queued behind a long turn does not spend it waiting |

Pick the mode that matches how your agents emit their useful output:

- **`head`** — first N characters. Best for agents whose verdict appears
  up front (verdict-then-evidence).
- **`tail`** — last N characters. Best for agents that narrate throughout
  and summarize at the end (developer agents, code reviewers, on-call
  triage).
- **`both`** — roughly N/2 from the head, a middle marker, and N/2 from
  the tail. Best for parent agents that need both the task framing and
  the conclusion.

Set `completion_keep_chars: 0` to disable truncation entirely.

Set via `kirocrew config set agent.completion_keep tail` or by editing
`~/.kiro/crew/config.json` directly.

### Reading the full transcript on demand

`spawn_status` reads a run's transcript by agent ID and supports
line-oriented paging (like reading code) for large results:

- `spawn_status(agent_id, limit=200)` — first 200 lines
- `spawn_status(agent_id, offset=200, limit=200)` — next page
- `spawn_status(agent_id, grep="ERROR|FAIL")` — only lines matching the regex

For newly completed runs, the response also reports cumulative credit usage and
elapsed time. The credit total includes billed retry attempts, whether the run
ultimately succeeds or fails. Older retained records remain readable without a
usage line. When credit billing is not reported, only elapsed time is shown,
not a zero-cost claim. The dashboard keeps elapsed time in each card header and
shows a localized “Used … credits” summary when you expand a managed card with
positive reported credits. When credits are not reported, the dashboard identifies
that state in the expanded managed card body. Native subagents share their parent
turn's billing and omit this summary. Elapsed time rounds to whole seconds before
switching to minute-and-second formatting.

A paged/filtered response is prefixed with a continuation header
(`showing lines X-Y of N | more available — call again with offset=Y`). With no
paging args it returns the full transcript of a completed run. You can also
point the generic `read` / `grep` tools straight at the `result_path` from the
completion event.

While the run is still going, the same call returns its live status and the
redacted partial transcript streamed so far, under a
`[RUNNING · <elapsed>s · <N> turns · last tool: <X>]` header. A run still
parked on the spawn-approval prompt has launched no process, so its header
leads with `AWAITING-APPROVAL` instead of `RUNNING` and the body says to
approve it in the dashboard (Approvals) to start it. The partial view grows
as the run streams and, past the manager's bound, is truncated from the
front, so line offsets can shift between polls: `offset`/`limit` paging is
best-effort until completion. Completed-run output is unchanged.
