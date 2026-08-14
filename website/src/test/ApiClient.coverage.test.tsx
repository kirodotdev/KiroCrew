/**
 * Behavioural coverage for the shared dashboard API client (`src/api/client.ts`).
 *
 * `client.ts` is one transport layer plus ~450 thin typed methods over it. What
 * can actually break here is (a) the transport contract — session-key header,
 * auth recovery, `ApiError` + error journalling, artifact-write tracking — and
 * (b) URL/body CONSTRUCTION in the methods that are not one-liners: query
 * builders, conditionally-omitted body keys, path encoding, the SSE reader, the
 * blob download and the multipart upload.
 *
 * So the file is in three parts:
 *   1. transport + auth-banner lifecycle (real DOM assertions on the injected banner)
 *   2. curated exact-URL/body tests for every method with non-trivial construction
 *   3. a sweep over EVERY method on `api`, asserting each issues exactly one
 *      `/api/...` request whose URL carries no `undefined` / `[object Object]`.
 *      That invariant is what a missing argument or a botched template literal
 *      actually produces, and it holds the whole surface — including the methods
 *      no page-level test happens to exercise.
 *
 * Conventions follow the existing client tests (`instancesApi.test.ts`,
 * `artifactReference.test.ts`): `vi.stubGlobal('fetch', …)` with a hand-rolled
 * Response stub, and assertions read off `fetchMock.mock.calls`.
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
import { recentErrors, __resetErrorJournalForTests } from '../utils/errorReport'
import { copyToClipboard } from '../utils/clipboard'
import { resizeImageForModel } from '../utils/resizeImage'
import { hasPendingArtifactWrite, __resetArtifactWrites } from '../lib/artifactWrites'
import { CONSENT_PREFIX } from '../utils/themeConsent'

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

type Init = RequestInit & { headers?: Record<string, string> }

function res(
  status: number,
  body: unknown,
  opts: { headers?: Record<string, string>; text?: string } = {},
): Response {
  const text = opts.text ?? (typeof body === 'string' ? body : JSON.stringify(body))
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/probe',
    headers: { get: (k: string) => opts.headers?.[k] ?? opts.headers?.[k.toLowerCase()] ?? null },
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => text,
    blob: async () => new Blob([text]),
  } as unknown as Response
}

const okJson = (body: unknown = { ok: true }) => res(200, body)

const fetchMock = vi.fn()

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

/** The nth fetch call, destructured the way every assertion below wants it. */
function call(n = 0) {
  const [url, init] = fetchMock.mock.calls[n] as [string, Init | undefined]
  return {
    url,
    init,
    method: init?.method,
    headers: (init?.headers ?? {}) as Record<string, string>,
    // Only JSON bodies are decoded; a multipart upload stays a FormData.
    body: typeof init?.body === 'string'
      ? (JSON.parse(init.body) as Record<string, unknown>)
      : undefined,
  }
}

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
    await api.trustApp('demo')
    expect(call().headers).toMatchObject({ 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' })
    await api.setTrustAllApps(true)
    expect(call(1).method).toBe('PUT')
    expect(call(1).headers['Content-Type']).toBe('application/json')
    await api.toggleUserDeniedCommand('r1', false)
    expect(call(2).method).toBe('PATCH')
    expect(call(2).body).toEqual({ enabled: false })
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

  it('maps an API-Gateway throttle body to a readable message', () => {
    const msg = friendlyErrText(429, '{"message":"Rate exceeded","throttlingReasons":null}')
    expect(msg).not.toContain('throttlingReasons')
    expect(msg.toLowerCase()).toContain('rate')
  })

  it('jNullable returns null on 204 rather than exploding on an empty body', async () => {
    fetchMock.mockResolvedValue(res(204, null, { text: '' }))
    await expect(api.tipsNext()).resolves.toBeNull()
    await expect(api.onboardingImportState({ completed: true })).resolves.toBeNull()
  })

  it('jNullable still rejects with ApiError on a real failure', async () => {
    fetchMock.mockResolvedValue(res(500, 'boom'))
    await expect(api.tipsNext()).rejects.toBeInstanceOf(ApiError)
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
    expect(el.querySelector('code')?.textContent).toBe('kirocrew token')
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

    async function pasteAndEnter(value: string) {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      const input = banner()!.querySelector('input') as HTMLInputElement
      input.value = value
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      return window.location.href
    }

    it('extracts the token out of a pasted `kirocrew token` URL', async () => {
      expect(await pasteAndEnter('http://127.0.0.1:6776/?token=abc123&x=1'))
        .toBe('https://desk.example:6776?token=abc123')
    })

    it('accepts a bare token, which is not a parseable URL', async () => {
      expect(await pasteAndEnter('rawtoken')).toBe('https://desk.example:6776?token=rawtoken')
    })

    it('percent-encodes a token containing URL-significant characters', async () => {
      expect(await pasteAndEnter('a+b/c=')).toBe('https://desk.example:6776?token=a%2Bb%2Fc%3D')
    })

    it('does nothing on an empty field', async () => {
      expect(await pasteAndEnter('   ')).toBe('https://desk.example:6776/')
    })

    it('ignores keys other than Enter', async () => {
      fetchMock.mockResolvedValue(res(401, 'revoked'))
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      await vi.waitFor(() => expect(banner()).not.toBeNull())
      const input = banner()!.querySelector('input') as HTMLInputElement
      input.value = 'abc'
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
      expect(window.location.href).toBe('https://desk.example:6776/')
    })
  })

  it('hands recovery to the hub instead of bannering when embedded in the Instances pane', () => {
    // Inside the hub's iframe the user cannot fetch the REMOTE token, so the
    // parent is signalled and re-mints instead. The message carries no secret.
    const post = vi.fn()
    const original = Object.getOwnPropertyDescriptor(window, 'parent')
    Object.defineProperty(window, 'parent', {
      value: { postMessage: post },
      writable: true,
      configurable: true,
    })
    try {
      checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
      expect(post).toHaveBeenCalledWith({ type: 'mc-auth-expired' }, '*')
      expect(banner()).toBeNull()
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      if (original) Object.defineProperty(window, 'parent', original)
    }
  })

  it('falls through to the banner path when the parent frame is unreachable', async () => {
    const original = Object.getOwnPropertyDescriptor(window, 'parent')
    Object.defineProperty(window, 'parent', {
      value: { postMessage: () => { throw new Error('cross-origin') } },
      writable: true,
      configurable: true,
    })
    try {
      // The postMessage throw is swallowed; the call still returns the response.
      expect(() => checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))).not.toThrow()
    } finally {
      if (original) Object.defineProperty(window, 'parent', original)
      await Promise.resolve()
    }
  })

  it('attemptSilentRefresh reports false on a terminal 401 and true on a rotation', async () => {
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    await expect(attemptSilentRefresh()).resolves.toBe(false)
    __resetAuthRecoveryStateForTests()
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    await expect(attemptSilentRefresh()).resolves.toBe(true)
  })
})

/* ──────────────────── 2. URL and body construction ──────────────────── */

describe('query-string builders', () => {
  it('kiroPrerequisite distinguishes latched read, coalesced poll and explicit probe', async () => {
    await api.kiroPrerequisite()
    expect(call().url).toBe('/api/kiro-prerequisite')
    await api.kiroPrerequisite('auto')
    expect(call(1).url).toBe('/api/kiro-prerequisite?refresh=auto')
    await api.kiroPrerequisite('explicit')
    expect(call(2).url).toBe('/api/kiro-prerequisite?refresh=explicit')
  })

  it('cloud lifecycle calls carry only the coordinates they were given', async () => {
    await api.cloudStop('kc-1')
    expect(call().url).toBe('/api/cloud/kc-1/stop')
    await api.cloudStart('kc-1', { region: 'us-west-2' })
    expect(call(1).url).toBe('/api/cloud/kc-1/start?region=us-west-2')
    await api.cloudDestroy('kc-1', { profile: 'dev', region: 'us-east-1', instanceId: 'i-0abc' })
    expect(call(2).url).toBe('/api/cloud/kc-1?profile=dev&region=us-east-1&instance_id=i-0abc')
    expect(call(2).method).toBe('DELETE')
  })

  it('cloudPreflight omits absent profile/region', async () => {
    await api.cloudPreflight()
    expect(call().url).toBe('/api/cloud/preflight')
    await api.cloudPreflight('dev')
    expect(call(1).url).toBe('/api/cloud/preflight?profile=dev')
    await api.cloudPreflight('dev', 'eu-west-1')
    expect(call(2).url).toBe('/api/cloud/preflight?profile=dev&region=eu-west-1')
  })

  it('artifacts() serialises every filter, and sends pinned=0 for an explicit false', async () => {
    await api.artifacts()
    expect(call().url).toBe('/api/artifacts')
    await api.artifacts({
      tag: 'cr', kind: 'widget', q: 'queue', source_path: '/n/a.md',
      snippet: true, contentMatch: true, session: 'chat-1', touchedBy: 'chat-2', pinned: true,
    })
    expect(call(1).url).toBe(
      '/api/artifacts?tag=cr&kind=widget&q=queue&source_path=%2Fn%2Fa.md' +
      '&snippet=1&content=1&session=chat-1&touched_by=chat-2&pinned=1',
    )
    // `pinned: false` must survive as an explicit "unpinned only", not be dropped.
    await api.artifacts({ pinned: false })
    expect(call(2).url).toBe('/api/artifacts?pinned=0')
    await api.artifacts({ snippet: false, contentMatch: false })
    expect(call(3).url).toBe('/api/artifacts')
  })

  it('cronHistory sends offset/limit only when supplied, including zero', async () => {
    await api.cronHistory('job-1')
    expect(call().url).toBe('/api/crons/job-1/history')
    await api.cronHistory('job-1', 0, 25)
    expect(call(1).url).toBe('/api/crons/job-1/history?offset=0&limit=25')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:ui')
  })

  it('cronHistoryAll can filter by job', async () => {
    await api.cronHistoryAll()
    expect(call().url).toBe('/api/crons/history')
    await api.cronHistoryAll({ offset: 10, limit: 5, jobId: 'job-2' })
    expect(call(1).url).toBe('/api/crons/history?offset=10&limit=5&job_id=job-2')
  })

  it('chatSlotDetail paginates with limit/before', async () => {
    await api.chatSlotDetail('chat 1')
    expect(call().url).toBe('/api/chat/slots/chat%201?')
    await api.chatSlotDetail('chat-1', 50, 0)
    expect(call(1).url).toBe('/api/chat/slots/chat-1?limit=50&before=0')
  })

  it('suggestions can force regeneration', async () => {
    await api.suggestions()
    expect(call().url).toBe('/api/suggestions')
    await api.suggestions(true)
    expect(call(1).url).toBe('/api/suggestions?force=1')
  })

  it('instanceStatus opts into the diagnosis probe', async () => {
    await api.instanceStatus('cd-1', true)
    expect(call().url).toBe('/api/instances/cd-1/status?diagnose=1')
  })

  it('sessions/search/episodic pass their paging and tag filters through', async () => {
    await api.sessions()
    expect(call().url).toBe('/api/sessions?limit=30&offset=0')
    await api.sessions(10, 20, true)
    expect(call(1).url).toBe('/api/sessions?limit=10&offset=20&preview=1')
    await api.sessionsSearch('a b', 5)
    expect(call(2).url).toBe('/api/sessions/search?q=a%20b&limit=5')
    await api.vectorEpisodic(10, 5, 'promo,l6')
    expect(call(3).url).toBe('/api/memory/episodic?limit=10&offset=5&tags=promo%2Cl6')
    await api.vectorEpisodic()
    expect(call(4).url).toBe('/api/memory/episodic?limit=50&offset=0')
    await api.vectorEpisodicSearch('q', 'tag')
    expect(call(5).url).toBe('/api/memory/episodic/search?q=q&tags=tag')
  })

  it('discovery endpoints append provider and limit only when set', async () => {
    await api.discoverSkills('grill')
    expect(call().url).toBe('/api/skills/-/discover?q=grill')
    await api.discoverSkills('grill', { provider: 'skills.sh', limit: 5 })
    expect(call(1).url).toBe('/api/skills/-/discover?q=grill&provider=skills.sh&limit=5')
    await api.mcpDiscover('fs')
    expect(call(2).url).toBe('/api/mcp/discover?q=fs')
    await api.mcpDiscover('fs', { provider: 'official', limit: 3 })
    expect(call(3).url).toBe('/api/mcp/discover?q=fs&provider=official&limit=3')
  })

  it('browseRemoteArtifacts defaults to the caller-owned scope', async () => {
    await api.browseRemoteArtifacts('quip')
    expect(call().url).toBe('/api/remote-artifacts/quip/browse?scope=mine')
    await api.browseRemoteArtifacts('quip', { scope: 'shared', q: 'a b', pageToken: 't/1' })
    expect(call(1).url).toBe('/api/remote-artifacts/quip/browse?scope=shared&q=a%20b&pageToken=t%2F1')
  })

  it('effortLevels and mcpActive scope to a slot/agent when given one', async () => {
    await api.effortLevels()
    expect(call().url).toBe('/api/effort-levels')
    await api.effortLevels('chat-1')
    expect(call(1).url).toBe('/api/effort-levels?slot=chat-1')
    await api.mcpActive()
    expect(call(2).url).toBe('/api/mcp/active')
    await api.mcpActive('kirocrew')
    expect(call(3).url).toBe('/api/mcp/active?agent=kirocrew')
  })

  it('browse/project helpers encode the path they were handed', async () => {
    await api.browseDirs()
    expect(call().url).toBe('/api/browse-dirs')
    await api.browseDirs('/a b/c')
    expect(call(1).url).toBe('/api/browse-dirs?path=%2Fa%20b%2Fc')
    await api.browseFiles('/a b')
    expect(call(2).url).toBe('/api/browse-files?path=%2Fa%20b')
    await api.projectGit('/repo x')
    expect(call(3).url).toBe('/api/project/git?path=%2Frepo%20x')
    await api.fileDiff('/repo/a b.ts')
    expect(call(4).url).toBe('/api/file-diff?path=%2Frepo%2Fa%20b.ts')
  })

  it('fileSearch scopes to a project and forwards the abort signal', async () => {
    const ctl = new AbortController()
    await api.fileSearch('cli', 'kirocrew', ctl.signal)
    expect(call().url).toBe('/api/file-search?q=cli&project=kirocrew')
    expect(call().init?.signal).toBe(ctl.signal)
    await api.fileSearch('cli')
    expect(call(1).url).toBe('/api/file-search?q=cli')
    expect(call(1).init).toBeUndefined()
  })

  it('artifactSessionDocs can scope to one session', async () => {
    await api.artifactSessionDocs()
    expect(call().url).toBe('/api/artifacts/session-docs')
    await api.artifactSessionDocs('chat-1')
    expect(call(1).url).toBe('/api/artifacts/session-docs?session=chat-1')
  })

  it('vectorContextPreview and telemetryContextTrace encode their query', async () => {
    await api.vectorContextPreview()
    expect(call().url).toBe('/api/memory/context-preview')
    await api.vectorContextPreview('a b')
    expect(call(1).url).toBe('/api/memory/context-preview?q=a%20b')
    await api.telemetryContextTrace('chat 1')
    expect(call(2).url).toBe('/api/telemetry/context-trace?slot=chat%201')
  })

  it('deleteArtifactFolder states the cascade explicitly in the query', async () => {
    await api.deleteArtifactFolder('f1', false)
    expect(call().url).toBe('/api/artifact-folders/f1?delete_contents=false')
    await api.deleteArtifactFolder('f1', true)
    expect(call(1).url).toBe('/api/artifact-folders/f1?delete_contents=true')
  })

  it('getArtifactPublishProviders and weixinQrStatus encode their single param', async () => {
    await api.getArtifactPublishProviders('markdown')
    expect(call().url).toBe('/api/artifacts/publish-providers?kind=markdown')
    await api.weixinQrStatus('s/1')
    expect(call(1).url).toBe('/api/channels/weixin/qr/status?session_id=s%2F1')
  })

  it('knowledgeSearch and autocomplete encode the query', async () => {
    await api.knowledgeSearch('a&b')
    expect(call().url).toBe('/api/knowledge/search-for-context?q=a%26b')
    await api.autocomplete('a&b')
    expect(call(1).url).toBe('/api/autocomplete?q=a%26b')
  })

  it('agentResolvedModel resolves the configured default for an empty agent', async () => {
    await api.agentResolvedModel('')
    expect(call().url).toBe('/api/agents/resolved-model?agent=')
  })
})

describe('path encoding', () => {
  it('percent-encodes single-segment ids', async () => {
    await api.artifact('a/b')
    expect(call().url).toBe('/api/artifacts/a%2Fb')
    await api.deleteChatSlot('chat/1')
    expect(call(1).url).toBe('/api/chat/slots/chat%2F1')
    await api.untrustApp('my app')
    expect(call(2).url).toBe('/api/security/trusted-apps/my%20app')
    await api.deleteWorkspace('w s')
    expect(call(3).url).toBe('/api/workspaces/w%20s')
    await api.deleteTheme('a b')
    expect(call(4).url).toBe('/api/themes/a%20b')
  })

  it('keeps the separators of nested skill/prompt/steering keys while encoding each segment', async () => {
    // These names are genuinely hierarchical (`auto/resolve-i18n/...`), so the
    // slashes are structure and must NOT be encoded — only the segments are.
    await api.skill('auto/my skill')
    expect(call().url).toBe('/api/skills/auto/my%20skill')
    await api.skillTree('auto/my skill')
    expect(call(1).url).toBe('/api/skills/auto/my%20skill/-/tree')
    await api.skillFile('auto/s', 'rules/a b.md')
    expect(call(2).url).toBe('/api/skills/auto/s/-/file?path=rules%2Fa%20b.md')
    await api.promptDetail('team/sop x')
    expect(call(3).url).toBe('/api/prompts/team/sop%20x')
    await api.steeringFile('project/a b.md')
    expect(call(4).url).toBe('/api/steering/project/a%20b.md')
    await api.updateSkill('auto/s', '# hi')
    expect(call(5).url).toBe('/api/skills/auto/s')
    expect(call(5).body).toEqual({ content: '# hi' })
  })

  it('builds the versioned artifact paths', async () => {
    await api.artifactVersion('cr-queue', 3)
    expect(call().url).toBe('/api/artifacts/cr-queue/versions/3')
    await api.artifactVersions('cr-queue')
    expect(call(1).url).toBe('/api/artifacts/cr-queue/versions')
    await api.artifactEvents('cr-queue')
    expect(call(2).url).toBe('/api/artifacts/cr-queue/events')
  })

  it('encodes both ids in two-segment comment paths', async () => {
    await api.replyArtifactComment('a/b', 'c/d', { text: 'ok' })
    expect(call().url).toBe('/api/artifacts/a%2Fb/comments/c%2Fd/reply')
    await api.deleteArtifactComment('a/b', 'c/d')
    expect(call(1).url).toBe('/api/artifacts/a%2Fb/comments/c%2Fd')
    await api.markCommentReview('s', 'c')
    expect(call(2).url).toBe('/api/artifacts/s/comments/c/review')
    await api.resolveComment('s', 'c')
    expect(call(3).url).toBe('/api/artifacts/s/comments/c/resolve')
    await api.reopenComment('s', 'c')
    expect(call(4).url).toBe('/api/artifacts/s/comments/c/reopen')
    await api.editArtifactComment('s', 'c', { text: 'x' })
    expect(call(5).url).toBe('/api/artifacts/s/comments/c')
    expect(call(5).method).toBe('PATCH')
  })

  it('percent-encodes provider-native remote ids, which may contain slashes', async () => {
    await api.remoteArtifactDetail('quip', 'folder/doc')
    expect(call().url).toBe('/api/remote-artifacts/quip/folder%2Fdoc')
    await api.remoteArtifactComments('quip', 'folder/doc')
    expect(call(1).url).toBe('/api/remote-artifacts/quip/folder%2Fdoc/comments')
    await api.deleteRemoteComment('quip', 'folder/doc', 'c/1')
    expect(call(2).url).toBe('/api/remote-artifacts/quip/folder%2Fdoc/comments/c%2F1')
    // clone/fork keep the external id in the BODY — a path segment cannot carry it.
    await api.cloneRemoteArtifact('quip', 'folder/doc')
    expect(call(3).url).toBe('/api/remote-artifacts/quip/clone')
    expect(call(3).body).toEqual({ external_id: 'folder/doc' })
    await api.forkRemoteArtifact('quip', 'folder/doc')
    expect(call(4).body).toEqual({ external_id: 'folder/doc' })
  })
})

describe('request bodies with conditionally-omitted keys', () => {
  it('createChatSlot sends only the fields it was given', async () => {
    await api.createChatSlot()
    expect(call().body).toEqual({})
    await api.createChatSlot('n', 'a', 'm', 'mode', 'mem', 't', false, 'slug', 'f1')
    expect(call(1).body).toEqual({
      name: 'n', agent: 'a', model: 'm', mode: 'mode', memory_mode: 'mem',
      title: 't', clean_mode: false, artifact: 'slug', folder_id: 'f1',
    })
  })

  it('sessionStorageRestore adds the uid list only for a partial restore', async () => {
    await api.sessionStorageRestore('b1')
    expect(call().body).toEqual({ batch_id: 'b1' })
    await api.sessionStorageRestore('b1', ['u1'])
    expect(call(1).body).toEqual({ batch_id: 'b1', uids: ['u1'] })
  })

  it('forkChatSlot omits an unspecified fork point', async () => {
    await api.forkChatSlot('chat-1')
    expect(call().body).toEqual({})
    // index 0 is a legitimate fork point and must not be dropped as falsy.
    await api.forkChatSlot('chat-1', 0, 'why', 'plan', 'down')
    expect(call(1).body).toEqual({ at_message_index: 0, prompt: 'why', mode: 'plan', direction: 'down' })
  })

  it('slackLink sends no body at all when it is only asking for the existing link', async () => {
    await api.slackLink('chat-1')
    expect(call().init?.body).toBeUndefined()
    await api.slackLink('chat-1', 'C123', '1699.1')
    expect(call(1).body).toEqual({ channel: 'C123', thread_ts: '1699.1' })
  })

  it('channelClearContext narrows to one agent only in agent scope', async () => {
    await api.channelClearContext('ch1', 'all')
    expect(call().body).toEqual({ scope: 'all' })
    await api.channelClearContext('ch1', 'agent', 'a1')
    expect(call(1).body).toEqual({ scope: 'agent', agent_id: 'a1' })
  })

  it('answerQuestion distinguishes an answer from a dismissal', async () => {
    await api.answerQuestion('ask-1', { q1: 'yes' })
    expect(call().body).toEqual({ answers: { q1: 'yes' } })
    await api.answerQuestion('ask-1')
    expect(call(1).body).toEqual({ dismissed: true })
  })

  it('interruptSlot targets a specific queued message when asked to', async () => {
    await api.interruptSlot('chat-1')
    expect(call().body).toEqual({})
    await api.interruptSlot('chat-1', 'q1')
    expect(call(1).body).toEqual({ queue_id: 'q1' })
  })

  it('uninstallApp only names the retention exceptions it needs', async () => {
    await api.uninstallApp('demo')
    expect(call().body).toEqual({})
    await api.uninstallApp('demo', false, true, ['dep-a'])
    expect(call(1).body).toEqual({ purge_data: true, keep_dependencies: true, keep_specific: ['dep-a'] })
    await api.uninstallApp('demo', true, false, [])
    expect(call(2).body).toEqual({})
  })

  it('createChatFolder folds the modal settings into the create body', async () => {
    await api.createChatFolder('Work')
    expect(call().body).toEqual({ name: 'Work', parent_id: '' })
    await api.createChatFolder('Work', 'p1', { project_dir: '/r', default_agent: 'kirocrew', color: 'blue' })
    expect(call(1).body).toEqual({
      name: 'Work', parent_id: 'p1', project_dir: '/r', default_agent: 'kirocrew', color: 'blue',
    })
  })

  it('chatSlotContext marks an ephemeral injection and names its source', async () => {
    await api.chatSlotContext('chat-1', 'ctx')
    expect(call().body).toEqual({ content: 'ctx' })
    await api.chatSlotContext('chat-1', 'ctx', { source: 'artifact', ephemeral: false })
    expect(call(1).body).toEqual({ content: 'ctx', source: 'artifact', ephemeral: false })
  })

  it('sideTurn flags a steer, and sideQueueCancel names the owning tab', async () => {
    await api.sideTurn('chat-1', 'why?')
    expect(call().body).toEqual({ question: 'why?' })
    await api.sideTurn('chat-1', 'why?', { steer: true })
    expect(call(1).body).toEqual({ question: 'why?', steer: true })
    await api.sideQueueCancel('chat-1', 'q1')
    expect(call(2).method).toBe('DELETE')
    expect(call(2).body).toHaveProperty('client')
  })

  it('handoffSlot posts no body when the channel is left to the server', async () => {
    await api.handoffSlot('chat-1')
    expect(call().init?.body).toBeUndefined()
    await api.handoffSlot('chat-1', 'slack')
    expect(call(1).body).toEqual({ channel: 'slack' })
  })

  it('cancelTaskRunner and installDiscoveredSkill keep their optional fields optional', async () => {
    await api.cancelTaskRunner()
    expect(call().init?.body).toBeUndefined()
    await api.cancelTaskRunner('t1')
    expect(call(1).body).toEqual({ task_id: 't1' })
    await api.installDiscoveredSkill('skills.sh', 'grill', { name: 'grill2', overwrite: true })
    expect(call(2).body).toEqual({ provider: 'skills.sh', skill_id: 'grill', name: 'grill2', overwrite: true })
  })

  it('cleanupSessions coerces its flags and defaults the active slot', async () => {
    await api.cleanupSessions(30)
    expect(call().body).toEqual({ max_inactive_days: 30, active_slot: '', dry_run: false })
    await api.cleanupSessions(7, 'chat-1', true)
    expect(call(1).body).toEqual({ max_inactive_days: 7, active_slot: 'chat-1', dry_run: true })
  })

  it('planTask and startTaskRunner blank out absent optional strings', async () => {
    await api.startTaskRunner('spec')
    expect(call().body).toEqual({ spec: 'spec', agent: '', workspace_dir: '' })
    await api.planTask('do it', 'chat')
    expect(call(1).body).toEqual({ input: 'do it', source: 'chat', spec: '', agent: '', workspace_dir: '' })
    await api.executePlan('t1')
    expect(call(2).body).toEqual({ agent: '', auto_approve: false })
    await api.planFromChat([{ title: 'a' }])
    expect(call(3).body).toEqual({ steps: [{ title: 'a' }], task_id: '', original_input: '' })
  })

  it('createWebhookToken requires a signature by default', async () => {
    await api.createWebhookToken('ci')
    expect(call().body).toEqual({ label: 'ci', require_signature: true })
    await api.createWebhookToken('legacy', false)
    expect(call(1).body).toEqual({ label: 'legacy', require_signature: false })
  })

  it('addUserDeniedCommand defaults the operator note to empty', async () => {
    await api.addUserDeniedCommand('rm -rf /')
    expect(call().body).toEqual({ pattern: 'rm -rf /', note: '' })
  })

  it('setSlotFolder and setSlotColor send a clearing value rather than dropping the key', async () => {
    await api.setSlotFolder('chat-1', null)
    expect(call().body).toEqual({ folder_id: '' })
    await api.setSlotColor('chat-1', null)
    expect(call(1).body).toEqual({ color_index: null })
    await api.setSlotColor('chat-1', 0)
    expect(call(2).body).toEqual({ color_index: 0 })
  })

  it('mcpGatewaySetStub has a single and a batch form', async () => {
    await api.mcpGatewaySetStub('fs', true)
    expect(call().body).toEqual({ name: 'fs', stub: true })
    await api.mcpGatewaySetStubMany(['fs', 'git'], false)
    expect(call(1).body).toEqual({ names: ['fs', 'git'], stub: false })
  })

  it('createTagColumn/updateTagColumn pass the filter mode straight through', async () => {
    await api.createTagColumn({ name: 'Doing', tag_ids: ['t1'], mode: 'any', include_untagged: true })
    expect(call().body).toEqual({ name: 'Doing', tag_ids: ['t1'], mode: 'any', include_untagged: true })
    await api.updateTagColumn('c1', { mode: 'none', order: 2 })
    expect(call(1).method).toBe('PATCH')
    expect(call(1).body).toEqual({ mode: 'none', order: 2 })
  })

  it('vectorSemanticWrite stamps the write as an explicit user edit', async () => {
    await api.vectorSemanticWrite('k', 'v')
    expect(call().body).toEqual({ key: 'k', value: 'v', source: 'user_explicit' })
  })

  it('enablePullRequestAutoMerge requires opting in to an immediate merge', async () => {
    await api.enablePullRequestAutoMerge('https://gh/pr/1')
    expect(call().body).toEqual({ url: 'https://gh/pr/1', confirmImmediateMerge: false })
    await api.enablePullRequestAutoMerge('https://gh/pr/1', true)
    expect(call(1).body).toEqual({ url: 'https://gh/pr/1', confirmImmediateMerge: true })
  })

  it('pull-request source calls carry the refresh flag and the thread ids', async () => {
    await api.pullRequestSource('u')
    expect(call().body).toEqual({ url: 'u', refresh: false })
    await api.fetchIssueSource('u', true)
    expect(call(1).body).toEqual({ url: 'u', refresh: true })
    await api.resolvePullRequestThread('u', 't1')
    expect(call(2).body).toEqual({ url: 'u', threadId: 't1' })
    await api.unresolvePullRequestThread('u', 't1')
    expect(call(3).url).toBe('/api/source/pull-request/unresolve')
    await api.submitPullRequestReview('u', 'r1', 'APPROVE', 'digest')
    expect(call(4).body).toEqual({ url: 'u', reviewId: 'r1', event: 'APPROVE', contentDigest: 'digest' })
  })

  it('resumeChatSlot repeats the key as name and falls back to it for the title', async () => {
    await api.resumeChatSlot('k1')
    expect(call().body).toEqual({ name: 'k1', key: 'k1', title: 'k1' })
    await api.resumeChatSlot('k1', 'Real title')
    expect(call(1).body).toEqual({ name: 'k1', key: 'k1', title: 'Real title' })
  })

  it('approveChatSlot merges caller-supplied extras into the action body', async () => {
    await api.approveChatSlot('chat-1', 'allow', { tool: 'fs_write' })
    expect(call().body).toEqual({ action: 'allow', tool: 'fs_write' })
  })
})

/* ─────────── theme-consent wire token (two-tier consent) ─────────── */

describe('sendChat theme consent', () => {
  const slug = 'neon'

  beforeEach(() => { localStorage.clear() })
  afterEach(() => { localStorage.clear() })

  it('sends no theme fields at all when no colour theme is active', async () => {
    await api.sendChat('hi', 'chat-1')
    expect(call().url).toBe('/api/chat?ws=1')
    expect(call().body).toEqual({ message: 'hi', slot: 'chat-1' })
  })

  it('sends the theme but no consent token for a built-in theme', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, 'sha-abc')
    await api.sendChat('hi', 'chat-1', 'ocean')
    expect(call().body).toMatchObject({ color_theme: 'ocean' })
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('transmits the RAW stored grant for an installed pack', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, 'sha-abc')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).toMatchObject({ color_theme: 'custom-neon', theme_consent_sha: 'sha-abc' })
    // The legacy boolean is deliberately not sent — gating is content-bound server-side.
    expect(call().body).not.toHaveProperty('theme_consent')
  })

  it('omits the token when the pack has no stored grant', async () => {
    await api.sendChat('hi', 'chat-1', 'custom-unknown')
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('treats a legacy "1" grant as no grant so the user re-prompts exactly once', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, '1')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('treats an empty stored token as no grant', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, '')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('carries meta, the steer intent and an abort signal', async () => {
    const ctl = new AbortController()
    await api.sendChat('hi', 'chat-1', undefined, ctl.signal, { attachments: ['a.png'] }, true)
    expect(call().body).toEqual({
      message: 'hi', slot: 'chat-1', meta: { attachments: ['a.png'] }, steer: true,
    })
    expect(call().init?.signal).toBe(ctl.signal)
  })

  it('steerChat always injects into the running turn', async () => {
    await api.steerChat('now', 'chat-1')
    expect(call().url).toBe('/api/chat')
    expect(call().body).toEqual({ message: 'now', slot: 'chat-1', steer: true })
  })
})

/* ─────────────── 3. the non-trivial method implementations ─────────────── */

describe('revealPath', () => {
  it('copies the path when the host is headless and cannot reveal it', async () => {
    fetchMock.mockResolvedValue(okJson({ copy: '/home/u/report.zip' }))
    await api.revealPath('/home/u/report.zip')
    expect(call().body).toEqual({ path: '/home/u/report.zip', action: 'reveal' })
    expect(vi.mocked(copyToClipboard)).toHaveBeenCalledWith('/home/u/report.zip')
  })

  it('does not touch the clipboard when the OS handled it', async () => {
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    const r = await api.revealPath('/f', 'open')
    expect(call().body).toEqual({ path: '/f', action: 'open' })
    expect(vi.mocked(copyToClipboard)).not.toHaveBeenCalled()
    expect(r).toMatchObject({ ok: true })
  })
})

describe('sttTranscribe', () => {
  // happy-dom's FormData does not retain the third (filename) argument — the
  // stored entry is always named `blob` — so the filename the client CHOSE is
  // read off the append call rather than out of the FormData.
  function appendSpy() {
    const calls: unknown[][] = []
    const spy = vi.spyOn(FormData.prototype, 'append').mockImplementation(function (
      this: FormData,
      ...args: unknown[]
    ) {
      calls.push(args)
    })
    return { calls, spy }
  }

  it('uploads the recording as multipart under the field the handler reads', async () => {
    const { calls, spy } = appendSpy()
    const blob = new Blob(['audio'], { type: 'audio/webm' })
    try {
      await api.sttTranscribe(blob, 'wav')
    } finally { spy.mockRestore() }
    const { url, init } = call()
    expect(url).toBe('/api/stt/transcribe')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeInstanceOf(FormData)
    expect(calls).toEqual([['audio', blob, 'recording.wav']])
  })

  it('defaults the container to webm', async () => {
    const { calls, spy } = appendSpy()
    try {
      await api.sttTranscribe(new Blob(['a']))
    } finally { spy.mockRestore() }
    expect(calls[0][2]).toBe('recording.webm')
  })
})

describe('uploadFiles', () => {
  const png = (name: string) => new File(['x'], name, { type: 'image/png' })

  it('keys resize details by the exact server path the attachment chip renders from', async () => {
    const info = { name: 'big.png', fromW: 4000, fromH: 3000, toW: 1000, toH: 750, fromBytes: 90, toBytes: 9 }
    vi.mocked(resizeImageForModel)
      .mockImplementationOnce(async (f: File) => ({ file: f, info }))
      .mockImplementationOnce(async (f: File) => ({ file: f, info: null }))
    fetchMock.mockResolvedValue(okJson({ paths: ['/up/big.png', '/up/small.png'] }))

    const out = await api.uploadFiles([png('big.png'), png('small.png')])
    expect(out.paths).toEqual(['/up/big.png', '/up/small.png'])
    expect(out.resized).toEqual([info])
    // paths[i] is prepared[i]'s stored location, so the zip must key by path.
    expect(out.resizedByPath).toEqual({ '/up/big.png': info })
    expect(out.error).toBeUndefined()
    expect(call().init?.body).toBeInstanceOf(FormData)
  })

  it('reports the server error and no paths on a failed upload', async () => {
    fetchMock.mockResolvedValue(res(413, { error: 'too large' }))
    const out = await api.uploadFiles([png('a.png')])
    expect(out).toMatchObject({ paths: [], error: 'too large' })
    expect(out.resizedByPath).toEqual({})
  })

  it('falls back to the status text when the failure body is unreadable', async () => {
    fetchMock.mockResolvedValue({
      ok: false, status: 500, statusText: 'Internal Server Error',
      headers: { get: () => null },
      json: async () => { throw new Error('not json') },
    } as unknown as Response)
    const out = await api.uploadFiles([png('a.png')])
    expect(out).toMatchObject({ paths: [], error: 'Internal Server Error' })
  })

  it('refuses to trust a 200 whose paths field is not an array', async () => {
    fetchMock.mockResolvedValue(okJson({ paths: 'oops' }))
    const out = await api.uploadFiles([png('a.png')])
    expect(out.paths).toEqual([])
    expect(out.error).toBeTruthy()
  })
})

describe('exportPlanYaml', () => {
  let clicks: { download: string; href: string }[]
  let createObjectURL: typeof URL.createObjectURL
  let revokeObjectURL: typeof URL.revokeObjectURL
  let clickSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    clicks = []
    createObjectURL = URL.createObjectURL
    revokeObjectURL = URL.revokeObjectURL
    URL.createObjectURL = vi.fn(() => 'blob:plan')
    URL.revokeObjectURL = vi.fn()
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push({ download: this.download, href: this.getAttribute('href') ?? '' })
    })
  })
  afterEach(() => {
    clickSpy.mockRestore()
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
  })

  it('honours the sanitized filename the server sent', async () => {
    fetchMock.mockResolvedValue(
      res(200, 'steps: []', { headers: { 'Content-Disposition': 'attachment; filename="my plan.yaml"' } }),
    )
    await api.exportPlanYaml('task 1')
    expect(call().url).toBe('/api/taskrunner/task%201/plan.yaml')
    expect(clicks).toEqual([{ download: 'my plan.yaml', href: 'blob:plan' }])
    // The object URL is released, and the anchor is not left in the document.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:plan')
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('falls back to <taskId>.yaml when no Content-Disposition arrives', async () => {
    fetchMock.mockResolvedValue(res(200, 'steps: []'))
    await api.exportPlanYaml('t1')
    expect(clicks[0].download).toBe('t1.yaml')
  })

  it('throws an ApiError instead of downloading an error page', async () => {
    fetchMock.mockResolvedValue(res(404, 'no such run'))
    await expect(api.exportPlanYaml('t1')).rejects.toBeInstanceOf(ApiError)
    expect(clicks).toEqual([])
  })

  it('reports the bare status when the failure body is empty', async () => {
    fetchMock.mockResolvedValue(res(500, '', { text: '' }))
    await expect(api.exportPlanYaml('t1')).rejects.toThrow('HTTP 500')
  })
})

describe('installFromRegistryStream', () => {
  /** A ReadableStream-shaped stub that hands back the given chunks in order. */
  function sseBody(chunks: string[]) {
    const enc = new TextEncoder()
    let i = 0
    const releaseLock = vi.fn()
    return {
      body: {
        getReader: () => ({
          read: async () =>
            i < chunks.length
              ? { done: false, value: enc.encode(chunks[i++]) }
              : { done: true, value: undefined },
          releaseLock,
        }),
      },
      releaseLock,
    }
  }

  function streamRes(chunks: string[]) {
    const { body, releaseLock } = sseBody(chunks)
    return { response: res(200, '') as unknown as Record<string, unknown>, body, releaseLock }
  }

  it('streams every log line and resolves with the done payload', async () => {
    const { response, body, releaseLock } = streamRes([
      'event: log\ndata: cloning\n\nevent: log\ndata: build',
      'ing\n\nevent: done\ndata: {"ok":true}\n\n',
    ])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    const out = await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual(['cloning', 'building'])
    expect(out).toEqual({ ok: true })
    expect(call().body).toEqual({ name: 'demo' })
    expect(call().headers['Content-Type']).toBe('application/json')
    expect(releaseLock).toHaveBeenCalled()
  })

  it('joins a multi-line data payload and tolerates a bare `data:` line', async () => {
    const { response, body } = streamRes(['event: log\ndata: one\ndata:\ndata: three\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual(['one\n\nthree'])
  })

  it('carries the refusal code so the consent modal can open on a resolved refusal', async () => {
    // The execution gate refuses BEFORE cloning and RESOLVES (SSE done) rather
    // than rejecting, so the code has to travel on the result.
    const { response, body } = streamRes([
      'event: done\ndata: {"ok":false,"code":"app_execution_denied"}\n\n',
    ])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const out = await api.installFromRegistryStream('demo', () => {})
    expect(out).toEqual({ ok: false, code: 'app_execution_denied' })
  })

  it('degrades a malformed done payload into a failure carrying the raw text', async () => {
    const { response, body } = streamRes(['event: done\ndata: not-json\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    await expect(api.installFromRegistryStream('demo', () => {})).resolves.toEqual({
      ok: false, error: 'not-json',
    })
  })

  it('fails when the stream ends without a done frame', async () => {
    const { response, body } = streamRes(['event: log\ndata: half\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const out = await api.installFromRegistryStream('demo', () => {})
    expect(out.ok).toBe(false)
    expect(out.error).toBeTruthy()
  })

  it('skips blank frames rather than emitting empty log lines', async () => {
    const { response, body } = streamRes(['\n\n   \n\nevent: done\ndata: {"ok":true}\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    const logs: string[] = []
    await api.installFromRegistryStream('demo', (l) => logs.push(l))
    expect(logs).toEqual([])
  })

  it('throws before reading when the request itself is rejected', async () => {
    fetchMock.mockResolvedValue(res(500, 'gateway said no'))
    await expect(api.installFromRegistryStream('demo', () => {})).rejects.toThrow('gateway said no')
  })

  it('forwards the abort signal', async () => {
    const ctl = new AbortController()
    const { response, body } = streamRes(['event: done\ndata: {"ok":true}\n\n'])
    fetchMock.mockResolvedValue({ ...response, body } as unknown as Response)
    await api.installFromRegistryStream('demo', () => {}, ctl.signal)
    expect(call().init?.signal).toBe(ctl.signal)
  })
})

describe('publishToProvider', () => {
  it('routes to the provider-declared endpoint with the deploy payload shape', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws', {
      id: 'aws', label: 'AWS', icon: '', kinds: ['webapp'],
      configured: true, setupRoute: '/s', endpoint: '/api/deploy/custom',
    })
    expect(call().url).toBe('/api/deploy/custom')
    expect(call().body).toEqual({ site_id: 'my-site', artifact_slug: 'my-site', provider_id: 'aws' })
  })

  it('defaults the endpoint when the provider descriptor is unavailable', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws')
    expect(call().url).toBe('/api/deploy/deploy')
  })

  it('sends ttl_hours so the previewed TTL matches what is deployed', async () => {
    fetchMock.mockResolvedValue(okJson({ url: 'https://x' }))
    await api.publishToProvider('my-site', 'aws', undefined, 24)
    expect(call().body).toMatchObject({ ttl_hours: 24 })
  })

  it('returns a 409 scan-block body instead of throwing, so the findings can render', async () => {
    fetchMock.mockResolvedValue(res(409, { findings: [{ rule: 'secret' }] }))
    await expect(api.publishToProvider('s', 'aws')).resolves.toEqual({ findings: [{ rule: 'secret' }] })
  })

  it('throws an ApiError on any other failure', async () => {
    fetchMock.mockResolvedValue(res(500, 'deploy exploded'))
    await expect(api.publishToProvider('s', 'aws')).rejects.toBeInstanceOf(ApiError)
  })

  it('reports the bare status when a failure carries no body', async () => {
    fetchMock.mockResolvedValue(res(502, '', { text: '' }))
    await expect(api.publishToProvider('s', 'aws')).rejects.toThrow('HTTP 502')
  })
})

/* ─────────────────── 4. whole-surface request invariant ─────────────────── */

describe('every api method issues one well-formed /api request', () => {
  // Exercised individually above with the fixtures they need (a Blob, a
  // ReadableStream, a File list, an object-URL download).
  const HAND_TESTED = new Set(['sttTranscribe', 'uploadFiles', 'installFromRegistryStream', 'exportPlanYaml'])

  type AnyFn = (...args: unknown[]) => unknown
  const methods = Object.entries(api as unknown as Record<string, AnyFn>)
    .filter(([name, fn]) => typeof fn === 'function' && !HAND_TESTED.has(name))

  it('covers the whole surface (guards against the table silently shrinking)', () => {
    expect(methods.length).toBeGreaterThan(300)
  })

  it.each(methods.map(([name]) => name))('%s', async (name) => {
    const fn = (api as unknown as Record<string, AnyFn>)[name]
    // Generic positional arguments. Every method is a thin URL/body builder, so
    // what this asserts is the construction itself: a forgotten argument or a
    // botched template literal shows up as `undefined` inside the path.
    await Promise.resolve(fn('sw-1', 'sw-2', 'sw-3', 'sw-4'))

    expect(fetchMock, `${name} issued no request`).toHaveBeenCalledTimes(1)
    const { url, init } = call()
    expect(typeof url, `${name} did not pass a string URL`).toBe('string')
    expect(url.startsWith('/api/'), `${name} escaped the /api prefix: ${url}`).toBe(true)
    for (const junk of ['undefined', '[object Object]', 'NaN', '/null']) {
      expect(url.includes(junk), `${name} leaked ${junk} into ${url}`).toBe(false)
    }
    // A body is only ever sent with a method that can carry one.
    if (init?.body !== undefined) {
      expect(['POST', 'PUT', 'PATCH', 'DELETE']).toContain(init.method)
    }
  })
})
