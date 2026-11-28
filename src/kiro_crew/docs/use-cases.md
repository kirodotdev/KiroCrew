# Use Cases & Workflows

Real-world workflows from the Kiro Crew community. These combine Kiro Crew's
capabilities — cron jobs, subagents, memory, chat channels, and task runner —
into end-to-end automation.

## Backlog Crusher

An example workflow: Kiro Crew works through your issue backlog, picks up tasks,
implements them, runs tests, opens pull requests, and handles review comments
without you driving each step.

The task runner reads a spec you write. Save the steps below as
`backlog-crusher.md`:

```markdown
# Backlog crusher

1. List open issues in the project's tracker.
2. Pick the highest-priority unassigned issue.
3. Read the issue description and the code it links to.
4. Implement the change.
5. Run the test suite (e.g. `pytest`).
6. Open a pull request targeting the correct branch.
7. Move to the next issue.
```

Then hand that file to the runner. The path is resolved as given, so run this from
the directory holding the file, or pass its absolute path:

```
kirocrew run backlog-crusher.md
```

`kirocrew run --help` lists the flags that matter for a long run: `--fresh`
ignores an existing checkpoint, `--no-test` skips verification between steps,
and `--timeout` caps the whole run.

Standalone `kirocrew run` has nobody to ask for approval, so it is
deny-by-default: a tool runs only when it matches `hooks.auto_approve_tools`.
Allowlist the tools this spec needs (see
[Task Runner → Tool Approval](task-runner.md#tool-approval)), or start the run
from the dashboard Task Runner, where you approve tool calls as they come or
switch on auto-approve for that run.

The `yolo` approval mode applies to the gateway only (`kirocrew gateway
--approval yolo`), not to `kirocrew run`. The gateway refuses it unless
`KIROCREW_HOME` points at an isolated data home rather than the main one.

## Repetitive Refactors

Automate repetitive cleanup across a codebase. Kiro Crew reads a config or
feature-flag list, identifies dead code paths, removes them, and opens pull
requests.

## Slack → Issue Pipeline

An example of composing a cron job with a task-runner spec: a cron prompt turns
requests in a Slack channel into issues, and a separate run works on them.
Nothing picks the new issues up on its own; you start that run.

Setup:
1. Enable observe mode for the source channel.
2. Create a cron job: "Every hour, check #my-channel for new requests and create issues for actionable items"
3. Run a backlog-crusher spec (above) over the new issues

## Daily Briefings

Schedule morning briefings that summarize what matters:

- "Every weekday at 9am, give me a CI/pipeline health summary"
- "Every Monday at 8am, list my open pull requests and their review status"
- "Every day at 5pm, summarize today's Slack activity in #my-team"

These run as cron jobs with results posted to your Slack DM.

Use `skip_dates` and `timezone` to skip holidays or vacation days — the next
run automatically covers the gap. See [Cron Jobs](cron-and-scheduling.md#skipping-dates).

## Parallel Research

Fan out research across multiple sources simultaneously:

> "Research EC2 pricing changes across all regions"

Kiro Crew spawns subagents — one per region or source — and synthesizes the
results into a single summary.

## Auto-Collect Information

Replace manual information-gathering workflows. Use Kiro Crew cron jobs to
gather data from your sources and publish to a dashboard or static site.

## Oncall Automation

- "Every 30 minutes, check my service health and alert me if anything is red"
- "When I get paged, pull the last 15 minutes of logs for my service"
- Combine with a custom oncall skill for ticket triage

## Code Review Assistance

Select an installed review agent from the terminal:

```
kirocrew agent list
kirocrew chat --agent <installed-review-agent>
```

The agent reads the pull-request diff, checks for common issues, and posts
review comments.

## Multi-Agent Workflows

List available agents with `kirocrew agent list`, then select one for a CLI chat with `kirocrew chat --agent <agent-name>`.

Each installed agent has its own system prompt, tools, and skills — scoped via
per-agent MCP configuration.

## Bring In Past Claude Code Sessions

Kiro Crew does not import another tool's chat transcripts. To bring the context
of your past Claude Code sessions in, have an agent summarize them and file each
summary into a folder named after the project it came from:

1. In **Settings → Developer → Feature Previews → Agent MCP servers**, click
   **Add dashboard and debug MCPs to the default agent**. This gives the default
   agent the folder and session tools ([Session Control](session-control.md)).
2. Open a new chat and ask for it, for example:

> "Read my Claude Code sessions under `~/.claude/projects/`. Make a sidebar
> folder for each project they ran in, named after the project. For each
> session, open a new chat in that folder that starts with a summary: what I
> worked on, what was decided, what is still open, and the session's date.
> Leave tokens, keys and passwords out of the summaries."

Claude Code keeps one folder per working directory under `~/.claude/projects/`
(`$CLAUDE_CONFIG_DIR/projects/` when you set that) on the machine the gateway
runs on. To bring in only some projects, name them in the request. Chats you ran
on the Claude Code harness inside Kiro Crew are stored there too, and are
summarized with the rest unless you leave their projects out. A second run
summarizes the same sessions again, so name only the new ones when you run it
again.

One chat can keep at most 50 chats it opened at a time, and open at most 20 in
five minutes. To bring in more, close the summaries you have read, or raise
**Settings → Chat → Sessions → Sessions per creator**.

The agent reads each transcript in full, tool output included, so whatever those
sessions printed or you pasted into them reaches Kiro Crew's model. Read each
chat it is about to open before you approve it.

Each summary is a new chat, dated the day it is created. It holds the summary,
not the original turns. The new chat's agent answers the summary as soon as the
chat opens, so every session brought in costs one model turn. The agent
cannot link a folder to a directory: to tie one to its project, open the
folder's **Folder settings** and set **Project directory**.

## Tips from the Community

- **Unattended overnight runs**: For `kirocrew run`, allowlist the tools the spec needs in `hooks.auto_approve_tools`; for a gateway started with `--approval yolo`, use an isolated `KIROCREW_HOME`. Review the pull requests in the morning.
- **Trust and auto-approve outlast a restart**: A chat's Trust or Trust reads, and auto-approve (YOLO) turned on from the dashboard, Slack or the API, stay on when the gateway restarts, updates, crashes or the machine reboots; a timed auto-approve keeps the time it had left. They end only when you stop Kiro Crew yourself (`kirocrew stop`, Quit in the desktop app, or Ctrl+C on a gateway running in your terminal), or pick another mode. A policy that now forbids a mode still turns it off at the next start.
- **Workspace isolation**: Use `KIROCREW_HOME` and `KIROCREW_PORT` env vars to run multiple Kiro Crew instances with separate data.
- **Background gateway**: Use a macOS Launch Agent or systemd service to keep the gateway running across reboots (see [Getting Started](getting-started.md)).
