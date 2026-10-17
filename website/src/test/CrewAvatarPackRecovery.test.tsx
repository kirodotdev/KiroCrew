/**
 * A crew wearing a pack, through the REAL `PackAvatar` and the real query hook,
 * when the pack read fails and then the gateway comes back.
 *
 * `CrewAvatarPack.test.tsx` stubs the renderer and pins the hand-off. This file
 * exists for the one claim a stub cannot carry: that a read failure is not a
 * permanent ghost. The claim holds only if `CrewAvatar` keeps `PackAvatar`
 * mounted while it shows the fallback — an unmounted renderer has no query
 * observer, and a refetch then has nobody to draw for. So the recovery is driven
 * through `CrewAvatar`, not through `PackAvatar` on its own.
 */
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

import CrewAvatar from '../components/CrewAvatar'
import { useInvalidatePackDetail } from '../hooks/usePackDetail'
import { retryPolicy } from '../api/queryClient'

let qc: QueryClient
const wrap = (ui: React.ReactElement) => <QueryClientProvider client={qc}>{ui}</QueryClientProvider>

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: retryPolicy, retryDelay: 10, staleTime: Infinity } } })
  detail.mockReset()
  // The suite's observer stub reports intersecting; visibility is not under test.
  class Visible {
    constructor(cb: IntersectionObserverCallback) {
      queueMicrotask(() => cb([{ isIntersecting: true } as IntersectionObserverEntry], this as never))
    }
    observe() {}
    disconnect() {}
    unobserve() {}
  }
  ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = Visible
})

afterEach(() => {
  cleanup()
  qc.clear()
})

/** Forget one pack the way the Library tab does. */
async function invalidate(id: string) {
  const { result } = renderHook(() => useInvalidatePackDetail(), {
    wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
  })
  await act(async () => {
    result.current(id)
  })
}

const ghostSrc = (container: HTMLElement) =>
  [...container.querySelectorAll('img')].map((i) => i.getAttribute('src') ?? '').find((s) => s.startsWith('data:image/svg+xml'))

describe('CrewAvatar — a pack read that fails and then recovers', () => {
  it('shows the seeded ghost while the read is failed, then the art once it is re-read', async () => {
    detail
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    const onImageError = vi.fn()
    const { container } = render(
      wrap(<CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} onImageError={onImageError} />),
    )

    // Both reads fail (the shared policy's one retry): the crew face, and one report.
    await waitFor(() => expect(ghostSrc(container)).toBeTruthy())
    expect(onImageError).toHaveBeenCalledTimes(1)
    expect(detail).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('pack-avatar-svg')).toBeNull()

    // The gateway is back and the query is refetched (a focus, a reconnect, or as
    // here the Library invalidating it). The row is still mounted, so it redraws.
    await invalidate('aurora')
    await screen.findByTestId('pack-avatar-svg')
    expect(ghostSrc(container)).toBeUndefined()
    expect(container.querySelector('img')?.getAttribute('src')).toContain('/api/appearances/aurora/slot/idle')
    // Recovery is not a second failure.
    expect(onImageError).toHaveBeenCalledTimes(1)
  })

  it('reports again when a recovered pack fails anew, and not for a re-render while failed', async () => {
    detail.mockRejectedValue(new Error('pack_not_found'))
    const onImageError = vi.fn()
    const { rerender } = render(
      wrap(<CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'gone' }} onImageError={onImageError} />),
    )
    await waitFor(() => expect(onImageError).toHaveBeenCalledTimes(1))

    // A fresh callback identity on a re-render is not a new failure.
    const again = vi.fn()
    rerender(wrap(<CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'gone' }} state="working" onImageError={again} />))
    await act(async () => {})
    expect(again).not.toHaveBeenCalled()

    // Recovered, then broken again: the edge fires once more.
    detail.mockReset()
    detail.mockResolvedValueOnce({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    await invalidate('gone')
    await screen.findByTestId('pack-avatar-svg')
    detail.mockRejectedValue(new Error('ECONNREFUSED'))
    await invalidate('gone')
    await waitFor(() => expect(again).toHaveBeenCalledTimes(1))
  })
})
