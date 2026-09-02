# Cron Jobs & Scheduling

Kiro Crew can run tasks on a schedule — recurring checks, daily briefings, periodic monitoring, or one-shot reminders.

## Creating Cron Jobs

### Via Chat (Natural Language)

Just ask naturally:
- "Check my pipeline health every 30 minutes"
- "Remind me to review CRs every day at 9am"
- "Run a status check every 5 minutes"
- "Send me a briefing tomorrow at 8am"

Kiro Crew uses the `cron_add` MCP tool to create the job.

### Via Dashboard

Schedule page → fill in the form:
- **Name**: descriptive label
- **Message**: the prompt the agent will execute
- **Schedule**: interval in seconds, or cron expression
- **Agent**: optionally pick a specific agent for this job

### Via CLI

```bash
kirocrew cron add "pipeline-check" "check pipeline health" --every 1800
kirocrew cron add "weekday-9am" "check tickets" --cron "0 9 * * MON-FRI" --approval-mode auto
kirocrew cron update <job-id> --name "new name" --message "new prompt" --approval-mode auto
kirocrew cron list
kirocrew cron remove <id>
```

### Via Slack

```
cron list
cron remove <id>
cron pause <id>
cron resume <id>
```

## Schedule Types

| Type | Syntax | Example |
|------|--------|---------|
| Interval | MCP `every=<seconds>` or CLI `--every <seconds>` | `every=300` (5 min, minimum 60s) |
| One-shot | MCP `at=<Unix timestamp>`, `delay=<seconds>`, or `at_time=<human time>` | `at_time="tomorrow 9am"` |
| Cron expression | MCP `cron_expr=<5-field expression>` or CLI `--cron <expression>` | `cron_expr="0 9 * * 1-5"` (weekdays 9am) |

`cron_expr` uses five fields: `min hour dom month dow`; the MCP schema documents numeric day-of-week values `0=Sun` through `6=Sat`.

## How It Works

1. The cron timer fires at the scheduled time.
2. An agent cron sends its message to an LLM session; by default, that session is reused across runs.
3. A script or command cron bypasses the LLM and executes its configured callable or shell command.
4. Results follow the job's delivery and `silent` settings.

The default per-wake execution budget is 30 minutes. Script and command subprocesses have separate defaults of 30 seconds and 300 seconds respectively.

## The Chat Tab a Cron Writes Into

A `persistent_session` job (the default) has ONE dashboard chat tab, `Cron: <name>`, for its whole life — every run appends to the same conversation rather than opening a new tab per run. Replying in that tab is a normal follow-up turn against the job's accumulated session.

Each run appends a pair of rows so the runs stay distinguishable inside that one tab:

| Row | Header | Content |
|-----|--------|---------|
| `user` | `# Cron Run: <name> \| <date time tz>` | the job's `message` — what this run was asked to do |
| `assistant` | `# Cron Job Result: <name> \| <date time tz>` | what the run produced |

The timestamp is the moment the run produced its result, to the second, rendered in the job's `timezone` (then the config timezone, then UTC). It is what lets a follow-up turn tell which run it is answering, so when you reply to a daily job, answer the newest pair. Rows written before this behaviour shipped carry no timestamp.

The stamp is rendered ONCE, when the result is recorded, and stored with it — later edits to the job's `timezone` do not respell an existing row. That matters because a row is recognised as already-written by its exact text, so a re-render under changed settings would append a second copy of a run instead of recognising the first.

Each row also ends with an invisible identity marker, `<!-- cron-run:<job-id>:<epoch> -->`, carrying the run's timestamp at full precision. It does not render, and it is what keeps two runs distinct: the visible stamp is written for a person to read, so its resolution must not decide whether two fast runs collapse into one row.

Re-opening the last result from the Schedule page (`/to-chat`) reuses that stored stamp, so it never duplicates a run the executor already wrote. It shows the result row only: the prompt behind a stored result is not recoverable from the job's current `message`, which may have been edited since, so only the run that produced a result writes the `user` row.

## Per-Agent Cron

Jobs can specify an agent — useful for running specialized agents on a schedule (e.g., a code-reviewer agent checking for open CRs).

## Script and Command Crons

`cron_add` supports three execution modes:
- **Agent cron**: omit `script` and `command`; the message is sent to the selected LLM agent.
- **Script cron**: set `script` to `~/.kiro/crew/crons/file.py:function`; it bypasses the LLM and cannot be combined with `command`.
- **Command cron**: set `command` to a shell command; it bypasses the LLM and cannot be combined with `script`.

For script crons, `message` is available as `ctx.message`. Import the context helpers and flow-control exceptions from `kiro_crew.cron_script`:

```python
from kiro_crew.cron_script import Done, Report, Skip

def run(ctx):
    result = ctx.call_tool("server-name", "tool-name", {"key": "value"})
    ctx.notify(result)
    raise Report("Result recorded; keep this cron scheduled")
```

`ctx.notify(text, **kwargs)` sends a gateway message and `ctx.call_tool(server, tool, args)` invokes an MCP tool. `raise Skip()` ends this tick silently, `raise Done()` completes and removes the job, and `raise Report(message)` delivers a message while keeping the job scheduled.

Preview a script cron locally with real MCP tools; notifications are captured and printed instead of delivered:

```bash
kirocrew cron preview ~/.kiro/crew/crons/my.py:run --message "arguments for ctx.message"
```

## Approval Mode

Jobs can override the global tool approval mode. Set `approval_mode` to `"auto"` to auto-approve all tools without prompting, or leave empty to use the default hook-based approval.

```bash
kirocrew cron add "auto-check" "check pipeline" --every 300 --approval-mode auto
```

Via MCP: `cron_add(name="auto-check", message="check pipeline", every=300, approval_mode="auto")`

## Next Run Display

The dashboard and Slack `cron list` command show the next scheduled run time for each job, making it easy to see when jobs will fire next.

## Silent Mode

Jobs with `silent: true` suppress auto-delivery to Slack and dashboard. The agent decides when to notify using the `send_message` MCP tool. Useful for monitoring jobs that should only alert when something changes.

## Stateless Cron (Ephemeral Sessions)

By default, each agent cron reuses the same session across runs — context accumulates and the agent can reference previous results. For polling or scanner-style jobs where context accumulation causes OOM or LLM slowdown, set `persistent_session: false`:

```
cron_add(
    name="pipeline-scanner",
    message="check pipeline health",
    every=300,
    persistent_session=false
)
```

| Mode | Session Key | Context | Use Case |
|------|-------------|---------|----------|
| `persistent_session: true` (default) | `cron:{id}` (stable) | Accumulates across runs, `last_result` prepended | Digests, trend tracking |
| `persistent_session: false` | `cron:{id}:{uuid}` (fresh each run) | Clean slate, no `last_result` | Polling, scanners, alerting |

The reaper tracks per-run session keys so it can still SIGKILL stuck ephemeral sessions.

## Skipping Dates

Jobs can skip specific dates — useful for holidays, vacation, or one-off exceptions. Two optional fields control this:

| Field | Type | Description |
|-------|------|-------------|
| `skip_dates` | `list[str]` | ISO dates to skip: `["2026-04-06", "2026-12-25"]` |
| `timezone` | `str` | IANA timezone for date evaluation (e.g. `"Europe/Luxembourg"`) |

When a job is due but the current local date is in `skip_dates`, the job is silently not fired. `last_run_ts` is not updated, so the next run naturally covers the skipped period — agents using "since last run" logic handle gaps automatically.

The `timezone` field determines what "today" means. Without it, the global config timezone is used, falling back to UTC. This matters when a UTC date boundary differs from the user's local date.

### Examples

Skip Luxembourg holidays on a morning digest:

```
cron_add(
    name="morning-digest",
    message="Summarize what happened since your last run",
    cron_expr="0 7 * * 1-5",
    timezone="Europe/Luxembourg",
    skip_dates=["2026-04-06", "2026-05-01", "2026-05-14"]
)
```

Add skip dates to an existing job:

```
cron_update(job_id="abc123", skip_dates=["2026-12-25", "2026-12-26"])
```

## Execution Jitter

To avoid traffic spikes from thousands of users' jobs all firing at the same instant, Kiro Crew adds a small random delay before executing scheduled jobs:

| Schedule frequency | Jitter range |
|--------------------|-------------|
| Hourly (every 1–23h, or hourly cron) | 0–5 minutes |
| Daily/weekly (every ≥24h, or daily cron like `0 9 * * *`) | 0–59 minutes |
| Sub-hourly (every <1h, or `*/5 * * * *`) | None |
| One-shot (`at`) | None |

### Opting out: strict schedule

If your workflow requires exact timing or depends on job ordering, set `strict_schedule: true` to disable jitter entirely.

Via MCP: `cron_add(name="standup-prep", message="...", cron_expr="0 9 * * 1-5", strict_schedule=true)`

Via Dashboard: toggle "Strict schedule" when creating or editing a job.

The CLI has no flag for this. `kirocrew cron add` leaves `strict_schedule` at its default of `false`, so a job created there takes the jitter for its frequency; use MCP or the dashboard when you need exact firing.

## Managing Jobs

| Action | Dashboard | Slack | CLI |
|--------|-----------|-------|-----|
| List | Schedule page | `cron list` | `kirocrew cron list` |
| Pause | Pause button | `cron pause <id>` | `kirocrew cron pause <id>` |
| Resume | Resume button | `cron resume <id>` | `kirocrew cron resume <id>` |
| Delete | Delete button | `cron remove <id>` | `kirocrew cron remove <id>` |
| Adopt / release | — | — | `kirocrew cron adopt <id> --session-of <slot>` / `--release` |

### Which chat session owns a job

A job's `session_key` names the chat session it belongs to, and that one field carries one meaning: it is also where the job's output is delivered (`session="origin"` sends and script results both resolve a slot from it). A job created outside a chat — `kirocrew cron add`, the dashboard Schedule page, an onboarding import — therefore has no owning session, which is truthful rather than a defect: there is no chat to deliver into.

The consequence is worth stating plainly, because it is easy to read as a job that disappeared. A job with no owning session is outside every chat session's scope: `cron_list` from chat does not list it, and the mutating cron tools answer a deliberately vague `job not found` so the reply cannot be used to enumerate jobs the caller may not see. Those jobs are managed from the operator surfaces instead: `kirocrew cron list`, which is the only surface that shows *who* owns a job, and the dashboard Schedule page, which lists and manages every job regardless of owner but does not yet display ownership (its API payload does not carry `session_key`).

`kirocrew cron adopt` is the way across that line, in both directions:

```
kirocrew cron adopt <id> --session-of chat-3-1712793600   # hand it to a session
kirocrew cron adopt <id> --session-of dashboard:chat-3-1712793600
kirocrew cron adopt <id> --release                        # back to operator-only
```

`--session-of` takes either a bare dashboard slot name or a fully-qualified session key; a bare name gets the `dashboard:` namespace added. There is no separate flag for the qualified form, because a key carrying no namespace at all could never match any session's own key and so could only ever produce a job nobody can own.

Adopting makes that session able to manage the job **and** the destination for its results — for this field those are the same fact. Ownership is asserted by the operator, so the CLI is deliberately the only surface that can write it: the MCP `cron_update` tool and the dashboard `PATCH` cannot, or a session could repoint where another job's output lands.

Because an agent can reach the CLI through bash, that restriction is enforced rather than assumed: `cron adopt` is covered by the `self-protection-cron-adopt` denied-command rule, so a session cannot claim a job for itself by shelling out. Like every built-in rule it is default-ON and can be turned off in Settings → Security.

That rule is **best-effort against a shell-capable agent, not an invariant**. It guards the command; it does not guard the store. `crons.json` is not among the protected leaves, so a direct edit of the file sets the same field without the rule ever matching, and the regex tier cannot see a spelling the shell has yet to expand (`$K cron adopt`) any more than it can for the rest of the catalog. The control that does not depend on spelling is the structural one: `session_key` is not writable through MCP `cron_update` or the dashboard `PATCH` at all.

Ownership and delivery do not have the same reach. A job can be owned by any session namespace -- a Slack or Telegram session can own and manage one -- but only a `dashboard:` key resolves to a slot results are injected into, so adopting into another namespace transfers management without delivery, and the command says so.

If the key names no recorded session the command still succeeds but warns: a brand-new tab that has logged nothing yet is a legitimate target (delivery resolves a live slot first), so the unknown case cannot be refused — but a typo would otherwise leave the job delivering to nobody, which is the state this command exists to undo.

## Reliability

- **Failure alerts** — a failed run delivers its REASON to the dashboard bell and (when Slack is configured) to the job's channel or the owner's DM, not only to the gateway log. Covers all three job kinds and a fire-time policy denial, which is otherwise invisible.
- **Failure dedup** — a repeat of the same reason withholds the Slack DM and marks the bell as suppressed; a different reason alerts in full, and the DM returns after an hour. A `silent: true` job alerts nowhere. Both still count toward auto-pause: dedup silences the DM, never the evidence. Success clears the failure state, so a relapse alerts fresh.
- **Zombie reaper** — a periodic sweep (60s interval, 30 min deadline) force-kills cron executions that exceed their deadline. Prevents resource leaks from stuck jobs.
