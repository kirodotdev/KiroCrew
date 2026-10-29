// The Issue Radar backend's wire contract for repos, issues and pull requests
// (backend/routes.py): the shapes of the repo, issue and pull-request responses
// `issueRadarApi` returns, the error classes it throws, the two decoders that read
// a failed response (`parseErrorBody` for the message, `gitlabGroupError` for a
// `GitLabGroupError`), the repo-settings defaults, and how a request names its
// repo (`RepoRef`, `repoQuery`, `repoBody`, `accountQuery`).
//
// `api.ts` re-exports every public name here except those two decoders, which only
// its calls use, and is the only import path: the specs mock that module id, so a
// module importing this file directly would slip past their mocks. Nothing here
// imports `api.ts`.

export interface ConnectResponse {
  owner: string
  repo: string
  /** Resolved by the SERVER from the URL — the client cannot nominate it. This is
   * the identity every later request for this repo must carry. */
  provider: SourceProvider
  host: string
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
  /** Set by the `first_page=1` fast path: `true` when these are only the newest
   * page (a cold-cache first paint, the full set still loading), `false`/absent
   * when they are the complete list (served from a warm cache). */
  partial?: boolean
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
  /** Head COMMIT sha (not the branch name). Carried on the list row so a bulk
   * review can pin each verdict to the revision its row was rendered at; `null`
   * when the provider did not report one, in which case that row is not
   * approvable in bulk. */
  head_sha?: string | null
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
  /**
   * Merge READINESS, in the same vocabulary as the detail pane's `mergeable_state`
   * (GitHub's `mergeable_state` / GraphQL's lowercased `mergeStateStatus`).
   *
   * On the LIST row because a bulk action cannot otherwise tell the two merge verbs
   * apart: `clean` means mergeable NOW (GitHub *refuses* to arm auto-merge — "Pull
   * request is in clean status"), while `blocked`/`behind`/`unstable` mean not yet,
   * which is what auto-merge is for. Without it the bulk bar offered auto-merge for
   * every ticked row and GitHub rejected each ready one individually.
   *
   * `null` / absent means UNKNOWN — GitHub computes mergeability asynchronously, so a
   * cold read answers `unknown`. Treat it as "cannot tell", never as "not ready": a
   * gate that cannot tell must refuse rather than guess.
   */
  mergeable_state?: string | null
  /** Whether the PR has no merge CONFLICTS. Deliberately NOT "ready to merge" —
   * a PR with unsatisfied required reviews is `mergeable: true` with
   * `mergeable_state: 'blocked'`. `null` means unknown. */
  mergeable?: boolean | null
  body?: string
}

export interface PullsResponse {
  owner: string
  repo: string
  /** The server's own bulk-action cap. Read from the response rather than
   * hardcoded: a client-side copy silently turns every large selection into a 400
   * the day the backend cap changes (same reasoning as `/tagging`'s `bulk_max`). */
  bulk_max?: number
  state?: string
  pulls: PullRequest[]
  from_cache: boolean
  /** Set by the `first_page=1` fast path: `true` when these are only the newest
   * page (un-enriched) while the full enriched list loads behind them; absent or
   * `false` when the set is complete. Mirrors `IssuesResponse.partial`. */
  partial?: boolean
  /** Set by /pulls/search when the result hit the server's cap, so the UI can say
   * "newest N" instead of implying it listed every match. */
  truncated?: boolean
  /** The cap that produced `truncated`. */
  limit?: number
}

/** A GitLab group followed for review requests: an instance host and a group
 * path, nothing else. Its projects are never listed or stored. */
export interface GitLabGroup {
  host: string
  path: string
}

export interface GitLabGroupsResponse {
  groups: GitLabGroup[]
}

/** One merge request in a group's review list. Every field is bounded by the
 * server; `truncated_fields` names any it shortened (an over-long `url` is
 * dropped to `''` instead). */
export interface GitLabGroupReview {
  number: number | null
  title: string
  url: string
  /** Full path of the project the merge request belongs to, e.g. `acme/web/api`. */
  project: string
  author: string | null
  updated_at: string | null
  truncated_fields: string[]
}

export interface GitLabGroupReviewsResponse extends GitLabGroup {
  /** The GitLab username the search ran as (the signed-in `glab` user). */
  login: string
  pulls: GitLabGroupReview[]
  /** More matches exist than the `limit` newest shown. */
  truncated: boolean
  limit: number
}

/** A failed GitLab-group request, carrying the server's `code` so the UI can say
 * what to do (`group_not_found`, `setup_required`, …) instead of echoing the
 * provider's raw message. */
export class GitLabGroupError extends Error {
  readonly code: string

  constructor(message: string, code: string) {
    super(message)
    this.name = 'GitLabGroupError'
    this.code = code
  }
}

export async function gitlabGroupError(r: Response): Promise<GitLabGroupError> {
  let body: { error?: unknown; code?: unknown } = {}
  try {
    body = (await r.json()) as typeof body
  } catch {
    // A non-JSON body still yields an error with the status as its message.
  }
  const message = typeof body.error === 'string' && body.error ? body.error : `HTTP ${r.status}`
  return new GitLabGroupError(message, typeof body.code === 'string' ? body.code : '')
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
  /** Set when the PROVIDER's own auto-merge is already armed (GitHub auto-merge /
   * GitLab "merge when pipeline succeeds"), null when it is off. The actions bar
   * reads this to decide whether it offers "enable" or "cancel", so it cannot
   * offer to arm a PR that is already armed. */
  auto_merge?: { method: string | null; enabled_by: string | null } | null
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
  // comment / review_comment — the forge's own id for the comment. Unique within
  // its thread (Azure DevOps numbers comments per thread), so a key pairs it with
  // `thread_id`. Absent when the forge did not send one.
  id?: number | string | null
  // comment / review_comment — the opaque reply handle (GitHub: root inline review
  // comment id; GitLab: discussion id; Azure DevOps: PR thread id). Absent or null
  // when the comment cannot be replied to in a thread, e.g. a GitHub conversation
  // comment; the timeline then falls back to a quote-reply.
  thread_id?: string | null
}

export interface IssueDetailResponse {
  owner: string
  repo: string
  number: number
  detail: IssueDetailData
  timeline: TimelineEvent[]
  from_cache: boolean
}

/** The compact summary of one REFERENCED issue/PR (`GET /ref`). Backs the hover
 * preview on a cross-reference and the issue-vs-PR resolution a bare `#123`
 * needs — GitHub's `/issues/<n>` redirects to `/pull/<n>`, so the path alone
 * cannot say which it is. Deliberately no body/timeline: it is paid on hover. */
export interface RefSummary {
  number: number
  title: string
  state: string
  state_reason: string | null
  url: string
  author: string | null
  author_association: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  comments: number
  /** True when this number is a pull request rather than an issue. */
  is_pr: boolean
  draft: boolean
  /** ISO timestamp when merged, else null — the merged-vs-closed split. */
  merged_at: string | null
  labels: Array<{ name: string; color: string }>
}

export interface RefSummaryResponse {
  owner: string
  repo: string
  number: number
  summary: RefSummary
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

/** Thrown by `setIssueAssignees` on a 409: somebody else changed the assignees
 * between the read this client rendered and the write. Carries the set the forge
 * actually holds so the caller can re-render instead of retrying blindly. */
export class AssigneesConflictError extends Error {
  current: string[]
  constructor(message: string, current: string[]) {
    super(message)
    this.name = 'AssigneesConflictError'
    this.current = current
  }
}

/** Response to an assignee edit — the issue's authoritative assignee logins
 * after the replace. Read back from the provider (not the request), because a
 * success is not required to be an exact echo (GitLab Free keeps only one). */
export interface IssueAssigneesResponse {
  owner: string
  repo: string
  number: number
  assignees: string[]
}

/** The pull-request actions the UI can invoke on ONE PR.
 *
 * Merging comes in two forms and neither can land code the repo's rules have not
 * cleared: `merge` is for a PR the provider already reports ready (and the server
 * re-checks its merge state and pins it to the reviewed `head_sha` before issuing
 * it), while `auto_merge` hands one that is NOT ready yet to the provider to land by
 * itself once its checks pass. */
export type PrAction =
  | 'close' | 'reopen' | 'approve' | 'request_changes' | 'comment'
  | 'merge' | 'auto_merge' | 'cancel_auto_merge'

/** The subset of {@link PrAction} the BULK endpoint accepts. Mirrors the server's
 * `_BULK_PR_ACTIONS` allowlist. `request_changes` is per-PR only (a mass
 * change-request needs per-PR reasoning to be worth anything), and so is `merge`
 * (irreversible — 50 from one click is a blast radius no confirmation makes
 * reasonable; arming auto-merge is the bulk-safe equivalent). */
export type BulkPrAction = Exclude<PrAction, 'request_changes' | 'merge'>

export interface PrMergeResponse {
  owner: string
  repo: string
  number: number
  merged: boolean
  sha: string | null
  message: string
}

export interface PrStateResponse {
  owner: string
  repo: string
  number: number
  state: string
  merged: boolean
  draft: boolean
}

export interface PrReviewResponse {
  owner: string
  repo: string
  number: number
  id: number | null
  state: string | null
  submitted_at: string | null
}

/** One comment of a timeline batch: a new top-level comment, or a reply into the
 * thread `thread_id` names. */
export interface CommentBatchItem {
  body: string
  thread_id: string | null
}

/** `POST /comments`. `posted` lists the items that reached the forge by their index
 * in the request; `failed` names the first item that did not (later items were not
 * attempted). */
export interface CommentBatchResponse {
  owner: string
  repo: string
  number: number
  target: 'issue' | 'pull'
  posted: { index: number; id: number | string | null; url: string | null }[]
  failed: { index: number; error: string } | null
  /** Indexes of items the forge may or may not have taken: never resent. */
  unknown?: number[]
}

export interface PrCommentResponse {
  owner: string
  repo: string
  number: number
  id: number | null
  url: string | null
  created_at: string | null
}

export interface PrAutoMergeResponse {
  owner: string
  repo: string
  number: number
  auto_merge: boolean
  method: string | null
  enabled_at: string | null
}

/** One CI run on a PR's head commit. Distinct from {@link PrCheck}: a check is a
 * per-job RESULT, while cancel/re-run acts on the parent RUN and needs its id.
 * `cancellable`/`rerunnable` are server-computed so the UI never offers an action
 * the provider will refuse. */
export interface PrWorkflowRun {
  id: number
  name: string
  status: string
  conclusion: string | null
  url: string | null
  event: string | null
  created_at: string | null
  cancellable: boolean
  rerunnable: boolean
}

export interface PrRunsResponse {
  owner: string
  repo: string
  number: number
  runs: PrWorkflowRun[]
}

/** One GitHub Actions run on the CI runs dashboard. */
export interface CiRun {
  id: number
  name: string
  status: string
  conclusion: string | null
  url: string | null
  branch: string | null
  /** PR numbers GitHub links to the run; empty for a run from a fork. */
  pull_requests: number[]
  created_at: string | null
  started_at: string | null
  updated_at: string | null
  /** Seconds from start to last update; null until the run has started. */
  duration_sec: number | null
}

/** One connected GitHub repo's recent runs. `error` is a fixed code when the
 * read failed (`provider_error`, `gh_setup`), never the provider's own text. */
export interface CiRepoRuns {
  owner: string
  repo: string
  provider: string
  host: string
  /** Whether CI-failure alerts are on for this repo. */
  alerts: boolean
  runs: CiRun[]
  error: string | null
}

export interface CiRunsResponse {
  repos: CiRepoRuns[]
  /** More GitHub repos are connected than one response lists. */
  truncated: boolean
}

export interface PrRunActionResponse {
  owner: string
  repo: string
  number: number
  run_id: number
  cancelled?: boolean
  rerun?: boolean
  failed_only?: boolean
}

/** A bulk action's per-PR outcome. Partial failure is EXPECTED (a locked or
 * already-merged PR fails on its own), so the response reports both lists rather
 * than one status code — the caller is never told about a write that did not
 * happen, and the rows that succeeded stay applied. */
export interface BulkPrResponse {
  owner: string
  repo: string
  action: string
  applied: Array<{ number: number } & Record<string, unknown>>
  failed: Array<{ number: number; error: string }>
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
  /** Watch this repo in the background and push a Kiro Crew notification when a
   * new issue is opened. Opt-in (default false). */
  notify_on_new_issue: boolean
  /** Push a Kiro Crew notification when a GitHub Actions run fails on an open
   * pull request the signed-in `gh` user authored. GitHub only; opt-in
   * (default false). */
  notify_on_own_pr_ci_failure: boolean
  /** Local absolute path to this repo's working copy. Empty string means "use
   * the default cwd". Local-only, never written back to the source host. The
   * Investigate action opens its chat session with this as the working
   * directory so the agent sees the repo's real source. */
  workspace_path: string
  /** Monotonic counter bumped by every write. A PUT replaces the whole document,
   * so it must echo the revision it read — the server refuses (409) a write built
   * on a snapshot that has since moved, which is what stops one tab from erasing
   * a label another tab appended. */
  revision: number
}

/** Backwards-compatible defaults: no configured labels + "unlabeled == untriaged"
 * (exactly the heuristic the dashboards used before settings existed). */
export const DEFAULT_REPO_SETTINGS: RepoSettings = {
  triage_labels: [],
  unlabeled_is_untriaged: true,
  good_first_issue_labels: [],
  notify_on_new_issue: false,
  notify_on_own_pr_ci_failure: false,
  workspace_path: '',
  revision: 0,
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

/** Cached label proposals for the untagged queue, keyed by issue number (as a
 * string, because it comes straight off a JSON object). Each entry is the labels
 * the model proposed for that issue; an EMPTY array means "analysed, nothing
 * clearly applies" — which is why it is kept rather than omitted. */
export type TaggingSuggestions = Record<string, SuggestedLabel[]>

/** One row of the untagged queue. Carried in the response rather than resolved
 * client-side against the shared issue list, which follows the user's
 * open/closed filter — resolving it client-side would show an empty queue when
 * entering Tagging from a Closed filter even with untagged issues waiting. */
export interface UntaggedIssue {
  number: number
  title: string
  url: string
  author?: string | null
  created_at?: string
  updated_at?: string
}

/** GET /tagging — the untagged queue for a repo plus any cached suggestions.
 * Read-only: opening the Tagging dashboard never runs the model. */
export interface TaggingResponse {
  owner: string
  repo: string
  /** Open issues carrying NO labels, newest first. */
  issues: UntaggedIssue[]
  /** Their numbers, in the same order — the key the suggestion map uses. */
  untagged: number[]
  /** OPEN-issue count per label name. Served here rather than derived from the
   * shared issue list, which follows the user's open/closed filter. */
  label_counts: Record<string, number>
  /** Open-issue titles by number, for rendering example links. Same reason.
   * Bounded to the slice a recommendation's examples can cite, not every open
   * issue. */
  titles: Record<string, string>
  /** How many issues ONE bulk-apply request accepts. Served rather than
   * hardcoded: a copy in the client silently 400s if the backend cap changes. */
  bulk_max: number
  /** Total open issues, so the dashboard can show untagged as a share. */
  open_count: number
  suggestions: TaggingSuggestions
  generated_at: string | null
  /** How many issues one generate call covers — drives the button's label. */
  batch_size: number
}

/** POST /tagging — result of one batched generate. */
export interface GenerateTaggingResponse {
  owner: string
  repo: string
  /** The merged cache (this batch plus everything generated before). */
  suggestions: TaggingSuggestions
  /** Issue numbers this call analysed (including ones it declined to label). */
  analyzed: number[]
  /** Untagged issues still awaiting a first analysis after this call. */
  remaining: number
  generated_at: string | null
}

/** POST /labels/apply-bulk — per-issue outcome of a bulk apply. Partial failure
 * is normal (GitHub can reject one issue), so successes and failures both come
 * back and the caller reports rather than retries blindly. */
export interface BulkApplyResponse {
  owner: string
  repo: string
  applied: { number: number; labels: DetailLabel[] }[]
  failed: { number: number; error: string }[]
}

export interface ConnectedRepo {
  owner: string
  repo: string
  /** Absent on records written before GitLab support — treat as 'github'. */
  provider?: SourceProvider
  /** Absent on legacy records — treat as 'github.com'. */
  host?: string
  enabled?: boolean
  permissions?: RepoPermissions | null
  settings?: RepoSettings
}

export interface ReposResponse {
  repos: ConnectedRepo[]
}

/** One row of the connect dialog's picker — a repo the `gh` user personally
 * contributed to inside the requested window. `last_contributed_at` is that
 * user's OWN latest contribution (push / PR / review / issue / comment), not
 * the repo's last push, and is what the row renders. `connected` is
 * server-computed against the config, so the picker can disable repos already
 * wired up. */
export interface RecentRepo {
  owner: string
  repo: string
  full_name: string
  /** ISO-8601 UTC timestamp of the user's most recent contribution. */
  last_contributed_at: string
  /** How many contribution events the user made in the window. */
  contribution_count: number
  connected: boolean
  /** Echoed by the server so the picker can build a ref without re-deriving
   * which provider the list came from. */
  provider?: SourceProvider
  host?: string
}

/** Why the host can't talk to GitHub yet, when it can't. The picker turns this
 * into install / `gh auth login` instructions rather than an error string. */
export type GhSetupReason = 'not_installed' | 'not_authenticated'

export interface RecentReposResponse {
  repos: RecentRepo[]
  /** True when the event page came back full, so repos contributed to earlier
   * in the window may be missing. The picker must not claim completeness. */
  truncated?: boolean
  /** Present only when `gh` is unusable; `repos` is then empty. */
  setup_required?: GhSetupReason | null
  /** The server's diagnostic detail (e.g. which dirs were searched). */
  error?: string
}

export interface MeResponse {
  /** The login on THIS provider. Not portable: the same person may be `alice` on
   * GitHub and `alice.smith` on a company GitLab, so a login fetched for the
   * wrong provider makes the "assigned to me" filters match nobody. */
  login: string | null
  provider?: SourceProvider
  host?: string
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
  /** Which session's run the stored `findings` were written under. Server-owned
   * (it is not part of `InvestigationPatch`): the store stamps it on every write
   * and uses it to REPLACE rather than merge the first findings of a new run, so
   * a re-run's verdict never blends with the previous one's. Null when no
   * findings are stored. */
  findings_slot_key?: string | null
}

/** Which sequence a number belongs to. Only load-bearing on GitLab, where issues
 * and merge requests are numbered independently, so `#5` and `!5` are different
 * items that must not share one investigation record. */
export type ItemKind = 'issue' | 'pull'

export interface InvestigationResponse {
  owner: string
  repo: string
  number: number
  kind?: ItemKind
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

/** Thrown by `putSettings` on a 409. Carries the settings the server currently
 * holds, so the caller can re-apply its edit on top instead of losing it. */
export class SettingsConflictError extends Error {
  current: RepoSettings
  constructor(message: string, current: RepoSettings) {
    super(message)
    this.name = 'SettingsConflictError'
    this.current = current
  }
}

export async function parseErrorBody(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as ApiError
    return body.error || `HTTP ${r.status}`
  } catch {
    return `HTTP ${r.status}`
  }
}

/** One dependency edge in the repo's dependency graph: `blocked` cannot proceed
 * until `blocker` is closed/merged. `source` records where the edge came from —
 * `native` is a GitHub-native issue dependency; `inferred` is derived from
 * timeline cross-references (and never written back to GitHub). */
export interface DepEdge {
  blocked: number
  blocker: number
  source: 'native' | 'inferred'
}

/** A node in the dependency graph's node map, keyed by its number as a string.
 * A thin descriptor the client joins against the live issue/PR list rows where
 * present, and falls back to when a referenced number is not in the loaded list. */
export interface DepNode {
  kind: 'issue' | 'pull'
  state: 'open' | 'closed' | 'merged'
  title: string
}

/** The `GET /api/apps/issue-radar/deps` payload. Schema-versioned so a client
 * can refuse a shape it does not understand rather than mis-render it. */
export interface DepsResponse {
  schema: number
  fetched_at?: string
  edges: DepEdge[]
  /** Node descriptors keyed by number-as-string (e.g. `"5190"`). */
  nodes: Record<string, DepNode>
}

/** The full identity of a connected repository.
 *
 * A ref is `owner`/`repo` plus the provider and — for self-managed instances —
 * the HOST, because `group/project` names an entirely different project on
 * gitlab.com than on a private instance. Every API call takes a ref rather than
 * two loose strings, so a call cannot be made without saying WHICH repo it means.
 *
 * `provider`/`host` are optional and default to public GitHub server-side, so a
 * legacy `ConnectedRepo` record (written before GitLab support) is a valid ref.
 */
export interface RepoRef {
  owner: string
  repo: string
  provider?: SourceProvider
  host?: string
}

/** Which forge a repo lives on.
 *
 * `azure` is Azure DevOps on `dev.azure.com`, where `owner` carries
 * `{organization}/{project}` — a slash-joined pair, the same way `owner` carries a
 * nested group path on GitLab. */
export type SourceProvider = 'github' | 'gitlab' | 'azure'

/** Which provider account an account-scoped endpoint should ask about.
 *
 * Separate from `RepoRef` because `/me` and `/recent-repos` are about the USER on
 * a provider, not about a repo — there is no owner/repo to supply, and pretending
 * there is would invite passing a half-built ref. */
export interface AccountScope {
  provider?: SourceProvider
  host?: string
}

/** Query params naming an account scope. Omitted fields default to public GitHub
 * server-side, so an absent scope is the pre-GitLab behaviour. */
export function accountQuery(scope?: AccountScope): Record<string, string> {
  const params: Record<string, string> = {}
  if (scope?.provider) params.provider = scope.provider
  if (scope?.host) params.host = scope.host
  return params
}

/** Identity query params for a ref, for a GET request. */
export function repoQuery(ref: RepoRef): Record<string, string> {
  const params: Record<string, string> = { owner: ref.owner, repo: ref.repo }
  if (ref.provider) params.provider = ref.provider
  if (ref.host) params.host = ref.host
  return params
}

/** Identity body fields for a ref, for a POST/PUT/DELETE request. */
export function repoBody(ref: RepoRef): Record<string, string> {
  return repoQuery(ref)
}
