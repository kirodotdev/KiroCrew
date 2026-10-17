/**
 * `usePathKind` — the stat gate behind markdown path chips, diff-block headers
 * and tool-call lines.
 *
 * Covers the cache semantics specifically, because they are the part a reader
 * cannot infer from the component: `file`/`dir` are cached for the session,
 * `missing` expires. A permanently cached `missing` would leave a chip inert for
 * the rest of the session once the agent writes the file it just mentioned.
 * And covers the batching: every path asked about in one commit shares ONE
 * request, which is the point of the shared probe.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

import { usePathKind, __resetPathKindCache } from '../hooks/usePathKind'
import { kindsRequestCount, probedPaths, stubPathKinds, type StubKind } from './pathKindStub'

function stub(kind: StubKind) {
  return stubPathKinds(() => kind)
}

describe('usePathKind', () => {
  const realFetch = globalThis.fetch
  beforeEach(() => { __resetPathKindCache(); vi.useRealTimers() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks(); vi.useRealTimers() })

  it('reports null path as unknown without probing', async () => {
    const fn = stub('file')
    const { result } = renderHook(() => usePathKind(null))
    expect(result.current).toBeUndefined()
    await Promise.resolve()
    expect(fn).not.toHaveBeenCalled()
  })

  it('classifies a file and a directory from the batch reply', async () => {
    stub('file')
    const a = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(a.result.current).toBe('file'))

    __resetPathKindCache()
    stub('dir')
    const b = renderHook(() => usePathKind('/x/dir'))
    await waitFor(() => expect(b.result.current).toBe('dir'))
  })

  it('treats a refused, malformed or failed reply as missing — fails closed', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve(new Response('', { status: 403 }))) as unknown as typeof fetch
    const a = renderHook(() => usePathKind('/x/denied'))
    await waitFor(() => expect(a.result.current).toBe('missing'))

    __resetPathKindCache()
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ kinds: { '/x/odd': 'socket' } }), { status: 200 })),
    ) as unknown as typeof fetch
    const b = renderHook(() => usePathKind('/x/odd'))
    await waitFor(() => expect(b.result.current).toBe('missing'))

    // A rejected probe must resolve to `missing`, never leave the chip pending
    // or poison the in-flight map.
    __resetPathKindCache()
    globalThis.fetch = vi.fn(() => Promise.reject(new Error('offline'))) as unknown as typeof fetch
    const c = renderHook(() => usePathKind('/x/boom'))
    await waitFor(() => expect(c.result.current).toBe('missing'))
  })

  it('asks about every path of one commit in a single request', async () => {
    const fn = stubPathKinds(p => (p.endsWith('/') ? 'dir' : p.includes('gone') ? null : 'file'))
    const { result } = renderHook(() => ({
      a: usePathKind('/x/a.md'),
      b: usePathKind('/x/b.md'),
      d: usePathKind('/x/dir/'),
      g: usePathKind('/x/gone.md'),
      again: usePathKind('/x/a.md'),
    }))
    await waitFor(() => expect(result.current.g).toBe('missing'))
    expect(result.current).toEqual({ a: 'file', b: 'file', d: 'dir', g: 'missing', again: 'file' })
    expect(kindsRequestCount(fn)).toBe(1)
    expect(probedPaths(fn).sort()).toEqual(['/x/a.md', '/x/b.md', '/x/dir/', '/x/gone.md'])
  })

  it('splits a very large commit across requests within the endpoint cap', async () => {
    const fn = stub('file')
    const paths = Array.from({ length: 150 }, (_, i) => `/x/${i}.md`)
    const { result } = renderHook(() => paths.map(p => usePathKind(p)))
    await waitFor(() => expect(result.current.every(k => k === 'file')).toBe(true))
    expect(kindsRequestCount(fn)).toBe(2)
    expect(probedPaths(fn)).toHaveLength(150)
  })

  it('serves a positive verdict from cache without re-probing', async () => {
    const fn = stub('file')
    const a = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(a.result.current).toBe('file'))
    expect(fn).toHaveBeenCalledTimes(1)

    const b = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(b.result.current).toBe('file'))
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('re-probes a missing path after the negative TTL expires', async () => {
    const fn = stub(null)
    const a = renderHook(() => usePathKind('/x/soon.md'))
    await waitFor(() => expect(a.result.current).toBe('missing'))
    expect(fn).toHaveBeenCalledTimes(1)

    // Within the TTL the negative verdict is reused.
    renderHook(() => usePathKind('/x/soon.md'))
    expect(fn).toHaveBeenCalledTimes(1)

    // Past it, the file the agent just wrote becomes reachable again.
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 11_000)
    const fn2 = stub('file')
    const c = renderHook(() => usePathKind('/x/soon.md'))
    await waitFor(() => expect(c.result.current).toBe('file'))
    expect(fn2).toHaveBeenCalledTimes(1)
  })

  it('does not carry a resolved verdict across a path change', async () => {
    // Callers gate an affordance on `undefined` meaning "still deciding" — the
    // markdown chip withholds itself until every probe reports. Returning the
    // PREVIOUS path's `file` for the render after `path` changes would punch
    // straight through that barrier, briefly offering to open a path the text no
    // longer names.
    stub('file')
    const { result, rerender } = renderHook(({ p }: { p: string }) => usePathKind(p), {
      initialProps: { p: '/x/first.md' },
    })
    await waitFor(() => expect(result.current).toBe('file'))
    rerender({ p: '/x/second.md' })
    // Synchronously on the re-render, before any effect has run for the new path.
    expect(result.current).toBeUndefined()
    await waitFor(() => expect(result.current).toBe('file'))
  })

  it('answers instantly from cache when the new path is already known', async () => {
    // The flip side: resetting to `undefined` must not cost a known path an extra
    // render of "unknown", or every repeat mention would flicker inert.
    //
    // The two paths resolve to DIFFERENT kinds on purpose. With both as `file` the
    // assertion is satisfied by the stale verdict too, so it would pass with or
    // without the fix and lock in nothing.
    stubPathKinds(p => (p === '/x/known-dir' ? 'dir' : 'file'))
    const warm = renderHook(() => usePathKind('/x/known-dir'))
    await waitFor(() => expect(warm.result.current).toBe('dir'))
    const { result, rerender } = renderHook(({ p }: { p: string }) => usePathKind(p), {
      initialProps: { p: '/x/other.md' },
    })
    await waitFor(() => expect(result.current).toBe('file'))
    rerender({ p: '/x/known-dir' })
    // Synchronously: the cached 'dir' for the NEW path, never the previous 'file'.
    expect(result.current).toBe('dir')
  })
})
