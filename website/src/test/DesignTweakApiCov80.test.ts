// Coverage-focused tests for design-tweak/api.ts and design-tweak/prompts.ts.
// Exercises exported fetch wrappers and URL builders directly (no React rendering).
// Network is fully stubbed — no real requests leave this process.

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  addProject,
  api,
  chatRoute,
  clearRequest,
  createChatSlot,
  deleteComment,
  deleteRequest,
  detectDevServer,
  fetchHealth,
  fetchHistory,
  fetchProjects,
  fetchQueue,
  loopbackPreviewSrc,
  markDelivered,
  pickFolder,
  removeProject,
  requestPayloadPath,
  selectProject,
  sendChatMessage,
  sendRequest,
  setPreviewUrl,
  startDevServer,
  stopDevServer,
  submitComment,
  threadEndpoint,
} from '../apps/design-tweak/api'
import { REQUEST_PROMPT, SESSION_SEED, SESSION_TITLE } from '../apps/design-tweak/prompts'

// ── Helpers ──────────────────────────────────────────────────────────────────

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function emptyOkResponse(): Response {
  return new Response('', { status: 200 })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── Pure URL helpers ─────────────────────────────────────────────────────────

describe('loopbackPreviewSrc', () => {
  it('appends nonce as first query param when URL has none', () => {
    const result = loopbackPreviewSrc('http://localhost:3456', 42)
    expect(result).toBe('http://localhost:3456?_t=42')
  })

  it('appends nonce with & when URL already has a query string', () => {
    const result = loopbackPreviewSrc('http://localhost:3456?hot=1', 99)
    expect(result).toBe('http://localhost:3456?hot=1&_t=99')
  })
})

describe('requestPayloadPath', () => {
  it('builds the canonical queue file path from dataDir and requestId', () => {
    const p = requestPayloadPath('/home/user/.kiro/crew/apps/design-tweak', 'req-abc')
    expect(p).toBe('/home/user/.kiro/crew/apps/design-tweak/queue/req-abc.json')
  })

  it('strips trailing slashes from dataDir', () => {
    const p = requestPayloadPath('/data/', 'r1')
    expect(p).toBe('/data/queue/r1.json')
  })

  it('strips trailing backslashes from dataDir', () => {
    const p = requestPayloadPath('C:\\data\\', 'r2')
    expect(p).toBe('C:\\data/queue/r2.json')
  })

  it('returns empty string when dataDir is empty', () => {
    expect(requestPayloadPath('', 'r3')).toBe('')
  })
})

describe('threadEndpoint', () => {
  it('builds the per-comment progress URL with both id and cid params', () => {
    const url = threadEndpoint('req-5', '<cid>')
    expect(url).toBe('/apps/design-tweak/api/thread?id=req-5&cid=%3Ccid%3E')
  })

  it('URL-encodes special characters in the request id', () => {
    const url = threadEndpoint('a&b', 'c#d')
    expect(url).toContain('id=a%26b')
    expect(url).toContain('cid=c%23d')
  })
})

describe('chatRoute', () => {
  it('returns /chat with sid param when slotKey is provided', () => {
    expect(chatRoute('dt-abc')).toBe('/chat?sid=dt-abc')
  })

  it('returns plain /chat when no slotKey', () => {
    expect(chatRoute()).toBe('/chat')
    expect(chatRoute(undefined)).toBe('/chat')
  })

  it('URL-encodes a slot key containing special characters', () => {
    expect(chatRoute('a/b')).toBe('/chat?sid=a%2Fb')
  })
})

// ── GET wrappers ─────────────────────────────────────────────────────────────

describe('fetchProjects', () => {
  it('GETs the projects endpoint and returns parsed JSON', async () => {
    const payload = { projects: [{ id: 'p1', path: '/proj' }], activeId: 'p1' }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchProjects()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/projects')
    expect(init.credentials).toBe('same-origin')
    expect(result).toEqual(payload)
  })

  it('throws on non-OK response with the response body as message', async () => {
    const fetchMock = vi.fn(async () => new Response('forbidden', { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(fetchProjects()).rejects.toThrow('forbidden')
  })

  it('throws with HTTP status when body is empty on error', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 500 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(fetchProjects()).rejects.toThrow('HTTP 500')
  })
})

describe('fetchHealth', () => {
  it('GETs /health and returns the parsed response', async () => {
    const payload = { status: 'ok', dataDir: '/home/.kiro/crew/apps/design-tweak' }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchHealth()
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/health')
    expect(result).toEqual(payload)
  })
})

describe('fetchQueue', () => {
  it('GETs /queue and returns parsed response', async () => {
    const payload = { pending: [{ id: 'r1', number: 1 }] }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchQueue()
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/queue')
    expect(result).toEqual(payload)
  })
})

describe('fetchHistory', () => {
  it('GETs /history and returns parsed response', async () => {
    const payload = { history: [{ id: 'r2', number: 2, status: 'done' }] }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchHistory()
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/history')
    expect(result).toEqual(payload)
  })
})

describe('detectDevServer', () => {
  it('GETs /detect-dev-server with the project id as query param', async () => {
    const payload = { suggested: 'http://localhost:5173', candidates: [] }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await detectDevServer('proj-1')
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/detect-dev-server?id=proj-1')
    expect(result).toEqual(payload)
  })
})

// ── POST wrappers ────────────────────────────────────────────────────────────

describe('selectProject', () => {
  it('POSTs with the project id in JSON body', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await selectProject('my-proj')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/projects/select')
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(String(init.body))).toEqual({ id: 'my-proj' })
  })
})

describe('removeProject', () => {
  it('POSTs to /projects/remove with the project id', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await removeProject('p-del')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/projects/remove')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ id: 'p-del' })
  })
})

describe('addProject', () => {
  it('POSTs the path to /projects', async () => {
    const payload = { ok: true, project: { id: 'new', path: '/new' } }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await addProject('/new')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/projects')
    expect(JSON.parse(String(init.body))).toEqual({ path: '/new' })
    expect(result).toEqual(payload)
  })
})

describe('pickFolder', () => {
  it('POSTs an empty body to /pick-folder', async () => {
    const payload = { ok: true, path: '/chosen' }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await pickFolder()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/pick-folder')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({})
    expect(result).toEqual(payload)
  })
})

describe('submitComment', () => {
  it('POSTs arbitrary payload to /submit', async () => {
    const body = { comment: 'make it red', element: 'h1' }
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true, cid: 'c-1' }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await submitComment(body)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/submit')
    expect(JSON.parse(String(init.body))).toEqual(body)
    expect(result.cid).toBe('c-1')
  })
})

describe('sendRequest', () => {
  it('POSTs to /send with the request id as query param', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await sendRequest('req-7')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/send?id=req-7')
    expect(init.method).toBe('POST')
  })
})

describe('markDelivered', () => {
  it('POSTs to /delivered with the request id as query param', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await markDelivered('req-7')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/delivered?id=req-7')
    expect(init.method).toBe('POST')
  })
})

describe('clearRequest', () => {
  it('POSTs to /clear with the request id as query param', async () => {
    const fetchMock = vi.fn(async () => emptyOkResponse())
    vi.stubGlobal('fetch', fetchMock)

    const result = await clearRequest('req-8')
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/clear?id=req-8')
    // Empty body 200 parses as null — callers treat null as success.
    expect(result).toBeNull()
  })
})

describe('deleteRequest', () => {
  it('POSTs to /delete with the request id as query param', async () => {
    const fetchMock = vi.fn(async () => emptyOkResponse())
    vi.stubGlobal('fetch', fetchMock)

    const result = await deleteRequest('req-9')
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/delete?id=req-9')
    expect(result).toBeNull()
  })
})

describe('deleteComment', () => {
  it('POSTs to /delete-comment with both id and cid as query params', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)

    await deleteComment('req-10', 'c-2')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/delete-comment?id=req-10&cid=c-2')
    expect(init.method).toBe('POST')
  })
})

describe('setPreviewUrl', () => {
  it('POSTs both id and previewUrl in the JSON body', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)

    await setPreviewUrl('proj-1', 'http://localhost:3000')
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      id: 'proj-1',
      previewUrl: 'http://localhost:3000',
    })
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/projects/preview-url')
  })
})

describe('startDevServer', () => {
  it('POSTs to /dev-server/start with the project id as query param', async () => {
    const payload = { ok: true, devUrl: 'http://localhost:5173' }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await startDevServer('proj-1')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/apps/design-tweak/api/dev-server/start?id=proj-1')
    expect(init.method).toBe('POST')
    expect(result).toEqual(payload)
  })
})

describe('stopDevServer', () => {
  it('POSTs to /dev-server/stop with the project id as query param', async () => {
    // stop answers 200 with no JSON body
    const fetchMock = vi.fn(async () => emptyOkResponse())
    vi.stubGlobal('fetch', fetchMock)

    const result = await stopDevServer('proj-1')
    expect(fetchMock.mock.calls[0][0]).toBe('/apps/design-tweak/api/dev-server/stop?id=proj-1')
    expect(result).toBeNull()
  })
})

// ── Chat slot wrappers ───────────────────────────────────────────────────────

describe('createChatSlot', () => {
  it('POSTs to /api/chat/slots with name, agent, and title', async () => {
    const payload = { key: 'dt-slot', messages: 0 }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await createChatSlot('dt-slot', 'Design Tweak — My App')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/slots')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      name: 'dt-slot',
      agent: '',
      title: 'Design Tweak — My App',
    })
    expect(result).toEqual(payload)
  })

  it('throws on non-OK with "chat API <status>"', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(createChatSlot('x', 'y')).rejects.toThrow('chat API 401')
  })
})

describe('sendChatMessage', () => {
  it('POSTs to /api/chat?ws=1 with message, slot, and agent', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage('apply edits', 'dt-slot')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat?ws=1')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      message: 'apply edits',
      slot: 'dt-slot',
      agent: '',
    })
  })

  it('tolerates non-JSON response bodies from SSE fallback', async () => {
    // Without ?ws=1 the host returns SSE — the chatApi helper must not throw.
    const fetchMock = vi.fn(async () => new Response('data: {"ok":true}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await sendChatMessage('test', 'slot')
    // Falls through JSON.parse catch to a fallback object.
    expect(result).toHaveProperty('ok', true)
    expect(result).toHaveProperty('raw', 'data: {"ok":true}')
  })
})

// ── api.get / api.post exported helpers ──────────────────────────────────────

describe('api.get and api.post', () => {
  it('api.get sets credentials: same-origin', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ x: 1 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.get<{ x: number }>('/test')
    expect(fetchMock.mock.calls[0][1]).toHaveProperty('credentials', 'same-origin')
    expect(result).toEqual({ x: 1 })
  })

  it('api.post sends POST with JSON body when body is provided', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ y: 2 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.post<{ y: number }>('/test', { a: 1 })
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(String(init.body))).toEqual({ a: 1 })
    expect(result).toEqual({ y: 2 })
  })

  it('api.post omits Content-Type and body when no body argument', async () => {
    const fetchMock = vi.fn(async () => emptyOkResponse())
    vi.stubGlobal('fetch', fetchMock)

    await api.post('/empty')
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.headers).toBeUndefined()
    expect(init.body).toBeUndefined()
  })

  it('api.get throws with body text on error', async () => {
    const fetchMock = vi.fn(async () => new Response('not found', { status: 404 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.get('/x')).rejects.toThrow('not found')
  })

  it('api.get throws with HTTP status when body read fails', async () => {
    // Simulate a response whose text() rejects.
    const r = new Response(null, { status: 502 })
    Object.defineProperty(r, 'ok', { value: false })
    Object.defineProperty(r, 'text', {
      value: () => Promise.reject(new Error('stream error')),
    })
    const fetchMock = vi.fn(async () => r)
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.get('/broken')).rejects.toThrow('HTTP 502')
  })
})

// ── prompts.ts ───────────────────────────────────────────────────────────────

describe('SESSION_TITLE', () => {
  it('includes the project label after an em-dash', () => {
    const title = SESSION_TITLE('My App')
    expect(title).toContain('Design Tweak')
    expect(title).toContain('My App')
    expect(title).toContain('\u2014')
  })
})

describe('SESSION_SEED', () => {
  it('mentions the project label and absolute working directory', () => {
    const seed = SESSION_SEED('App X', '/home/user/proj')
    expect(seed).toContain('App X')
    expect(seed).toContain('/home/user/proj')
    // The agent must know it handles visual edit requests.
    expect(seed).toMatch(/visual edit/i)
  })

  it('instructs the agent to post concise summaries', () => {
    const seed = SESSION_SEED('x', '/p')
    expect(seed).toMatch(/concise/i)
  })
})

describe('REQUEST_PROMPT', () => {
  const req = { id: 'req-42', number: 7 } as import('../apps/design-tweak/types').Request
  const comments = [
    { cid: 'c-1', index: 1, status: 'pending', comment: 'make header blue', element: 'h1', sourceFile: 'src/App.tsx' },
    { cid: 'c-2', index: 2, status: 'pending', comment: 'fix padding', count: 3, sourceFile: 'src/Layout.tsx', followUpTo: 'c-1' },
  ] as import('../apps/design-tweak/types').Comment[]

  it('includes the request number so the agent knows which batch', () => {
    const prompt = REQUEST_PROMPT(req, comments, '/data/queue/req-42.json')
    expect(prompt).toContain('#7')
    expect(prompt).toContain('req-42')
  })

  it('lists each comment with its cid for per-comment progress', () => {
    const prompt = REQUEST_PROMPT(req, comments, '/data/queue/req-42.json')
    expect(prompt).toContain('[cid c-1]')
    expect(prompt).toContain('[cid c-2]')
  })

  it('includes the absolute payload path when provided', () => {
    const prompt = REQUEST_PROMPT(req, comments, '/data/queue/req-42.json')
    expect(prompt).toContain('/data/queue/req-42.json')
  })

  it('omits the payload sentence when payloadPath is empty', () => {
    const prompt = REQUEST_PROMPT(req, comments, '')
    expect(prompt).not.toContain('full payload')
  })

  it('quotes the thread endpoint template for agent progress POSTs', () => {
    const prompt = REQUEST_PROMPT(req, comments, '')
    // Must contain the actual endpoint the agent should POST to.
    expect(prompt).toContain('/apps/design-tweak/api/thread?id=req-42&cid=%3Ccid%3E')
  })

  it('marks follow-up comments so the agent reads prior context', () => {
    const prompt = REQUEST_PROMPT(req, comments, '')
    expect(prompt).toContain('follow-up to comment c-1')
  })

  it('shows element count for multi-element selections', () => {
    const prompt = REQUEST_PROMPT(req, comments, '')
    expect(prompt).toContain('3 elements')
  })

  it('instructs per-comment reporting, not once for the batch', () => {
    const prompt = REQUEST_PROMPT(req, comments, '')
    expect(prompt).toMatch(/per comment/i)
    expect(prompt).toContain('"status":"done"')
  })
})
