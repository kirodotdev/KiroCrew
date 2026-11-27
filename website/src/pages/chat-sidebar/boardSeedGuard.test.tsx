/** The state-lane seed reads the column list to decide which lanes are missing,
 *  so a list that has not loaded (or failed to) must never reach a write: every
 *  lane reads as missing from its `[]` default, including ones the user deleted. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const createTagColumn = vi.hoisted(() => vi.fn())
vi.mock('../../api/client', () => ({ api: { createTagColumn } }))

import { useBoardColumnMutations } from './board'

function renderMutations(columnsReady: boolean) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return renderHook(() => useBoardColumnMutations({
    queryClient, setBoardError: vi.fn(), orderedColumns: [], rawColumns: [], columnsReady,
    paintedWidthRef: { current: 260 }, widenForBoard: vi.fn(), setSeedError: vi.fn(),
  }), { wrapper })
}

beforeEach(() => {
  createTagColumn.mockReset()
  createTagColumn.mockResolvedValue({ id: 'new' })
})

describe('useBoardColumnMutations seed guard (#42763)', () => {
  it('writes nothing and reports no missing lane before the columns load', async () => {
    const { result } = renderMutations(false)
    expect(result.current.missingLanes).toEqual([])
    await act(async () => { await result.current.seedStateLanesMutation.mutateAsync() })
    expect(createTagColumn).not.toHaveBeenCalled()
  })

  it('seeds every lane once a loaded board holds none', async () => {
    const { result } = renderMutations(true)
    await act(async () => { await result.current.seedStateLanesMutation.mutateAsync() })
    expect(createTagColumn.mock.calls.map(c => c[0].state_key)).toEqual(['needs_approval', 'waiting', 'working', 'idle'])
  })
})
