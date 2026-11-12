// The dock and the Bots panel each mount this write path. A move from one is
// refused while the other's is saving, so two renumberings never interleave.
import { describe, it, expect, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('../api/client', () => ({ api: { updateKirocrewAgent: vi.fn(() => Promise.resolve({})) } }))

import { api, type MemberRosterRow } from '../api/client'
import { useStarOrderReorder } from './useStarOrderReorder'

const row = (name: string) => ({ name, slug: name, starred: true }) as MemberRosterRow

describe('useStarOrderReorder', () => {
  it('refuses a move from one list while a move from the other is saving', async () => {
    const qc = new QueryClient()
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const dock = renderHook(() => useStarOrderReorder(), { wrapper })
    const panel = renderHook(() => useStarOrderReorder(), { wrapper })
    let release = () => {}
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((done) => { release = () => done({}) }),
    )
    const order = [row('a'), row('b')]
    act(() => { expect(dock.result.current.move(order, 1, 0)).toBe(true) })
    expect(panel.result.current.move(order, 0, 1)).toBe(false)
    await waitFor(() => expect(panel.result.current.busy).toBe(true))
    act(() => release())
    await waitFor(() => expect(panel.result.current.busy).toBe(false))
    expect((api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mock.calls).toEqual([
      ['b', { star_order: 1 }],
      ['a', { star_order: 2 }],
    ])
    act(() => { expect(panel.result.current.move(order, 0, 1)).toBe(true) })
  })
})
