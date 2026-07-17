# Task Runner

The task runner executes multi-step autonomous tasks from spec files. It's
useful for complex workflows that need structured execution with progress
tracking.

## Running a Task

### Via Chat

```
run docs/task-specs/2026/03/my-task/spec.md
```

Or ask naturally: "run the task in my-task/spec.md"

### Via Dashboard

Tasks page → enter the spec file path → click ▶ Start.

### Via Slack

```
run <path-to-spec>
run status
run cancel
```

### Via MCP Tool

The `task_run` MCP tool accepts a spec file path or inline content:

```
task_run(spec="path/to/spec.md")
task_run(spec="__inline__: Step 1: do X\nStep 2: do Y")
```

## Spec File Format

Task specs are markdown files with structured steps:

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

- **Dashboard / chat `run` / Slack `run`** (inside the gateway): tool calls that aren't allow/deny-listed **prompt** interactively.
- **`kirocrew run TASK.md`** (standalone CLI): no interactive channel, so it's **deny-by-default** — a tool runs only if it matches `hooks.auto_approve_tools`; otherwise it's rejected and logged with `reason: headless_no_authorization`. (`TOOL_DENY` / `auto_deny_tools` always wins; the allowlist works with or without a handler.)

To let `kirocrew run` use tools, allowlist them in `~/.kirocrew/config.json`:

```json
{
  "hooks": {
    "auto_approve_tools": ["read", "Reading *", "Running: pytest *", "fs_write"]
  }
}
```

Patterns match the tool title with or without the `Running: `/`Reading ` prefix and support `*` globs. Scope it to the tools the task needs — a blanket `*` re-opens the gap. Or run from the dashboard to approve interactively instead.

## Progress Tracking

The dashboard shows live step progress with status icons:
- ✅ Completed
- 🔄 In progress
- ❌ Failed
- ⏳ Pending

## Multi-Turn Refinement

After a task completes, you can refine the results interactively:
- The agent can ask clarifying questions
- You can provide additional instructions
- The refinement loop has full tool access

## Per-Agent Tasks

Tasks can specify which agent to use, allowing specialized agents for
different types of work.

## Cancellation

Cancel a running task via:
- Dashboard: ■ Cancel button
- Slack: `run cancel`
- API: `POST /api/taskrunner/cancel`
