# Use Cases & Workflows

Real-world workflows from the KiroCrew community. These combine KiroCrew's
capabilities — cron jobs, subagents, memory, Slack, and task runner — into
end-to-end automation.

## Backlog Crusher

The most powerful workflow: KiroCrew digs through your issue backlog, picks up
tasks, implements them, runs tests, opens pull requests, and handles review
comments — all autonomously. It can push 20+ PRs overnight.

```
run docs/task-specs/backlog-crusher.md
```

A typical spec:
1. List open issues in the project's tracker
2. Pick the highest-priority unassigned issue
3. Read the issue description and linked code
4. Implement the change
5. Run the test suite (e.g. `pytest`)
6. Open a pull request targeting the correct branch
7. Move to the next issue

Best paired with `!yolo on` for unattended operation.

## Repetitive Refactors

Automate repetitive cleanup across a codebase. KiroCrew reads a config or
feature-flag list, identifies dead code paths, removes them, and opens pull
requests.

## Slack → Issue Pipeline

Monitor a Slack channel for incoming requests and auto-create issues. Your
coding KiroCrew instance picks them up automatically.

Setup:
1. Set the channel to `observe` mode: `!channel observe`
2. Create a cron job: "Every hour, check #my-channel for new requests and
   create issues for actionable items"
3. Your backlog crusher picks up the new tasks

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

KiroCrew spawns subagents — one per region or source — and synthesizes the
results into a single summary.

## Auto-Collect Information

Replace manual information-gathering workflows. Use KiroCrew cron jobs to
gather data from your sources and publish to a dashboard or static site.

## Oncall Automation

- "Every 30 minutes, check my service health and alert me if anything is red"
- "When I get paged, pull the last 15 minutes of logs for my service"
- Combine with a custom oncall skill for ticket triage

## Code Review Assistance

Switch to a code-reviewer agent for focused review work:

```
!agent code-reviewer
```

Or set it per-thread in Slack:
```
!ta set code-reviewer
```

The agent reads the pull-request diff, checks for common issues, and posts
review comments.

## Multi-Agent Workflows

Run different agents for different concerns:

| Agent | Purpose | Trigger |
|-------|---------|---------|
| `kirocrew` (default) | General tasks, chat | Always |
| `code-reviewer` | CR review | `!ta set code-reviewer` in review threads |
| `oncall-agent` | Incident response | Cron-triggered on alarm channels |
| `doc-writer` | Documentation | Per-tab in dashboard |

Each agent has its own system prompt, tools, and skills — scoped via
per-agent MCP configuration.

## Tips from the Community

- **Unattended overnight runs**: Use `!yolo on` + task runner for long
  autonomous sessions. Review the CRs in the morning.
- **Workspace isolation**: Use `KIROCREW_HOME` and `KIROCREW_PORT` env vars
  to run multiple KiroCrew instances with separate data.
- **Background gateway**: Use a macOS Launch Agent or systemd service to keep
  the gateway running across reboots (see [Getting Started](getting-started.md)).
