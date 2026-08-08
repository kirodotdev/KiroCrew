// Thin fetch wrapper for the Issue Radar backend (registered directly on the
// main gateway's aiohttp Application — see backend/routes.py:register_routes
// — so the base path is /api/apps/issue-radar, matching code-review-sage's
// convention, NOT the /apps/{name}/api reverse-proxy prefix used by apps like
// file-explorer that run as a separate child process).
import { i18nT } from '../../i18n/t'

const API = '/api/apps/issue-radar'

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
  /** Watch this repo in the background and push a KiroCrew notification when a
   * new issue is opened. Opt-in (default false). */
  notify_on_new_issue: boolean
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

async function parseErrorBody(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as ApiError
    return body.error || `HTTP ${r.status}`
  } catch {
    return `HTTP ${r.status}`
  }
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

/** Which forge a repo lives on. */
export type SourceProvider = 'github' | 'gitlab'

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

  issues: async (ref: RepoRef, opts?: { refresh?: boolean; poll?: boolean; state?: 'open' | 'closed' }): Promise<IssuesResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    if (opts?.state) q.set('state', opts.state)
    if (opts?.refresh) q.set('refresh', '1')
    if (opts?.poll) q.set('poll', '1')
    const r = await fetch(`${API}/issues?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** The newest single page of OPEN issues, for the progressive first paint on a
   * cold cache — one round-trip, versus the tens of paginated requests the full
   * `issues()` fetch needs on a large repo. A warm cache is returned whole
   * (`partial: false`); a cold one returns just the first page (`partial: true`)
   * WITHOUT writing the server cache, so the authoritative full fetch below still
   * owns it. Open state only. */
  issuesFirstPage: async (ref: RepoRef): Promise<IssuesResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    q.set('first_page', '1')
    const r = await fetch(`${API}/issues?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  issueDetail: async (ref: RepoRef, number: number, opts?: { refresh?: boolean }): Promise<IssueDetailResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issue?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** List pull requests for a repo. `state` is 'open' (default) or 'closed'
   * (closed is bounded to the 100 most-recently-updated, merged + unmerged). */
  pulls: async (ref: RepoRef, opts?: { refresh?: boolean; poll?: boolean; state?: 'open' | 'closed' }): Promise<PullsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    if (opts?.state) q.set('state', opts.state)
    if (opts?.refresh) q.set('refresh', '1')
    if (opts?.poll) q.set('poll', '1')
    const r = await fetch(`${API}/pulls?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** The newest single page of OPEN PRs, for the progressive first paint on a
   * cold cache — one round-trip, versus the full pagination PLUS GraphQL
   * enrichment the `pulls()` fetch runs before it can return. A warm cache is
   * returned whole (`partial: false`); a cold one returns just the first page,
   * un-enriched, (`partial: true`) WITHOUT writing the server cache, so the
   * authoritative fetch still owns it. Open state only. Mirrors `issuesFirstPage`. */
  pullsFirstPage: async (ref: RepoRef): Promise<PullsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    q.set('first_page', '1')
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
    ref: RepoRef,
    opts: { state?: 'open' | 'closed' | 'merged'; author?: string; assignee?: string; reviewRequested?: string },
  ): Promise<PullsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
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
  pullDetail: async (ref: RepoRef, number: number, opts?: { refresh?: boolean }): Promise<PullDetailResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/pull?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Compact summary of one referenced issue/PR — one cheap request, no
   * timeline. Cache-first server-side with a short TTL; backs the reference
   * hover card and the issue-vs-PR resolution for a bare `#123`. */
  refSummary: async (ref: RepoRef, number: number, opts?: { refresh?: boolean }): Promise<RefSummaryResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/ref?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** AI triage (summary + suggested labels), cache-first server-side; pass
   * refresh to force a regenerate. */
  issueAi: async (ref: RepoRef, number: number, opts?: { refresh?: boolean }): Promise<IssueAiResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issue-ai?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** AI summary of a pull request — its description, whole conversation, and
   * check state. Cache-first server-side, and the cache self-invalidates when
   * the PR moves (new comment / push / flipped check), so no manual refresh is
   * needed to pick up changes; pass refresh to force a regenerate anyway. */
  pullAi: async (ref: RepoRef, number: number, opts?: { refresh?: boolean }): Promise<PrAiResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/pull-ai?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Apply a label change (add and/or remove). Requires triage/push access on
   * the repo (403 otherwise). Returns the issue's authoritative label set. */
  applyLabels: async (
    ref: RepoRef, number: number, add: string[], remove: string[],
  ): Promise<ApplyLabelsResponse> => {
    const r = await fetch(`${API}/labels/apply`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, add, remove }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Close or reopen an issue. Requires triage/push access (403 otherwise).
   * On close, reason is 'completed' (default) or 'not_planned'. */
  setIssueState: async (
    ref: RepoRef, number: number,
    state: 'open' | 'closed', stateReason?: 'completed' | 'not_planned',
  ): Promise<IssueStateResponse> => {
    const r = await fetch(`${API}/issue/state`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, state, state_reason: stateReason }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  // ── pull-request actions ───────────────────────────────────────────────────
  //
  // All of these require triage/push access on the repo (403 otherwise) and are
  // SEL-audited server-side.
  //
  // Two merge affordances, and neither bypasses a gate. `mergePr` merges a PR the
  // provider already reports ready — the server re-reads its merge state, refuses
  // anything outside `_MERGE_ALLOWED_STATES`, and pins the merge to the `head_sha`
  // this client rendered, so a push landing mid-review is a 409 rather than a merge.
  // `setPrAutoMerge` is the complement for a PR that is not ready yet: it arms the
  // PROVIDER's own auto-merge, which merges only once its required reviews and checks
  // pass (GitHub only — refused on GitLab; see gitlab_client.enable_auto_merge).
  // There is deliberately no "override and merge".

  /** Close or reopen a pull request. */
  setPrState: async (
    ref: RepoRef, number: number, state: 'open' | 'closed',
  ): Promise<PrStateResponse> => {
    const r = await fetch(`${API}/pull/state`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, state }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Submit a review. `body` is required for 'request_changes' and 'comment'
   * (the provider rejects them bodyless). GitLab has no 'request_changes' verb
   * and the server refuses rather than recording a weaker verdict. */
  submitPrReview: async (
    ref: RepoRef, number: number,
    event: 'approve' | 'request_changes' | 'comment', body?: string, headSha?: string,
  ): Promise<PrReviewResponse> => {
    const r = await fetch(`${API}/pull/review`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      // `head_sha` is REQUIRED, for the same reason `mergePr` requires it: a review is
      // a verdict on a REVISION. It rides to the provider as GitHub's `commit_id` /
      // GitLab's `sha`, but only GitLab's is a real precondition — GitHub's is
      // attribution and would accept a stale approval — so for a VERDICT the server
      // also re-reads the live head and answers 409 `review_conflict` on a moved one.
      // Callers must submit the sha they SHOWED, not a fresh read (see PrActionsBar).
      body: JSON.stringify({
        ...repoBody(ref), number, event, body: body ?? '', head_sha: headSha ?? '',
      }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Post a conversation comment on a pull request. */
  addPrComment: async (
    ref: RepoRef, number: number, body: string,
  ): Promise<PrCommentResponse> => {
    const r = await fetch(`${API}/pull/comment`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, body }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Merge a pull request NOW. Per-PR only — there is no bulk merge.
   *
   * This cannot bypass a gate: branch protection, required reviews and required
   * checks are enforced by the PROVIDER on its own merge endpoint, so a PR that has
   * not satisfied them is refused (409 `merge_not_allowed`) and nothing merges.
   * `setPrAutoMerge` is the complement, for a PR that is not mergeable yet.
   * `headSha` is required and pins the merge to the reviewed commit. */
  mergePr: async (
    ref: RepoRef, number: number, headSha: string,
    method: 'MERGE' | 'SQUASH' | 'REBASE' = 'SQUASH',
  ): Promise<PrMergeResponse> => {
    const r = await fetch(`${API}/pull/merge`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      // `head_sha` is REQUIRED: the merge is pinned to the commit this client
      // rendered, so a push landing between the read and the click is refused rather
      // than merging code nobody reviewed.
      body: JSON.stringify({ ...repoBody(ref), number, method, head_sha: headSha }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Arm or disarm the provider's own auto-merge — for a PR that is not mergeable
   * YET: the provider merges it once ITS required reviews and checks pass. Fails
   * when the repo has no auto-merge rule (or the PR is already clean, with nothing
   * to wait for), and that error surfaces rather than being reported as a false
   * success. */
  setPrAutoMerge: async (
    ref: RepoRef, number: number, enabled: boolean,
    method: 'MERGE' | 'SQUASH' | 'REBASE' = 'SQUASH',
  ): Promise<PrAutoMergeResponse> => {
    const r = await fetch(`${API}/pull/auto-merge`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, enabled, method }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** The CI runs on a PR's head commit, each carrying the id and the
   * cancellable/rerunnable flags the run actions need. */
  pullRuns: async (
    ref: RepoRef, number: number, sha: string,
  ): Promise<PrRunsResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number), sha })
    const r = await fetch(`${API}/pull/runs?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Cancel or re-run one CI run on a PR. `failedOnly` re-runs just the failed
   * jobs (the common intent after a flake). */
  pullRunAction: async (
    ref: RepoRef, number: number, runId: number,
    action: 'cancel' | 'rerun', failedOnly = false,
  ): Promise<PrRunActionResponse> => {
    const r = await fetch(`${API}/pull/run`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...repoBody(ref), number, run_id: runId, action, failed_only: failedOnly,
      }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Apply ONE action to many pull requests. Resolves even when some PRs failed
   * — read `failed` — because a batch is never abandoned over one locked or
   * already-merged PR. */
  bulkPrAction: async (
    ref: RepoRef, numbers: number[], action: BulkPrAction,
    opts?: {
      body?: string
      method?: 'MERGE' | 'SQUASH' | 'REBASE'
      /** `{ "<number>": "<sha>" }` — REQUIRED for `approve`, which is N verdicts and
       * so pins each one to the commit its row was rendered at. Keyed by NUMBER, not
       * a parallel array, so a reordered selection cannot pair a sha with the wrong
       * PR. The server rejects a missing or partial map. */
      headShas?: Record<string, string>
    },
  ): Promise<BulkPrResponse> => {
    const r = await fetch(`${API}/pulls/bulk`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...repoBody(ref), numbers, action,
        body: opts?.body ?? '', method: opts?.method ?? 'SQUASH',
        head_shas: opts?.headShas ?? {},
      }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  labels: async (ref: RepoRef, opts?: { refresh?: boolean }): Promise<LabelsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/labels?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  members: async (ref: RepoRef, opts?: { refresh?: boolean }): Promise<MembersResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
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

  /** Repos the `gh` user personally contributed to within the last `days` —
   * the connect dialog's multi-select picker. Live call (not cached).
   * `days` is required: the window belongs to the caller (see
   * RECENT_WINDOW_DAYS) so the value isn't defined in two places. */
  /** Repos the current user contributed to on ONE provider. `scope` names which
   * provider/host to ask — the answer is per-account, so a GitHub list would be
   * meaningless for a GitLab connect flow. */
  recentRepos: async (days: number, scope?: AccountScope): Promise<RecentReposResponse> => {
    const q = new URLSearchParams({ days: String(days), ...accountQuery(scope) })
    const r = await fetch(`${API}/recent-repos?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** The current user's login on ONE provider — see `MeResponse.login`. */
  me: async (scope?: AccountScope): Promise<MeResponse> => {
    const q = new URLSearchParams(accountQuery(scope))
    const r = await fetch(`${API}/me?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  getSettings: async (ref: RepoRef): Promise<SettingsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/settings?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Replace a repo's settings. `settings.revision` is REQUIRED — the whole
   * document is replaced, so the server refuses (409) a write built on a revision
   * that has since moved, which is what stops one tab erasing another's change.
   * A 409 throws `SettingsConflictError` carrying the newer settings. */
  putSettings: async (ref: RepoRef, settings: RepoSettings): Promise<SettingsResponse> => {
    const r = await fetch(`${API}/settings`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), settings }),
    })
    if (r.status === 409) {
      const body = (await r.json().catch(() => ({}))) as { error?: string; settings?: RepoSettings }
      throw new SettingsConflictError(
        body.error || i18nT('apps.issueRadar.api.these_settings_changed_elsewhere'),
        body.settings ?? DEFAULT_REPO_SETTINGS,
      )
    }
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  disconnect: async (ref: RepoRef): Promise<{ ok: boolean }> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/repos?${q.toString()}`, { method: 'DELETE', credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Read an issue's investigation record (`investigation` is null if the issue
   * has never been investigated). */
  getInvestigation: async (
    ref: RepoRef, number: number, kind: ItemKind = 'issue',
  ): Promise<InvestigationResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), number: String(number), kind })
    const r = await fetch(`${API}/investigation?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Upsert an issue's investigation record — link the session (slot_key +
   * folder_id), bump status, or store findings. The server merges + normalizes,
   * so a partial patch (even `{}`, which just bumps the last-opened stamp on
   * resume) is valid. */
  saveInvestigation: async (
    ref: RepoRef, number: number, patch: InvestigationPatch, kind: ItemKind = 'issue',
  ): Promise<InvestigationResponse> => {
    const r = await fetch(`${API}/investigation`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, kind, ...patch }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Read the repo's cached AI label recommendations (`recommendations` is null
   * if none generated yet). Never runs the model. */
  getRecommendations: async (ref: RepoRef): Promise<RecommendationsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/recommendations?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Generate (and cache) label recommendations via one model call over the
   * repo's labels + a sample of its open issues. */
  generateRecommendations: async (ref: RepoRef): Promise<RecommendationsResponse> => {
    const r = await fetch(`${API}/recommendations`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(repoBody(ref)),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Create a NEW label on the repo. Requires triage/push access (403
   * otherwise); idempotent if the label already exists. */
  createLabel: async (
    ref: RepoRef, label: { name: string; color?: string; description?: string },
  ): Promise<CreateLabelResponse> => {
    const r = await fetch(`${API}/labels/create`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), ...label }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Read the untagged queue + any cached label suggestions for it. Never runs
   * the model, so it is safe to call whenever the Tagging dashboard mounts.
   * Pass refresh to re-read the issues from GitHub rather than the local cache
   * (needed to notice labels added on GitHub itself). */
  tagging: async (
    ref: RepoRef, opts?: { refresh?: boolean },
  ): Promise<TaggingResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/tagging?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Generate label suggestions with ONE batched model call. Omit `numbers` to
   * take the next un-analysed slice of the queue (repeat to walk a long backlog);
   * pass `numbers` to (re)analyse specific issues. */
  generateTagging: async (
    ref: RepoRef, numbers?: number[],
  ): Promise<GenerateTaggingResponse> => {
    const r = await fetch(`${API}/tagging`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      // `=== undefined`, not truthiness: an explicit empty array means
      // "analyse exactly these (none)", and collapsing it to an omission
      // started a whole automatic batch.
      body: JSON.stringify(
        numbers === undefined ? repoBody(ref) : { ...repoBody(ref), numbers },
      ),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Append ONE label to a repo's local triage-label role, server-side under the
   * config lock. Use INSTEAD of getSettings + putSettings: the PUT replaces the
   * whole document, so a client read-modify-write can only serialize itself and
   * two tabs would drop each other's label. */
  addSettingLabel: async (
    ref: RepoRef,
    role: 'triage_labels' | 'good_first_issue_labels', label: string,
  ): Promise<SettingsResponse> => {
    const r = await fetch(`${API}/settings/role`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), role, label }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Apply label ADDITIONS to many issues in one request. Requires triage/push
   * access (403 otherwise). Resolves even when some issues fail — inspect
   * `failed` rather than assuming success. */
  applyLabelsBulk: async (
    ref: RepoRef, changes: { number: number; add: string[] }[],
  ): Promise<BulkApplyResponse> => {
    const r = await fetch(`${API}/labels/apply-bulk`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), changes }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },
}
