// Shared types for jobs and runs: scheduled jobs (`CronJob`), task-runner
// projects and their steps, and dynamic-workflow runs. Consumers import them
// from `types/index.ts`.

export interface CronJob {
  member_id?: string
  memory_store?: string
  id: string; name: string; message: string
  enabled: boolean; schedule: string; last_status: string
  cron_expr?: string | null; every?: number | null; every_secs?: number | null
  at?: number | null; created_ts?: number | null
  agent?: string; model?: string; channel?: string; approval_mode?: string; silent?: boolean
  /** Crews a sequence job runs, in order. Takes PRECEDENCE over `agent` at run
   *  time, so any consumer attributing a job to a crew must read this first. */
  agent_sequence?: string[]
  strict_schedule?: boolean
  /** When true, this cron's runs do not appear as a chat session in the active
   * session list (results still go to Slack/notifications + History). Default false. */
  hide_in_chat?: boolean
  /** When true, omit optional saved context and prior session history. V1 also
   * skips its memory, lessons, steering and skills injection; member V2 retains
   * its complete essential guidance and protected identity. Default false. */
  minimal_context?: boolean
  /** Whole-run time limit in seconds (the cron service's `timeout_secs`). */
  timeout_secs?: number
  last_run_ts?: number; next_run_ts?: number | null; has_result?: boolean; has_slot?: boolean
  /** Retry telemetry for the LAST completed run: how many transient-backend
   *  retries it took (0 = none needed) and when that run finished. Absent on
   *  an older gateway or a job that has never run. */
  last_retry_count?: number
  /** The `last_run_ts` `last_retry_count` describes; a mismatch means the count
   *  belongs to an earlier run (a cancelled run advances `last_run_ts` only). */
  last_retry_run_ts?: number
  /** Failed runs in a row; a success resets it to 0. Absent on an older gateway. */
  consecutive_failures?: number
  /** Epoch seconds before which a repeatedly failing job will not fire, or null
   *  when it is not backing off. `next_run_ts` already includes the shift; this
   *  says why the next run moved. Compare against the clock: a past value means
   *  the backoff has elapsed. */
  failure_backoff_until?: number | null
  /** IANA timezone the cron expression's hour/minute fields are stored in.
   * Absent / null for legacy jobs created without an explicit TZ — treat as UTC. */
  timezone?: string | null
  skip_dates?: string[] | null
  script?: string | null; command?: string | null; last_result?: string | null; last_error?: string | null
  is_running?: boolean; running_since?: number | null
  /** The installed app that owns this job, or null/absent for a person-owned
   * one. Host-derived from the job's `created_by` stamp, which an app never
   * supplies, so it cannot be used to claim another app's jobs. Absent on an
   * older gateway. */
  app?: string | null
  /** True only when the USER paused the job; execution never sets it. */
  user_paused?: boolean
  /** Operator-granted vault secrets injected into a script/command job's env at
   * fire time: env-var name -> vault secret NAME (values never leave the vault).
   * Absent/null when the job holds no grant. */
  secret_env?: Record<string, string> | null
  /** Agent-requested grant awaiting the operator's approve/deny. Approving
   * re-verifies the request's code pin server-side, so a job whose script or
   * command changed after the request refuses with `code_changed`. */
  secret_env_pending?: Record<string, string> | null
  secret_env_pending_ts?: number | null
  folder_id?: string
  /** Sidebar chat folder this job's `cron-{id}` tab is filed into, or ""/absent
   *  for a job that is not filed. Persistent jobs only: a stateless job has no
   *  job-wide tab, and the backend refuses the pair at save time. A different
   *  tree from `folder_id` directly above, which groups this job's ROW on the
   *  Schedule page — the two are never read off each other. */
  chat_folder_id?: string
  /** False for a job that runs on a fresh session every fire. The form does not
   *  edit it; it only reads it to disable the chat-folder picker. */
  persistent_session?: boolean
  /** Consecutive failed runs that auto-pause this job (0..10000). 0 never
   *  pauses; a job at 0 or above 5 backs off from its 5th failure instead.
   *  Absent on an older gateway. */
  auto_pause_after_failures?: number
  /** Consecutive failed runs before the failure message is sent (1..10000);
   *  earlier failures only ring the dashboard bell. Absent on an older gateway. */
  alert_after_failures?: number
  /** Chat session that owns this job — ownership decides chat-side reachability
   * (cron_list only lists a session its own jobs). Null for an ownerless job,
   * which is invisible to every chat session and manageable only from the
   * Schedule page or the CLI. */
  session_key?: string | null
  /** Schedule-page template preset id this job was seeded from (e.g.
   * "error-digest"), or null/absent for a blank create or any non-dashboard
   * create surface. Compared against the live SCHEDULE_PRESETS catalog on the
   * Schedule page to hint when the source template's prompt has since changed. */
  source_preset?: string | null
  /** The source template's prompt text as it was when this job was saved. The
   * Schedule page compares THIS against the live template prompt (did the
   * template move?), never the job's current message (which the user may have
   * edited), so the "template updated" hint is attributable. Null/absent when
   * the job carries no template lineage. */
  source_template_prompt?: string | null
}

export interface TaskDetail {
  index: number; title: string; description: string; status: string; error: string; result: string; attempts: number
  depends_on: number[]; requires_approval: boolean; force_approval?: boolean; task_type?: string
  created_at?: number; started_at?: number; finished_at?: number
}
export type RunStatus = 'planning' | 'planned' | 'running' | 'completed' | 'failed' | 'cancelled' | 'paused' | 'pausing';
export interface ProjectRun {
  task_id: string; name?: string; running: boolean; status: RunStatus
  /** Total step count. Named for the wire: `build_status` emits `tasks`, and
   *  `steps`/`current_step` — the names this interface used to declare — are
   *  sent by no producer, so both read `undefined` on every row. */
  tasks: number; completed: number; failed: number; skipped: number
  current_task: number; spec: string; spec_name: string; error: string
  tokens_used: number; replan_count: number; task_details: TaskDetail[]
  started_at: number; finished_at: number
  work_dir: string; branch_name: string
  spec_content: string; lessons_learned: string[]; commits: number
  original_input: string; source: string; groups: number[][]
  /** Whether this run should auto-approve tool calls (per-run trust toggle).
   * Reflects the last chosen value; deny-lists and force_approval gates still block. */
  auto_approve?: boolean
  auto_approve_remaining_secs?: number
}
export interface TaskRunnerStatus {
  running: boolean; available: boolean; runs: ProjectRun[]
  /** Pre-fill value for the per-run workspace-folder selector: configured
   *  taskrunner.workspace_dir if set, else the default per-run base directory. */
  default_workspace_dir?: string
}

/** One row of `GET /api/workflows/runs` — the compact run view (no events).
 *
 *  This is the AUTHORITATIVE status of a dynamic-workflow run: the live
 *  `workflow_run_event` WS stream is one-shot, so a client that was closed,
 *  asleep, or disconnected when a run ended never sees its terminal frame.
 *  Field names are the backend's (`RunHandle.snapshot`), snake_case on the wire.
 */
export interface WorkflowRunSummary {
  run_id: string;
  name?: string;
  /** `running` | `finished` | `failed` | `cancelled`. Typed loosely on purpose —
   *  an unrecognised value is treated as "no evidence" rather than coerced. */
  status?: string;
  error?: string | null;
  /** Originating chat session, `""` for a UI-launched run that belongs to no chat. */
  session_key?: string;
  source_format?: 'python' | 'task-plan';
  driver?: 'workflow' | 'taskrunner' | string;
  task_id?: string;
  capabilities?: string[];
  workflow_id?: string;
  workflow_slug?: string;
  workflow_revision?: number;
  /** Title of the most recent `phase_started` event. */
  phase?: string;
  /** Most recent narrator `log` message. */
  last_log?: string;
}
