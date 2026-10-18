/**
 * Behavioural coverage for the shared dashboard API client (`src/api/client.ts`).
 *
 * `client.ts` is one transport layer plus the `api` object of thin typed methods
 * over it, most of them defined by domain in `src/api/client/*.ts`. What can
 * actually break here is (a) the transport contract — session-key header,
 * auth recovery, `ApiError` + error journalling, artifact-write tracking — and
 * (b) URL/body CONSTRUCTION in the methods that are not one-liners: query
 * builders, conditionally-omitted body keys, path encoding, the SSE reader, the
 * blob download and the multipart upload.
 *
 * So the suite is in three parts:
 *   1. transport + auth-banner lifecycle (real DOM assertions on the injected banner),
 *      in this file
 *   2. curated exact-URL/body tests for every method with non-trivial construction,
 *      in `ApiClient.coverage.construction.test.tsx` (the URL and body builders)
 *      and `ApiClient.coverage.methods.test.tsx` (the method implementations)
 *   3. in `ApiClient.coverage.methods.test.tsx`, a sweep over EVERY method on
 *      `api`, asserting each issues exactly one
 *      `/api/...` request whose URL carries no `undefined` / `[object Object]`.
 *      That invariant is what a missing argument or a botched template literal
 *      actually produces, and it holds the whole surface — including the methods
 *      no page-level test happens to exercise.
 *
 * Conventions follow the existing client tests (`instancesApi.test.ts`,
 * `artifactReference.test.ts`): `vi.stubGlobal('fetch', …)` with a hand-rolled
 * Response stub, and assertions read off `fetchMock.mock.calls`. That stub,
 * `fetchMock` and `call()` live in `ApiClient.coverageTestUtils.tsx`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  api,
  ApiError,
  friendlyErrText,
  checkSessionExpired,
  isAuthBannerShown,
  removeAuthBanner,
  attemptSilentRefresh,
  __resetAuthRecoveryStateForTests,
  SEARCH_MIN_CHARS,
} from '../api/client'
import { queryClient } from '../api/queryClient'
import { recentErrors, reportForError, findReport, recordError, __resetErrorJournalForTests, TRANSPORT_REJECTION_COOLDOWN_MS, TRANSPORT_REJECTION_WINDOW_CAP } from '../utils/errorReport'
import { buildErrorPrompt } from '../utils/errorReport.prompt'
import { copyToClipboard } from '../utils/clipboard'
import { resizeImageForModel } from '../utils/resizeImage'
import { hasPendingArtifactWrite, __resetArtifactWrites } from '../lib/artifactWrites'
import {
  res,
  okJson,
  fetchMock,
  call,
} from './ApiClient.coverageTestUtils'

// `revealPath` funnels a headless host's `copy` fallback into the clipboard, and
// `uploadFiles` downscales through the canvas helper — neither of which exists
// under happy-dom. Both are stubbed at their module boundary so the client's own
// wiring is what gets asserted.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => {}),
  copyCode: vi.fn(async () => {}),
}))
vi.mock('../utils/resizeImage', () => ({
  MODEL_IMAGE_LIMITS: { maxEdge: 8000, maxBytes: 3_750_000 },
  resizeImageForModel: vi.fn(async (file: File) => ({ file, info: null })),
}))

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(okJson())
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
  __resetArtifactWrites()
  vi.mocked(copyToClipboard).mockClear()
  vi.mocked(resizeImageForModel).mockClear()
  vi.mocked(resizeImageForModel).mockImplementation(async (file: File) => ({ file, info: null }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

/* ────────────────────────── 1. transport contract ────────────────────────── */

describe('client transport', () => {
  it('exposes the backend search threshold as a shared constant', () => {
    // Must match kiro_crew.history.SEARCH_MIN_CHARS — the pages gate typing on it.
    expect(SEARCH_MIN_CHARS).toBe(2)
  })

  it('GET sends the session-key header and no JSON content type', async () => {
    await api.securityStats()
    const { url, method, headers } = call()
    expect(url).toBe('/api/security/stats')
    expect(method).toBeUndefined()
    expect(headers['X-Session-Key']).toBe('dashboard:ui')
    expect(headers['Content-Type']).toBeUndefined()
  })

  it('POST/PUT/PATCH send a JSON content type alongside the placeholder key', async () => {
    await api.trustApp('demo', 'https://example.test/owner/demo')
    expect(call().headers).toMatchObject({ 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' })
    expect(call().body).toEqual({ repository: 'https://example.test/owner/demo' })
    await api.setTrustAllApps(true)
    expect(call(1).method).toBe('PUT')
    expect(call(1).headers['Content-Type']).toBe('application/json')
    await api.toggleUserDeniedCommand('r1', false)
    expect(call(2).method).toBe('PATCH')
    expect(call(2).body).toEqual({ enabled: false })
  })

  it('uses a distinct adoption route before ordinary tag policy PATCH', async () => {
    await api.adoptChatTag('tag / one', true)
    expect(call()).toMatchObject({
      url: '/api/chat/tags/tag%20%2F%20one/adopt',
      method: 'POST',
      body: { status: true },
    })

    await api.updateChatTag('tag / one', { agent: 'add-only' })
    expect(call(1)).toMatchObject({
      url: '/api/chat/tags/tag%20%2F%20one',
      method: 'PATCH',
      body: { agent: 'add-only' },
    })
  })

  it('DELETE omits the JSON content type when it carries no body, and sets it when it does', async () => {
    await api.deleteUserDeniedCommand('r1')
    expect(call().method).toBe('DELETE')
    expect(call().headers['Content-Type']).toBeUndefined()
    expect(call().init?.body).toBeUndefined()

    await api.deleteLesson('never force push')
    expect(call(1).headers['Content-Type']).toBe('application/json')
    expect(call(1).body).toEqual({ rule: 'never force push' })
  })

  it('deleteLesson sends the repo_scope selector only when it is a string (#10651)', async () => {
    // A lesson's identity is (rule, repo_scope). The route reads the selector by
    // presence: "" names the global row, a fragment names that scope's row, and
    // an ABSENT key deletes every scope's same-rule row.
    await api.deleteLesson('never force push', 'src/pkg')
    expect(call().body).toEqual({ rule: 'never force push', repo_scope: 'src/pkg' })
    await api.deleteLesson('never force push', '')
    expect(call(1).body).toEqual({ rule: 'never force push', repo_scope: '' })
    // null marks a row whose stored scope is unusable: the route refuses a null
    // selector (400), so the client must send none -- the unselective delete is
    // the only path that reaches such a row.
    await api.deleteLesson('never force push', null)
    expect(call(2).body).toEqual({ rule: 'never force push' })
    expect(call(2).body).not.toHaveProperty('repo_scope')
    // The row's JSONL tier rides along when the list reported one, so the route
    // deletes from the file the row was read from rather than its global default.
    await api.deleteLesson('never force push', '', { scope: 'workspace', workspace: 'ws-1' })
    expect(call(3).body).toEqual({ rule: 'never force push', repo_scope: '', scope: 'workspace', workspace: 'ws-1' })
    await api.deleteLesson('never force push', '', { scope: undefined, workspace: undefined })
    expect(call(4).body).toEqual({ rule: 'never force push', repo_scope: '' })
    // `exact` is sent only when true: the route's default is the substring match
    // the CLI relies on, and a table row that holds the whole rule opts out of it.
    await api.deleteLesson('never force push', '', { exact: true })
    expect(call(5).body).toEqual({ rule: 'never force push', repo_scope: '', exact: true })
    await api.deleteLesson('never force push', '', { exact: false })
    expect(call(6).body).not.toHaveProperty('exact')
  })

  it('POST omits the body entirely when none is given', async () => {
    await api.mcpProbe()
    expect(call().init?.body).toBeUndefined()
  })

  it('an explicit session key replaces the shared dashboard:ui placeholder', async () => {
    // `dashboard:ui` satisfies the server's `if sk:` gate but names no session,
    // so a restricted (incognito) slot was never recognised as restricted.
    await api.setArtifactPinned('cr-queue', true, 'dashboard:chat-3')
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-3')
  })

  it('the feature-video routes put the caller\'s session key on the wire', async () => {
    // Both routes are gated server-side by `_is_restricted_session`, which treats
    // the shared `dashboard:ui` placeholder as NOT restricted -- so a key that never
    // reaches the header makes that guard unreachable and lets a session which keeps
    // nothing record a PERMANENT verdict.
    await api.featureVideoNext('dashboard:chat-4')
    expect(call().url).toBe('/api/feature-videos/next')
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-4')

    await api.featureVideoFeedback('vid-1', 'seen', 'dashboard:chat-4')
    expect(call(1).url).toBe('/api/feature-videos/feedback')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:chat-4')
    expect(call(1).body).toEqual({ id: 'vid-1', status: 'seen' })
  })

  it('the feature-video routes fall back to the placeholder when no key is passed', async () => {
    await api.featureVideoNext()
    expect(call().headers['X-Session-Key']).toBe('dashboard:ui')
    await api.featureVideoFeedback('vid-1', 'dismissed')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:ui')
  })

  it('createArtifact derives the session key from origin_session_key when none is passed', async () => {
    await api.createArtifact({ name: 'Doc', content: '<p/>', origin_session_key: 'chat-9' })
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-9')
  })

  it('createArtifact lets an explicit session key win over origin_session_key', async () => {
    await api.createArtifact({ name: 'Doc', content: '<p/>', origin_session_key: 'chat-9' }, 'dashboard:other')
    expect(call().headers['X-Session-Key']).toBe('dashboard:other')
  })

  it('createArtifact falls back to the placeholder when the body names no session', async () => {
    await api.createArtifact({ name: 'Doc', content: '<p/>' })
    expect(call().headers['X-Session-Key']).toBe('dashboard:ui')
  })

  // The steering verbs are the case the placeholder actively broke: the server
  // resolves a `workspace/` key against the project of the slot named by this
  // header, so `dashboard:ui` — which names no slot — made project steering
  // unreachable as soon as two chats sat on different projects. All five verbs
  // take the key, because a file created under one project must stay readable,
  // editable and deletable from the same page load.
  it('the steering reads send the caller\'s session key, on the GET as well as the POST family', async () => {
    await api.steeringFiles('dashboard:chat-4')
    expect(call().url).toBe('/api/steering')
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-4')

    await api.steeringFile('workspace/api.md', 'dashboard:chat-4')
    expect(call(1).url).toBe('/api/steering/workspace/api.md')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:chat-4')
  })

  it('the steering writes send the caller\'s session key', async () => {
    await api.createSteering('api.md', '# API', 'workspace', 'dashboard:chat-4')
    expect(call().method).toBe('POST')
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-4')

    await api.updateSteering('workspace/api.md', '# API v2', 'dashboard:chat-4')
    expect(call(1).method).toBe('PUT')
    expect(call(1).url).toBe('/api/steering/workspace/api.md')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:chat-4')

    await api.deleteSteering('workspace/api.md', 'dashboard:chat-4')
    expect(call(2).method).toBe('DELETE')
    expect(call(2).headers['X-Session-Key']).toBe('dashboard:chat-4')
  })

  it('a workspace write states which project the caller was listed', async () => {
    // The session key names a chat slot and that slot's project can be
    // re-pointed, so the slot alone cannot say which project the user believed
    // they were editing. The listing's `project_key` rides along and the server
    // refuses (409) when it no longer resolves to the same one.
    await api.createSteering('api.md', '# API', 'workspace', 'dashboard:chat-4', 'pk-abc')
    expect(call().headers['X-Steering-Project']).toBe('pk-abc')

    await api.updateSteering('workspace/api.md', '# v2', 'dashboard:chat-4', 'pk-abc')
    expect(call(1).headers['X-Steering-Project']).toBe('pk-abc')

    await api.deleteSteering('workspace/api.md', 'dashboard:chat-4', 'pk-abc')
    expect(call(2).headers['X-Steering-Project']).toBe('pk-abc')
  })

  it('omits the project header when the caller has no project key', async () => {
    // Absent is not "any project": the server fails a workspace write closed on
    // a missing header, so sending nothing is the honest wire form.
    await api.createSteering('api.md', '# API', 'user', 'dashboard:chat-4')
    expect(call().headers['X-Steering-Project']).toBeUndefined()
    expect(call().headers['X-Session-Key']).toBe('dashboard:chat-4')
  })

  it('every steering verb falls back to the placeholder when no session key is passed', async () => {
    // The tab passes `undefined` when no chat is open rather than inventing a
    // slot name, so the fallback is what a settings page with no chat sends.
    await api.steeringFiles()
    await api.steeringFile('workspace/api.md')
    await api.createSteering('api.md', '# API', 'workspace')
    await api.updateSteering('workspace/api.md', '# API v2')
    await api.deleteSteering('workspace/api.md')
    for (let i = 0; i < 5; i++) {
      expect(call(i).headers['X-Session-Key']).toBe('dashboard:ui')
    }
  })
})

describe('client response handling', () => {
  it('resolves the parsed JSON body on 2xx', async () => {
    fetchMock.mockResolvedValue(okJson({ denied_commands: 41 }))
    await expect(api.securityStats()).resolves.toMatchObject({ denied_commands: 41 })
  })

  it('rejects with an ApiError carrying the status, unwrapped message and raw body', async () => {
    fetchMock.mockResolvedValue(res(409, '{"error":"already installed","code":"conflict"}'))
    const err = await api.installDiscoveredSkill('skills.sh', 'grill').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const e = err as ApiError
    expect(e.name).toBe('ApiError')
    expect(e.status).toBe(409)
    // friendlyErrText unwraps {"error": …}; `body` keeps the envelope so a caller
    // can still read the structured `code`.
    expect(e.message).toBe('already installed')
    expect(e.body).toContain('"code":"conflict"')
  })

  it('falls back to "HTTP <status>" when the failure body is empty', async () => {
    fetchMock.mockResolvedValue(res(500, '', { text: '' }))
    await expect(api.securityStats()).rejects.toThrow('HTTP 500')
  })

  it('journals every failure with status, endpoint and backend code', async () => {
    fetchMock.mockResolvedValue(res(403, '{"error":"denied","code":"app_execution_denied"}'))
    await api.trustApp('demo').catch(() => {})
    const [report] = recentErrors()
    expect(report).toMatchObject({
      source: 'api',
      status: 403,
      message: 'denied',
      code: 'app_execution_denied',
      endpoint: '/api/probe',
    })
  })

  it('pins each failure its OWN journal entry, so two same-message refusals do not alias', async () => {
    // The journal resolves by exact message, newest first. Two endpoints refused with the one
    // server line ("Access denied") therefore both resolved to whichever failed LAST, and the
    // first notice handed the agent the second read's endpoint. The transport pins the entry
    // to the error it throws; `reportForError` reads that before the journal.
    const denied = (url: string) =>
      res(403, '{"error":"Access denied","code":"access_denied"}', { url })
    fetchMock.mockResolvedValueOnce(denied('http://localhost:6776/api/security/stats'))
    const first = await api.securityStats().catch((e: unknown) => e)
    fetchMock.mockResolvedValueOnce(denied('http://localhost:6776/api/apps/demo/trust'))
    const second = await api.trustApp('demo').catch((e: unknown) => e)

    expect(first).toBeInstanceOf(ApiError)
    expect(second).toBeInstanceOf(ApiError)
    // Both journaled under one message; the newest is the second read's.
    expect(recentErrors().map(r => r.message)).toEqual(['Access denied', 'Access denied'])
    expect(recentErrors()[0].endpoint).toBe('/api/apps/demo/trust')

    expect(reportForError(first)?.endpoint).toBe('/api/security/stats')
    expect(reportForError(second)?.endpoint).toBe('/api/apps/demo/trust')
  })

  it('maps an API-Gateway throttle body to a readable message', () => {
    const msg = friendlyErrText(429, '{"message":"Rate exceeded","throttlingReasons":null}')
    expect(msg).not.toContain('throttlingReasons')
    expect(msg.toLowerCase()).toContain('rate')
  })

  it('jNullable returns null on 204 rather than exploding on an empty body', async () => {
    // The fixture's `json()` must REJECT, as a real 204 `Response` does: there is no
    // body to parse. With the lenient `res()` default (`json: async () => null` for a
    // null body) this test passed even with the 204 branch removed entirely, so it
    // could not fail and was not guarding the behaviour its name claims. Verified by
    // removing the branch: lenient fixture green, this one red.
    const noContent = {
      ...res(204, null, { text: '' }),
      json: async () => { throw new SyntaxError('Unexpected end of JSON input') },
    } as unknown as Response
    fetchMock.mockResolvedValue(noContent)
    await expect(api.tipsNext()).resolves.toBeNull()
    await expect(api.onboardingImportState({ completed: true })).resolves.toBeNull()
  })

  it('jNullable still rejects with ApiError on a real failure', async () => {
    fetchMock.mockResolvedValue(res(500, 'boom'))
    await expect(api.tipsNext()).rejects.toBeInstanceOf(ApiError)
  })
})

/* ───────────────── transport rejection (no HTTP Response) journaling ───────────────── */

describe('transport rejection journaling', () => {
  // #9579 / #12433: a fetch that rejects at the network layer never produces a
  // Response, so `apiFailure` (which takes one) never runs and the failure was
  // journaled nowhere — the "ask the agent" hand-off degraded to a bare
  // "Failed to fetch" with no endpoint. The five helpers and the raw-fetch
  // family both route through `jfetch`, which journals the rejection.

  it('journals a network rejection on a five-helper (GET) call with endpoint and method, no status', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(api.securityStats()).rejects.toThrow('Failed to fetch')
    const [report] = recentErrors()
    expect(report).toMatchObject({
      source: 'api',
      message: 'Failed to fetch',
      endpoint: '/api/security/stats',
      method: 'GET',
      code: 'network',
    })
    // No response arrived: a synthetic status would mis-route a reader to the server.
    expect(report.status).toBeUndefined()
    // The method renders in the hand-off prompt alongside the endpoint.
    expect(buildErrorPrompt(report, 'lead')).toContain('- Request: GET /api/security/stats')
  })

  it('journals a network rejection on a raw-fetch-family method too', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(api.sessionsMemory()).rejects.toThrow('Failed to fetch')
    expect(recentErrors()[0]).toMatchObject({
      source: 'api',
      message: 'Failed to fetch',
      endpoint: '/api/sessions/memory',
    })
  })

  it('records the rejection message VERBATIM so findReport/ask-agent can resolve it', async () => {
    // findReport matches on exact message; rewording here would orphan the entry.
    fetchMock.mockRejectedValue(new TypeError('Load failed')) // Safari's wording
    const err = await api.securityStats().catch((e: unknown) => e)
    expect(recentErrors()[0].message).toBe('Load failed')
    // The report is pinned to the rejection, so the notice resolves THIS endpoint.
    expect(reportForError(err)?.endpoint).toBe('/api/security/stats')
  })

  it('does NOT journal a deliberate AbortError (unmount / superseded react-query key)', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    fetchMock.mockRejectedValue(abort)
    await expect(api.securityStats()).rejects.toBe(abort)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal a withDeadline TimeoutError (a genuine failure, not an abort)', async () => {
    const timeout = new Error('deadline exceeded')
    timeout.name = 'TimeoutError'
    fetchMock.mockRejectedValue(timeout)
    await expect(api.securityStats()).rejects.toThrow('deadline exceeded')
    expect(recentErrors()[0]).toMatchObject({
      source: 'api',
      message: 'deadline exceeded',
      code: 'timeout',
      endpoint: '/api/security/stats',
    })
  })

  it('burst-caps repeats per endpoint so offline polling cannot flush the 20-deep ring', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    // Same endpoint rejected many times in a tight burst -> one entry, not many.
    for (let i = 0; i < 25; i++) await api.securityStats().catch(() => {})
    const sameEndpoint = recentErrors().filter(r => r.endpoint === '/api/security/stats')
    expect(sameEndpoint).toHaveLength(1)
  })

  it('still records a DIFFERENT endpoint on its own first miss during the burst', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await api.securityStats().catch(() => {})
    await api.sessionsMemory().catch(() => {})
    const endpoints = recentErrors().map(r => r.endpoint).sort()
    expect(endpoints).toEqual(['/api/security/stats', '/api/sessions/memory'])
  })

  it('records the same endpoint again once the cooldown window has elapsed', async () => {
    vi.useFakeTimers()
    try {
      fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
      await api.securityStats().catch(() => {})
      await api.securityStats().catch(() => {})
      expect(recentErrors().filter(r => r.endpoint === '/api/security/stats')).toHaveLength(1)
      vi.advanceTimersByTime(TRANSPORT_REJECTION_COOLDOWN_MS + 1)
      await api.securityStats().catch(() => {})
      expect(recentErrors().filter(r => r.endpoint === '/api/security/stats')).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  // ── review (#17378, iamwhatever): the wrong endpoint must not reach the hand-off ──

  it('two endpoints failing with the SAME network message each resolve their OWN endpoint', async () => {
    // The blocker: every network drop carries one message ("Failed to fetch"),
    // and the message-only `findReport` returns whichever journaled LAST, so a
    // notice during an outage named another poll's endpoint. The report is
    // pinned to each rejection, so `reportForError` resolves THIS request's.
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const first = await api.securityStats().catch((e: unknown) => e)
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const second = await api.sessionsMemory().catch((e: unknown) => e)

    // Both journaled under the one message, newest first — exactly the ambiguity.
    expect(recentErrors().map(r => r.message)).toEqual(['Failed to fetch', 'Failed to fetch'])
    expect(recentErrors()[0].endpoint).toBe('/api/sessions/memory')

    // The pin disambiguates: each error resolves its own request's endpoint.
    expect(reportForError(first)?.endpoint).toBe('/api/security/stats')
    expect(reportForError(second)?.endpoint).toBe('/api/sessions/memory')
  })

  it('findReport (message-only lookup) refuses a transport rejection, so no surface picks up the wrong endpoint', async () => {
    // The message-only sites (AskAgentButton, lifecycle.ts, …) call findReport(e.message).
    // A transport rejection shares its message with every other drop, so it must
    // NOT be findable that way; it is reachable only through the pinned report.
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await api.securityStats().catch(() => {})
    expect(findReport('Failed to fetch')).toBeUndefined()
  })

  it('findReport still resolves an ANSWERED failure that shares the message of a later rejection', async () => {
    // A real HTTP failure (status, endpoint) stays findable by message. Only the
    // transport rejections are skipped, so the message-only path keeps working
    // for the answered failures it was built for.
    recordError({ source: 'api', message: 'Load failed', status: 502, endpoint: '/api/answered' })
    fetchMock.mockRejectedValue(new TypeError('Load failed')) // Safari wording, same text
    await api.securityStats().catch(() => {})
    // The newest "Load failed" is the rejection, but findReport skips it and
    // returns the answered failure's own report.
    expect(findReport('Load failed')?.endpoint).toBe('/api/answered')
    expect(findReport('Load failed')?.status).toBe(502)
  })

  it('keys the burst cap on method + endpoint, so a POST and a GET on one path do not share a bucket', async () => {
    // GET /api/workspaces (a poll) and POST /api/workspaces (a user write) are
    // different requests. Sharing a bucket suppressed the POST and left its
    // notice resolving the GET's method; separate buckets keep each honest.
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await api.workspaces().catch(() => {}) // GET
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const post = await api.createWorkspace({ name: 'w' }).catch((e: unknown) => e) // POST

    const workspaceEntries = recentErrors().filter(r => r.endpoint === '/api/workspaces')
    expect(workspaceEntries.map(r => r.method).sort()).toEqual(['GET', 'POST'])
    // The POST's notice resolves its own method, not the earlier GET poll's.
    expect(reportForError(post)?.method).toBe('POST')
  })

  it('a suppressed repeat still resolves THIS endpoint through the stored report, not a message fallback', async () => {
    // A second rejection of one endpoint inside the cooldown records nothing new,
    // but the error still carries the first miss's report (pinned), so a notice
    // resolves this endpoint rather than falling through to a wrong message match.
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await api.securityStats().catch(() => {}) // first miss records
    const repeat = await api.securityStats().catch((e: unknown) => e) // suppressed
    expect(recentErrors().filter(r => r.endpoint === '/api/security/stats')).toHaveLength(1)
    expect(reportForError(repeat)?.endpoint).toBe('/api/security/stats')
  })

  it('a wide outage cannot flush the ring: answered failures recorded before it survive', async () => {
    // The per-endpoint cap alone did not bound the TOTAL, so 20+ distinct polling
    // endpoints each recording once still emptied the 20-deep ring of real
    // reports. The window ceiling caps distinct rejection keys per window.
    const answered = recordError({ source: 'api', message: 'denied', status: 403, endpoint: '/api/answered-before' })
    // Far more distinct endpoints fail than the ceiling admits.
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    for (let i = 0; i < TRANSPORT_REJECTION_WINDOW_CAP + 10; i++) {
      await api.artifact(`art-${i}`).catch(() => {}) // GET /api/artifacts/<slug> — distinct endpoints
    }
    const rejectionCount = recentErrors().filter(r => r.code === 'network').length
    expect(rejectionCount).toBeLessThanOrEqual(TRANSPORT_REJECTION_WINDOW_CAP)
    // The answered failure from before the outage is still in the journal.
    expect(recentErrors().some(r => r.id === answered.id)).toBe(true)
  })
})

describe('artifact write tracking', () => {
  it('marks a slug as pending for the life of a mutating request and clears it after', async () => {
    let release: (r: Response) => void = () => {}
    fetchMock.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const p = api.updateArtifact('cr-queue', { content: '<p/>' })
    expect(hasPendingArtifactWrite('cr-queue')).toBe(true)
    release(okJson())
    await p
    expect(hasPendingArtifactWrite('cr-queue')).toBe(false)
  })

  it('clears the pending marker even when the write fails — the server never applied it', async () => {
    fetchMock.mockResolvedValue(res(500, 'nope'))
    await api.updateArtifact('cr-queue', { content: '<p/>' }).catch(() => {})
    expect(hasPendingArtifactWrite('cr-queue')).toBe(false)
  })

  it('does not count the settle call as a user write — it is the cleanup', async () => {
    let release: (r: Response) => void = () => {}
    fetchMock.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const p = api.settleBlankArtifact('draft-1', { untitled_name: 'Untitled', draft: '', allow_delete: true })
    expect(hasPendingArtifactWrite('draft-1')).toBe(false)
    release(okJson({ outcome: 'deleted' }))
    await p
  })

  it('does not track reads', async () => {
    let release: (r: Response) => void = () => {}
    fetchMock.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const p = api.artifact('cr-queue')
    expect(hasPendingArtifactWrite('cr-queue')).toBe(false)
    release(okJson())
    await p
  })

  it('falls back to the raw path segment when the slug is not valid percent-encoding', async () => {
    // A hand-typed `%zz` makes decodeURIComponent throw; the write must still be
    // tracked (under the raw segment) rather than taking the transport down.
    let release: (r: Response) => void = () => {}
    fetchMock.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const p = api.deleteArtifact('%zz')
    expect(hasPendingArtifactWrite('%zz')).toBe(true)
    release(okJson())
    await p
    expect(hasPendingArtifactWrite('%zz')).toBe(false)
  })
})

/* ─────────────────────── auth-banner recovery lifecycle ─────────────────────── */

describe('session-expired banner', () => {
  const banner = () => document.getElementById('mc-session-expired')

  afterEach(() => { banner()?.remove() })

  it('ignores a 403 that is not an auth challenge', () => {
    checkSessionExpired(res(403, 'forbidden'))
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('tries a silent refresh first and shows no banner when the cookie rotates', async () => {
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/auth/refresh', expect.anything()))
    expect(banner()).toBeNull()
  })

  it('shows the banner only once the refresh comes back terminal (401)', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(banner()).not.toBeNull())
    expect(isAuthBannerShown()).toBe(true)
    const el = banner() as HTMLElement
    // The recovery instructions are the point of the banner: the command to run
    // and a field to paste the resulting URL into.
    expect(el.textContent).toContain('kirocrew token')
    expect(el.querySelector('input')).not.toBeNull()
    expect(el.querySelector('button')?.textContent).toBe('✕')
  })

  it('is idempotent — a second challenge does not stack a second banner', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(banner()).not.toBeNull())
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    expect(document.querySelectorAll('#mc-session-expired')).toHaveLength(1)
  })

  it('self-dismisses on the next 2xx through the shared response handler', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(banner()).not.toBeNull())

    fetchMock.mockResolvedValue(okJson({ denied_commands: 1 }))
    await api.securityStats()
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('emits mc-auth-required / mc-auth-cleared so components can drop their offline UI', async () => {
    const required = vi.fn()
    const cleared = vi.fn()
    window.addEventListener('mc-auth-required', required)
    window.addEventListener('mc-auth-cleared', cleared)
    try {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(required).toHaveBeenCalled())
      removeAuthBanner()
      expect(cleared).toHaveBeenCalled()
    } finally {
      window.removeEventListener('mc-auth-required', required)
      window.removeEventListener('mc-auth-cleared', cleared)
    }
  })

  it('emits mc-auth-recovered on real recovery but never on a dismiss', async () => {
    // Two events, and the whole point is that they are NOT interchangeable.
    // `mc-auth-cleared` means the banner is gone, which a dismiss also achieves;
    // `mc-auth-recovered` means authentication works. A consumer that resets a
    // stale auth failure must only ever hear the second, so this pins the
    // difference at the producer -- the side no synthetic dispatchEvent can test.
    const cleared = vi.fn()
    const recovered = vi.fn()
    window.addEventListener('mc-auth-cleared', cleared)
    window.addEventListener('mc-auth-recovered', recovered)
    try {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())

      const dismiss = Array.from(banner()?.querySelectorAll('button') ?? []).find(
        b => b.textContent === '✕',
      )
      expect(dismiss).toBeDefined()
      dismiss?.click()
      // Precondition, not the claim: prove the click landed, so the assertions
      // below cannot pass merely because nothing happened.
      expect(banner()).toBeNull()
      expect(cleared).toHaveBeenCalled()
      expect(recovered).not.toHaveBeenCalled()

      // Now the real thing. Raise the banner again and clear it the way every
      // 2xx and every accepted token exchange does.
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      removeAuthBanner()
      expect(recovered).toHaveBeenCalled()
    } finally {
      window.removeEventListener('mc-auth-cleared', cleared)
      window.removeEventListener('mc-auth-recovered', recovered)
    }
  })

  it('removeAuthBanner is a no-op when no banner is up', () => {
    expect(() => removeAuthBanner()).not.toThrow()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('the dismiss button tears the banner down and clears the shown flag', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(banner()).not.toBeNull())
    ;(banner()!.querySelector('button') as HTMLButtonElement).click()
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('highlights the paste field on focus and restores it on blur', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(banner()).not.toBeNull())
    const input = banner()!.querySelector('input') as HTMLInputElement
    input.dispatchEvent(new FocusEvent('focus'))
    expect(input.style.borderColor).toBe('#fff')
    expect(input.style.boxShadow).not.toBe('none')
    input.dispatchEvent(new FocusEvent('blur'))
    expect(input.style.boxShadow).toBe('none')
  })

  describe('pasting a token', () => {
    let original: Location

    beforeEach(() => {
      original = window.location
      Object.defineProperty(window, 'location', {
        value: { protocol: 'https:', host: 'desk.example:6776', href: 'https://desk.example:6776/' },
        writable: true,
        configurable: true,
      })
    })
    afterEach(() => {
      Object.defineProperty(window, 'location', { value: original, writable: true, configurable: true })
    })

    /**
     * Paste *value*, press Enter, and report the URL the exchange requested.
     *
     * The paste used to navigate, so these cases read `location.href`. It now
     * exchanges the token in place against `/api/auth/me?token=...`, which
     * authenticates by the same mechanism the navigation did -- the auth
     * middleware takes a query token ahead of the cookie and writes the session
     * cookie onto the response -- without discarding the page's in-memory state
     * (#12240). What each case is really about, extracting the token out of a
     * pasted URL and encoding it for a query string, is unchanged; only where the
     * token is sent has moved. `location.href` is asserted separately to stay
     * untouched, so a regression back to navigating fails here.
     */
    async function pasteAndEnter(value: string) {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      const input = banner()!.querySelector('input') as HTMLInputElement
      fetchMock.mockClear()
      // The exchange is awaited by the handler, so the request is visible on the
      // mock but the paste itself resolves nothing for the caller to await.
      fetchMock.mockResolvedValue(res(200, '{}'))
      input.value = value
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await vi.waitFor(() => {
        if (!fetchMock.mock.calls.length) throw new Error('no exchange requested')
      }).catch(() => {})
      expect(window.location.href).toBe('https://desk.example:6776/')
      return (fetchMock.mock.calls[0]?.[0] as string | undefined) ?? null
    }

    it('extracts the token out of a pasted `kirocrew token` URL', async () => {
      expect(await pasteAndEnter('http://127.0.0.1:6776/?token=abc123&x=1'))
        .toBe('/api/auth/me?token=abc123')
    })

    it('accepts a bare token, which is not a parseable URL', async () => {
      expect(await pasteAndEnter('rawtoken')).toBe('/api/auth/me?token=rawtoken')
    })

    it('percent-encodes a token containing URL-significant characters', async () => {
      expect(await pasteAndEnter('a+b/c=')).toBe('/api/auth/me?token=a%2Bb%2Fc%3D')
    })

    it('does nothing on an empty field', async () => {
      expect(await pasteAndEnter('   ')).toBeNull()
    })

    it('ignores keys other than Enter', async () => {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      const input = banner()!.querySelector('input') as HTMLInputElement
      fetchMock.mockClear()
      input.value = 'abc'
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
      expect(fetchMock).not.toHaveBeenCalled()
      expect(window.location.href).toBe('https://desk.example:6776/')
    })

    /**
     * Raise the banner and press Enter on *value*, with the exchange answering
     * *exchange*. Returns the banner so a case can read what it now says.
     */
    async function pasteWithExchange(value: string, exchange: Response) {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      const input = banner()!.querySelector('input') as HTMLInputElement
      fetchMock.mockClear()
      fetchMock.mockResolvedValue(exchange)
      input.value = value
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
      return input
    }

    it('says so when the exchange refuses the token, instead of failing silently', async () => {
      const input = await pasteWithExchange('stale-token', res(401, 'expired'))
      // The refusal has to be VISIBLE. Re-enabling the field is the only other
      // cue and is indistinguishable from nothing having happened, which is what
      // makes a user press Enter again and conclude the banner is broken.
      await vi.waitFor(() => {
        expect(banner()!.textContent).toContain('sign-in URL was not accepted')
      })
      expect(input.disabled).toBe(false)
      // Still shown: the user corrects a refused token rather than re-pasting.
      expect(banner()).not.toBeNull()
    })

    it('announces the refusal to a screen reader as well as showing it', async () => {
      await pasteWithExchange('stale-token', res(401, 'expired'))
      await vi.waitFor(() => {
        const live = banner()!.querySelector('[role="status"]')
        expect(live?.textContent).toContain('sign-in URL was not accepted')
      })
    })

    it('drops the previous refusal when a new attempt starts', async () => {
      const input = await pasteWithExchange('stale-token', res(401, 'expired'))
      await vi.waitFor(() =>
        expect(banner()!.textContent).toContain('sign-in URL was not accepted'),
      )
      // A second attempt must not leave the old answer on screen while it runs,
      // or a user cannot tell which attempt the text belongs to.
      fetchMock.mockClear()
      fetchMock.mockImplementation(() => new Promise(() => {}))
      input.value = 'another-token'
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
      expect(banner()!.querySelector('[role="status"]')!.textContent).toBe('')
    })

    it('sets the command as its own <code> element, not as prose', async () => {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      // The sentence is ONE translatable unit, so the chip is found by splitting
      // the rendered text on the command itself. Without it the command reads as
      // prose and a reader cannot see where it begins and ends.
      const chip = banner()!.querySelector('code')
      expect(chip?.textContent).toBe('kirocrew token')
      // Still one sentence around it, not a fragment.
      expect(banner()!.textContent).toContain('in a terminal')
      expect(banner()!.textContent).toContain('press Enter')
    })

    it('every catalog keeps the command its instruction is split on', async () => {
      // The chip rests on a relationship between two catalog values that this
      // module does not own: each locale's instruction must contain that same
      // locale's `reauth_command` verbatim. Pin the RELATIONSHIP, so a
      // translation that breaks it fails here instead of silently costing the
      // chip.
      const catalogs = import.meta.glob<Record<string, unknown>>(
        '../i18n/locales/*.json',
        { eager: true },
      )
      const broken: string[] = []
      let checked = 0
      for (const [path, mod] of Object.entries(catalogs)) {
        // `en.json` holds no api.client section, and `en-XA` is the GENERATED
        // pseudolocale: its generator accents every ASCII letter, so it carries
        // no verbatim command by construction and is not a shipping locale.
        if (path.endsWith('en.json') || path.endsWith('en-XA.json')) continue
        const root = (mod as { default?: Record<string, unknown> }).default ?? mod
        const client = (root as { api?: { client?: Record<string, string> } }).api?.client
        const command = client?.reauth_command
        const instruction = client?.run_kirocrew_token_then_paste_sign_in_url
        if (command === undefined || instruction === undefined) continue
        checked += 1
        if (!instruction.includes(command)) broken.push(path)
      }
      // A vacuous pass is the failure mode here: zero catalogs checked would
      // assert nothing at all.
      expect(checked).toBeGreaterThanOrEqual(12)
      expect(broken).toEqual([])
    })

    it('refetches only the queries that FAILED and hold nothing, so no draft is overwritten', async () => {
      const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
      try {
        await pasteWithExchange('good-token', res(200, '{}'))
        await vi.waitFor(() => expect(spy).toHaveBeenCalled())
        const arg = spy.mock.calls.at(-1)?.[0] as
          | { predicate?: (q: { state: { status: string; data?: unknown } }) => boolean }
          | undefined
        // A no-argument invalidateQueries() refetches EVERY active query,
        // including ones holding good data -- and a panel whose effect syncs
        // editor state from its query would then overwrite an unsaved draft.
        // Asserting the filter exists is what stops a regression back to that.
        expect(arg?.predicate).toBeTypeOf('function')
        expect(arg!.predicate!({ state: { status: 'error' } })).toBe(true)
        expect(arg!.predicate!({ state: { status: 'success' } })).toBe(false)
        expect(arg!.predicate!({ state: { status: 'pending' } })).toBe(false)
        // The status is not enough on its own. React Query keeps the last
        // successful `data` when a refetch fails, so an error-state query can
        // still be holding a value -- and refetching THAT is what re-delivers
        // server data to a sync effect and wipes the draft. `McpCustomServerModal`
        // has exactly such an effect on `specQuery.data`.
        expect(arg!.predicate!({ state: { status: 'error', data: { spec: {} } } })).toBe(false)
        expect(arg!.predicate!({ state: { status: 'error', data: null } })).toBe(false)
        expect(arg!.predicate!({ state: { status: 'error', data: undefined } })).toBe(true)
      } finally {
        spy.mockRestore()
      }
    })
  })

  describe('banner copy', () => {
    it('tells the reader WHERE to run the command, and asks for a sign-in URL', async () => {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      // "Run kirocrew token" alone left a reader guessing where to run it; the
      // panel's own error sentence already said "in a terminal", so the banner
      // saying less than the card was the inconsistency. The command sits inside
      // that sentence rather than in its own <code>, because a value that stops
      // mid-sentence cannot be reordered by a translator.
      expect(banner()!.textContent).toContain('in a terminal')
      expect(banner()!.textContent).toContain('kirocrew token')
      // On this page the user is already holding a credential for the secrets
      // form, so a field labelled "token" invites pasting the wrong one.
      const input = banner()!.querySelector('input') as HTMLInputElement
      expect(input.placeholder).toContain('sign-in URL')
      expect(input.placeholder).not.toContain('raw token')
    })
  })

  function withMockedParent(post: (...args: unknown[]) => unknown, body: () => Promise<void>) {
    const original = Object.getOwnPropertyDescriptor(window, 'parent')
    Object.defineProperty(window, 'parent', {
      value: { postMessage: post },
      writable: true,
      configurable: true,
    })
    return body().finally(() => {
      if (original) Object.defineProperty(window, 'parent', original)
    })
  }

  const embedded403 = () =>
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))

  it('recovers an embedded pane with its OWN silent refresh before troubling the hub', async () => {
    // The pane holds the same 30-day refresh cookie as the top-level path. A
    // lapsed access cookie (laptop slept through the proactive refresh) is fixed
    // by one refresh call — no SSH mint, no iframe reload, no lost pane state.
    const post = vi.fn()
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    await withMockedParent(post, async () => {
      embedded403()
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
      expect(post).not.toHaveBeenCalled()
      expect(banner()).toBeNull()
    })
  })

  it('hands recovery to the hub — ONCE — when the pane cannot refresh itself', async () => {
    // A terminal 401 means the refresh chain is gone, so the hub must re-mint.
    // Every later 403 in this document is the SAME unrepaired session: the hub
    // answers each ask with an SSH mint, so asking once is the whole budget.
    const post = vi.fn()
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    await withMockedParent(post, async () => {
      embedded403()
      await vi.waitFor(() => expect(post).toHaveBeenCalledWith({ type: 'mc-auth-expired' }, '*'))
      for (let i = 0; i < 5; i++) embedded403()
      await Promise.resolve()
      expect(post).toHaveBeenCalledTimes(1)
      // Still no banner: the hub owns recovery inside a pane.
      expect(banner()).toBeNull()
    })
  })

  it('re-opens the hub hand-off after auth is restored', async () => {
    // The latch is about one UNREPAIRED session, not the lifetime of the
    // document: a 2xx proves recovery, so a later genuine lapse may ask again.
    const post = vi.fn()
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    await withMockedParent(post, async () => {
      embedded403()
      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1))
      removeAuthBanner() // what a successful response does
      embedded403()
      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    })
  })

  it('falls through to the banner path when the parent frame is unreachable', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    // Exhaust the pane's own refresh first (terminal 401), so the 403 below goes
    // straight to the hand-off — which throws. The throw is swallowed and the
    // banner is the documented fallback.
    await expect(attemptSilentRefresh()).resolves.toBe(false)
    await withMockedParent(() => { throw new Error('cross-origin') }, async () => {
      expect(() => embedded403()).not.toThrow()
      await vi.waitFor(() => expect(banner()).not.toBeNull())
    })
  })

  it('attemptSilentRefresh reports false on a terminal 401 and true on a rotation', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    await expect(attemptSilentRefresh()).resolves.toBe(false)
    __resetAuthRecoveryStateForTests()
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    await expect(attemptSilentRefresh()).resolves.toBe(true)
  })
})
