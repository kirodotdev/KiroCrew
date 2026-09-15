// The image-dims cache persists to localStorage, so its KEYS outlive the
// session that produced them. The chat transcript keys entries by an image's
// resolved URL, and a message body can carry a presigned link whose signature
// sits in the query string — persisting that verbatim would leave a credential
// on disk after the session was deleted. These tests pin that no query
// parameter reaches a persisted key unless it is on the allowlist, and that the
// two allowed parameters still discriminate one image from another.
import { describe, it, expect, beforeEach, vi } from 'vitest'

const CACHE_KEY = 'mc-image-dims'

/** Fresh module per test — the cache is module-level state hydrated at import. */
async function freshModule() {
  vi.resetModules()
  return import('../utils/imageDims')
}

beforeEach(() => {
  localStorage.clear()
  vi.useRealTimers()
})

describe('imageDims cache keys: credentials never persist', () => {
  it('drops a presigned signature from the key', async () => {
    const { cacheKeyFor } = await freshModule()
    const key = cacheKeyFor(
      'https://bucket.s3.amazonaws.com/a.png?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=deadbeef&X-Amz-Expires=900',
    )
    expect(key).toBe('https://bucket.s3.amazonaws.com/a.png')
    expect(key).not.toContain('deadbeef')
    expect(key).not.toContain('AKIAEXAMPLE')
    expect(key.includes('?')).toBe(false)
  })

  it('drops a bearer-style query parameter and a fragment', async () => {
    const { cacheKeyFor } = await freshModule()
    expect(cacheKeyFor('https://host/i.png?token=SECRET123')).toBe('https://host/i.png')
    expect(cacheKeyFor('https://host/i.png?sig=SECRET123#frag')).toBe('https://host/i.png')
    expect(cacheKeyFor('https://host/i.png#access_token=SECRET123')).toBe('https://host/i.png')
  })

  it('never writes a dropped parameter into localStorage', async () => {
    const { rememberImageDims } = await freshModule()
    vi.useFakeTimers()
    rememberImageDims('https://bucket.s3.amazonaws.com/a.png?X-Amz-Signature=deadbeef', 800, 600)
    vi.advanceTimersByTime(2000)
    // Nothing at all is stored for an external reference — the provenance rule
    // below is stricter than key sanitizing, and supersedes it for this input.
    const stored = localStorage.getItem(CACHE_KEY) ?? ''
    expect(stored).not.toContain('deadbeef')
    expect(stored).not.toContain('X-Amz-Signature')
    expect(stored).not.toContain('bucket.s3.amazonaws.com')
  })

  it('a credentialed URL still reads back its own dimensions in-session', async () => {
    // Sanitizing must not break the feature: the same image, whose presigned
    // URL is re-minted with a fresh signature on the next render, must still
    // hit the entry learned under the previous signature.
    const { rememberImageDims, getImageDims } = await freshModule()
    rememberImageDims('https://host/i.png?X-Amz-Signature=first', 400, 300)
    expect(getImageDims('https://host/i.png?X-Amz-Signature=second')).toEqual({ w: 400, h: 300 })
  })

  it('strips a `path` parameter that is NOT on the first-party endpoint', async () => {
    // The allowlist names are OURS. An external image proxy that happens to
    // take `?path=` would otherwise have its credential preserved by the very
    // allowlist meant to strip credentials, so the allowlist only applies to
    // the endpoint whose parameters this cache actually understands.
    const { cacheKeyFor } = await freshModule()
    expect(cacheKeyFor('https://proxy.example/img?path=SECRET123')).toBe('https://proxy.example/img')
    expect(cacheKeyFor('/api/other?path=SECRET123&v=2')).toBe('/api/other')
    expect(cacheKeyFor('https://proxy.example/api/file-raw?path=SECRET123'))
      .toBe('https://proxy.example/api/file-raw')
  })

  it('never writes an off-endpoint `path` into localStorage', async () => {
    const { rememberImageDims } = await freshModule()
    vi.useFakeTimers()
    rememberImageDims('https://proxy.example/img?path=SECRET123', 640, 480)
    vi.advanceTimersByTime(2000)
    const stored = localStorage.getItem(CACHE_KEY) ?? ''
    expect(stored).not.toContain('SECRET123')
  })
})

describe('imageDims cache: only app-constructed references persist', () => {
  // Sanitizing an arbitrary URL is unwinnable — a credential can sit in a path
  // segment or in userinfo, where no amount of query stripping reaches it. So
  // eligibility is decided by provenance: relative references are ones the app
  // built, absolute ones came from untrusted message content.
  it('keeps a path-tokenized external image out of localStorage entirely', async () => {
    const { rememberImageDims, getImageDims } = await freshModule()
    vi.useFakeTimers()
    const url = 'https://cdn.example/t/BEARER_SECRET/photo.png'
    rememberImageDims(url, 800, 600)
    vi.advanceTimersByTime(2000)
    expect(localStorage.getItem(CACHE_KEY) ?? '').not.toContain('BEARER_SECRET')
    // Still reserved for the rest of the session — memory-only, not uncached.
    expect(getImageDims(url)).toEqual({ w: 800, h: 600 })
  })

  it('keeps userinfo credentials out of localStorage', async () => {
    const { rememberImageDims } = await freshModule()
    vi.useFakeTimers()
    rememberImageDims('https://user:PASSWORD_SECRET@host/i.png', 100, 100)
    vi.advanceTimersByTime(2000)
    expect(localStorage.getItem(CACHE_KEY) ?? '').not.toContain('PASSWORD_SECRET')
  })

  it('does not persist data: or blob: references', async () => {
    const { rememberImageDims } = await freshModule()
    vi.useFakeTimers()
    rememberImageDims('data:image/png;base64,AAAA', 10, 10)
    rememberImageDims('blob:http://localhost/abcd-1234', 20, 20)
    vi.advanceTimersByTime(2000)
    const stored = localStorage.getItem(CACHE_KEY) ?? ''
    expect(stored).not.toContain('data:image')
    expect(stored).not.toContain('blob:')
  })

  it('still persists the first-party URL and a bare slug', async () => {
    const { rememberImageDims } = await freshModule()
    vi.useFakeTimers()
    rememberImageDims('/api/file-raw?path=%2Ftmp%2Fa.png', 300, 200)
    rememberImageDims('my-artifact-slug', 50, 40)
    vi.advanceTimersByTime(2000)
    const stored = localStorage.getItem(CACHE_KEY) ?? ''
    expect(stored).toContain('file-raw')
    expect(stored).toContain('my-artifact-slug')
  })

  it('drops an entry an earlier build already persisted', async () => {
    // Migration: a user who ran a build that persisted raw URLs has one on disk
    // already. The key here carries its secret in a PATH SEGMENT and has no
    // query, so key sanitizing is identity on it — only the provenance rule can
    // reject it, which is what makes this assertion discriminating.
    localStorage.setItem(CACHE_KEY, JSON.stringify([
      ['https://cdn.example/t/OLD_SECRET/x.png', { w: 1, h: 1 }],
      ['my-artifact-slug', { w: 2, h: 2 }],
    ]))
    const { getImageDims } = await freshModule()
    expect(getImageDims('https://cdn.example/t/OLD_SECRET/x.png')).toBeUndefined()
    expect(getImageDims('my-artifact-slug')).toEqual({ w: 2, h: 2 })
  })

  it('purges the poisoned blob on READ, without waiting for the next write', async () => {
    // A session that only scrolls never calls rememberImageDims, so a
    // write-time filter alone would leave the secret on disk indefinitely.
    localStorage.setItem(CACHE_KEY, JSON.stringify([
      ['https://cdn.example/t/OLD_SECRET/x.png', { w: 1, h: 1 }],
      ['my-artifact-slug', { w: 2, h: 2 }],
    ]))
    await freshModule()
    const stored = localStorage.getItem(CACHE_KEY) ?? ''
    expect(stored).not.toContain('OLD_SECRET')
    expect(stored).toContain('my-artifact-slug')
  })
})

describe('imageDims cache keys: the allowlist still discriminates', () => {
  it('keeps `path`, so two first-party images do not collide', async () => {
    const { rememberImageDims, getImageDims } = await freshModule()
    rememberImageDims('/api/file-raw?path=%2Ftmp%2Fa.png', 100, 50)
    rememberImageDims('/api/file-raw?path=%2Ftmp%2Fb.png', 200, 80)
    expect(getImageDims('/api/file-raw?path=%2Ftmp%2Fa.png')).toEqual({ w: 100, h: 50 })
    expect(getImageDims('/api/file-raw?path=%2Ftmp%2Fb.png')).toEqual({ w: 200, h: 80 })
  })

  it('keeps `v`, so a rewritten file does not reserve its predecessor box', async () => {
    const { rememberImageDims, getImageDims } = await freshModule()
    rememberImageDims('/api/file-raw?path=%2Ftmp%2Fa.png&v=1', 100, 50)
    expect(getImageDims('/api/file-raw?path=%2Ftmp%2Fa.png&v=2')).toBeUndefined()
  })

  it('is order-independent, so one image yields one entry', async () => {
    const { cacheKeyFor } = await freshModule()
    expect(cacheKeyFor('/api/file-raw?v=2&path=%2Ftmp%2Fa.png'))
      .toBe(cacheKeyFor('/api/file-raw?path=%2Ftmp%2Fa.png&v=2'))
  })

  it('leaves a bare slug untouched, so the gallery caller is unaffected', async () => {
    const { cacheKeyFor } = await freshModule()
    expect(cacheKeyFor('my-artifact-slug')).toBe('my-artifact-slug')
  })
})
