/**
 * The endpoint allowlist (`app_endpoint_allowed` in `apps/manifest.py`, mirrored by
 * `endpointAllowed`) checks the url the dispatch is ABOUT to request. `fetch` follows a
 * 3xx by default and a 307 preserves the method, the body and the `X-Session-Key`
 * header, so an approved app endpoint answering `307 /api/apps/<victim>/disable` would
 * have the reader's own session disable another app on a row they merely clicked — the
 * checked url and the url that actually ran would differ, and every character-level
 * guard in that allowlist would be walked around rather than defeated.
 *
 * So the dispatch must refuse to follow. Asserted at the transport, because that is the
 * only layer where the option exists.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const okResponse = () => ({
  ok: true,
  status: 200,
  json: async () => ({}),
  text: async () => '',
  headers: new Headers({ 'content-type': 'application/json' }),
})

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(okResponse())
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

describe('invokeFileMenuItem transport', () => {
  it("refuses to follow a redirect out of the validated endpoint", async () => {
    const { api } = await import('../api/client')
    await api.invokeFileMenuItem(
      { id: 'send', endpoint: '/api/apps/doc-store/send' },
      { surface: 'folder-row', path: 'notes/x.md', kind: 'file' },
      'dashboard:slot-1',
    )
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/apps/doc-store/send')
    expect(init.redirect).toBe('error')
  })

  it('sends the owning slot as X-Session-Key rather than the shared placeholder', async () => {
    const { api } = await import('../api/client')
    await api.invokeFileMenuItem(
      { id: 'send', endpoint: '/api/apps/doc-store/send' },
      { surface: 'folder-row', path: 'notes/x.md', kind: 'file' },
      'dashboard:incognito-9',
    )
    const init = fetchMock.mock.calls[0][1] as RequestInit
    const headers = init.headers as Record<string, string>
    expect(headers['X-Session-Key']).toBe('dashboard:incognito-9')
    // The body is the app-facing contract; the slot must not leak into it.
    expect(JSON.parse(init.body as string)).toEqual({
      item_id: 'send', surface: 'folder-row', path: 'notes/x.md', kind: 'file',
    })
  })

  it('leaves every other POST following redirects as before', async () => {
    // The option is opt-in: adding it must not change a core-routed caller, whose url
    // core itself chose.
    const { api } = await import('../api/client')
    await api.artifactTeardown('some-slug').catch(() => {})
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.redirect).toBeUndefined()
  })
})
