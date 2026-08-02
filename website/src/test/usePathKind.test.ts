/**
 * `usePathKind` — the stat gate behind markdown path chips.
 *
 * Covers the cache semantics specifically, because they are the part a reader
 * cannot infer from the component: `file`/`dir` are cached for the session,
 * `missing` expires. A permanently cached `missing` would leave a chip inert for
 * the rest of the session once the agent writes the file it just mentioned.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

import { usePathKind, __resetPathKindCache } from '../hooks/usePathKind'

function stub(kind: string | null, ok: boolean) {
  const fn = vi.fn(() =>
    Promise.resolve({
      ok,
      status: ok ? 200 : 404,
      headers: new Headers(kind ? { 'X-Path-Kind': kind } : {}),
    } as Response),
  )
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

describe('usePathKind', () => {
  const realFetch = globalThis.fetch
  beforeEach(() => { __resetPathKindCache(); vi.useRealTimers() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks(); vi.useRealTimers() })

  it('reports null path as unknown without probing', async () => {
    const fn = stub('file', true)
    const { result } = renderHook(() => usePathKind(null))
    expect(result.current).toBeUndefined()
    expect(fn).not.toHaveBeenCalled()
  })

  it('classifies a file and a directory from the response header', async () => {
    stub('file', true)
    const a = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(a.result.current).toBe('file'))

    __resetPathKindCache()
    stub('dir', false)
    const b = renderHook(() => usePathKind('/x/dir'))
    await waitFor(() => expect(b.result.current).toBe('dir'))
  })

  it('treats a header-less or failed response as missing — fails closed', async () => {
    stub(null, false)
    const a = renderHook(() => usePathKind('/x/gone'))
    await waitFor(() => expect(a.result.current).toBe('missing'))

    // A rejected probe must resolve to `missing`, never leave the chip pending
    // or poison the in-flight map.
    __resetPathKindCache()
    globalThis.fetch = vi.fn(() => Promise.reject(new Error('offline'))) as unknown as typeof fetch
    const b = renderHook(() => usePathKind('/x/boom'))
    await waitFor(() => expect(b.result.current).toBe('missing'))
  })

  it('serves a positive verdict from cache without re-probing', async () => {
    const fn = stub('file', true)
    const a = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(a.result.current).toBe('file'))
    expect(fn).toHaveBeenCalledTimes(1)

    const b = renderHook(() => usePathKind('/x/a.md'))
    await waitFor(() => expect(b.result.current).toBe('file'))
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('re-probes a missing path after the negative TTL expires', async () => {
    const fn = stub(null, false)
    const a = renderHook(() => usePathKind('/x/soon.md'))
    await waitFor(() => expect(a.result.current).toBe('missing'))
    expect(fn).toHaveBeenCalledTimes(1)

    // Within the TTL the negative verdict is reused.
    renderHook(() => usePathKind('/x/soon.md'))
    expect(fn).toHaveBeenCalledTimes(1)

    // Past it, the file the agent just wrote becomes reachable again.
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 11_000)
    const fn2 = stub('file', true)
    const c = renderHook(() => usePathKind('/x/soon.md'))
    await waitFor(() => expect(c.result.current).toBe('file'))
    expect(fn2).toHaveBeenCalledTimes(1)
  })
})
