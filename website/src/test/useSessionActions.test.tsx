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

const mocks = vi.hoisted(() => ({ setSlotPin: vi.fn(), forkChatSlot: vi.fn(), chatSlots: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

// close() gates on loadChatConfig().confirmCloseSession — mock it so each test
// controls the branch deterministically (no localStorage dependency).
const cfgMock = vi.hoisted(() => ({ loadChatConfig: vi.fn(() => ({ confirmCloseSession: false })) }))
vi.mock('../pages/chat/ChatSettings', () => cfgMock)

import { store } from '../store'
import { sseSlots, sseSlotPatch, markSlotUnread, updateSlotPin } from '../store/dashboardSlice'
import { useSessionActions } from '../hooks/useSessionActions'

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
  cfgMock.loadChatConfig.mockReturnValue({ confirmCloseSession: false })
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  store.dispatch(sseSlots([]))
})

describe('useSessionActions', () => {
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

  it('togglePin reconciles from the PATCH answer without re-reading the slot list', async () => {
    mocks.setSlotPin.mockResolvedValue({ ok: true, pinned: true })
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalledWith(SLOT, true))
    await waitFor(() => expect(slotOf()?.pinned).toBe(true))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(mocks.chatSlots).not.toHaveBeenCalled()
  })

  it('togglePin re-reads the slot list after a stale full frame reverts the row', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: true } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // A list serialized before the PATCH committed lands while it is in flight.
    act(() => { store.dispatch(sseSlots([{ key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false }])) })
    expect(slotOf()?.pinned).toBe(false)
    await act(async () => { answer({ ok: true, pinned: true }) })
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(slotOf()?.pinned).toBe(true))
  })

  it('togglePin keeps a later full-frame unpin over the PATCH answer', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    mocks.chatSlots.mockResolvedValue([
      { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false } as ChatSlot,
    ])
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // Another tab's full snapshot unpins after this tab's PATCH committed.
    act(() => { store.dispatch(sseSlots([{ key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '', pinned: false }])) })
    await act(async () => { answer({ ok: true, pinned: true }) })
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(slotOf()?.pinned).toBe(false))
  })

  it('togglePin keeps a newer single-row write over its own answer', async () => {
    let answer: (value: unknown) => void = () => {}
    mocks.setSlotPin.mockReturnValue(new Promise(resolve => { answer = resolve }))
    seed(false)
    const a = renderActions()
    act(() => a.current.togglePin(SLOT))
    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalled())
    // Another tab unpinned after this PATCH committed; its slot_patch arrives
    // before this tab's HTTP answer.
    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: SLOT, pinned: false }] })) })
    await act(async () => { answer({ ok: true, pinned: true }) })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(slotOf()?.pinned).toBe(false)
    expect(mocks.chatSlots).not.toHaveBeenCalled()
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
