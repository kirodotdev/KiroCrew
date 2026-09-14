/**
 * A rejected gateway write must reach the in-page notice, not just roll back.
 *
 * Each case drives the real mutation with a rejecting api so the assertion is on
 * the failure path the user actually hits, and reads the shared store rather
 * than the component that raised it — the menu subtree unmounts on close.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const apiMock = vi.hoisted(() => ({
  forkChatSlot: vi.fn(),
  setSlotPin: vi.fn(),
  setSlotMode: vi.fn(),
  chatSlots: vi.fn(),
  setSlotFolder: vi.fn(),
  chatSlotReload: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: apiMock, ApiError: class ApiError extends Error { body = '' } }))

const copySessionLink = vi.hoisted(() => vi.fn())
vi.mock('../utils/shareUrl', () => ({ copySessionLink }))

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: true }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { ApiError } from '../api/client'
import { useSessionActions } from '../hooks/useSessionActions'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { __resetActionFailureForTests, useActionFailure } from '../utils/actionFailure'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

function seedSlot() {
  store.dispatch(sseSlots([
    { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null } as never,
  ]))
}

describe('rejected session writes surface in the page', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    vi.clearAllMocks()
    // Both toggles confirm first, and jsdom answers falsy, which would bail out
    // before the mutation this test is about ever runs.
    vi.stubGlobal('confirm', () => true)
    apiMock.chatSlots.mockResolvedValue({ slots: [] })
    seedSlot()
  })

  it('reports a rejected mode switch', async () => {
    apiMock.setSlotMode.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/mode/i))
  })

  it('reports a rejected pin', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
  })

  it('reports a rejected reload instead of raising a native alert', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new Error('nope'))
    const alerted = vi.fn()
    vi.stubGlobal('alert', alerted)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(alerted).not.toHaveBeenCalled()
  })

  it('drops the unreachable hedge when the gateway answered', async () => {
    // An ApiError means the gateway replied, and reload is offline-gated by this
    // PR, so naming the gateway unreachable sends the reader down a dead end.
    apiMock.chatSlotReload.mockRejectedValue(new ApiError('busy'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(result.current.failure.failure?.message).not.toMatch(/unreachable/i)
  })

  it('keeps the unreachable wording when nothing answered', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new TypeError('Failed to fetch'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(result.current.failure.failure?.message).toMatch(/unreachable/i)
  })

  it('reports a rejected folder move', async () => {
    apiMock.setSlotFolder.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      move: useMoveSlotToFolder(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.move('chat-a', 'f1') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/move/i))
  })

  it('reports a rejected fork, which otherwise switched to no session at all', async () => {
    apiMock.forkChatSlot.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.duplicate('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/duplicate/i))
  })

  it('shows a sentence rather than the transport text a reader cannot act on', async () => {
    apiMock.setSlotMode.mockRejectedValue(new Error('Failed to fetch'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toBeTruthy())
    expect(result.current.failure.failure?.message).not.toContain('Failed to fetch')
  })
})
