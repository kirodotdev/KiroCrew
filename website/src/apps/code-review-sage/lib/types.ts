// Shared types for Code Review Sage.
//
// A "thread" in the UI is a RUN on the backend: one review of one or more pull
// requests, with its own private results + report. Several can be live at once.

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

export type MainView = 'reviews' | 'learning' | 'settings'
/** Which list the middle column shows: the active repo's PRs, or the threads. */
export type ListTab = 'pulls' | 'reviews'
export type RailSection = 'repos' | 'reviews' | 'learning' | 'settings'