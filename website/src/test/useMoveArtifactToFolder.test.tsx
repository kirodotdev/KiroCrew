/**
 * useMoveArtifactToFolder: a refused move rolls back AND says so.
 *
 * The rollback is a refetch, so the card jumps back to its old folder. Without
 * a notice that reads as the move never having been asked for, which is the
 * silent-rollback gap useMoveSlotToFolder already closed for sessions.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const mocks = vi.hoisted(() => ({ setArtifactFolder: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue({})),
  }),
}))

import { store } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'

function renderMove() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}>{children}</Provider></QueryClientProvider>
  )
  return { ...renderHook(() => useMoveArtifactToFolder(), { wrapper }), qc }
}

const settled = (qc: QueryClient) => qc.getMutationCache().getAll().map(m => m.state.status)

afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setAgentSwitchNotice(null))
})

describe('useMoveArtifactToFolder', () => {
  it('tells the user a refused move did not happen, with the gateway reason', async () => {
    mocks.setArtifactFolder.mockRejectedValueOnce(new ApiError(403, 'that folder belongs to another Bot'))
    const { result: move, qc } = renderMove()
    act(() => move.current('report', 'f2'))
    await waitFor(() => expect(settled(qc)).toEqual(['error']))
    expect(store.getState().chat.agentSwitchNotice).toEqual({
      title: i18nT('hooks.useArtifactFolders.move_refused'),
      message: 'that folder belongs to another Bot',
      error: true,
    })
  })

  it('says nothing when the move lands', async () => {
    mocks.setArtifactFolder.mockResolvedValueOnce({})
    const { result: move, qc } = renderMove()
    act(() => move.current('report', 'f2'))
    await waitFor(() => expect(settled(qc)).toEqual(['success']))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  const keysOf = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.map(([filters]) => JSON.stringify((filters as { queryKey?: unknown } | undefined)?.queryKey))

  it('reports nothing for a failed move a later move already superseded, and refetches only once both settle', async () => {
    let rejectFirst!: (e: Error) => void
    let resolveSecond!: () => void
    mocks.setArtifactFolder
      .mockReturnValueOnce(new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockReturnValueOnce(new Promise<void>(resolve => { resolveSecond = resolve }))
    const { result: move, qc } = renderMove()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    act(() => move.current('report', 'f2'))
    act(() => move.current('report', 'f1'))
    await waitFor(() => expect(mocks.setArtifactFolder).toHaveBeenCalledWith('report', 'f1'))
    await act(async () => { rejectFirst(new Error('boom')) })
    await waitFor(() => expect(settled(qc)).toEqual(['error', 'pending']))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
    // No refetch while the later move is still in flight: read now, the server
    // would hand back the old folder and drop the card into it.
    expect(keysOf(invalidate)).not.toContain(JSON.stringify(['artifacts']))
    await act(async () => { resolveSecond() })
    await waitFor(() => expect(settled(qc)).toEqual(['error', 'success']))
    expect(keysOf(invalidate)).toContain(JSON.stringify(['artifacts']))
    expect(keysOf(invalidate)).toContain(JSON.stringify(['artifact', 'report']))
  })

  it('reconciles with the server when an older move lands after a newer one', async () => {
    // The detail header fires concurrent requests; the newer one can finish
    // first, and the older then writes its folder last on the server.
    let resolveFirst!: () => void
    mocks.setArtifactFolder
      .mockReturnValueOnce(new Promise<void>(resolve => { resolveFirst = resolve }))
      .mockResolvedValueOnce({})
    const { result: move, qc } = renderMove()
    act(() => move.current('report', 'f2'))
    act(() => move.current('report', 'f1'))
    await waitFor(() => expect(settled(qc)).toEqual(['pending', 'success']))
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    await act(async () => { resolveFirst() })
    await waitFor(() => expect(settled(qc)).toEqual(['success', 'success']))
    expect(keysOf(invalidate)).toContain(JSON.stringify(['artifact', 'report']))
    expect(keysOf(invalidate)).toContain(JSON.stringify(['artifacts']))
  })
})
