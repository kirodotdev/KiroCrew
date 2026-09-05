import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Tests the API client at the fetch boundary (fetch mocked, client real), so the
// request/response translation is pinned: the page tests mock this client, and a
// silent regression here — a dropped body, a lost 403 status — would otherwise
// only surface as "the page stopped working" in a live gateway.
const { chatStatusTagsApi, ChatStatusTagsApiError } = await import(
  '../apps/chat-status-tags/api'
)

const CRON = { present: true, enabled: true, schedule: 'every hour' }
const PROMPT = { prompt: 'p', isDefault: false, defaultPrompt: 'd', cron: CRON }
const SETTINGS = { reconcilerEnabled: true, autoResumeEnabled: false }

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

function brokenJsonResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => {
      throw new Error('not json')
    },
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('chatStatusTagsApi.reconcilePrompt', () => {
  it('reads the prompt same-origin', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, PROMPT))
    await expect(chatStatusTagsApi.reconcilePrompt()).resolves.toEqual(PROMPT)
    expect(fetchMock).toHaveBeenCalledWith('/api/apps/chat-status-tags/reconcile-prompt', {
      credentials: 'same-origin',
    })
  })

  it('surfaces the backend error message and status', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(403, { error: 'app disabled' }))
    const err = await chatStatusTagsApi.reconcilePrompt().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ChatStatusTagsApiError)
    expect((err as InstanceType<typeof ChatStatusTagsApiError>).status).toBe(403)
    expect((err as Error).message).toBe('app disabled')
  })

  it('falls back to HTTP <status> when the error body is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(brokenJsonResponse(500))
    const err = await chatStatusTagsApi.reconcilePrompt().catch((e: unknown) => e)
    expect((err as Error).message).toBe('HTTP 500')
  })
})

describe('chatStatusTagsApi.setReconcilePrompt', () => {
  it('PUTs the prompt as a JSON body', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, PROMPT))
    await expect(chatStatusTagsApi.setReconcilePrompt('new text')).resolves.toEqual(PROMPT)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/apps/chat-status-tags/reconcile-prompt')
    expect(init.method).toBe('PUT')
    expect(init.credentials).toBe('same-origin')
    expect(JSON.parse(init.body as string)).toEqual({ prompt: 'new text' })
  })

  it('throws the parsed error on failure', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(400, { error: 'prompt too long' }))
    await expect(chatStatusTagsApi.setReconcilePrompt('x')).rejects.toThrow('prompt too long')
  })
})

describe('chatStatusTagsApi.repairCron', () => {
  it('POSTs and returns the fresh cron state', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true, cron: CRON }))
    await expect(chatStatusTagsApi.repairCron()).resolves.toEqual({ ok: true, cron: CRON })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/apps/chat-status-tags/reconcile-cron/repair')
    expect(init.method).toBe('POST')
  })

  it('surfaces a 503 (scheduler unavailable) as an error with that status', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { error: 'scheduler unavailable' }))
    const err = await chatStatusTagsApi.repairCron().catch((e: unknown) => e)
    expect((err as InstanceType<typeof ChatStatusTagsApiError>).status).toBe(503)
  })
})

describe('chatStatusTagsApi settings', () => {
  it('fetchSettings reads same-origin', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, SETTINGS))
    await expect(chatStatusTagsApi.fetchSettings()).resolves.toEqual(SETTINGS)
    expect(fetchMock).toHaveBeenCalledWith('/api/apps/chat-status-tags/settings', {
      credentials: 'same-origin',
    })
  })

  it('updateSettings PUTs a PARTIAL patch and returns the full state', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, SETTINGS))
    await expect(
      chatStatusTagsApi.updateSettings({ autoResumeEnabled: false }),
    ).resolves.toEqual(SETTINGS)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/apps/chat-status-tags/settings')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body as string)).toEqual({ autoResumeEnabled: false })
  })

  it('updateSettings throws the parsed error on failure', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(500, { error: 'store write failed' }))
    await expect(chatStatusTagsApi.updateSettings({})).rejects.toThrow('store write failed')
  })
})
