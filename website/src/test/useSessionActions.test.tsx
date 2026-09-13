/**
 * useSessionActions — the single hook backing the surface-agnostic session
 * actions (duplicate / mark-read / pin / copy-link / move / close) shared by all
 * four session-menu surfaces AND the sidebar row's non-menu Duplicate/Close
 * buttons. It is the highest-fan-in unit, so it gets its own test (its siblings
 * collapseGroups / orderFoldersWithPaths / useMoveSlotToFolder already have
 * theirs).
 *
 * Priority: the two behaviors whose state source matters —
 *   - toggleRead reads store.getState().dashboard.unreadSlots
 *   - pin rollback reads store.getState().dashboard.slots[].pinned
 *
 * Pattern mirrors ChatSidebar.moveToFolder.test.tsx: renderHook wrapped in a
 * Provider over the app's singleton store (so the hook's store.getState() reads
 * the seeded state) plus a QueryClientProvider for the useMutation calls.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({
  setSlotPin: vi.fn(), forkChatSlot: vi.fn(), chatSlots: vi.fn(), dashboardConfig: vi.fn(),
}))
vi.mock('../api/client', async () => ({
  SEARCH_MIN_CHARS: 2,
  // Re-exported so the hook's `err instanceof ApiError` has a real right-hand side.
  ApiError: (await import('../api/apiError')).ApiError,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

// close() gates on loadChatConfig().confirmCloseSession — mock it so each test
// controls the branch deterministically (no localStorage dependency).
const cfgMock = vi.hoisted(() => ({ loadChatConfig: vi.fn(() => ({ confirmCloseSession: false })) }))
vi.mock('../pages/chat/ChatSettings', () => cfgMock)

import { store } from '../store'
import { sseSlots, markSlotUnread, updateSlotPin } from '../store/dashboardSlice'
import { useSessionActions } from '../hooks/useSessionActions'
import { ApiError } from '../api/apiError'
import { recordError, __resetErrorJournalForTests } from '../utils/errorReport'

const SLOT = 'chat-actions-1'

function seed(pinned = false) {
  const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '' }
  store.dispatch(sseSlots([slot]))
  store.dispatch(updateSlotPin({ key: SLOT, pinned }))
}
const slotOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)
const unread = () => store.getState().dashboard.unreadSlots.includes(SLOT)

function renderActions() {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
  )
  return renderHook(() => useSessionActions('personal'), { wrapper }).result
}

beforeEach(() => {
  mocks.setSlotPin.mockResolvedValue({})
  mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'forked' })
  mocks.chatSlots.mockResolvedValue([])
  mocks.dashboardConfig.mockResolvedValue({})
  cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  store.dispatch(sseSlots([]))
})

describe('useSessionActions', () => {
  function overCapacity() {
    return new ApiError(400, 'too large', JSON.stringify({ code: 'fork_corpus_too_large' }))
  }

  it('reports a refused duplicate as state for the caller to render, raising no dialog', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const nativeDialog = vi.fn()
    vi.stubGlobal('alert', nativeDialog)
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('too large to duplicate whole')
    // Attributed to the row it was invoked on, which is where it renders.
    expect(result.current.forkError?.slotKey).toBe(SLOT)
    expect(nativeDialog).not.toHaveBeenCalled()
  })

  it('advises the end a tail-fork deployment actually copies from', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    mocks.dashboardConfig.mockResolvedValue({ tail_fork_enabled: true })
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('later message')
    expect(result.current.forkError?.message).not.toContain('earlier message')
  })

  it('still surfaces the refusal when the config lookup fails too', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    mocks.dashboardConfig.mockRejectedValue(new Error('config unavailable'))
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('too large to duplicate whole')
    expect(result.current.forkError?.message).not.toContain('earlier message')
    expect(result.current.forkError?.message).not.toContain('later message')
  })

  it('keeps the structured report the localized line can no longer be matched to', async () => {
    seed()
    __resetErrorJournalForTests()
    // The journal is keyed on the RAW wire text, which the localized copy replaces.
    const journalled = recordError({
      source: 'api', message: 'too large', status: 400,
      code: 'fork_corpus_too_large', endpoint: '/api/chat/slots/fork',
    })
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    expect(result.current.forkError?.message).toContain('too large to duplicate whole')
    expect(result.current.forkError?.report?.id).toBe(journalled.id)
    expect(result.current.forkError?.report?.code).toBe('fork_corpus_too_large')
    expect(result.current.forkError?.report?.endpoint).toBe('/api/chat/slots/fork')
  })

  it('does not tell an off-transcript reader to pick a message that is not there', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()

    act(() => result.current.duplicate(SLOT))

    await waitFor(() => expect(result.current.forkError).toBeTruthy())
    // The session list shows no message list, so "Pick an earlier message" names an
    // affordance the reader cannot reach; and it must not answer Duplicate with "fork".
    expect(result.current.forkError?.message).not.toContain('Pick an')
    expect(result.current.forkError?.message).not.toContain('too large to fork')
    expect(result.current.forkError?.message).toContain('Open it')
    // The recovery must name the control that exists ("Fork conversation from here"),
    // not the clipboard Copy the Duplicate icon otherwise suggests.
    expect(result.current.forkError?.message).toContain('Fork conversation from here')
  })

  it('clears a refused duplicate on dismiss', async () => {
    seed()
    mocks.forkChatSlot.mockRejectedValue(overCapacity())
    const result = renderActions()
    act(() => result.current.duplicate(SLOT))
    await waitFor(() => expect(result.current.forkError).toBeTruthy())

    act(() => result.current.clearForkError())

    expect(result.current.forkError).toBeNull()
  })

  it('toggleRead flips based on dashboard.unreadSlots', () => {
    seed()
    store.dispatch(markSlotUnread(SLOT))          // start unread
    expect(unread()).toBe(true)
    const a = renderActions()
    act(() => a.current.toggleRead(SLOT))          // unread -> read
    expect(unread()).toBe(false)
    act(() => a.current.toggleRead(SLOT))          // read -> unread
    expect(unread()).toBe(true)
  })

  it('togglePin optimistically pins then rolls back when setSlotPin rejects', async () => {
    mocks.setSlotPin.mockRejectedValueOnce(new Error('boom'))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    expect(slotOf()?.pinned).toBe(true)                        // optimistic update
    await waitFor(() => expect(slotOf()?.pinned).toBe(false))  // rolled back on failure
  })

  it('togglePin persists when setSlotPin succeeds', async () => {
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: true } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    expect(slotOf()?.pinned).toBe(true)
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalledWith(SLOT, true))
    expect(slotOf()?.pinned).toBe(true)                        // no rollback
  })

  it('close honours confirmCloseSession', () => {
    seed()
    // disabled -> no confirm prompt, deleteSlot dispatched
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
    const skipConfirm = vi.fn(() => true)
    vi.stubGlobal('confirm', skipConfirm)
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const a = renderActions()
    dispatchSpy.mockClear()
    act(() => a.current.close(SLOT))
    expect(skipConfirm).not.toHaveBeenCalled()
    expect(dispatchSpy).toHaveBeenCalled()

    // enabled + user declines -> confirm prompted, nothing dispatched
    cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: true })
    const decline = vi.fn(() => false)
    vi.stubGlobal('confirm', decline)
    dispatchSpy.mockClear()
    act(() => a.current.close(SLOT))
    expect(decline).toHaveBeenCalled()
    expect(dispatchSpy).not.toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })
})
