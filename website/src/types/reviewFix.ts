export type ReviewFixState =
  | 'draft'
  | 'planning'
  | 'awaiting_group_confirmation'
  | 'running'
  | 'awaiting_validation'
  | 'ready_to_apply'
  | 'awaiting_commit'
  | 'committed'
  | 'awaiting_push'
  | 'pushed'
  | 'rereviewing'
  | 'done'
  | 'paused'
  | 'failed'
  | 'blocked_model_resolution'
  | 'blocked_dirty_overlap'
  | 'blocked_validation'

export type ReviewFixTargetMode = 'current_branch'

export interface ReviewFixFindingSnapshot {
  key: string
  title?: string
  severity?: string
  body?: string
  file_path?: string
  line?: number | null
  end_line?: number | null
  fingerprint?: string
  suggested_fix?: string
}

export interface ReviewFixTarget {
  mode: ReviewFixTargetMode
  repo_root: string
  target_path: string
  target_ref: string
  branch_name: string
  head_sha: string
  dirty_fingerprint: string
  tracked_paths: string[]
  untracked_paths: string[]
  upstream: string
  remote: string
}

export interface ReviewFixModel {
  requested_model: string
  provider: string
  resolved_model_id: string
  advertised_model_ids: string[]
  resolved_at: number
}

export interface ReviewFixValidation {
  validation_id: string
  group_id: string
  group_revision: number
  kind: string
  command: string[]
  exit_code: number | null
  passed: boolean
  output_path?: string
  artifact_path?: string
  started_at: number
  finished_at: number
  duration_secs: number
  error?: string
}

export type ReviewFixGroupState =
  | 'proposed'
  | 'confirmed'
  | 'executing'
  | 'validating'
  | 'ready_to_apply'
  | 'applied'
  | 'committed'

export interface ReviewFixGroup {
  group_id: string
  finding_keys: string[]
  hard_edges: Array<Record<string, string>>
  soft_edges: Array<Record<string, string>>
  reasons: string[]
  affected_files: string[]
  hard: boolean
  state: ReviewFixGroupState
  revision: number
  candidate_patch_id: string
  candidate_base_sha: string
  candidate_head_sha: string
  patch_path: string
  diff_path: string
  validation_runs: ReviewFixValidation[]
  apply_confirmed: boolean
  applied_at: number
  commit_hash: string
  commit_message: string
}

export interface ReviewFixGit {
  candidate_worktree_path: string
  candidate_branch: string
  candidate_ref: string
  destination_worktree_path: string
  destination_branch: string
  proposed_branch: string
  confirmed_branch: string
  remote: string
  upstream: string
  push_preview: Record<string, unknown>
  push_result: Record<string, unknown>
  rereview_run_id: string
}

export interface ReviewFixAuditEvent {
  action: string
  from_state: string
  to_state: string
  revision: number
  actor: string
  timestamp: number
  details: Record<string, unknown>
}

export interface ReviewFixMetadata {
  review_run_id: string
  pr_url: string
  source_head_sha: string
  selected_finding_keys: string[]
  finding_snapshots: ReviewFixFindingSnapshot[]
  state: ReviewFixState
  revision: number
  target: ReviewFixTarget
  model: ReviewFixModel
  groups: ReviewFixGroup[]
  git: ReviewFixGit
  blocked_reason: string
  attempts: Record<string, number>
  logs: string[]
  diff_paths: string[]
  artifact_paths: string[]
  audit_log: ReviewFixAuditEvent[]
  created_at: number
  updated_at: number
}

export interface ReviewFixCreateInput {
  target_path: string
  findings: ReviewFixFindingSnapshot[]
  review_run_id?: string
  pr_url?: string
  source_head_sha?: string
  target_mode?: ReviewFixTargetMode
  model?: string | null
  provider?: string
  groups?: Array<{ group_id?: string; finding_keys: string[]; hard?: boolean }>
}

export interface ReviewFixActionRequest {
  action: string
  expected_revision: number
  target_fingerprint: string
  confirmed?: boolean
  confirmation_id?: string
  confirmation_intent?: string
  group_id?: string
  expected_group_revision?: number
  groups?: Array<{ group_id?: string; finding_keys: string[]; hard?: boolean }>
  model?: string
  test_command?: string[]
  build_command?: string[]
  commit_message?: string
  agent?: string
  fresh?: boolean
  auto_approve?: boolean
}

export interface ReviewFixTaskResponse {
  ok?: boolean
  task_id: string
  revision: number
  state: ReviewFixState
  review_fix: ReviewFixMetadata | null
  run?: Record<string, unknown>
  execution_task_id?: string
  review_run_id?: string
  run_id?: string
}
