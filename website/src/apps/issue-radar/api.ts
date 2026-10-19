// Thin fetch wrapper for the Issue Radar backend (registered directly on the
// main gateway's aiohttp Application — see backend/routes.py:register_routes
// — so the base path is /api/apps/issue-radar, matching code-review-sage's
// convention, NOT the /apps/{name}/api reverse-proxy prefix used by apps like
// file-explorer that run as a separate child process).
import { i18nT } from '../../i18n/t'
import {
  accountQuery, AssigneesConflictError, DEFAULT_REPO_SETTINGS, gitlabGroupError, parseErrorBody,
  repoBody, repoQuery, SettingsConflictError,
} from './apiTypes'
import type {
  AccountScope, ApplyLabelsResponse, BulkApplyResponse, BulkPrAction, BulkPrResponse,
  CiRunsResponse, ConnectResponse, CreateLabelResponse, DepsResponse, GenerateTaggingResponse,
  GitLabGroup, GitLabGroupReviewsResponse, GitLabGroupsResponse, InvestigationPatch,
  InvestigationResponse, IssueAiResponse, IssueAssigneesResponse, IssueDetailResponse,
  IssuesResponse, IssueStateResponse, ItemKind, LabelsResponse, MembersResponse, MeResponse,
  PrAiResponse, PrAutoMergeResponse, PrCommentResponse, PrMergeResponse, PrReviewResponse,
  PrRunActionResponse, PrRunsResponse, PrStateResponse, PullDetailResponse, PullsResponse,
  RecentReposResponse, RecommendationsResponse, RefSummaryResponse, RepoRef, RepoSettings,
  ReposResponse, SettingsResponse, TaggingResponse,
} from './apiTypes'
import type {
  CrewDetailResponse, CrewNamesResponse, CrewPatch, CrewResponse, CrewSettingsPatch,
  CrewSettingsResponse, CrewSpec, CrewsResponse, CrewWorkResponse, WorkItemPatch,
} from './apiCrewTypes'

const API = '/api/apps/issue-radar'

/** The per-request UI-language hint for the backend's AI-prose calls.
 *
 * `aiLanguage` must ALREADY be resolved through the app's own
 * `resolveAiLanguage()` (`lib/format.ts`) by the calling component, which is the
 * single place Issue Radar decides what language its AI output should be in --
 * the same resolver `investigate.ts` and `review.ts` use. Deriving it here from
 * a raw `activeLocale()` instead would give the app two disagreeing resolvers
 * and ignore the user's explicit "Agent output language" pick, so a `ja` picker
 * would produce Japanese investigations beside English summaries.
 *
 * Why a hint has to exist at all: the dashboard's default language is "follow
 * the browser", resolved entirely client-side, and the gateway reads
 * `Accept-Language` nowhere -- so without this the backend cannot know the
 * language of any install that never set `dashboard.language`, and every AI card
 * comes back English inside a fully localized UI. Sending the already-resolved
 * tag also keeps the catalog matcher in one place (`detect.ts`) instead of
 * growing a second copy in Python.
 *
 * Deliberately per-request and never written to config: "Auto" is
 * browser-relative, so persisting what this browser resolved would tell a
 * different browser to discard its own pick -- see the `adoptedServerValue`
 * rationale in `LanguageProvider.tsx`. An explicitly configured
 * `dashboard.language` still outranks it server-side.
 *
 * An empty tag sends NO field. `resolveAiLanguage` returns `''` both for "follow
 * an English browser" and for an explicit English pick, and the directive-free
 * prompt already produces English -- so omitting it leaves an English user's
 * request byte-identical to what it has always been, caches included.
 *
 * Spread into the query for GETs and into the body for POSTs. */
function langHint(aiLanguage: string): Record<string, string> {
  return aiLanguage ? { lang: aiLanguage } : {}
}

// The shapes these calls send and return live in two owners, and every public name
// of both is re-exported here except the two error decoders (`parseErrorBody`,
// `gitlabGroupError`) that only the calls below use. So this file stays the one
// import path the app's modules use and the one module id their specs mock, and
// neither owner imports it. `apiTypes.ts` holds the repo, issue and pull-request
// shapes, the error classes the calls throw, the repo-settings defaults and how a
// request names its repo; `apiCrewTypes.ts` holds the crew records, phase
// classifications and event kinds that mirror the backend crew store.
export {
  GitLabGroupError, AssigneesConflictError, DEFAULT_REPO_SETTINGS, SettingsConflictError,
  accountQuery, repoQuery, repoBody,
} from './apiTypes'
export type {
  ConnectResponse, Issue, IssuesResponse, PullRequest, PullsResponse, GitLabGroup,
  GitLabGroupsResponse, GitLabGroupReview, GitLabGroupReviewsResponse, PrDetailData, PrCheck,
  PullDetailResponse, Reactions, DetailLabel, Milestone, IssueDetailData, TimelineEvent,
  IssueDetailResponse, RefSummary, RefSummaryResponse, SuggestedLabel, IssueAiResponse,
  PrAiResponse, ApplyLabelsResponse, IssueStateResponse, IssueAssigneesResponse, PrAction,
  BulkPrAction, PrMergeResponse, PrStateResponse, PrReviewResponse, PrCommentResponse,
  PrAutoMergeResponse, PrWorkflowRun, PrRunsResponse, CiRun, CiRepoRuns, CiRunsResponse,
  PrRunActionResponse, BulkPrResponse, RepoLabel, LabelsResponse, RepoMember, MembersResponse,
  RepoPermissions, RepoSettings, SettingsResponse, RecommendationCategory, LabelRecommendation,
  RecommendationsResponse, CreateLabelResponse, TaggingSuggestions, UntaggedIssue, TaggingResponse,
  GenerateTaggingResponse, BulkApplyResponse, ConnectedRepo, ReposResponse, RecentRepo,
  GhSetupReason, RecentReposResponse, MeResponse, InvestigationFindings, InvestigationRecord,
  ItemKind, InvestigationResponse, InvestigationPatch, ApiError, DepEdge, DepNode, DepsResponse,
  RepoRef, SourceProvider, AccountScope,
} from './apiTypes'
export {
  CREW_PHASES, CREW_EVENT_KINDS, TERMINAL_PHASES, TTL_ACTIVE_PHASES, EDITING_PHASES,
  countsTowardOpen,
} from './apiCrewTypes'
export type {
  CrewPhase, CrewEventKind, CrewTriedEntry, CrewCiState, Crew, WorkItem, CrewEvent, CrewSettings,
  CrewCounts, CrewPatch, CrewSpec, WorkItemPatch, CrewSettingsPatch, CrewsResponse, CrewResponse,
  CrewNamesResponse, CrewDetailResponse, CrewWorkResponse, CrewSettingsResponse,
} from './apiCrewTypes'

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
   * refresh to force a regenerate. `aiLanguage` is the tag from
   * `resolveAiLanguage()` -- see `langHint`. */
  issueAi: async (ref: RepoRef, number: number, aiLanguage: string, opts?: { refresh?: boolean }): Promise<IssueAiResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), ...langHint(aiLanguage), number: String(number) })
    if (opts?.refresh) q.set('refresh', '1')
    const r = await fetch(`${API}/issue-ai?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** AI summary of a pull request — its description, whole conversation, and
   * check state. Cache-first server-side, and the cache self-invalidates when
   * the PR moves (new comment / push / flipped check), so no manual refresh is
   * needed to pick up changes; pass refresh to force a regenerate anyway.
   * `aiLanguage` is the tag from `resolveAiLanguage()` -- see `langHint`. */
  pullAi: async (ref: RepoRef, number: number, aiLanguage: string, opts?: { refresh?: boolean }): Promise<PrAiResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), ...langHint(aiLanguage), number: String(number) })
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

  /** REPLACE an issue's assignees with `assignees` (the FINAL set of logins, not
   * an add/remove delta). Requires triage/push access (403 otherwise). An empty
   * array clears all assignees; a junk entry is a 400, never a silent clear.
   *
   * `expected` is the set you last READ and is REQUIRED: the write only lands if
   * the forge still holds it. That is what stops replace semantics from silently
   * erasing a concurrent edit — two people who each add one name would otherwise
   * have the later write overwrite the earlier addition. A stale `expected` throws
   * {@link AssigneesConflictError} carrying the current set; re-render from it and
   * let the user redo the edit rather than retrying the same body.
   *
   * A login the forge will not assign is a 400 whose `error` sentence names the
   * refused logins (the body also carries `invalid_assignees`), and NOTHING is
   * applied — GitHub answers 422 for the whole request and GitLab is pre-checked
   * against the project roster. Rendering the thrown message is therefore already
   * actionable; it is not an upstream failure to retry.
   *
   * On success the returned `assignees` is read back from the write rather than
   * echoed from the request, because a success is not required to be an exact echo
   * (GitLab Free keeps only the first assignee) — render THAT. */
  setIssueAssignees: async (
    ref: RepoRef, number: number, assignees: string[], expected: string[],
  ): Promise<IssueAssigneesResponse> => {
    const r = await fetch(`${API}/issue/assignees`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), number, assignees, expected }),
    })
    if (r.status === 409) {
      const body = (await r.json().catch(() => ({}))) as { error?: string; assignees?: string[] }
      throw new AssigneesConflictError(
        body.error || i18nT('apps.issueRadar.api.assignees_changed_elsewhere'),
        body.assignees ?? [],
      )
    }
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

  /** Recent GitHub Actions runs across every connected GitHub repo, for the CI
   * runs dashboard. Read on demand only. */
  ciRuns: async (): Promise<CiRunsResponse> => {
    const r = await fetch(`${API}/ci-runs`, { credentials: 'same-origin' })
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

  /** The GitLab groups the user follows for review requests. A local read: the
   * server answers from its config without calling GitLab. */
  gitlabGroups: async (): Promise<GitLabGroupsResponse> => {
    const r = await fetch(`${API}/gitlab-groups`, { credentials: 'same-origin' })
    if (!r.ok) throw await gitlabGroupError(r)
    return r.json()
  },

  /** Follow a GitLab group. `group` is a bare path such as `acme/platform` (a
   * gitlab.com group) or a pasted group link, which names its own instance. */
  addGitlabGroup: async (group: string): Promise<GitLabGroup> => {
    const r = await fetch(`${API}/gitlab-groups`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group }),
    })
    if (!r.ok) throw await gitlabGroupError(r)
    return r.json()
  },

  /** Stop following a GitLab group. Local only: nothing on GitLab changes. */
  removeGitlabGroup: async (group: GitLabGroup): Promise<GitLabGroup> => {
    const q = new URLSearchParams({ host: group.host, path: group.path })
    const r = await fetch(`${API}/gitlab-groups?${q.toString()}`, { method: 'DELETE', credentials: 'same-origin' })
    if (!r.ok) throw await gitlabGroupError(r)
    return r.json()
  },

  /** Open merge requests across the group and every subgroup where the signed-in
   * `glab` user is a requested reviewer. Capped like `searchPulls`. */
  gitlabGroupReviews: async (group: GitLabGroup): Promise<GitLabGroupReviewsResponse> => {
    const q = new URLSearchParams({ host: group.host, path: group.path })
    const r = await fetch(`${API}/gitlab-groups/review-requests?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw await gitlabGroupError(r)
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
   * if none generated yet). Never runs the model. `aiLanguage` must match what
   * the generate call used: the server refuses to serve a set written in another
   * language, so a mismatch reads as "none generated yet". */
  getRecommendations: async (ref: RepoRef, aiLanguage: string): Promise<RecommendationsResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), ...langHint(aiLanguage) })
    const r = await fetch(`${API}/recommendations?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Generate (and cache) label recommendations via one model call over the
   * repo's labels + a sample of its open issues. `aiLanguage` is the tag from
   * `resolveAiLanguage()` -- see `langHint`. */
  generateRecommendations: async (ref: RepoRef, aiLanguage: string): Promise<RecommendationsResponse> => {
    const r = await fetch(`${API}/recommendations`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), ...langHint(aiLanguage) }),
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
   * (needed to notice labels added on GitHub itself).
   *
   * Sends NO language hint, deliberately -- see `generateTagging`. */
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
   * pass `numbers` to (re)analyse specific issues.
   *
   * Sends NO language hint, deliberately. The tagging cache is ONE document per
   * repo that ACCUMULATES across many batched calls, and the store drops every
   * accumulated entry when the language it was written in changes -- safe while
   * the language is install-wide (a deliberate operator switch, once), but a
   * per-browser hint would make two browsers on different languages alternate
   * forever, each wiping the queue the other just paid a model to build. This
   * surface needs its cache partitioned by language first; until then it follows
   * `dashboard.language` only. */
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

  // ── crews ──────────────────────────────────────────────────────────────────
  //
  // Record shapes and the phase classifications mirror `crew_store.py` — see the
  // interface block in `apiCrewTypes.ts` for which module owns each list.
  //
  // The request ENVELOPES are not uniform, and the differences are load-bearing
  // because a wrong key is a 400 rather than a type error. Checked against
  // `crew_routes.py` handler by handler:
  //
  //   GET  /crews, /crews/names, /crews/settings   ?owner&repo
  //   GET  /crew                     ?owner&repo&id
  //   POST /crews                    owner/repo + the crew fields at the ROOT
  //   PUT  /crew                     owner/repo + `id` + the patch at the ROOT
  //   DELETE /crew                   owner/repo + `id`
  //   POST /crew/pause               owner/repo + `id` + `paused` (bool) + `reason`
  //   PUT  /crew/work                owner/repo + `crew_id` + `number` + patch
  //   PUT  /crews/settings           owner/repo + a NESTED `settings` object
  //
  // The last two are the exceptions; every other write names the crew `id` and
  // carries its payload flat.

  /** Every non-retired crew in the repo, plus the repo-wide protocol settings and
   * the header tallies. One request, because the crew list cannot be rendered
   * without all three. */
  crews: async (ref: RepoRef): Promise<CrewsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/crews?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Create a crew. A duplicate name is refused server-side (the name field is
   * free text, so uniqueness cannot live in the suggestion chips). */
  createCrew: async (ref: RepoRef, spec: CrewSpec): Promise<CrewResponse> => {
    const r = await fetch(`${API}/crews`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), ...spec }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Unused names for the create dialog's chips. Server-side, because taken names
   * include RETIRED crews' — which the crew list does not return. */
  suggestCrewNames: async (ref: RepoRef): Promise<CrewNamesResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/crews/names?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** One crew's page payload: the record, its work items, its recent ledger
   * lines, and its slot usage. */
  crew: async (ref: RepoRef, id: string): Promise<CrewDetailResponse> => {
    const q = new URLSearchParams({ ...repoQuery(ref), id })
    const r = await fetch(`${API}/crew?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Merge a patch into one crew. A rename re-checks uniqueness but leaves
   * `avatar_seed` alone, so the crew keeps its face. */
  updateCrew: async (ref: RepoRef, id: string, patch: CrewPatch): Promise<CrewResponse> => {
    const r = await fetch(`${API}/crew`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), id, ...patch }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Retire a crew — it stops working, but its record, its NAME RESERVATION and
   * its work log all survive. Deliberately not "delete": reusing the name would
   * make the retired crew's old claim comments read as live claims. */
  retireCrew: async (ref: RepoRef, id: string): Promise<CrewResponse> => {
    const r = await fetch(`${API}/crew`, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), id }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Upsert one work item AND append at most one ledger line, in one request —
   * see `WorkItemPatch`. Refused (409) when a second item tries to enter an
   * editing phase while another still holds the crew's worktree. */
  recordCrewWork: async (
    ref: RepoRef, id: string, number: number, patch: WorkItemPatch,
  ): Promise<CrewWorkResponse> => {
    const r = await fetch(`${API}/crew/work`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      // `crew_id`, NOT `id`: this route is the odd one out — `_handle_crew_work`
      // reads `crew_id`, while /crew and /crew/pause read `id` — and it answers
      // 400 `missing_crew_id` for the wrong spelling.
      //
      // The envelope keys go LAST so no field of a future `WorkItemPatch` can
      // shadow one. The server has the mirror of this rule (`_WORK_PATCH_FIELDS`
      // is an allowlist, so the envelope cannot land in the patch either).
      body: JSON.stringify({ ...repoBody(ref), ...patch, crew_id: id, number }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Pause or resume a crew. `reason` is stored on the record as
   * `paused_reason`; pass it only when pausing. */
  setCrewPaused: async (
    ref: RepoRef, id: string, paused: boolean, reason?: string,
  ): Promise<CrewResponse> => {
    const r = await fetch(`${API}/crew/pause`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), id, paused, reason: reason ?? '' }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** "Switch back to triage model": release a model picked in the crew's chat, so
   * the crew switches between its triage and work models again. The one
   * `model_held` value a patch may carry; only the runtime sets the hold. */
  resumeCrewModelSwitching: async (ref: RepoRef, id: string): Promise<CrewResponse> => {
    const r = await fetch(`${API}/crew`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...repoBody(ref), id, model_held: false }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  getCrewSettings: async (ref: RepoRef): Promise<CrewSettingsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/crews/settings?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** Merge a patch into the repo's protocol settings. A PATCH-style merge, not a
   * whole-document replace, so this needs no revision guard: two tabs editing
   * different fields cannot erase each other. */
  putCrewSettings: async (
    ref: RepoRef, patch: CrewSettingsPatch,
  ): Promise<CrewSettingsResponse> => {
    const r = await fetch(`${API}/crews/settings`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      // NESTED under `settings`, matching this app's own `PUT /settings` so the
      // two configuration surfaces share one client shape. Spreading the patch at
      // the root is a 400 `invalid_settings`: the handler requires the key to be
      // an object and never falls back to reading loose fields.
      body: JSON.stringify({ ...repoBody(ref), settings: patch }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },

  /** The repo's dependency edges (blocked-by / blocking) + a node map, for the
   * Graph tab and the detail-pane "Blocked by / Blocking" section. Cache-first,
   * like `/issues`. The backend route lands in a SEPARATE PR (M1), so callers
   * must treat a 404/500/empty answer as "no dependency data yet" and render a
   * designed empty state rather than an error — see GraphView / DepsSection. */
  deps: async (ref: RepoRef): Promise<DepsResponse> => {
    const q = new URLSearchParams(repoQuery(ref))
    const r = await fetch(`${API}/deps?${q.toString()}`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return r.json()
  },
}
