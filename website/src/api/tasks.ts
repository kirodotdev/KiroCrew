/**
 * Wire shapes of `/api/tasks*` (`dashboard/handlers/tasks.py`).
 *
 * The durable task queue's read surface: one row per accepted unit of work
 * (subagent run, workflow agent, TaskRunner step), its wait reason while it
 * yields capacity, and the capacity view the "Tasks & capacity" card renders
 * (queue depth by state, effective cap vs the user's ceiling per lane, the
 * degrade reason). Kept beside the client rather than in `types.ts` so the
 * card and the client share one definition without widening the global
 * types module.
 */

/** A `WaitRecord` as the API spells it — why a live run yielded its slot. */
export interface TaskWait {
  reason: string
  since: number | null
  deadline_at: number | null
  /** `at_time` | `signal` | `children` | `input` | `permission`. */
  resume_kind: string
  dependency_scope: string | null
  cancel_semantics: string
  tool_call_id: string
}

export interface TaskRow {
  id: string
  kind: string
  /** One of the taskq states: `queued`, `running`, `waiting_*`, `recovering`, … */
  state: string
  /** Fairness lane: a root session key, or `system` for automation roots. */
  lane: string
  session_key: string
  parent_id: string | null
  root_id: string
  attempts: number
  generation: number
  next_run_at: number | null
  deadline_at: number | null
  lease_owner: string | null
  lease_expires_at: number | null
  wait: TaskWait | null
  wait_reason: string | null
  wait_since: number | null
  wait_deadline_at: number | null
  /** Seconds in the current condition (wait start, else last state write). */
  age_secs: number
  created_at: number
  updated_at: number
  terminal: boolean
}

export interface TasksListResponse {
  available: boolean
  tasks: TaskRow[]
  count: number
  limit?: number
  filters?: { state: string | null; lane: string | null }
}

export interface TaskEvent {
  seq: number
  ts: number
  kind: string
  data: Record<string, unknown>
}

export interface TaskDetailResponse {
  task: TaskRow
  events: TaskEvent[]
}

/** Effective cap for one lane against the user's configured ceiling. */
export interface LaneCap {
  effective?: number | null
  user_max?: number | null
  running?: number | null
  window_queued?: number | null
}

/** One live slot's health classification (`session_health`). */
export interface SlotHealth {
  key: string
  /** `running` | `waiting_children` | `waiting_permission` | `waiting_dependency` | `waiting_input` | `recovering` | `stalled`. */
  classification: string
  age_secs: number
  evidence: string[]
}

export interface RecoveryLadderRow {
  layer: string
  trigger: string
  cleanup_deadline_secs: number | null
  backoff_base_secs: number
  backoff_max_secs: number
  jitter: boolean
  attempts_before_escalation: number
  cooldown_secs: number
  escalates_to: string | null
  automatic: boolean
}

export interface TasksSummary {
  generated_at: number
  /** False when this gateway runs without a task store (legacy in-memory queue). */
  available: boolean
  depth: {
    by_state: Record<string, number>
    queued: number
    waiting: number
    recovering: number
    running: number
    total: number
  }
  oldest_wait_secs: number
  lanes: Record<string, LaneCap>
  degrade_reason: string | null
  /** `resource_status.adaptive_state()`; null when no controller runs here. */
  adaptive: Record<string, unknown> | null
  slots: SlotHealth[]
  waiting: TaskRow[]
  recovering: {
    tasks: TaskRow[]
    task_attempts: number
    slots: Array<{ key: string; age_secs: number; evidence: string[] }>
    ladder: RecoveryLadderRow[]
  }
  stalled: Record<string, { reason: string; since_ts: number; age_secs: number; evidence: string[] }>
  counts: Record<string, number>
  stall_after_secs: number | null
}
