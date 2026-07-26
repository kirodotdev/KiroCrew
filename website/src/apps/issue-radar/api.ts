// Thin fetch wrapper for the Issue Radar backend (registered directly on the
// main gateway's aiohttp Application — see backend/routes.py:register_routes
// — so the base path is /api/apps/issue-radar, matching code-review-sage's
// convention, NOT the /apps/{name}/api reverse-proxy prefix used by apps like
// file-explorer that run as a separate child process).
const API = '/api/apps/issue-radar'

export interface ConnectResponse {
  owner: string
  repo: string
  full_name: string
  private: boolean
  open_issues_count: number
}

export interface Issue {
  number: number
  title: string
  url: string
  labels: string[]
  comments: number
  /** Total reaction count across all emoji (populated on next refresh). */
  reactions?: number
  /** +1 (thumbs-up) reactions — the community-demand signal used by the Overview. */
  thumbs_up?: number
  /** GitHub author association, e.g. "FIRST_TIME_CONTRIBUTOR", "MEMBER". */
  author_association?: string | null
  updated_at: string
  created_at?: string
  state?: string
  author?: string | null
  assignees?: string[]
  body?: string
}

export interface IssuesResponse {
  owner: string
  repo: string
  state?: string
  issues: Issue[]
  from_cache: boolean
}

/** One pull-request list row. A PR-native shape (from the `pulls` endpoint, not
 * `issues`): it carries `draft`, base/head refs, requested reviewers, and
 * `merged_at` (the signal that a closed PR was merged vs closed-unmerged). */
export interface PullRequest {
  number: number
  title: string
  url: string
  /** GitHub state — "open" | "closed". Merged PRs are "closed" with merged_at set. */
  state: string
  draft: boolean
  labels: string[]
  author?: string | null
  author_association?: string | null
  updated_at: string
  created_at?: string
  closed_at?: string | null
  /** ISO timestamp when merged, or null. The only reliable merged/closed split. */
  merged_at?: string | null
  assignees?: string[]
  requested_reviewers?: string[]
  base?: string | null
  head?: string | null
  /** Lines added — from the GraphQL list enrichment. `null` means UNKNOWN (the
   * enrichment call failed); it is deliberately not 0, which would claim the PR
   * changes nothing. */
  additions?: number | null
  /** Lines removed — same enrichment, same null-means-unknown rule. */
  deletions?: number | null
  /** Files touched — same enrichment, same null-means-unknown rule. */
  changed_files?: number | null
  /** Aggregate status-check rollup, bucketed exactly like `PrCheck.bucket`;
   * null when the PR has no checks or the enrichment call failed. */
  checks_state?: 'failure' | 'running' | 'success' | 'other' | null
  /** Per-bucket tally of the individual checks, using the same buckets as
   * `PrCheck.bucket`. All four keys are present when it is there at all; `null`
   * means the enrichment did not run. */
  checks_counts?: Record<'failure' | 'running' | 'success' | 'other', number> | null
  /** True when the PR has more checks than one API page, so `checks_counts` is
   * incomplete and the card must show the aggregate rollup instead. */
  checks_truncated?: boolean
  body?: string
}

export interface PullsResponse {
  owner: string
  repo: string
  state?: string
  pulls: PullRequest[]
  from_cache: boolean
  /** Set by /pulls/search when the result hit the server's cap, so the UI can say
   * "newest N" instead of implying it listed every match. */
  truncated?: boolean
  /** The cap that produced `truncated`. */
  limit?: number
}

/** The full single-PR payload the detail pane renders — a superset of the list
 * `PullRequest` (adds diff stats, review/comment counts, mergeability, full
 * label objects, milestone). */
export interface PrDetailData {
  number: number
  title: string
  body: string
  state: string
  draft: boolean
  merged: boolean
  url: string
  author: string | null
  author_association: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  merged_at: string | null
  merged_by: string | null
  comments: number
  review_comments: number
  commits: number
  additions: number
  deletions: number
  changed_files: number
  /** GitHub mergeability: true/false, or null while GitHub is still computing. */
  mergeable: boolean | null
  mergeable_state: string | null
  base: string | null
  head: string | null
  /** Head commit sha — the commit the automated checks hang off. */
  head_sha: string | null
  labels: DetailLabel[]
  assignees: string[]
  requested_reviewers: string[]
  milestone: Milestone | null
}

/** One automated check on a PR's head commit — a CI job, a Checks-API review
 * bot, or a legacy commit status, all normalized to one shape. `bucket` is the
 * coarse server-computed grouping the UI acts on, so it never has to re-derive
 * GitHub's ~10 conclusion values. */
export interface PrCheck {
  name: string
  /** failure | running | success | other (neutral/skipped/cancelled). */
  bucket: 'failure' | 'running' | 'success' | 'other'
  /** Raw GitHub status (queued/in_progress/completed), for the tooltip. */
  status: string | null
  /** Raw GitHub conclusion (success/failure/timed_out/…), shown on the row. */
  conclusion: string | null
  /** Link to the run's details page, when the provider gave one. */
  url: string | null
  /** Short one-line summary/description from the check output. */
  summary: string
  /** The GitHub App that reported it (null for legacy commit statuses). */
  app: string | null
  started_at: string | null
  completed_at: string | null
}

export interface PullDetailResponse {
  owner: string
  repo: string
  number: number
  detail: PrDetailData
  timeline: TimelineEvent[]
  checks: PrCheck[]
  /** The card-level tally + rollup derived from `checks` by the same code the
   * list enrichment uses, so the client can patch its cached list row instead of
   * refetching the whole list. */
  checks_summary?: {
    checks_counts: Record<'failure' | 'running' | 'success' | 'other', number>
    checks_state: 'failure' | 'running' | 'success' | 'other' | null
    /** Always false here: a detail read is fully paginated, so its tally is
     * complete by construction. */
    checks_truncated?: boolean
  }
  from_cache: boolean
}

/** Per-reaction counts on an issue or comment (`total` is the sum). */
export interface Reactions {
  total: number
  plus1: number
  minus1: number
  laugh: number
  hooray: number
  confused: number
  heart: number
  rocket: number
  eyes: number
}

export interface DetailLabel {
  name: string
  color: string
  description: string
}

export interface Milestone {
  title: string
  state: string
  due_on: string | null
}

/** The full single-issue payload the detail pane renders — a superset of the
 * list `Issue` (adds body/state_reason/association/milestone/reactions/etc.). */
export interface IssueDetailData {
  number: number
  title: string
  body: string
  state: string
  state_reason: string | null
  url: string
  author: string | null
  author_association: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  closed_by: string | null
  comments: number
  locked: boolean
  labels: DetailLabel[]
  assignees: string[]
  milestone: Milestone | null
  reactions: Reactions | null
}

/** One normalized timeline entry. `kind` selects which optional fields apply
 * (see backend `_normalize_timeline_event`). */
export interface TimelineEvent {
  kind:
    | 'comment' | 'labeled' | 'unlabeled' | 'assigned' | 'unassigned'
    | 'closed' | 'reopened' | 'renamed' | 'milestoned' | 'demilestoned'
    | 'cross-referenced' | 'referenced'
    | 'reviewed' | 'committed' | 'review_comment'
  actor: string | null
  created_at: string
  // comment
  body?: string
  author_association?: string | null
  reactions?: Reactions | null
  // labeled / unlabeled
  label?: { name: string; color: string }
  // assigned / unassigned
  assignee?: string | null
  // closed
  state_reason?: string | null
  commit_id?: string | null
  // renamed
  rename?: { from: string; to: string }
  // milestoned / demilestoned
  milestone?: string | null
  // cross-referenced
  source?: { number: number; title: string; url: string; state: string; is_pr: boolean }
  // reviewed (PR only) — "approved" | "changes_requested" | "commented" | "dismissed"
  review_state?: string | null
  // committed (PR only) — first line of the commit message
  message?: string
  // review_comment (PR only) — an INLINE comment anchored to a file + line. These
  // come from /pulls/{n}/comments, which the issues timeline does not carry.
  path?: string | null
  line?: number | null
  url?: string | null
}

export interface IssueDetailResponse {
  owner: string
  repo: string
  number: number
  detail: IssueDetailData
  timeline: TimelineEvent[]
  from_cache: boolean
}

/** One AI-proposed label: an exact repo label name + a short justification. */
export interface SuggestedLabel {
  name: string
  reason: string
}

/** The AI triage result for one issue (summary shown at the top of the detail
 * pane; suggested_labels surfaced in the Labels sidebar as accept-able chips). */
export interface IssueAiResponse {
  owner: string
  repo: string
  number: number
  summary: string
  suggested_labels: SuggestedLabel[]
  /** ISO timestamp the summary was produced. Null for caches written before it
   * was stamped — the UI then omits the age. */
  generated_at?: string | null
  from_cache: boolean
}

/** GET /pull-ai — a PR's AI summary. No label suggestions: a PR's actionable
 * output is the review itself (see the Review button), not a taxonomy edit. */
export interface PrAiResponse {
  owner: string
  repo: string
  number: number
  summary: string
  /** ISO timestamp the summary was produced (null for pre-stamp caches). */
  generated_at?: string | null
  from_cache: boolean
}

/** Response to a label edit — the issue's authoritative label set after the
 * add/remove was applied (full objects, so chips re-render with real colours). */
export interface ApplyLabelsResponse {
  owner: string
  repo: string
  number: number
  labels: DetailLabel[]
}

/** Response to a close/reopen — the issue's state after the change. */
export interface IssueStateResponse {
  owner: string
  repo: string
  number: number
  state: string
  state_reason: string | null
}

export interface RepoLabel {
  name: string
  color: string
  description: string
}

export interface LabelsResponse {
  owner: string
  repo: string
  labels: RepoLabel[]
  from_cache: boolean
}

/** A repo member: someone with access to the repo. From the authoritative
 * collaborators roster (with a GitHub role) when available, else inferred from
 * issue authors on a read-only repo (see MembersResponse.source). */
export interface RepoMember {
  login: string
  /** collaborators roster: "admin" | "maintain" | "write" | "triage" | "read".
   * Derived fallback: "OWNER" | "MEMBER" | "COLLABORATOR". */
  role: string
}

export interface MembersResponse {
  owner: string
  repo: string
  members: RepoMember[]
  /** "collaborators" = authoritative roster; "derived" = read-only fallback
   * inferred from issue authors (needs no push access, but incomplete). */
  source?: 'collaborators' | 'derived' | null
  from_cache: boolean
}

export interface RepoPermissions {
  admin?: boolean
  maintain?: boolean
  push?: boolean
  pull?: boolean
  triage?: boolean
}

/** Per-repo, local-only triage preferences (never written back to GitHub).
 * Teaches Issue Radar how THIS repo labels its work. */
export interface RepoSettings {
  /** Label names that mean "still needs triage" on this repo. */
  triage_labels: string[]
  /** Also treat issues that carry no labels at all as needing triage. */
  unlabeled_is_untriaged: boolean
  /** Label names that mark newcomer / first-issue-friendly work. */
  good_first_issue_labels: string[]
  /** Watch this repo in the background and push a KiroCrew notification when a
   * new issue is opened. Opt-in (default false). */
  notify_on_new_issue: boolean
}

/** Backwards-compatible defaults: no configured labels + "unlabeled == untriaged"
 * (exactly the heuristic the dashboards used before settings existed). */
export const DEFAULT_REPO_SETTINGS: RepoSettings = {
  triage_labels: [],
  unlabeled_is_untriaged: true,
  good_first_issue_labels: [],
  notify_on_new_issue: false,
}

export interface SettingsResponse {
  owner: string
  repo: string
  settings: RepoSettings
}

export type RecommendationCategory = 'priority' | 'area' | 'type' | 'triage' | 'first-issue'

/** An AI-proposed NEW label for a repo (does not yet exist on GitHub). */
export interface LabelRecommendation {
  name: string
  category: RecommendationCategory
  color: string
  description: string
  rationale: string
  examples: number[]
}

export interface RecommendationsResponse {
  owner: string
  repo: string
  /** null when none have been generated yet. */
  recommendations: LabelRecommendation[] | null
  generated_at: string | null
  from_cache: boolean
}

export interface CreateLabelResponse {
  owner: string
  repo: string
  label: RepoLabel
  created: boolean
}

export interface ConnectedRepo {
  owner: string
  repo: string
  enabled?: boolean
  permissions?: RepoPermissions | null
  settings?: RepoSettings
}

export interface ReposResponse {
  repos: ConnectedRepo[]
}

export interface MeResponse {
  login: string | null
}

/** Agent-written conclusions for an investigation (all optional; populated when
 * the investigating session — or the user — PUTs a summary back). */
export interface InvestigationFindings {
  verdict: string | null
  root_cause: string | null
  suggested_labels: string[]
  next_action: string | null
  summary: string | null
}

/** The local record linking an issue to its investigation chat session. There
 * is no shared ledger — one small per-issue file, used to RESUME the session,
 * badge its status, and retain findings. */
export interface InvestigationRecord {
  owner: string
  repo: string
  number: number
  /** Chat slot (session) key opened for this investigation — drives resume. */
  slot_key: string | null
  /** The "Issue Radar - <repo>" chat folder the session was filed into. */
  folder_id: string | null
  status: 'investigating' | 'resolved' | 'archived'
  started_at: string
  last_opened_at: string
  findings: InvestigationFindings | null
}

export interface InvestigationResponse {
  owner: string
  repo: string
  number: number
  /** null when the issue has never been investigated. */
  investigation: InvestigationRecord | null
}

/** Fields the Investigate flow (or the agent) may patch onto a record. Partial
 * — even `{}` is valid (bumps the last-opened stamp on resume). */
export interface InvestigationPatch {
  slot_key?: string
  folder_id?: string
  status?: 'investigating' | 'resolved' | 'archived'
  findings?: Partial<InvestigationFindings> | null
}

export interface ApiError {
  error: string
}

async function parseErrorBody(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as ApiError
    return body.error || `HTTP ${r.status}`
  } catch {
    return `HTTP ${r.status}`
  }
}

export const issueRadarApi = {
  connect: async (url: string): Promise<ConnectResponse> => {
    const r = await fetch(`${API}/connect`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  issues: async (owner: string, repo: string, opts?: { refresh?: boolean; state?: 'open' | 'closed' }): Promise<IssuesResponse> => {
    const q = new URLSearchParams({ owner, repo })
    if (opts?.state) q.set('state', opts.state)
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issues?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  issueDetail: async (owner: string, repo: string, number: number, opts?: { refresh?: boolean }): Promise<IssueDetailResponse> => {
    const q = new URLSearchParams({ owner, repo, number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issue?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** List pull requests for a repo. `state` is 'open' (default) or 'closed'
   * (closed is bounded to the 100 most-recently-updated, merged + unmerged). */
  pulls: async (owner: string, repo: string, opts?: { refresh?: boolean; state?: 'open' | 'closed' }): Promise<PullsResponse> => {
    const q = new URLSearchParams({ owner, repo })
    if (opts?.state) q.set('state', opts.state)
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/pulls?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** PRs matching a per-person filter, resolved server-side by GitHub search.
   * Use INSTEAD of `pulls()` when a person filter is on: the bounded list caps
   * closed PRs at one page, so a client-side "authored by me" filter misses
   * older PRs, while search covers the whole repo. `state` is open | merged |
   * closed (closed = closed without merge). At least one person is required.
   * Rows come back in the same shape, with `base`/`head` null and
   * `requested_reviewers` empty (the search API doesn't expose them). */
  searchPulls: async (
    owner: string, repo: string,
    opts: { state?: 'open' | 'closed' | 'merged'; author?: string; assignee?: string; reviewRequested?: string },
  ): Promise<PullsResponse> => {
    const q = new URLSearchParams({ owner, repo })
    if (opts.state) q.set('state', opts.state)
    if (opts.author) q.set('author', opts.author)
    if (opts.assignee) q.set('assignee', opts.assignee)
    if (opts.reviewRequested) q.set('review_requested', opts.reviewRequested)
    const r = await fetch(`${API}/pulls/search?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** One PR's full detail + normalized timeline + changed files, cache-first;
   * pass refresh to force a fresh `gh` fetch. */
  pullDetail: async (owner: string, repo: string, number: number, opts?: { refresh?: boolean }): Promise<PullDetailResponse> => {
    const q = new URLSearchParams({ owner, repo, number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/pull?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** AI triage (summary + suggested labels), cache-first server-side; pass
   * refresh to force a regenerate. */
  issueAi: async (owner: string, repo: string, number: number, opts?: { refresh?: boolean }): Promise<IssueAiResponse> => {
    const q = new URLSearchParams({ owner, repo, number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issue-ai?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** AI summary of a pull request — its description, whole conversation, and
   * check state. Cache-first server-side, and the cache self-invalidates when
   * the PR moves (new comment / push / flipped check), so no manual refresh is
   * needed to pick up changes; pass refresh to force a regenerate anyway. */
  pullAi: async (owner: string, repo: string, number: number, opts?: { refresh?: boolean }): Promise<PrAiResponse> => {
    const q = new URLSearchParams({ owner, repo, number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/pull-ai?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Apply a label change (add and/or remove). Requires triage/push access on
   * the repo (403 otherwise). Returns the issue's authoritative label set. */
  applyLabels: async (
    owner: string, repo: string, number: number, add: string[], remove: string[],
  ): Promise<ApplyLabelsResponse> => {
    const r = await fetch(`${API}/labels/apply`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo, number, add, remove }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Close or reopen an issue. Requires triage/push access (403 otherwise).
   * On close, reason is 'completed' (default) or 'not_planned'. */
  setIssueState: async (
    owner: string, repo: string, number: number,
    state: 'open' | 'closed', stateReason?: 'completed' | 'not_planned',
  ): Promise<IssueStateResponse> => {
    const r = await fetch(`${API}/issue/state`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo, number, state, state_reason: stateReason }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  labels: async (owner: string, repo: string, opts?: { refresh?: boolean }): Promise<LabelsResponse> => {
    const q = new URLSearchParams({ owner, repo })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/labels?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  members: async (owner: string, repo: string, opts?: { refresh?: boolean }): Promise<MembersResponse> => {
    const q = new URLSearchParams({ owner, repo })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/members?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  repos: async (): Promise<ReposResponse> => {
    const r = await fetch(`${API}/repos`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  me: async (): Promise<MeResponse> => {
    const r = await fetch(`${API}/me`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  getSettings: async (owner: string, repo: string): Promise<SettingsResponse> => {
    const q = new URLSearchParams({ owner, repo })
    const r = await fetch(`${API}/settings?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  putSettings: async (owner: string, repo: string, settings: RepoSettings): Promise<SettingsResponse> => {
    const r = await fetch(`${API}/settings`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo, settings }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  disconnect: async (owner: string, repo: string): Promise<{ ok: boolean }> => {
    const q = new URLSearchParams({ owner, repo })
    const r = await fetch(`${API}/repos?${q.toString()}`, { method: 'DELETE', credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Read an issue's investigation record (`investigation` is null if the issue
   * has never been investigated). */
  getInvestigation: async (owner: string, repo: string, number: number): Promise<InvestigationResponse> => {
    const q = new URLSearchParams({ owner, repo, number: String(number) })
    const r = await fetch(`${API}/investigation?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Upsert an issue's investigation record — link the session (slot_key +
   * folder_id), bump status, or store findings. The server merges + normalizes,
   * so a partial patch (even `{}`, which just bumps the last-opened stamp on
   * resume) is valid. */
  saveInvestigation: async (
    owner: string, repo: string, number: number, patch: InvestigationPatch,
  ): Promise<InvestigationResponse> => {
    const r = await fetch(`${API}/investigation`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo, number, ...patch }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Read the repo's cached AI label recommendations (`recommendations` is null
   * if none generated yet). Never runs the model. */
  getRecommendations: async (owner: string, repo: string): Promise<RecommendationsResponse> => {
    const q = new URLSearchParams({ owner, repo })
    const r = await fetch(`${API}/recommendations?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Generate (and cache) label recommendations via one model call over the
   * repo's labels + a sample of its open issues. */
  generateRecommendations: async (owner: string, repo: string): Promise<RecommendationsResponse> => {
    const r = await fetch(`${API}/recommendations`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Create a NEW label on the repo. Requires triage/push access (403
   * otherwise); idempotent if the label already exists. */
  createLabel: async (
    owner: string, repo: string, label: { name: string; color?: string; description?: string },
  ): Promise<CreateLabelResponse> => {
    const r = await fetch(`${API}/labels/create`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner, repo, ...label }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },
}
