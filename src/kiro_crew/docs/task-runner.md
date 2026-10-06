# Task Runner

The task runner executes multi-step autonomous tasks from spec files. It's useful for complex workflows that need structured execution with progress tracking.

## Running a Task

### Via Chat

In a dashboard chat, ask naturally: "run the task in my-task/spec.md". The agent
starts it with the `task_run` tool.

### Via Dashboard

Open the **Task Runner** page and use **New Task**. Describe the task in plain
words (**Compose**), paste or upload a spec (**From Spec**), or paste or upload a
YAML plan (**From YAML**). Then press **Run**, or **Plan** to review the steps
before they run. The page has no spec-path field; a spec path is accepted by
`kirocrew run`, the `task_run` tool, the `task run` keyword below and
`POST /api/taskrunner`.

### Via Slack

```
task run <path-to-spec>
task run status
task run cancel
```

`project run` is an alias for `task run`. A bare `run <path>`, `run status` or
`run cancel` is not a command: it reaches the agent as an ordinary message.

### Via CLI

```bash
kirocrew run TASK.md
kirocrew run TASK.md --fresh
kirocrew run TASK.md --no-test
kirocrew run TASK.md --timeout 3600
```

### Via MCP Tool

The `task_run` MCP tool accepts a spec file path or inline content:

```
task_run(spec="path/to/spec.md")
task_run(spec="__inline__: Step 1: do X\nStep 2: do Y")
```

`spec` is required; `name` is optional and is otherwise derived from the spec.

## Spec File Format

A nonempty text or Markdown file is accepted. The runner sends its content to the task decomposer, which derives executable steps; Markdown headings and numbered steps are a useful convention, not a required schema.

```markdown
# Task: Implement Feature X

## Steps

1. Read the current implementation in `src/module.py`
2. Add the new function `process_data()`
3. Write tests in `test/test_module.py`
4. Run `pytest` and fix any failures
```

## Tool Approval

Approval depends on how the run was launched:

- **Dashboard / chat / Slack `task run`** (inside the gateway): tool calls that aren't allow/deny-listed **prompt** interactively.
- **`kirocrew run TASK.md`** (standalone CLI): no interactive channel, so it's **deny-by-default** — a tool runs only if it matches `hooks.auto_approve_tools`; otherwise it's rejected and logged with `reason: headless_no_authorization`. (`TOOL_DENY` / `auto_deny_tools` always wins; the allowlist works with or without a handler.)

During **step execution**, an allowlisted **shell** tool is additionally
verified before the grant is honoured: each program name in the command must
still resolve to the program it appears to name. A name that is shadowed on
`PATH`, resolves inside a tree the agent can write (the project checkout,
`.venv/bin`, `node_modules/.bin`), or has never been identified by a dashboard
approval is declined — interactively launched runs fall back to the prompt;
headless runs record the decline as `reason: name_grant` with the refusal
code, then reject the tool as `reason: headless_no_authorization` (the same
deny-by-default row as an unmatched tool). On Windows this verification cannot
model the shell's lookup at all, so headless shell auto-approve is declined
entirely there. This is deliberate: an unattended run is exactly where a
planted `~/.local/bin/head` would otherwise inherit the grant with nobody
watching. Programs the system directories carry (`/usr/bin/pytest` installed
system-wide) keep auto-approving; a project-local `.venv/bin/pytest` needs the
interactive prompt (or a dashboard-approved identity) instead.

To let `kirocrew run` use tools, allowlist them in `~/.kiro/crew/config.json`:

```json
{
  "hooks": {
    "auto_approve_tools": ["read", "Reading *", "Running: pytest *", "fs_write"]
  }
}
```

Patterns match the tool title with or without the `Running: `/`Reading ` prefix and support `*` globs. Scope it to the tools the task needs — a blanket `*` re-opens the gap. Or run from the dashboard to approve interactively instead.

## Progress Tracking

The Task Runner page lists each run with its state — running, completed,
failed, cancelled or planned — and its step progress.

A run moves through planning and running states, then finishes as completed, failed, cancelled, or paused. Paused, cancelled, and failed runs can be restarted from the saved plan.

## Refining a Request

Before a run, **Refine into Spec** (on the Compose tab) turns a plain request into
a structured spec. It makes no tool calls and asks no questions: every tool
request it raises is refused and audited.

After a run completes, press **Chat** to continue from its results in an
ordinary chat.

## Choosing the Agent

The agent is chosen per run, not by the spec: use the **Agent** selector on the
Task Runner page, or the `agent` field of `POST /api/taskrunner`.

## Cancellation

Cancel a running task via:
- Dashboard: the **Cancel** button on the run
- Slack: `task run cancel`
- API: `POST /api/taskrunner/cancel`
