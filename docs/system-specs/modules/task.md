# Task Module

Last Updated: 2026-02-16

## Overview

The task module (`kiro_crew/task.py`) provides a state machine for
tracking individual messages through their lifecycle. Each Slack message
or CLI prompt becomes a `Task` with validated state transitions.

## Task States

```
Pending → InProgress → AwaitingApproval → Completed
                     → Completed
                     → Failed
                     → Cancelled
```

Terminal states: `Completed`, `Failed`, `Cancelled` — no further
transitions allowed.

## APIs

### `Task(id)`
Creates a task in `PENDING` state with creation timestamp.

### `task.start()`
Transition to `IN_PROGRESS`. Records `started_at` timestamp.

### `task.complete()`
Transition to `COMPLETED`. Records `finished_at` timestamp.

### `task.fail(error="")`
Transition to `FAILED`. Records `finished_at` and `error` string.

### `task.cancel()`
Transition to `CANCELLED`. Records `finished_at` timestamp.

### `task.await_approval()`
Transition to `AWAITING_APPROVAL` (from `IN_PROGRESS`).

### `task.resume()`
Transition back to `IN_PROGRESS` (from `AWAITING_APPROVAL`).

### `task.is_terminal -> bool`
True if state is `COMPLETED`, `FAILED`, or `CANCELLED`.

### `task.transition(to)`
Low-level transition with validation. Raises `InvalidTransition` if
the transition is not in the allowed set.

## Valid Transitions

| From | To |
|------|----|
| PENDING | IN_PROGRESS, CANCELLED |
| IN_PROGRESS | AWAITING_APPROVAL, COMPLETED, FAILED, CANCELLED |
| AWAITING_APPROVAL | IN_PROGRESS, COMPLETED, FAILED, CANCELLED |

## Constants

Task state constants are in `task.py` as the `TaskState` enum.
