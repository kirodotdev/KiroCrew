# Task Runner

The task runner executes multi-step autonomous tasks from spec files. It's useful for complex workflows that need structured execution with progress tracking.

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

Runs launched from the dashboard, chat, or Slack can request interactive tool approval. `kirocrew run TASK.md` has no interactive approval surface and rejects tool calls unless they match `hooks.auto_approve_tools`; deny rules always win. Scope allowlist patterns to the tools the task needs rather than using a blanket `*`.

## Progress Tracking

The dashboard shows live step progress with status icons:
- ✅ Completed
- 🔄 In progress
- ❌ Failed
- ⏳ Pending

A run moves through planning and running states, then finishes as completed, failed, cancelled, or paused. Paused, cancelled, and failed runs can be restarted from the saved plan.

## Multi-Turn Refinement

After a task completes, you can refine the results interactively:
- The agent can ask clarifying questions
- You can provide additional instructions
- The refinement loop has full tool access

## Per-Agent Tasks

Tasks can specify which agent to use, allowing specialized agents for different types of work.

## Cancellation

Cancel a running task via:
- Dashboard: ■ Cancel button
- Slack: `run cancel`
- API: `POST /api/taskrunner/cancel`
