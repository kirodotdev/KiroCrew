// 429 edge-throttle UX: retry ladder + friendly error mapping.
//
// When the dashboard is served through Builder Tunnels (API Gateway), request
// bursts (e.g. opening Settings→Usage) can trip the edge throttle, which
// returns 429 {"message":"Rate exceeded","throttlingReasons":null} before the
// request reaches the gateway. These tests pin:
//   1. isThrottleError / retryPolicy / retryDelayPolicy (api/queryClient.ts)
//   2. the friendly 429 message thrown by the `j` helper (api/client.ts)
import { describe, it, expect, vi, afterEach } from 'vitest'
import { isThrottleError, retryPolicy, retryDelayPolicy } from '../api/queryClient'
import { api, ApiError } from '../api/client'
import { apiTransport } from '../api/apiTransport'
import { FakeXhr, nextXhr, stubXhr } from './fakeXhr'
import { isRetryableWrite } from '../api/edgeThrottleRetry'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('isThrottleError', () => {
  it('is true for ApiError with status 429', () => {
    expect(isThrottleError(new ApiError(429, 'Rate exceeded'))).toBe(true)
  })
  it('is false for other statuses and non-API errors', () => {
    expect(isThrottleError(new ApiError(500, 'boom'))).toBe(false)
    expect(isThrottleError(new Error('boom'))).toBe(false)
    expect(isThrottleError(null)).toBe(false)
    expect(isThrottleError(undefined)).toBe(false)
    expect(isThrottleError('Rate exceeded')).toBe(false)
  })
})

describe('retryPolicy', () => {
  it('retries throttles up to 4 times', () => {
    const err = new ApiError(429, 'Rate exceeded')
    expect(retryPolicy(0, err)).toBe(true)
    expect(retryPolicy(3, err)).toBe(true)
    expect(retryPolicy(4, err)).toBe(false)
  })
  it('keeps the single retry for non-throttle errors', () => {
    const err = new ApiError(500, 'boom')
    expect(retryPolicy(0, err)).toBe(true)
    expect(retryPolicy(1, err)).toBe(false)
  })
})

describe('retryDelayPolicy', () => {
  it('uses jittered exponential backoff for throttles, capped at 15s + jitter', () => {
    const err = new ApiError(429, 'Rate exceeded')
    const d0 = retryDelayPolicy(0, err)
    expect(d0).toBeGreaterThanOrEqual(1_000)
    expect(d0).toBeLessThanOrEqual(1_500)
    const d4 = retryDelayPolicy(4, err)
    expect(d4).toBeGreaterThanOrEqual(15_000)
    expect(d4).toBeLessThanOrEqual(15_500)
  })
  it('uses the default curve for non-throttle errors (no jitter)', () => {
    const err = new ApiError(500, 'boom')
    expect(retryDelayPolicy(0, err)).toBe(1_000)
    expect(retryDelayPolicy(2, err)).toBe(4_000)
  })
})

describe('j helper 429 mapping', () => {
  it('replaces the raw API Gateway throttle body with a human-readable message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{"message":"Rate exceeded","throttlingReasons":null}',
      { status: 429 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(429)
    expect((err as ApiError).message).toContain('Rate limited by the tunnel edge')
    expect((err as ApiError).message).not.toContain('throttlingReasons')
  })

  it('unwraps a non-429 JSON error body to the human message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{"error": "boom"}',
      { status: 500 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
    expect((err as ApiError).message).toBe('boom')
  })

  it('preserves a non-JSON raw body for non-429 errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      'plain boom',
      { status: 500 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
    expect((err as ApiError).message).toBe('plain boom')
  })

  it('says a proxy\'s HTML 502 page is temporary, not a bare status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<!DOCTYPE html><html><body>502 Bad Gateway</body></html>',
      { status: 502 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toBe('The server is temporarily unavailable (HTTP 502)')
  })
})

// A write is a one-shot: the `chat/forkSlot` thunk calls the api method once, so
// the QueryClient ladder (queries only) never covers it. The transport retries a
// write only when the 429 came from the edge.
describe('write-path edge throttle retry', () => {
  const edgeThrottle = () => new Response(
    '{"message":"Rate exceeded","throttlingReasons":null}', { status: 429 },
  )
  const ok = () => new Response('{"ok":true,"key":"chat-1-2"}', { status: 200 })

  it('re-sends an allowlisted POST the edge throttled and returns the eventual success', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(edgeThrottle())
      .mockResolvedValueOnce(edgeThrottle())
      .mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)
    const pending = api.forkChatSlot('chat-1-1', 3)
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toMatchObject({ ok: true, key: 'chat-1-2' })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it.each(['put', 'del', 'patch'] as const)('re-sends a %s the edge throttled', async (verb) => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValueOnce(edgeThrottle()).mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)
    const pending = apiTransport[verb]('/api/example', {})
    await vi.runAllTimersAsync()
    expect((await pending).status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('re-sends a write an endpoint issues on raw transport fetch', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValueOnce(edgeThrottle()).mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)
    const pending = api.agentPatch('kirocrew', { description: 'x' })
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toMatchObject({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PATCH' })
  })

  it('does not re-send a write cancelled during the backoff', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    const pending = api.updateInstance('crew-1', { name: 'x' }, { signal: controller.signal }).catch((e: unknown) => e)
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    controller.abort()
    await vi.runAllTimersAsync()
    expect(await pending).toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('sends a write to the same target only after the pending retry ends', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(edgeThrottle())
      .mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)
    const older = apiTransport.put('/api/example', { quick_send: true })
    await vi.advanceTimersByTimeAsync(0)
    const newer = apiTransport.put('/api/example', { quick_send: false })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.runAllTimersAsync()
    expect((await older).status).toBe(200)
    expect((await newer).status).toBe(200)
    const bodies = fetchMock.mock.calls.map((call) => (call[1] as RequestInit).body)
    expect(bodies).toEqual(['{"quick_send":true}', '{"quick_send":true}', '{"quick_send":false}'])
  })

  it('re-sends every write of a throttled burst to one target', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(edgeThrottle())
      .mockResolvedValueOnce(edgeThrottle())
      .mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)
    const first = apiTransport.del('/api/notifications', { ts: 1 })
    const second = apiTransport.del('/api/notifications', { ts: 2 })
    await vi.runAllTimersAsync()
    expect((await first).status).toBe(200)
    expect((await second).status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(4)
  })

  it('re-sends both of two settings PATCHes to one URL, in the order they were made', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(edgeThrottle())
      .mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)
    const first = api.patchConfig('agent.a', true)
    await vi.advanceTimersByTimeAsync(0)
    const second = api.patchConfig('agent.b', false)
    await vi.runAllTimersAsync()
    await expect(first).resolves.toMatchObject({ ok: true })
    await expect(second).resolves.toMatchObject({ ok: true })
    const paths = fetchMock.mock.calls.map((call) => JSON.parse((call[1] as RequestInit).body as string).path)
    expect(paths).toEqual(['agent.a', 'agent.a', 'agent.b'])
  })

  it('holds a newer write sent before the older one hears its 429', async () => {
    vi.useFakeTimers()
    let answerFirst: (r: Response) => void = () => {}
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { answerFirst = resolve }))
      .mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)
    const older = apiTransport.patch('/api/config/kirocrew', { value: 6 })
    const newer = apiTransport.patch('/api/config/kirocrew', { value: 7 })
    const newest = apiTransport.patch('/api/config/kirocrew', { value: 8 })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    answerFirst(edgeThrottle())
    await vi.runAllTimersAsync()
    await Promise.all([older, newer, newest])
    const values = fetchMock.mock.calls.map((call) => JSON.parse((call[1] as RequestInit).body as string).value)
    expect(values).toEqual([6, 6, 7, 8])
  })

  it('does not retry a throttled side-chat turn', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.sideTurn('chat-1-1', 'why?').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry a throttled chat-turn send', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.sendChat('hello', 'chat-1-1').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry a throttled Regenerate, which would pick a newer reply', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.regenerateSlot('chat-1-1').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry a throttled chat send to a remote crew', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.crewPeerPost('crew-1', 'api/chat?ws=1', { message: 'hi' }).catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry a throttled task plan, which Cancel cannot abort', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.planTask('build it', 'manual').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry a throttled Stop, which would end a newer turn', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.stopChatSlotForce('chat-1-1').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('retries only a write whose second landing equals its first', () => {
    for (const method of ['PUT', 'PATCH', 'DELETE', 'patch']) {
      expect(isRetryableWrite({ method, url: '/api/example' })).toBe(true)
    }
    expect(isRetryableWrite({ method: 'DELETE', url: '/api/chat/slots/chat-1/limit-continue' })).toBe(false)
    expect(isRetryableWrite({ method: 'POST', url: '/api/chat/mode' })).toBe(true)
    expect(isRetryableWrite({ method: 'POST', url: '/api/chat/slots/chat-1/fork', body: '{"at_message_index":3}' })).toBe(true)
    expect(isRetryableWrite({ method: 'POST', url: '/api/chat/slots/chat-1/fork', body: '{"at_message_id":"m-1"}' })).toBe(true)
    expect(isRetryableWrite({ method: 'POST', url: '/api/chat/slots/chat-1/fork', body: '{}' })).toBe(false)
    expect(isRetryableWrite({ method: 'POST', url: '/api/chat/slots/chat-1/fork' })).toBe(false)
    expect(isRetryableWrite({ method: 'POST', url: '/api/instances/crew-1/proxy/api/chat/mode' })).toBe(true)
    for (const url of [
      '/api/chat?ws=1', '/api/chat/slots/chat-1/side/turn', '/api/chat/slots/chat-1/stop',
      '/api/chat/slots/chat-1/regenerate', '/api/taskrunner/plan', '/api/upload/file',
      '/api/members/import', '/api/instances/crew-1/proxy/api/chat?ws=1', '/api/chat/mode/extra',
    ]) {
      expect(isRetryableWrite({ method: 'POST', url })).toBe(false)
    }
    expect(isRetryableWrite({ method: 'GET', url: '/api/example' })).toBe(false)
  })

  it('neither retries nor queues a POST that is not on the allowlist', async () => {
    let answerFirst: (r: Response) => void = () => {}
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { answerFirst = resolve }))
      .mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)
    const first = apiTransport.post('/api/taskrunner/plan', { input: 'a' })
    const second = apiTransport.post('/api/taskrunner/plan', { input: 'b' })
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    answerFirst(edgeThrottle())
    expect((await first).status).toBe(429)
    expect((await second).status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('neither retries nor queues a composer upload', async () => {
    stubXhr()
    const firstUpload = api.uploadFiles([new File(['one'], 'one.txt')]).catch((e: unknown) => e)
    const first = await nextXhr()
    const secondUpload = api.uploadFiles([new File(['two'], 'two.txt')]).catch((e: unknown) => e)
    const second = await nextXhr()
    expect(second.url).toBe(first.url)
    first.respond(429, '{"message":"Rate exceeded","throttlingReasons":null}')
    second.respond(200, { paths: ['/tmp/two.txt'] })
    expect(await firstUpload).toMatchObject({ paths: [] })
    expect(await secondUpload).toMatchObject({ paths: ['/tmp/two.txt'] })
    expect(FakeXhr.sent).toHaveLength(0)
  })

  it('re-sends a fork that names its message', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValueOnce(edgeThrottle()).mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)
    const pending = api.forkChatSlot('chat-1-1', 3)
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toMatchObject({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not retry a fork that names no message, which would copy a newer transcript', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.forkChatSlot('chat-1-1').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry cancelling a scheduled continue, which would cancel a newer one', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.cancelLimitContinue('chat-1-1').catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('re-sends a chat mode set, a POST on the allowlist', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValueOnce(edgeThrottle()).mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)
    const pending = api.chatMode('auto', 'chat-1-1')
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toMatchObject({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('stops after 3 attempts and reports the friendly edge message', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    const pending = api.forkChatSlot('chat-1-1', 3).catch((e: unknown) => e)
    await vi.runAllTimersAsync()
    const err = await pending
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(429)
    expect((err as ApiError).message).toContain('Rate limited by the tunnel edge')
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('does not retry a gateway slot-cap 429 and shows its guidance', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      '{"error":"slot cap reached (500)","code":"slot_cap_reached"}', { status: 429 },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const err = await api.forkChatSlot('chat-1-1', 3).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toContain('Too many sessions are open')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not retry any other gateway 429 and keeps its own reason', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      '{"error":"queue full","code":"queue_full"}', { status: 429 },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const err = await api.forkChatSlot('chat-1-1', 3).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toBe('queue full')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('leaves a GET to the QueryClient ladder instead of stacking retries', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => edgeThrottle())
    vi.stubGlobal('fetch', fetchMock)
    await api.kiroUsage().catch(() => undefined)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
