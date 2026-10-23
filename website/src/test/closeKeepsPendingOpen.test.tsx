/**
 * Closing the active chat while a create or a sidebar Fork is pending is not
 * the user choosing another session. The close lands the view on the closed
 * row's sidebar neighbour, and the pending request must still open its new
 * session when it resolves (`forkMayOpen`).
 *
 * A real switch made during the request still wins: a user who opened another
 * session and then closed it stays on the close landing.
 *
 * The real `createSlot` and `deleteSlot` thunks run, with the create and fork
 * responses held on deferred promises.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), forkChatSlot: vi.fn(), deleteChatSlot: vi.fn(), dashboardConfig: vi.fn(), chatSlotDetail: vi.fn() }))
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

import { store } from '../store'
import { removeSlotOptimistic, setSidebarOrder, sseSlots } from '../store/dashboardSlice'
import { createSlot, deleteSlot, setActiveSlot } from '../store/chatSlice'
import { useSessionActions } from '../hooks/useSessionActions'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { MemoryRouter } from 'react-router-dom'

const ACTIVE = 'chat-close-active'
const NEIGHBOUR = 'chat-close-neighbour'
const ROW = 'chat-close-row'
const NEW_KEY = 'chat-close-new'
const ONLY = 'chat-close-only'

function deferred(fn: ReturnType<typeof vi.fn>, value: unknown) {
  let resolve!: (v: unknown) => void
  fn.mockReturnValue(new Promise(r => { resolve = r }))
  return () => resolve(value)
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  return <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
}

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)

beforeEach(() => {
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ACTIVE), slot(NEIGHBOUR), slot(ROW)]))
  store.dispatch(setSidebarOrder([ACTIVE, NEIGHBOUR, ROW]))
  store.dispatch(setActiveSlot(ACTIVE))
  mocks.deleteChatSlot.mockResolvedValue({})
  mocks.dashboardConfig.mockResolvedValue({})
  mocks.chatSlotDetail.mockResolvedValue([])
})
afterEach(() => {
  vi.clearAllMocks()
  // A landed create is kept through slot frames that omit it, so drop it
  // here or it would be the close landing in the next test.
  store.dispatch(removeSlotOptimistic(NEW_KEY))
  store.dispatch(setActiveSlot(null))
  store.dispatch(setSidebarOrder([]))
  store.dispatch(sseSlots([]))
})

describe('closing the active chat while a create is pending', () => {
  it('opens the new chat once the create lands', async () => {
    const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    await store.dispatch(deleteSlot(ACTIVE))
    expect(activeSlot()).toBe(NEIGHBOUR)
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.lastCreatedActivation?.slot).toBe(NEW_KEY)
  })

  it('retires the landing load, so its late answer does not reach the new chat', async () => {
    const answer = deferred(mocks.chatSlotDetail, { key: NEIGHBOUR, messages: [{ role: 'user', content: 'neighbour row' }], running: false, total: 1 })
    const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    // The close waits for its landing's load, which is held, so it is not awaited yet.
    const closed = store.dispatch(deleteSlot(ACTIVE))
    await vi.waitFor(() => expect(mocks.chatSlotDetail).toHaveBeenCalledWith(NEIGHBOUR, expect.anything()))
    expect(activeSlot()).toBe(NEIGHBOUR)
    expect(store.getState().chat.slotLoading).toBe(true)
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.slotLoading).toBe(false)
    answer()
    await closed
    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.messages).toEqual([])
    expect(store.getState().chat.slotLoading).toBe(false)
  })

  it('keeps the new chat\'s run state when the landing load fails late', async () => {
    let fail!: (e: unknown) => void
    mocks.chatSlotDetail.mockReturnValue(new Promise((_, reject) => { fail = reject }))
    const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    const closed = store.dispatch(deleteSlot(ACTIVE))
    await vi.waitFor(() => expect(mocks.chatSlotDetail).toHaveBeenCalledWith(NEIGHBOUR, expect.anything()))
    land()
    await created
    expect(activeSlot()).toBe(NEW_KEY)
    store.dispatch({ type: 'chat/setSlotRunning', payload: true })
    fail(new Error('landing load failed'))
    await closed

    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.slotRunning).toBe(true)
  })

  it('opens the new chat when the closed chat was the only one listed', async () => {
    store.dispatch(sseSlots([{ key: ONLY, title: ONLY, messages: 2, running: false, folder_id: '' }]))
    store.dispatch(setSidebarOrder([ONLY]))
    store.dispatch(setActiveSlot(ONLY))
    const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    await store.dispatch(deleteSlot(ONLY))
    expect(activeSlot()).toBeNull()
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
  })

  it('opens the new chat after a keyboard close of the active chat', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderHook(
        () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
        { wrapper: ({ children }) => wrapper({ children: <MemoryRouter>{children}</MemoryRouter> }) },
      )
      const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
      const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
      await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
      act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyW', altKey: true, shiftKey: true, cancelable: true, bubbles: true })) })
      await waitFor(() => expect(mocks.deleteChatSlot).toHaveBeenCalledWith(ACTIVE))
      await waitFor(() => expect(activeSlot()).toBe(NEIGHBOUR))
      await act(async () => { land(); await created })

      expect(activeSlot()).toBe(NEW_KEY)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('leaves the user on the close landing when they opened another session first', async () => {
    const land = deferred(mocks.createChatSlot, { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(ROW))
    await store.dispatch(deleteSlot(ROW))
    const landing = activeSlot()
    expect(landing).not.toBe(ROW)
    land()
    await created

    expect(listed(NEW_KEY)).toBe(true)
    expect(activeSlot()).toBe(landing)
    expect(store.getState().chat.lastCreatedActivation).toBeNull()
  })
})

describe('closing the active chat while a sidebar Fork of another row is pending', () => {
  it('opens the child once the fork lands', async () => {
    const land = deferred(mocks.forkChatSlot, { ok: true, key: NEW_KEY, title: 'Fork of row' })
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(ROW))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(ROW))
    await act(async () => { await store.dispatch(deleteSlot(ACTIVE)) })
    expect(activeSlot()).toBe(NEIGHBOUR)
    await act(async () => { land() })

    await waitFor(() => expect(onOpened).toHaveBeenCalledWith(NEW_KEY))
    expect(activeSlot()).toBe(NEW_KEY)
  })

  it('leaves the user on the close landing when they opened another session first', async () => {
    const land = deferred(mocks.forkChatSlot, { ok: true, key: NEW_KEY, title: 'Fork of row' })
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(ROW))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(ROW))
    act(() => { store.dispatch(setActiveSlot(NEIGHBOUR)) })
    await act(async () => { await store.dispatch(deleteSlot(NEIGHBOUR)) })
    const landing = activeSlot()
    expect(landing).not.toBe(NEIGHBOUR)
    await act(async () => { land() })

    await waitFor(() => expect(listed(NEW_KEY)).toBe(true))
    expect(activeSlot()).toBe(landing)
    expect(onOpened).not.toHaveBeenCalled()
  })
})
