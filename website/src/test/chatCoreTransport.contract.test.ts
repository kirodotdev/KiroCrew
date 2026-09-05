/**
 * Receipt-contract tests for the chat-core transport layer.
 *
 * These pin the semantics of `POST /api/chat?ws=1` that every hand-rolled send
 * path had to re-learn -- and that were mis-learned at least once per surface:
 *
 * - The fetch promise RESOLVES on HTTP failure. Only transport-level errors
 *   and the abort deadline reject. A caller that treats a resolved response as
 *   success ships the "refused send silently reaches running state" defect.
 * - An abort at the deadline means the request WAS received -- the response is
 *   late, not lost -- so it must not be reported as a failure.
 *
 * The stubs sit at the real `fetch` seam (not at `api.sendChat`) so the tests
 * exercise the same resolve/reject boundary production hits.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { sendTurn, SEND_ABORT_MS } from '../chat-core/transport/sendTurn'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('sendTurn receipt contract', () => {
  it('normalizes an immediate dispatch: the HTTP response IS the delivery receipt', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('dispatched')
    expect(receipt.body.ok).toBe(true)
  })

  it('normalizes a queued acceptance as NOT a delivery receipt', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true, queued: true }))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('queued')
  })

  it('pins the resolves-not-rejects trap: an HTTP refusal RESOLVES and must be read from the body', async () => {
    // The defect class this exists for: a 4xx/5xx does NOT reject the fetch
    // promise. A caller that only catches rejections reports nothing while the
    // message quietly went nowhere.
    fetchMock.mockResolvedValue(jsonResponse(409, { ok: false, error: 'slot agent mismatch' }))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('refused')
    expect(receipt.reason).toBe('slot agent mismatch')
  })

  it('treats a bodyless / unreadable NON-2XX as refused (the status line survives a mangled response)', async () => {
    fetchMock.mockResolvedValue(new Response('gateway timeout', { status: 504 }))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('refused')
    expect(receipt.reason).toBeUndefined()
    expect(receipt.body).toEqual({})
  })

  it('classifies an unreadable 2XX as unknown, never as a refusal', async () => {
    // The request WAS accepted and only the answer is mangled (truncated body,
    // proxy cut-off). Reporting this as a failure hands the payload back and
    // invites a retry that duplicates a delivered turn -- the exact defect
    // readSendReceipt exists to prevent. Callers must act on nothing.
    fetchMock.mockResolvedValue(new Response('{"ok": true, "run_', { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('unknown')
    expect(receipt.body).toEqual({})
  })

  it('still queues a whitespace-free non-empty message', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true, queued: true }))
    const receipt = await sendTurn({ message: '  spaced  ', slot: 'chat-1' })
    expect(receipt.status).toBe('queued')
  })

  it('maps a transport rejection (never left the machine) to transport-error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const receipt = await sendTurn({ message: 'hi', slot: 'chat-1' })
    expect(receipt.status).toBe('transport-error')
    expect(receipt.body).toEqual({})
  })

  it('maps the abort deadline to response-late, NOT to a failure', async () => {
    vi.useFakeTimers()
    // A hung POST: resolve never comes; reject only when the signal aborts.
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      }))
    const pending = sendTurn({ message: 'hi', slot: 'chat-1' })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    const receipt = await pending
    expect(receipt.status).toBe('response-late')
  })

  it('maps a deadline while reading an accepted response body to response-late', async () => {
    vi.useFakeTimers()
    // Headers arrived with a 2xx, but the body never completes. Real fetch
    // rejects response.json() when the request signal aborts; model that exact
    // second phase so readSendReceipt's unreadable-body catch cannot erase the
    // deadline signal and strand a caller's already-cleared input.
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () => new Promise((_resolve, reject) => {
          init.signal?.addEventListener(
            'abort',
            () => reject(new DOMException('aborted', 'AbortError')),
            { once: true },
          )
        }),
      }))

    const pending = sendTurn({ message: 'hi', slot: 'chat-1' })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    await expect(pending).resolves.toEqual({ status: 'response-late', body: {} })
  })

  it('does not fire the deadline before SEND_ABORT_MS', async () => {
    vi.useFakeTimers()
    let settled = false
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      }))
    const pending = sendTurn({ message: 'hi', slot: 'chat-1' }).then((r) => { settled = true; return r })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await pending
    expect(settled).toBe(true)
  })

  it('carries slot and meta onto the wire and targets the streaming endpoint', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    await sendTurn({ message: 'hi', slot: 'chat-9', meta: { sendId: 's-1' } })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/chat?ws=1')
    const wire = JSON.parse((init as RequestInit).body as string)
    expect(wire.slot).toBe('chat-9')
    expect(wire.meta).toEqual({ sendId: 's-1' })
  })
})
