// HTTP client for the Code Review Sage backend.
//
// Routes are registered directly on the main gateway aiohttp app under
// ``/api/apps/code-review-sage``, so every call is same-origin and rides the
// dashboard session cookie — no tokens are added here.
import type {
  AddRepoResponse,
  ChatState,
  ConsolidateResponse,
  FollowupStart,
  LearningsResponse,
  NamespacesResponse,
  PinnedRepo,
  RecentReposResponse,
  RepoPrsResponse,
  Run,
  RunReport,
  RunsResponse,
  LocalReviewSession,
  Settings,
  SettingsResponse,
  UserReposResponse,
  ReviewFixActionRequest,
  ReviewFixCreateInput,
  ReviewFixTaskResponse,
} from './lib/types'
import { REVIEW_MODEL_AUTO } from './lib/types'

const API = '/api/apps/code-review-sage'

function reviewModelBody(model?: string | null): { model?: string } {
  const selected = model?.trim()
  return selected && selected !== REVIEW_MODEL_AUTO ? { model: selected } : {}
}

interface ApiError {
  error?: string
  code?: string
}

/** A failed request, carrying the backend's machine-readable `code`.
 *
 *  The prose in `message` is English produced in Python and is advisory only —
 *  rendering it inside a localized page is what the error-code contract exists to
 *  stop. Callers that show copy to the user switch on `code` and supply their own
 *  translated string; `message` is for logs and for codes nobody has mapped yet. */
export class SageApiError extends Error {
  code: string

  constructor(message: string, code: string) {
    super(message)
    this.name = 'SageApiError'
    this.code = code
  }
}

async function parseErrorBody(r: Response): Promise<SageApiError> {
  try {
    const body = (await r.json()) as ApiError
    return new SageApiError(body.error || `HTTP ${r.status}`, body.code || '')
  } catch {
    return new SageApiError(`HTTP ${r.status}`, '')
  }
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw await parseErrorBody(r)
  return r.json() as Promise<T>
}

async function sendJSON<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw await parseErrorBody(r)
  return r.json() as Promise<T>
}

export const sageApi = {
  // --- Follow-up sessions ---
  /** Stored history for one reviewed change, plus whether a follow-up session
   *  would restore the reviewer's own context. */
  chatState: (runId: string, changeId: string): Promise<ChatState> =>
    getJSON(`/chat?run_id=${encodeURIComponent(runId)}`
      + `&change_id=${encodeURIComponent(changeId)}`),

  /** Arm the resume and get what the chat slot needs. Rejects with a
   *  `SageApiError` whose `code` is `followup_not_recorded` /
   *  `followup_transcript_gone` / `chat_run_deleted` / ... */
  followupStart: (runId: string, changeId: string): Promise<FollowupStart> =>
    sendJSON('/followup', 'POST', { run_id: runId, change_id: changeId }),

  // --- Runs (threads) ---
  runs: (): Promise<RunsResponse> => getJSON('/runs'),

  run: (runId: string): Promise<{ run: Run }> =>
    getJSON(`/runs/${encodeURIComponent(runId)}`),

  /** The run's Focus Report as data — this is what renders INLINE, so no part of
   * viewing a report goes through the artifact store. */
  runReport: (runId: string): Promise<RunReport> =>
    getJSON(`/runs/${encodeURIComponent(runId)}/report`),

  cancelRun: (runId: string): Promise<{ ok: boolean; status: string; message: string }> =>
    sendJSON(`/runs/${encodeURIComponent(runId)}/cancel`, 'POST'),

  deleteRun: (runId: string): Promise<{ ok: boolean }> =>
    sendJSON(`/runs/${encodeURIComponent(runId)}`, 'DELETE'),

  /** Publish a finished run's findings to its pull request. Never automatic —
   *  reviews are read in the app unless the user asks for them to be posted. */
  postComments: (runId: string, select?: { changeId: string; keys?: string[] }): Promise<{
    ok: boolean; run_id: string; posting: boolean; pending: number
  }> => sendJSON(`/runs/${encodeURIComponent(runId)}/post`, 'POST',
    // `keys` omitted means every comment still pending for this change; the
    // backend filters by change_id and treats a null key list as "all".
    select
      ? { change_id: select.changeId, ...(select.keys ? { keys: select.keys } : {}) }
      : {}),

  /** Post a selection spanning several changes as ONE request.
   *
   *  `posting` is a per-run flag, so one request per change had every request
   *  after the first refused with `already_posting` — the endpoint returns
   *  before the poster clears the flag, so no amount of client-side sequencing
   *  helps. The backend applies each group's keys to its own change inside a
   *  single posting cycle.
   */
  postCommentGroups: (
    runId: string,
    groups: { changeId: string; keys?: string[] }[],
  ): Promise<{ ok: boolean; run_id: string; posting: boolean; pending: number }> =>
    sendJSON(`/runs/${encodeURIComponent(runId)}/post`, 'POST', {
      groups: groups.map(g => ({
        change_id: g.changeId, ...(g.keys ? { keys: g.keys } : {}),
      })),
    }),

  /** Publish this run's report as a shareable artifact (retry / share path). */
  archiveRun: (runId: string): Promise<{ ok: boolean; report_slug: string }> =>
    sendJSON(`/runs/${encodeURIComponent(runId)}/archive`, 'POST'),

  // --- Starting reviews ---
  /** Review specific PRs (the picker's path, and the pasted-links escape hatch). */
  review: (changes: string[], model?: string | null): Promise<{
    run_id: string; changes: string[]; model?: string | null
  }> =>
    sendJSON('/review', 'POST', { changes, ...reviewModelBody(model) }),

  reviewLinks: (links: string, model?: string | null): Promise<{
    run_id: string; changes: string[]; model?: string | null
  }> =>
    sendJSON('/review', 'POST', { links, ...reviewModelBody(model) }),

  reviewRepo: (
    repo: string,
    force = false,
    model?: string | null,
  ): Promise<{
    run_id?: string; repo: string; changes: string[]; skipped: number; status: string
    message?: string; model?: string | null
  }> =>
    sendJSON('/review-repo', 'POST', { repo, force, ...reviewModelBody(model) }),

  // --- Review Fix tasks -----------------------------------------------------
  createFixTask: (input: ReviewFixCreateInput): Promise<ReviewFixTaskResponse> => {
    const { model, ...rest } = input
    return sendJSON('/fix-tasks', 'POST', { ...rest, ...reviewModelBody(model) })
  },

  fixTask: (taskId: string): Promise<ReviewFixTaskResponse> =>
    getJSON(`/fix-tasks/${encodeURIComponent(taskId)}`),

  reviewAgain: (
    taskId: string,
    input: Pick<ReviewFixActionRequest, 'expected_revision' | 'target_fingerprint'>,
  ): Promise<ReviewFixTaskResponse> =>
    sendJSON(`/fix-tasks/${encodeURIComponent(taskId)}/review-again`, 'POST', input),

  localSessions: (): Promise<{ sessions: LocalReviewSession[] }> =>
    getJSON('/local/sessions'),

  localReview: (repository: string, mode = 'all-working-tree', previousSessionId?: string):
  Promise<{ session: LocalReviewSession }> =>
    sendJSON('/local/review', 'POST', {
      repository, mode, ...(previousSessionId ? { previous_session_id: previousSessionId } : {}),
    }),

  localSession: (id: string): Promise<{ session: LocalReviewSession }> =>
    getJSON(`/local/sessions/${encodeURIComponent(id)}`),

  localDisposition: (
    id: string,
    findingId: string,
    status: string,
    userInstruction?: string,
  ): Promise<{ finding: LocalReviewSession['findings'][number] }> =>
    sendJSON(`/local/sessions/${encodeURIComponent(id)}/disposition`, 'POST', {
      finding_id: findingId, status, user_instruction: userInstruction,
    }),

  localFix: (
    sessionId: string,
    findingIds: string[],
    instruction: string,
  ): Promise<{ fix_id: string; status: string; finding_ids: string[] }> =>
    sendJSON('/local/fix', 'POST', {
      session_id: sessionId, finding_ids: findingIds, instruction,
    }),

  // --- Repo + PR discovery ---
  recentRepos: (days?: number): Promise<RecentReposResponse> =>
    getJSON(`/recent-repos${days === undefined ? '' : `?days=${days}`}`),

  /** Every repo the gh user can reach — the "recently touched" list misses repos
   * you own but haven't pushed to lately. */
  myRepos: (): Promise<UserReposResponse> => getJSON('/my-repos'),

  pinnedRepos: (): Promise<{ repos: PinnedRepo[] }> => getJSON('/repos'),

  pinRepo: (owner: string, repo: string): Promise<{ repos: PinnedRepo[] }> =>
    sendJSON('/repos', 'POST', { owner, repo }),

  pinRepoUrl: (url: string): Promise<AddRepoResponse> =>
    sendJSON('/repos', 'POST', { repo: url }),

  unpinRepo: (owner: string, repo: string): Promise<{ repos: PinnedRepo[] }> =>
    sendJSON('/repos', 'DELETE', { owner, repo }),

  repoPrs: (repo: string): Promise<RepoPrsResponse> =>
    getJSON(`/repo-prs?repo=${encodeURIComponent(repo)}`),

  // --- Settings + learning ---
  settings: (): Promise<SettingsResponse> => getJSON('/settings'),

  putSettings: (patch: Partial<Settings>): Promise<{ ok: boolean; settings: Settings }> =>
    sendJSON('/settings', 'PUT', patch),

  namespaces: (): Promise<NamespacesResponse> => getJSON('/namespaces'),

  createNamespace: (name: string): Promise<{ ok: boolean }> =>
    sendJSON('/namespaces', 'POST', { name }),

  deleteNamespace: (name: string): Promise<{ ok: boolean }> =>
    sendJSON('/namespaces', 'DELETE', { name }),

  learnings: (namespace: string): Promise<LearningsResponse> =>
    getJSON(`/learnings?namespace=${encodeURIComponent(namespace)}`),

  /** Merge a namespace's staged learnings into the ruleset reviews load. The
   *  merge itself is one worker turn; this returns as soon as it is running. */
  consolidateLearnings: (namespace: string): Promise<ConsolidateResponse> =>
    sendJSON('/learnings/consolidate', 'POST', { namespace }),
}
