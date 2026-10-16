// Shared types for source-control payloads: pull requests, issues, the source
// provider id and an app repository's contributors. Consumers import them from
// `types/index.ts`.

export interface PullRequestCommit {
  sha: string; title: string; body: string; author: string; date: string; url: string
}

export interface PullRequestCheck {
  name: string; workflow: string; status: string; conclusion: string
  bucket: 'passed' | 'skipped' | 'failed' | 'pending'; url: string
  startedAt: string; completedAt: string
}

/** Lightweight per-URL status used by wayfinding chips (sidebar + Changes tab
 *  strip). Every field is present only when known: the backend serves them
 *  from a short-TTL cache and refreshes in the background, so a freshly seen
 *  pull request has no status until a later poll. The merge fields are omitted
 *  while the provider is still computing mergeability, so their absence means
 *  "no news" — never "nothing blocks the merge". */
export interface PullRequestStatus {
  state?: 'open' | 'draft' | 'merged' | 'closed'
  ci?: 'running' | 'passed' | 'failed'; ciPending?: string // '1': `ci` failed, other checks still running
  /** Normalized merge ability, same vocabulary as `PullRequestSource.mergeable`. */
  mergeable?: string
  /** Normalized merge-state detail, same vocabulary as `PullRequestSource.mergeStateStatus`. */
  mergeStateStatus?: string
}

/** Response of the batched status endpoint. `refreshing` names the URLs whose
 *  cached value is expected to change shortly (a background provider refresh is
 *  in flight), so the client can re-poll soon instead of waiting a full cache
 *  interval; `ttlSecs` is the server's own cache TTL, which paces the steady
 *  state so the client never hardcodes a copy of it. */
export interface PullRequestStatusBatch {
  statuses: Record<string, PullRequestStatus>
  refreshing?: string[]
  ttlSecs?: number
}

export interface PullRequestComment {
  id: string; kind: 'comment' | 'review' | 'inline'; author: string; body: string
  state: string; createdAt: string; url: string; path: string; line?: number | null
  threadId?: string; resolvable?: boolean; resolved?: boolean
}

export interface PullRequestFile {
  path: string; status: string; additions: number; deletions: number; patch: string
}

/* ── Issue sources (GitHub issues / GitLab issues) ─────────────────────────
 * A session that MENTIONS an issue url gets an Issues side-panel tab, the same
 * inferred association pull requests already have. Served by
 * POST /api/source/issue. */

/** `color` is a bare 6-hex-digit string with NO leading '#' (GitHub's format;
 *  GitLab's `#rrggbb` is normalized to it server-side). The UI adds the '#'. */
export interface IssueLabel {
  name: string; color: string; description: string
}

export interface IssueMilestone {
  title: string; state: string; dueOn: string
}

export interface IssueComment {
  id: string; author: string; body: string; createdAt: string; url: string
}

/** Which source system an extracted link, issue, or pull request belongs to.
 *
 *  The three built-ins are spelled out so they still autocomplete and so a
 *  `provider === 'github'` narrowing keeps working, but the type is OPEN: a
 *  downstream edition registers its own provider through
 *  `registerSourceProvider` (see `utils/pullRequestLinks`) and its id then flows
 *  through these payloads unchanged. `(string & {})` is the standard way to widen
 *  a literal union without collapsing it to `string` in editor completions.
 *
 *  Declared here rather than in `utils/pullRequestLinks` so the payload types
 *  never have to import from a util (which imports `ChatMessage` from
 *  `types/index.ts`); `PullRequestProvider` there is an alias of this. */
export type SourceProviderId = 'github' | 'gitlab' | 'jira' | (string & {})

/** A pull request / merge request the provider reports as linked to the issue. */
/** A linked change: a pull request (GitHub/GitLab) or a linked issue (Jira). */
export interface IssueLinkedChange {
  provider: SourceProviderId; url: string; number: number; title: string; state: string
  /** Jira link relationship label (e.g. "blocks", "is blocked by"). */
  relation?: string
  /** Full Jira issue key (e.g. "PROJ-123"). */
  issueKey?: string
}

/** Reaction tallies. Null on providers (or issues) that report none. */
export interface IssueReactions {
  total: number; plus1: number; minus1: number; laugh: number; hooray: number
  confused: number; heart: number; rocket: number; eyes: number
}

export interface IssueSource {
  provider: SourceProviderId
  /** Always the validated request url, never the provider's echo of it. */
  url: string
  number: number
  title: string
  /** Issue body, markdown. */
  description: string
  state: 'open' | 'closed'
  /** 'completed' | 'not_planned' | 'reopened' | '' (always '' on GitLab). */
  stateReason: string
  author: string
  /** ISO8601, or '' when the provider omitted it. */
  createdAt: string
  updatedAt: string
  closedAt: string
  closedBy: string
  labels: IssueLabel[]
  assignees: string[]
  milestone: IssueMilestone | null
  commentCount: number
  locked: boolean
  reactions: IssueReactions | null
  comments: IssueComment[]
  linkedChanges: IssueLinkedChange[]
  /** Sections potentially incomplete because a provider request failed or hit a limit. */
  partialSections?: string[]
}

/** A single contributor to an app's source repository (GitHub only, v1).
 *  Names and avatar URLs are provider-controlled — render as text / <img>. */
export interface AppContributor {
  login: string
  /** Display name, falling back to the login when the profile has none. */
  name: string
  avatarUrl: string
  profileUrl: string
}

export interface PullRequestSource {
  provider: SourceProviderId; url: string; number: number; title: string
  description: string; state: string; draft: boolean; mergedAt: string; updatedAt: string
  headBranch: string; baseBranch: string; headSha: string; author: string
  additions: number; deletions: number; changedFiles: number
  /** Normalized merge ability: 'mergeable' | 'conflicting' | 'unknown' ('' when the provider omitted it). */
  mergeable?: string
  /** Normalized merge-state detail: 'clean' | 'dirty' | 'behind' | 'blocked' | 'unstable' | 'draft' | 'need_rebase' (GitLab) | 'unknown' | ''. */
  mergeStateStatus?: string
  /** Auto-merge is armed: GitHub auto-merge, or GitLab merge-when-pipeline-succeeds. */
  autoMerge?: boolean
  commits: PullRequestCommit[]; checks: PullRequestCheck[]
  comments: PullRequestComment[]; files: PullRequestFile[]
  /** Sections potentially incomplete because a provider request failed or hit a page/output limit. */
  partialSections?: string[]
}
