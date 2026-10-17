import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useLinkMeta, __resetLinkMetaForTests, type LinkMeta } from './linkMeta'

const URL_A = 'https://example.com/post'

const wire = (over: Record<string, unknown> = {}) => ({
  url: URL_A,
  title: 'Example Title',
  description: 'A description of the page.',
  site_name: 'Example',
  domain: 'example.com',
  icon: 'data:image/png;base64,AAAA',
  icon_dark: '',
  fetched_at: 1770000000,
  ...over,
})

const ok = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response

const err = (status: number, code: string) =>
  ({
    ok: false,
    status,
    json: async () => ({ code }),
    text: async () => JSON.stringify({ code }),
  }) as unknown as Response

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  __resetLinkMetaForTests()
  fetchMock = vi.fn(async () => ok(wire()))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('useLinkMeta', () => {
  it('returns undefined while loading, then the meta on success', async () => {
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    expect(result.current).toBeUndefined()
    await waitFor(() => expect(result.current).toBeTruthy())
    const meta = result.current as LinkMeta
    expect(meta.title).toBe('Example Title')
    expect(meta.description).toBe('A description of the page.')
    expect(meta.siteName).toBe('Example')
    expect(meta.domain).toBe('example.com')
    expect(meta.icon).toBe('data:image/png;base64,AAAA')
    expect(meta.fetchedAt).toBe(1770000000)
  })

  it('percent-encodes the url and sends the session-key header', async () => {
    const { result } = renderHook(() => useLinkMeta('https://example.com/a?b=1&c=2', true))
    await waitFor(() => expect(result.current).toBeTruthy())
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/link-meta?url=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1%26c%3D2')
    expect((init.headers as Record<string, string>)['X-Session-Key']).toBe('dashboard:ui')
  })

  it.each([
    [400, 'invalid_url'],
    [400, 'blocked_url'],
    [403, 'link_previews_disabled'],
    [502, 'fetch_failed'],
  ])('returns null for %i %s', async (status, code) => {
    fetchMock.mockImplementation(async () => err(status, code))
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('returns null when the request rejects outright', async () => {
    fetchMock.mockImplementation(async () => {
      throw new Error('offline')
    })
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('retries a failure once the negative-cache window has passed', async () => {
    // A transient failure must not pin `null` for the tab's lifetime: the
    // backend expires its own negative entry after 10 min, and if the frontend
    // never asks again the link can never preview even after the site recovers.
    fetchMock.mockImplementation(async () => {
      throw new Error('offline')
    })
    const first = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(first.result.current).toBeNull())
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Still inside the window: the cached failure answers, no second request.
    first.unmount()
    const cached = renderHook(() => useLinkMeta(URL_A, true))
    expect(cached.result.current).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    cached.unmount()

    // Past the window, and now the site answers.
    const realNow = Date.now
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 11 * 60 * 1000)
    fetchMock.mockImplementation(async () => ok(wire()))
    const retried = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(retried.result.current).not.toBeNull())
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('returns null when neither title nor domain is usable', async () => {
    fetchMock.mockImplementation(async () => ok(wire({ title: '  ', domain: '' })))
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('drops an icon that is not an inlined raster data URI', async () => {
    fetchMock.mockImplementation(async () =>
      ok(wire({ icon: 'https://evil.example/track.png' })),
    )
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeTruthy())
    expect((result.current as LinkMeta).icon).toBe('')
  })

  it('drops an svg icon (active content in an image slot)', async () => {
    fetchMock.mockImplementation(async () =>
      ok(wire({ icon: 'data:image/svg+xml;base64,AAAA' })),
    )
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeTruthy())
    expect((result.current as LinkMeta).icon).toBe('')
  })

  it('carries the dark-scheme variant when the site declares one', async () => {
    fetchMock.mockImplementation(async () =>
      ok(wire({ icon_dark: 'data:image/png;base64,BBBB' })),
    )
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeTruthy())
    expect((result.current as LinkMeta).iconDark).toBe('data:image/png;base64,BBBB')
  })

  it('reports no variant when the site ships one icon for every surface', async () => {
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeTruthy())
    expect((result.current as LinkMeta).iconDark).toBe('')
  })

  it.each([
    ['a remote URL', 'https://evil.example/track.png'],
    ['an svg', 'data:image/svg+xml;base64,AAAA'],
  ])('holds the dark variant to the same data:-only screen — %s', async (_label, value) => {
    // A second icon field is a second `<img src>`, so it is exactly as
    // attractive a place to smuggle a tracking beacon or active content.
    fetchMock.mockImplementation(async () => ok(wire({ icon_dark: value })))
    const { result } = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(result.current).toBeTruthy())
    expect((result.current as LinkMeta).iconDark).toBe('')
  })

  it('never fetches when enabled is false', async () => {
    const { result } = renderHook(() => useLinkMeta(URL_A, false))
    await Promise.resolve()
    expect(result.current).toBeUndefined()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('never fetches a non-http(s) url', async () => {
    const { result } = renderHook(() => useLinkMeta('javascript:alert(1)', true))
    await Promise.resolve()
    expect(result.current).toBeUndefined()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('never fetches an undefined url', async () => {
    const { result } = renderHook(() => useLinkMeta(undefined, true))
    await Promise.resolve()
    expect(result.current).toBeUndefined()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('dedupes two concurrent consumers of the same url into one fetch', async () => {
    const a = renderHook(() => useLinkMeta(URL_A, true))
    const b = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(a.result.current).toBeTruthy())
    await waitFor(() => expect(b.result.current).toBeTruthy())
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('serves a later mount from cache with no second fetch', async () => {
    const first = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(first.result.current).toBeTruthy())
    expect(fetchMock).toHaveBeenCalledTimes(1)

    const second = renderHook(() => useLinkMeta(URL_A, true))
    // Cache is read synchronously, so the value is there on the first paint.
    expect(second.result.current).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('caches the unavailable verdict too, so a dead link is fetched once', async () => {
    fetchMock.mockImplementation(async () => err(502, 'fetch_failed'))
    const first = renderHook(() => useLinkMeta(URL_A, true))
    await waitFor(() => expect(first.result.current).toBeNull())

    const second = renderHook(() => useLinkMeta(URL_A, true))
    expect(second.result.current).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('fetches each distinct url once', async () => {
    const a = renderHook(() => useLinkMeta(URL_A, true))
    const b = renderHook(() => useLinkMeta('https://other.example/x', true))
    await waitFor(() => expect(a.result.current).toBeTruthy())
    await waitFor(() => expect(b.result.current).toBeTruthy())
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not touch state after unmount when the fetch resolves late', async () => {
    let release!: (r: Response) => void
    fetchMock.mockImplementation(
      () => new Promise<Response>((res) => { release = res }),
    )
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { unmount } = renderHook(() => useLinkMeta(URL_A, true))
    unmount()
    release(ok(wire()))
    await new Promise((r) => setTimeout(r, 0))

    expect(errSpy).not.toHaveBeenCalled()
    // The resolved value still lands in the shared cache — the fetch belongs to
    // the cache, not to the component that happened to start it.
    const revisit = renderHook(() => useLinkMeta(URL_A, true))
    expect(revisit.result.current).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('stops requesting when enabled flips to false mid-flight', async () => {
    const { result, rerender } = renderHook(
      ({ on }: { on: boolean }) => useLinkMeta(URL_A, on),
      { initialProps: { on: true } },
    )
    await waitFor(() => expect(result.current).toBeTruthy())
    rerender({ on: false })
    expect(result.current).toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
