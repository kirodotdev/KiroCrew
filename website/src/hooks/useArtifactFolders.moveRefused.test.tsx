import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { store } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { useMoveArtifactToFolder } from './useArtifactFolders'

/**
 * A refused artifact folder move rolls back by refetch. Without a notice the
 * artifact just jumps back and nothing says why (#34554), so the hook puts the
 * refusal on the shell banner with the gateway's reason, as session moves do.
 */

const mocks = vi.hoisted(() => ({ setArtifactFolder: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

describe('useMoveArtifactToFolder refusal notice', () => {
  beforeEach(() => {
    mocks.setArtifactFolder.mockReset()
    store.dispatch(setAgentSwitchNotice(null))
  })

  it('shows the gateway reason when the move is refused', async () => {
    mocks.setArtifactFolder.mockRejectedValue(new Error('folder belongs to another owner'))
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper })
    act(() => { result.current('demo-artifact', 'f-1') })
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).not.toBeNull())
    expect(store.getState().chat.agentSwitchNotice).toMatchObject({
      title: 'The artifact was not moved.',
      message: 'folder belongs to another owner',
      error: true,
    })
  })

  it('falls back to the title alone when the refusal has no message', async () => {
    mocks.setArtifactFolder.mockRejectedValue({})
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper })
    act(() => { result.current('demo-artifact', 'f-1') })
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).not.toBeNull())
    expect(store.getState().chat.agentSwitchNotice).toMatchObject({ message: 'The artifact was not moved.', error: true })
  })

  it.each([['another folder', 'f-2'], ['the same folder', 'f-1']])(
    'posts no notice when a later move to %s superseded the refused one',
    async (_label, second) => {
      let rejectFirst!: (e: Error) => void
      mocks.setArtifactFolder
        .mockImplementationOnce(() => new Promise((_, rej) => { rejectFirst = rej }))
        .mockResolvedValueOnce({})
      const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
      qc.setQueryData(['artifacts'], { artifacts: [{ slug: 'demo-artifact', folder_id: '' }] })
      const w = ({ children }: { children: ReactNode }) => (
        <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
      )
      const onCommitted = vi.fn()
      const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper: w })
      act(() => { result.current('demo-artifact', 'f-1') })
      act(() => { result.current('demo-artifact', second, { onCommitted }) })
      await waitFor(() => expect(onCommitted).toHaveBeenCalled())
      await act(async () => { rejectFirst(new Error('refused')) })
      await waitFor(() => expect(qc.isMutating()).toBe(0))
      expect(store.getState().chat.agentSwitchNotice).toBeNull()
    },
  )

  it('still reports the refusal when a refetch rewrote the cache mid-flight', async () => {
    let reject!: (e: Error) => void
    mocks.setArtifactFolder.mockImplementationOnce(() => new Promise((_, rej) => { reject = rej }))
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(['artifacts'], { artifacts: [{ slug: 'demo-artifact', folder_id: '' }] })
    const w = ({ children }: { children: ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper: w })
    act(() => { result.current('demo-artifact', 'f-1') })
    await waitFor(() => expect(mocks.setArtifactFolder).toHaveBeenCalled())
    // A websocket-driven refetch lands the server's unmoved value first.
    act(() => { qc.setQueryData(['artifacts'], { artifacts: [{ slug: 'demo-artifact', folder_id: '' }] }) })
    await act(async () => { reject(new Error('folder belongs to another owner')) })
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).not.toBeNull())
  })

  it('a stale refusal leaves a newer in-flight move in the cache alone', async () => {
    let rejectFirst!: (e: Error) => void
    let resolveSecond!: (v: unknown) => void
    mocks.setArtifactFolder
      .mockImplementationOnce(() => new Promise((_, rej) => { rejectFirst = rej }))
      .mockImplementationOnce(() => new Promise((res) => { resolveSecond = res }))
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(['artifacts'], { artifacts: [{ slug: 'demo-artifact', folder_id: '' }] })
    qc.setQueryData(['artifact', 'demo-artifact'], { slug: 'demo-artifact', folder_id: '' })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const w = ({ children }: { children: ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper: w })
    act(() => { result.current('demo-artifact', 'f-1') })
    act(() => { result.current('demo-artifact', 'f-2') })
    await waitFor(() => expect(mocks.setArtifactFolder).toHaveBeenCalledTimes(2))
    await act(async () => { rejectFirst(new Error('refused')) })
    await waitFor(() => expect(qc.isMutating()).toBe(1))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['artifacts'] })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['artifact', 'demo-artifact'] })
    expect(qc.getQueryData<{ artifacts: { folder_id: string }[] }>(['artifacts'])?.artifacts[0].folder_id).toBe('f-2')
    expect(qc.getQueryData<{ folder_id: string }>(['artifact', 'demo-artifact'])?.folder_id).toBe('f-2')
    await act(async () => { resolveSecond({}) })
    await waitFor(() => expect(qc.isMutating()).toBe(0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['artifact', 'demo-artifact'] })
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('the newer move failing after a stale refusal still rolls back and reports', async () => {
    let rejectFirst!: (e: Error) => void
    let rejectSecond!: (e: Error) => void
    mocks.setArtifactFolder
      .mockImplementationOnce(() => new Promise((_, rej) => { rejectFirst = rej }))
      .mockImplementationOnce(() => new Promise((_, rej) => { rejectSecond = rej }))
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(['artifacts'], { artifacts: [{ slug: 'demo-artifact', folder_id: '' }] })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const w = ({ children }: { children: ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper: w })
    act(() => { result.current('demo-artifact', 'f-1') })
    act(() => { result.current('demo-artifact', 'f-2') })
    await waitFor(() => expect(mocks.setArtifactFolder).toHaveBeenCalledTimes(2))
    await act(async () => { rejectFirst(new Error('stale')) })
    await act(async () => { rejectSecond(new Error('folder belongs to another owner')) })
    await waitFor(() => expect(qc.isMutating()).toBe(0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['artifacts'] })
    expect(store.getState().chat.agentSwitchNotice).toMatchObject({ message: 'folder belongs to another owner' })
  })

  it('posts no notice when the move succeeds', async () => {
    mocks.setArtifactFolder.mockResolvedValue({})
    const onCommitted = vi.fn()
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper })
    act(() => { result.current('demo-artifact', 'f-1', { onCommitted }) })
    await waitFor(() => expect(onCommitted).toHaveBeenCalled())
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})
