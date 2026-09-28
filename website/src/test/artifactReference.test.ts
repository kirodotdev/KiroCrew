import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api/client'

/**
 * `recordArtifactReference` is the breadcrumb that makes an artifact a chat
 * merely OPENED (rather than authored) show up under "This session" — the panel
 * list reads the same event log through the `touched_by` filter.
 *
 * The header is the whole subtlety. Every other api method goes through the
 * shared `post` helper, which hardcodes `X-Session-Key: dashboard:ui`; the
 * events handler deliberately maps that literal to "no session", so a breadcrumb
 * sent that way would be recorded against nothing and never surface. This method
 * therefore sends the REAL slot key, scope-qualified as `dashboard:<slot>` —
 * the form the store's `_strip_session_scope` normalizes to the bare slot that
 * `touched_by` compares against.
 */
describe('api.recordArtifactReference', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ slug: 'cr-queue', event: { type: 'referenced' } }),
    })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  function lastCall() {
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    return { url, init, headers: init.headers as Record<string, string>, body: JSON.parse(init.body as string) }
  }

  it('posts a referenced event scoped to the real slot, not dashboard:ui', async () => {
    await api.recordArtifactReference('cr-queue', 'chat-7-1785396512')
    const { url, init, headers, body } = lastCall()
    expect(url).toBe('/api/artifacts/cr-queue/events')
    expect(init.method).toBe('POST')
    // The bug this guards: `dashboard:ui` is dropped server-side, so the
    // breadcrumb would be attributed to no session and the row would never move
    // out of the library section.
    expect(headers['X-Session-Key']).toBe('dashboard:chat-7-1785396512')
    expect(headers['X-Session-Key']).not.toBe('dashboard:ui')
    // The endpoint accepts only this type; anything else is a 400 by design.
    expect(body).toEqual({ type: 'referenced' })
  })

  it('percent-encodes the slug into the path', async () => {
    await api.recordArtifactReference('a/b?c', 'slot-1')
    expect(lastCall().url).toBe('/api/artifacts/a%2Fb%3Fc/events')
  })

  it('forwards impression metadata when given', async () => {
    await api.recordArtifactReference('cr-queue', 'slot-1', { message_ts: '1699999999.1', widget_index: 2 })
    expect(lastCall().body).toEqual({
      type: 'referenced',
      metadata: { message_ts: '1699999999.1', widget_index: 2 },
    })
  })

  it('omits the metadata key entirely when not given', async () => {
    // The handler rejects a non-object `metadata`; sending nothing is cleaner
    // than sending an empty object it would have to special-case.
    await api.recordArtifactReference('cr-queue', 'slot-1')
    expect('metadata' in lastCall().body).toBe(false)
  })

  it('rejects on a non-2xx so callers can swallow a failed breadcrumb', async () => {
    // An incognito slot gets 403 by design (deny-by-default on event writes).
    // The promise must reject rather than resolve silently, so the caller's
    // .catch() is what decides the breadcrumb is best-effort.
    fetchMock.mockResolvedValue({ ok: false, status: 403, text: async () => 'restricted session' })
    await expect(api.recordArtifactReference('cr-queue', 'slot-1')).rejects.toThrow()
  })
})
