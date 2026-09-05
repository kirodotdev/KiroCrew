/**
 * Contract tests for the app-sdk send wire under the chat-core transport.
 *
 * The scoped `AppApi` an app embed must use does not look like the fetch seam
 * `readSendReceipt` reads (it throws on non-2xx and returns the parsed body on
 * 2xx). These pin that the adapter re-expresses every outcome in the seam's
 * shape, so `sendTurn` classifies an embed's send exactly as it classifies the
 * dashboard's -- including the outcome the old embed path mis-learned (a 2xx
 * whose body is not JSON was swallowed as success).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { sendTurn, SEND_ABORT_MS } from '../chat-core/transport/sendTurn'
import { appApiSendWire } from '../app-sdk/appSendWire'
import { AppApiError, AppApiPermissionError } from '../app-sdk/apiError'
import { AcceptedBodyUnreadable } from '../api/apiError'
import type { AppApi } from '../app-sdk/index'

function apiWith(post: AppApi['post']): AppApi {
  const never = () => Promise.reject(new Error('not used by the send wire'))
  return { get: never, post, put: never, patch: never, del: never }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('appApiSendWire', () => {
  it('sends through the scoped api at the JSON-receipt endpoint, carrying the agent the wire was built with', async () => {
    const post = vi.fn().mockResolvedValue({ ok: true })
    const receipt = await sendTurn({
      message: 'hi',
      slot: 'slot-1',
      meta: { sendId: 's1' },
      wire: appApiSendWire(apiWith(post), 'privacy-dev'),
    })
    // `?ws=1` selects the JSON receipt; the scoped path check reads only the
    // pathname, so the app's `/api/chat` grant covers it.
    expect(post).toHaveBeenCalledWith('/api/chat?ws=1', {
      message: 'hi',
      slot: 'slot-1',
      agent: 'privacy-dev',
      meta: { sendId: 's1' },
    })
    expect(receipt.status).toBe('dispatched')
  })

  it('classifies a queued acceptance as queued, not as a delivery receipt', async () => {
    const post = vi.fn().mockResolvedValue({ ok: true, queued: true })
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('queued')
  })

  it("turns the scoped api's non-2xx throw into a refusal that keeps the server's reason", async () => {
    const post = vi.fn().mockRejectedValue(
      new AppApiError(409, JSON.stringify({ error: 'slot agent mismatch', code: 'slot_agent' })),
    )
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('refused')
    expect(receipt.reason).toBe('slot agent mismatch')
    expect(receipt.body.code).toBe('slot_agent')
  })

  it('treats a non-JSON non-2xx body as a refusal too (the status line survives an HTML 500)', async () => {
    const post = vi.fn().mockRejectedValue(new AppApiError(500, '<html>Internal Server Error</html>'))
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('refused')
    expect(receipt.reason).toBeUndefined()
  })

  it('classifies a 2xx whose body is not JSON as unknown -- the case the old embed swallowed as success', async () => {
    // The scoped helper tags a post-2xx parse failure `AcceptedBodyUnreadable`.
    // The bare endpoint answers with an SSE stream, which is exactly this
    // shape -- and the old `.catch(SyntaxError => undefined)` reported it as a
    // successful send.
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const post = vi.fn().mockRejectedValue(new AcceptedBodyUnreadable(new SyntaxError('Unexpected token d in JSON')))
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('unknown')
  })

  it('classifies a 2xx whose body stream was cut as unknown, not transport-error', async () => {
    // A body read cut mid-stream is a TypeError -- the same class fetch throws
    // for a request that never left. Only the helper's phase tag tells them
    // apart; a bare TypeError stays transport-error (next test).
    const post = vi.fn().mockRejectedValue(new AcceptedBodyUnreadable(new TypeError('network error')))
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('unknown')
  })

  it('classifies a permission denial by the scoped api as a refusal that names the missing grant', async () => {
    // Nothing left the document, so the payload is safe to hand back -- but it
    // is not a network fault, and "check your connection" would never help.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const post = vi.fn().mockRejectedValue(
      new AppApiPermissionError('[app-sdk] App "x" not permitted to access /api/chat. Declared: [/api/apps/x]'),
    )
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('refused')
    // Human sentence as the reason; the raw grant detail goes to the console.
    expect(receipt.reason).toBe("This app isn't allowed to send chat messages.")
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('not permitted to access /api/chat'))
  })

  it('classifies a rejected fetch (offline, DNS) as transport-error: nothing left the document', async () => {
    const post = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    const receipt = await sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    expect(receipt.status).toBe('transport-error')
  })

  it('honours the transport deadline as response-late even though the scoped api takes no signal', async () => {
    vi.useFakeTimers()
    const post = vi.fn().mockReturnValue(new Promise(() => {}))
    const pending = sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    const receipt = await pending
    expect(receipt.status).toBe('response-late')
  })

  it('ignores a late settlement after the deadline already produced a receipt', async () => {
    vi.useFakeTimers()
    let resolvePost: (v: unknown) => void = () => {}
    const post = vi.fn().mockReturnValue(new Promise(r => { resolvePost = r }))
    const pending = sendTurn({ message: 'hi', wire: appApiSendWire(apiWith(post)) })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    const receipt = await pending
    // A late 2xx must not throw an unhandled rejection or flip the receipt.
    resolvePost({ ok: true })
    await vi.advanceTimersByTimeAsync(0)
    expect(receipt.status).toBe('response-late')
  })
})
