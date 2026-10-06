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
- `keep` — make the run a continuable conversation with guaranteed resumability and longer retention
- `cwd` — absolute launch directory, which must be under a configured `subagent_cwd_allowed_roots` entry

Setting `model` or `reasoning_effort`, or `keep: true`, forces the
dedicated-process path instead of session sharing.

#### When to delegate

Do focused work directly: one task is faster done in the parent, even a
multi-step one. Spawn when the work splits into two or more independent tasks,
when a step would flood the parent's context with bulk output and only the
distilled result is needed, when you need an independent review or reproduction
whose result would be wrong if it saw this session's context, or when the user
asks for delegation or the task needs a different agent, model or crew. A long task, empty slots or a different
model name alone is not a reason to delegate. Guidance lives in the prompt; no
runtime gate refuses a one-task call. (A solo-spawn gate shipped in #11710 and
was removed after audit data showed it passed 33 of 34 one-task calls.)

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
- `spawn_release` — end a continuable conversation (`conversation`) so it can no longer be continued
- `spawn_list` — list running, queued (accepted, not yet started) and completed subagents
- `spawn_status` — read a run's transcript: the live partial view while it runs, the retained full transcript once complete (see below). A spawn that was accepted but has not started yet answers `QUEUED` with the reason it waits; it starts on its own, so do not spawn it again
- `resource_status` — advisory host headroom (available memory, CPU load, posture, the current concurrent sub-agent cap, and the agent slice's task count against its `pids.max` ceiling)

## How It Works

1. Kiro Crew spawns one or more subagent processes
2. Each subagent gets its own agent session with full tool access
3. Results are automatically injected back as `[Subagent completion event]`
4. Kiro Crew synthesizes the results into a final response

After a gateway restart the dashboard's Subagents panel is rebuilt with recent
finished runs: the newest 50 from the last day. Dismissing a card is recorded
durably, so it stays dismissed after a restart. A dismissal that answers HTTP 503
`dismissal_unwritable` could not be saved, and the card may come back.

## Limits

- **Max concurrent**: free memory bounds how many start (each start must leave `agent.spawn_min_memory_gb` free); the count is a high ceiling, `agent.subagent_auto_max` = 32 by default (`agent.max_subagents = 0`); set a positive integer (3 or more) to pin a fixed cap. See [dynamic-subagent-sizing.md](dynamic-subagent-sizing.md)
- **Timeout**: 3 hours per subagent task (`agent.subagent_timeout_secs`, clamped to 60s..86400s at load; 0 means "use the default"), 20 minutes delivery (semaphore wait + injection), 15 minutes per injection attempt (`KIROCREW_INJECTION_TIMEOUT`, clamped to the delivery cap). A **blocking** `spawn_sub_agents` call collects for at most 2 hours regardless of that setting (`KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT`, itself capped at 7200s), so use `spawn_run` for work longer than 2 hours and read the results from its completion events
- **Turn limit**: 1000 tool calls per subagent by default (configurable via `agent.subagent_max_turns`, maximum 1000). A stored value, including 100, is preserved on upgrade; an unset key automatically uses the current default. Run `kirocrew config defaults` to inspect an older stored default, then use `kirocrew config defaults --adopt agent.subagent_max_turns` only if you want to replace that pin with the current default.
- **Memory guard**: a start is admitted only if at least 2 GB of memory stays available after it, counting what starts still warming up will take. A dedicated-process start (a `model`/`reasoning_effort` override, `keep`, `bare`, `allowed_tools`, or a role model pin) is priced at what such a runtime settles at: about 1 GB until runs of that agent have been measured, then their learned size capped at 2 GB, and never less than `agent.subagent_cost_gb` (0.5 GB by default). A start that shares its parent's runtime is priced about 0.35 GB lower, since it still starts the agent's MCP servers. Each owes that price until two reaper sweeps have measured it (or, where they cannot, two sweep intervals after it first answered), then nothing. If a shared start has to fall back to its own process, the guard re-checks before that process starts. When headroom is insufficient the start waits in the queue rather than failing (an incognito or temporary spawn, or one with the task queue off, waits in memory) and is re-checked as soon as memory can have moved (a subagent finishing, or a free-memory reading taken every few seconds while something waits, which also notices a warming one settling), at the latest when `agent.admit_wait_secs` passes, for at most `agent.subagent_queue_max_wait_secs` (30 minutes by default) of being held back; past that the spawn ends without starting and its parent gets the result `never started: waiting for memory`. Only this floor holds a spawn: the "critically low" memory posture (`agent.resource_critical_gb`) defers cron firings, not subagents. While another chat's subagents wait for memory, a chat that already runs its share of dedicated subagents waits for its next top-level start too, so the memory goes to the waiting chat first (shares follow `agent.lane_weights`); a nested subagent that shares its parent's runtime starts at the floor itself, since its parent already holds its memory. Configure the floor with `agent.spawn_min_memory_gb`; set it to 0 to disable this guard. An install still carrying the old 4.0 default moves to 2.0 once; a value set back afterwards is kept.
- **Nesting**: a subagent can spawn its own subagents. A nested spawn is tracked apart from the parent's wave, so its children are not counted against that wave's completion total
- **macOS memory pressure**: on macOS the memory guard also reads the kernel's memory-pressure level (the one Activity Monitor graphs). While it is WARN or CRITICAL and one of this gateway's dedicated subagents is running, a new start waits even when the free-memory figure is above the floor; the chip says "macOS reports memory pressure". Temporary and incognito spawns wait the same way. A held start is re-checked every 15 seconds and whenever a subagent finishes; if the pressure has not eased after `agent.subagent_queue_max_wait_secs` (30 minutes by default) it is ended without starting ("never started: waiting for memory"), and while an episode lasts longer than that, new starts it would hold end at once. With no dedicated subagent of ours running, the start is judged on the figure alone, and a subagent's own nested subagents are never held. `agent.spawn_min_memory_gb = 0` turns this off with the rest of the guard
- **Redaction**: task strings in SubagentInfo are redacted (credentials + exfiltration URLs) before surfacing to Slack/dashboard

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
