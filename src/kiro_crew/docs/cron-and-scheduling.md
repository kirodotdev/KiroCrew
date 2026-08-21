# Cron Jobs & Scheduling

Kiro Crew can run tasks on a schedule — recurring checks, daily briefings,
periodic monitoring, or one-shot reminders.

## Creating Cron Jobs

### Via Chat (Natural Language)

Just ask naturally:
- "Check my pipeline health every 30 minutes"
- "Remind me to review CRs every day at 9am"
- "Run a status check every 5 minutes"
- "Send me a briefing tomorrow at 8am"

Kiro Crew uses the `cron_add` MCP tool to create the job.

### Via Dashboard

Overview → Cron tab → fill in the form:
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
| Interval | `every <seconds>` | `every 300` (5 min, minimum 60s) |
| One-shot | `at <ISO timestamp>` | `at 2026-03-24T09:00:00` |
| Cron expression | `cron <5-field>` | `cron 0 9 * * 1-5` (weekdays 9am) |

## How It Works

1. The cron timer fires at the scheduled time
2. Kiro Crew creates a fresh LLM session for the job
3. The agent executes the prompt (with full tool access)
4. Results are posted to Slack DM and the dashboard
5. The session is cleaned up

Each cron job runs independently with a 5-minute timeout.

## Per-Agent Cron

Jobs can specify an agent — useful for running specialized agents on a
schedule (e.g., a code-reviewer agent checking for open CRs).

## Approval Mode

Jobs can override the global tool approval mode. Set `approval_mode` to
`"auto"` to auto-approve all tools without prompting, or leave empty to use
the default hook-based approval.

```bash
kirocrew cron add "auto-check" "check pipeline" --every 300 --approval-mode auto
```

Via MCP: `cron_add(name="auto-check", message="check pipeline", every=300, approval_mode="auto")`

## Next Run Display

The dashboard and Slack `cron list` command show the next scheduled run time
for each job, making it easy to see when jobs will fire next.

## Silent Mode

Jobs with `silent: true` suppress auto-delivery to Slack and dashboard. The
agent decides when to notify using the `send_message` MCP tool. Useful for
monitoring jobs that should only alert when something changes.

## Stateless Cron (Ephemeral Sessions)

By default, each cron job reuses the same session across runs — context
accumulates and the agent can reference previous results. For polling or
scanner-style jobs where context accumulation causes OOM or LLM slowdown,
set `persistent_session: false`:

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

The reaper tracks per-run session keys so it can still SIGKILL stuck
ephemeral sessions.

## Skipping Dates

Jobs can skip specific dates — useful for holidays, vacation, or one-off
exceptions. Two optional fields control this:

| Field | Type | Description |
|-------|------|-------------|
| `skip_dates` | `list[str]` | ISO dates to skip: `["2026-04-06", "2026-12-25"]` |
| `timezone` | `str` | IANA timezone for date evaluation (e.g. `"Europe/Luxembourg"`) |

When a job is due but the current local date is in `skip_dates`, the job is
silently not fired. `last_run_ts` is not updated, so the next run naturally
covers the skipped period — agents using "since last run" logic handle gaps
automatically.

The `timezone` field determines what "today" means. Without it, the global
config timezone is used, falling back to UTC. This matters when a UTC date
boundary differs from the user's local date.

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

To avoid traffic spikes from thousands of users' jobs all firing at the same
instant, Kiro Crew adds a small random delay before executing scheduled jobs:

| Schedule frequency | Jitter range |
|--------------------|-------------|
| Hourly (every 1–23h, or hourly cron) | 0–5 minutes |
| Daily/weekly (every ≥24h, or daily cron like `0 9 * * *`) | 0–59 minutes |
| Sub-hourly (every <1h, or `*/5 * * * *`) | None |
| One-shot (`at`) | None |

### Opting out: strict schedule

If your workflow requires exact timing or depends on job ordering, set
`strict_schedule: true` to disable jitter entirely:

```bash
kirocrew cron add "standup-prep" "prepare standup notes" --cron "0 9 * * MON-FRI" --strict-schedule
kirocrew cron update <job-id> --strict-schedule
```

Via MCP: `cron_add(name="standup-prep", message="...", cron_expr="0 9 * * 1-5", strict_schedule=true)`

Via Dashboard: toggle "Strict schedule" when creating or editing a job.

## Managing Jobs

| Action | Dashboard | Slack | CLI |
|--------|-----------|-------|-----|
| List | Overview → Cron tab | `cron list` | `kirocrew cron list` |
| Pause | Pause button | `cron pause <id>` | — |
| Resume | Resume button | `cron resume <id>` | — |
| Delete | Delete button | `cron remove <id>` | `kirocrew cron remove <id>` |
| Adopt / release | — | — | `kirocrew cron adopt <id> --session-of <slot>` / `--release` |

### Which chat session owns a job

A job's `session_key` names the chat session it belongs to, and that one field
carries one meaning: it is also where the job's output is delivered
(`session="origin"` sends and script results both resolve a slot from it). A job
created outside a chat — `kirocrew cron add`, the dashboard Schedule page, an
onboarding import — therefore has no owning session, which is truthful rather
than a defect: there is no chat to deliver into.

The consequence is worth stating plainly, because it is easy to read as a job
that disappeared. A job with no owning session is outside every chat session's
scope: `cron_list` from chat does not list it, and the mutating cron tools answer
a deliberately vague `job not found` so the reply cannot be used to enumerate
jobs the caller may not see. Those jobs are managed from the operator surfaces
instead: `kirocrew cron list`, which is the only surface that shows *who* owns a
job, and the dashboard Schedule page, which lists and manages every job regardless
of owner but does not yet display ownership (its API payload does not carry
`session_key`).

`kirocrew cron adopt` is the way across that line, in both directions:

```
kirocrew cron adopt <id> --session-of chat-3-1712793600   # hand it to a session
kirocrew cron adopt <id> --session-of dashboard:chat-3-1712793600
kirocrew cron adopt <id> --release                        # back to operator-only
```

`--session-of` takes either a bare dashboard slot name or a fully-qualified
session key; a bare name gets the `dashboard:` namespace added. There is no
separate flag for the qualified form, because a key carrying no namespace at all
could never match any session's own key and so could only ever produce a job
nobody can own.

Adopting makes that session able to manage the job **and** the destination for
its results — for this field those are the same fact. Ownership is asserted by
the operator, so the CLI is deliberately the only surface that can write it: the
MCP `cron_update` tool and the dashboard `PATCH` cannot, or a session could
repoint where another job's output lands.

Because an agent can reach the CLI through bash, that restriction is enforced
rather than assumed: `cron adopt` is covered by the `self-protection-cron-adopt`
denied-command rule, so a session cannot claim a job for itself by shelling out.
Like every built-in rule it is default-ON and can be turned off in
Settings → Security.

That rule is **best-effort against a shell-capable agent, not an invariant**. It
guards the command; it does not guard the store. `crons.json` is not among the
protected leaves, so a direct edit of the file sets the same field without the
rule ever matching, and the regex tier cannot see a spelling the shell has yet to
expand (`$K cron adopt`) any more than it can for the rest of the catalog. The
control that does not depend on spelling is the structural one: `session_key` is
not writable through MCP `cron_update` or the dashboard `PATCH` at all.

Ownership and delivery do not have the same reach. A job can be owned by any
session namespace -- a Slack or Telegram session can own and manage one -- but
only a `dashboard:` key resolves to a slot results are injected into, so adopting
into another namespace transfers management without delivery, and the command
says so.

If the key names no recorded session the command still succeeds but warns: a
brand-new tab that has logged nothing yet is a legitimate target (delivery
resolves a live slot first), so the unknown case cannot be refused — but a typo
would otherwise leave the job delivering to nobody, which is the state this
command exists to undo.

## Reliability

- **Failure alerts** — a failed run delivers its REASON to the dashboard bell
  and (when Slack is configured) to the job's channel or the owner's DM, not
  only to the gateway log. Covers all three job kinds and a fire-time policy
  denial, which is otherwise invisible.
- **Failure dedup** — a repeat of the same reason withholds the Slack DM and
  marks the bell as suppressed; a different reason alerts in full, and the DM
  returns after an hour. A `silent: true` job alerts nowhere. Both still count
  toward auto-pause: dedup silences the DM, never the evidence. Success clears
  the failure state, so a relapse alerts fresh.
- **Zombie reaper** — a periodic sweep (60s interval, 30 min deadline)
  force-kills cron jobs whose agent process has become an orphan.
  Prevents resource leaks from stuck jobs.
