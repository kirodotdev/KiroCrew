// Shared types for Code Review Sage.
//
// A "thread" in the UI is a RUN on the backend: one review of one or more pull
// requests, with its own private results + report. Several can be live at once.

export type {
  ReviewFixActionRequest,
  ReviewFixCreateInput,
  ReviewFixFindingSnapshot,
  ReviewFixGroup,
  ReviewFixMetadata,
  ReviewFixState,
  ReviewFixTargetMode,
  ReviewFixTaskResponse,
  ReviewFixValidation,
} from '../../../types/reviewFix'

/** The picker value for inheriting the account/session model. */
export const REVIEW_MODEL_AUTO = 'auto'

export interface ReviewStart {
  changes: string[]
  /** Concrete ACP wire id; Auto is represented by omission or null. */
  model?: string | null
}

export interface ReviewLinksStart {
  links: string
  /** Concrete ACP wire id; Auto is represented by omission or null. */
  model?: string | null
}

export interface ReviewRepoStart {
  repo: string
  force: boolean
  /** Concrete ACP wire id; Auto is represented by omission or null. */
  model?: string | null
}

export type ReviewStartInput = string[] | ReviewStart
export type ReviewLinksInput = string | ReviewLinksStart

/** Terminal + live states a run can be in (backend ``run["status"]``). */
export type RunStatus =
  | 'running'
  | 'done'
  | 'error'
  | 'cancelled'
  | 'interrupted'

/** Per-change phase the driver reports as it works (``run.progress[change_id]``). */
export type ChangePhase =
  | 'queued'
  | 'reviewing'
  | 'done'
  | 'failed'
  | 'cancelled'

/** What the reviewer is doing right now, relayed from its tool stream. The only
 * real evidence of forward motion inside a single multi-minute worker turn. */
export interface ChangeActivity {
  tool: string
  step: number
}

export interface RunProgressEntry {
  phase: ChangePhase | string
  activity?: ChangeActivity
  counts?: { red?: number; yellow?: number }
  design_block?: boolean
  posted?: number
  expected?: number
  error?: string
  /** The failure cause as a stable backend token (`skipped_reason`), carried
   *  BESIDE `error` rather than instead of it. `error` is prose, so keying a
   *  translation off it alone means a backend rewording silently drops the card
   *  to untranslated pass-through. Absent on runs recorded before the backend
   *  carried it, and on non-failure phases. */
  reason?: string
  coverage?: string
}

export interface ReportBands {
  red: number
  yellow: number
  green: number
}

/** The report summary the driver folds into a finished run's summary. */
export interface RunReportIndex {
  report_slug?: string | null
  bands?: ReportBands
  generated_at?: string
  total?: number
}

export interface RunSummary {
  ok?: boolean
  changes?: number
  deep_reviewed?: number
  cancelled?: number
  result_records?: number
  report?: RunReportIndex
  report_slug?: string
  archive_error?: string
  report_error?: string
  note?: string
  failures?: unknown[]
  per_change?: unknown[]
}

export interface Run {
  run_id: string
  /** Present on repo-wide runs ("owner/name"); absent for pasted/picked links. */
  repo?: string
  changes: string[]
  /** Parallel to ``changes``; the key ``progress`` is written under. */
  change_ids?: string[]
  status: RunStatus
  started_at: string
  finished_at?: string
  cancel_requested_at?: string
  progress?: Record<string, RunProgressEntry>
  summary?: RunSummary
  report_slug?: string | null
  error?: string
  /** The run-level failure cause as a stable backend token, beside `error`.
   *  Named to mirror `RunProgressEntry.reason` so the token resolves through the
   *  same per-change-then-run precedence the prose already uses. */
  reason?: string
  /** Concrete ACP wire id selected for this run; absent/null means Auto/inherit. */
  model?: string | null
  force?: boolean
  skipped_inflight?: number
  /** Set once this run's findings have been published to the pull request. */
  posted_at?: string
  posted_comments?: number
  /** Which comments are already on the pull request, keyed by change id. */
  posted_keys?: Record<string, string[]>
  /** Per change, the id of the pending draft THIS run created. `posted_keys` proves a
   *  delivery happened; this proves the draft still on the pull request is that one,
   *  because a later run replaces the draft rather than editing it. */
  posted_review_ids?: Record<string, string>
  posting?: boolean
  post_error?: string | null
}

export interface PoolStats {
  workers?: number
  idle?: number
  busy?: number
  max?: number
  active_sessions?: number
  batches?: number
}

export interface ReviewerInfo {
  model?: string | null
  effort?: string
  agent?: string
}

export interface RunsResponse {
  runs: Run[]
  pool: PoolStats | null
  reviewer: ReviewerInfo | null
}

// --- Report ------------------------------------------------------------------

export type Band = 'red' | 'yellow' | 'green'

export interface Finding {
  dimension?: string
  severity?: 'red' | 'yellow'
  file?: string
  line?: number | string
  end_line?: number | string
  fingerprint?: string
  /** One-sentence conclusion, written by the reviewer (see
   *  `review_driver.build_review_task`). Optional: reviews recorded before the
   *  field existed have none, and the card falls back to leading with the
   *  observation. */
  headline?: string
  observation?: string
  consequence?: string
  suggestion?: string
  snippet?: string
}

export interface ReportRow {
  change_id: string
  url: string
  title: string
  platform?: string
  band: Band
  /** Stored rationale for the band ("blast=LARGE + 2× red"). */
  why: string
  score: number
  design_risk: string
  blast: string
  red: number
  yellow: number
  deep_reviewed: boolean
  gate_verdict: string
  design_headline?: string
  problem?: string
  why_it_matters?: string
  solution_assessment?: string
  rationale?: string
  /** The exact body the ship-readiness comment would post. */
  ship_comment?: string
  findings?: Finding[]
}

export interface RunReport {
  run_id: string
  status: string
  /** False while a run is still working — the pane renders progress instead. */
  ready: boolean
  bands: ReportBands
  rows: ReportRow[]
  generated_at: string
  total: number
  report_slug: string | null
}

export type LocalReviewStatus = 'reviewing' | 'completed' | 'partially_completed' | 'failed'

export interface LocalFinding {
  id: string
  file: string
  side: 'new' | 'old'
  line: number
  end_line?: number | null
  severity: 'info' | 'warning' | 'error'
  category?: string | null
  title: string
  message: string
  suggestion?: string | null
  confidence?: number | null
  status: 'open' | 'accepted' | 'fixing' | 'resolved' | 'dismissed' | 'stale'
  user_instruction?: string | null
  fingerprint?: string
}

export interface LocalReviewSession {
  id: string
  repository: string
  mode: string
  status: LocalReviewStatus
  revision?: string
  files?: Array<{
    path: string
    status: string
    additions: number
    deletions: number
    hunks?: Array<{ old_start: number; new_start: number; lines: Array<{
      kind: 'context' | 'add' | 'delete'
      content: string
      old_line?: number | null
      new_line?: number | null
    }> }>
  }>
  skipped_files?: string[]
  warning?: string | null
  findings: LocalFinding[]
  error?: string
  fix_runs?: Array<{ id: string; status: string; changed_files?: string[]; error?: string }>
}

// --- Repos + PRs -------------------------------------------------------------

export interface PinnedRepo {
  owner: string
  repo: string
  full_name?: string
  added_at?: string
}

export interface RecentRepo {
  owner: string
  repo: string
  full_name: string
  last_contributed_at: string
  contribution_count: number
  pinned?: boolean
}

export interface RecentReposResponse {
  repos: RecentRepo[]
  pinned: PinnedRepo[]
  login?: string | null
  /** True when the event page was full: the list may be MISSING repos. */
  truncated?: boolean
  /** Set when `gh` is absent or unauthenticated — a first-run state, not an error. */
  setup_required?: boolean
  error?: string
}

/** A repo the `gh` user can reach at all (owned / collaborator / org member). */
export interface UserRepo {
  owner: string
  repo: string
  full_name: string
  pushed_at: string
  private: boolean
  archived: boolean
  can_push: boolean
  pinned?: boolean
}

export interface UserReposResponse {
  repos: UserRepo[]
  pinned: PinnedRepo[]
  /** True when the page came back full: repos beyond the newest N are missing. */
  truncated?: boolean
  /** Set when `gh` is absent or unauthenticated — a first-run state, not an error. */
  setup_required?: boolean
  error?: string
}

export interface RepoPr {
  url: string
  number: number
  title: string
  head_sha: string
  author?: string
  updated_at?: string
  draft?: boolean
  change_id: string
  reviewed: boolean
  reviewed_stale: boolean
  reviewed_at?: string
}

/** The minimum needed to open a PR in the detail pane.
 *
 * `RepoPr` satisfies this structurally, but a run reached from the thread list
 * carries only a change URL — the rest is filled in from the provider fetch. So
 * the pane takes this looser shape and treats the optional fields as hints. */
export interface PrRef {
  url: string
  number: number
  change_id: string
  title?: string
  author?: string
  updated_at?: string
  head_sha?: string
  draft?: boolean
  reviewed?: boolean
  reviewed_stale?: boolean
}

export interface RepoPrsResponse {
  repo: string
  prs: RepoPr[]
  count: number
}

// --- Settings + learning -----------------------------------------------------

export interface Settings {
  model: string | null
  effort: string
  active_namespaces: string[]
  max_concurrent: number
}

export interface SettingsResponse {
  settings: Settings
  models: string[]
  efforts: string[]
  namespaces: string[]
  reviewer?: ReviewerInfo | null
  max_concurrent_max: number
}

export interface LearnedPattern {
  id: string
  title: string
  scope: string
  impact: string
  guidance: string
}

export interface NamespacesResponse {
  namespaces: { name: string; patterns: number; candidate: number; active: boolean }[]
  active: string[]
}

/** What the repo-add endpoint reports back. */
export interface AddRepoResponse {
  ok: boolean
  repos: PinnedRepo[]
  added?: { owner: string; repo: string }
  /** Set when a PULL REQUEST url was pasted: its repo was pinned and this is the
   *  pull request itself, so the caller can open it. */
  pull_request?: {
    owner: string; repo: string; number: number; url: string; change_id: string
  }
}

export interface ConsolidateResponse {
  ok: boolean
  namespace: string
  staged: number
  running: boolean
}

export interface LearningsResponse {
  namespace: string
  patterns: LearnedPattern[]
  candidate: LearnedPattern[]
  /** A merge is running for this namespace right now. */
  consolidating?: boolean
  /** Why the last merge did not apply. The ruleset is unchanged when set. */
  consolidate_error?: string | null
}

// --- Navigation --------------------------------------------------------------

export type MainView = 'reviews' | 'local' | 'learning' | 'settings'
/** Which list the middle column shows: the active repo's PRs, or the threads. */
export type ListTab = 'pulls' | 'reviews'
export type RailSection = 'repos' | 'reviews' | 'learning' | 'settings'

// --- Follow-up sessions ------------------------------------------------------

/** One exchange from a review discussed before follow-ups became sessions. */
export interface ChatTurn {
  /** 'user' | 'reviewer' — widened to string because it is worker-written JSON
   *  crossing a trust boundary, and an unknown role must render as unknown
   *  rather than crash the panel. */
  role: string
  text: string
  /** The reviewer's reasoning for this answer. Empty when it did not think aloud. */
  thinking: string
  /** Titles of tools it ran while answering. */
  tools: string[]
  /** Tools it was NOT allowed to run. Non-empty means the answer is degraded. */
  refusals: string[]
  ts: number
}

export interface ChatState {
  run_id: string
  change_id: string
  /** Stored exchanges from before follow-ups became chat sessions. */
  turns: ChatTurn[]
  /** Whether opening a follow-up would restore the reviewer's own context. False
   *  means the session transcript is gone or was never kept, and a session opened
   *  anyway would answer without knowing what was reviewed. */
  resumable: boolean
  /** Why not, when `resumable` is false. */
  reason: string
  /** The chat session a follow-up on this review uses. */
  slot_key: string
  /** Whether that session already exists, so the panel offers to continue the
   *  conversation instead of inviting the user to start one they already had. */
  followup_open: boolean
}

/** What the follow-up endpoint hands back for the slot the caller then creates. */
export interface FollowupStart {
  ok: boolean
  slot_key: string
  agent: string
  folder_id: string
  title: string
}
