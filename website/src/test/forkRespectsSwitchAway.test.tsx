/**
 * A fork that resolves after the user opened another session must leave the
 * user there. Both dashboard fork callers are covered: the sidebar row's Fork
 * (useSessionActions' duplicate) and a transcript row's "Fork conversation
 * from here" (useForkSession's handleFork). Each fork is held open on a
 * deferred response, the active session changes, then the response lands.
 * Going to another session and back to the source counts as a choice too.
 * A move the chat page makes on its own after losing the source does not.
 *
 * The un-switched case is pinned too: with the user still on the source, the
 * fork opens its child and the sidebar's onOpened runs (it exits Split View).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ forkChatSlot: vi.fn(), dashboardConfig: vi.fn(), resumeChatSlot: vi.fn() }))
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
import { sseSlots } from '../store/dashboardSlice'
import { dropActiveSlot, resumeFromHistory, setActiveSlot, switchSlot } from '../store/chatSlice'
import { useSessionActions } from '../hooks/useSessionActions'
import { useForkSession } from '../pages/useChatPageSlotSwitches'

const SOURCE = 'chat-fork-source'
const OTHER = 'chat-fork-other'
const CHILD = 'chat-fork-child'

function deferredFork() {
  let resolve!: (v: unknown) => void
  mocks.forkChatSlot.mockReturnValue(new Promise(r => { resolve = r }))
  return () => resolve({ ok: true, key: CHILD, title: 'Fork of source' })
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  return <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
}

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)

beforeEach(() => {
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(SOURCE), slot(OTHER)]))
  store.dispatch(setActiveSlot(SOURCE))
  mocks.dashboardConfig.mockResolvedValue({})
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('sidebar Fork', () => {
  it('leaves the user on the session they opened while the fork ran', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    await act(async () => { land() })

    await waitFor(() => expect(listed(CHILD)).toBe(true))
    expect(activeSlot()).toBe(OTHER)
    expect(onOpened).not.toHaveBeenCalled()
  })

  it('leaves the user on the source when they went away and came back while the fork ran', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    act(() => { store.dispatch(setActiveSlot(OTHER)); store.dispatch(setActiveSlot(SOURCE)) })
    await act(async () => { land() })

    await waitFor(() => expect(listed(CHILD)).toBe(true))
    expect(activeSlot()).toBe(SOURCE)
    expect(onOpened).not.toHaveBeenCalled()
  })

  // The chat page lost the source from its list (closed in another tab, or a
  // frame dropped it) and moved the view itself: the user made no choice.
  it('opens the child when the page itself moved off the source while the fork ran', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    act(() => { store.dispatch(dropActiveSlot()); void store.dispatch(switchSlot({ key: OTHER, automatic: true })) })
    expect(activeSlot()).toBe(OTHER)
    await act(async () => { land() })

    await waitFor(() => expect(onOpened).toHaveBeenCalledWith(CHILD))
  })

  it('opens the child when the page cleared the selection and auto-selected the source again', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    act(() => { store.dispatch(dropActiveSlot()); void store.dispatch(switchSlot({ key: SOURCE, automatic: true })) })
    await act(async () => { land() })

    await waitFor(() => expect(onOpened).toHaveBeenCalledWith(CHILD))
  })

  it('leaves the user where they went after the page moved the view itself', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    act(() => { store.dispatch(dropActiveSlot()); void store.dispatch(switchSlot({ key: OTHER, automatic: true })) })
    act(() => { store.dispatch(setActiveSlot(SOURCE)) })
    await act(async () => { land() })

    await waitFor(() => expect(listed(CHILD)).toBe(true))
    expect(activeSlot()).toBe(SOURCE)
    expect(onOpened).not.toHaveBeenCalled()
  })

  it('opens the child, already listed, when the user stayed on the source', async () => {
    const land = deferredFork()
    const onOpened = vi.fn()
    const { result } = renderHook(() => useSessionActions('personal', { onOpened }), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    // No slots frame arrives in this test: the fork response alone must put the
    // child in the slot list before the view moves to it, or the chat page
    // drops a selection its list lacks.
    const listedWhenOpened: boolean[] = []
    const unsubscribe = store.subscribe(() => { if (activeSlot() === CHILD) listedWhenOpened.push(listed(CHILD)) })
    await act(async () => { land() })
    unsubscribe()

    await waitFor(() => expect(onOpened).toHaveBeenCalledWith(CHILD))
    expect(listedWhenOpened.length).toBeGreaterThan(0)
    expect(listedWhenOpened.every(Boolean)).toBe(true)
  })
})

describe('transcript Fork from here', () => {
  function renderFork() {
    const showActionError = vi.fn()
    const { result } = renderHook(
      () => useForkSession({ activeSlot: SOURCE, dispatch: store.dispatch, showActionError }),
      { wrapper },
    )
    return { result, showActionError }
  }

  // A resume clicked before the fork answers after the fork opened its
  // child: the fork is the later click, so the child keeps the view.
  it('keeps the child when a resume clicked before the fork answers after it', async () => {
    const land = deferredFork()
    let landResume!: (v: unknown) => void
    mocks.resumeChatSlot.mockReturnValue(new Promise(r => { landResume = r }))
    const { result } = renderFork()

    let resumed!: Promise<unknown>
    act(() => { resumed = store.dispatch(resumeFromHistory({ key: 'chat-fork-old', title: 'Old' })) })
    await waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    await act(async () => { land(); await forked })
    await waitFor(() => expect(activeSlot()).toBe(CHILD))
    await act(async () => { landResume({ ok: true, key: 'chat-fork-old', mode: '', messages: [] }); await resumed })

    expect(activeSlot()).toBe(CHILD)
  })

  it('leaves the user on the session they opened while the fork ran', async () => {
    const land = deferredFork()
    const { result, showActionError } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    await act(async () => { land(); await forked })

    expect(listed(CHILD)).toBe(true)
    expect(activeSlot()).toBe(OTHER)
    expect(showActionError).not.toHaveBeenCalled()
  })

  it('leaves the user on the source when they went away and came back while the fork ran', async () => {
    const land = deferredFork()
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    act(() => { store.dispatch(setActiveSlot(OTHER)); store.dispatch(setActiveSlot(SOURCE)) })
    await act(async () => { land(); await forked })

    expect(listed(CHILD)).toBe(true)
    expect(activeSlot()).toBe(SOURCE)
  })

  it('opens the child when the page itself moved off the source while the fork ran', async () => {
    const land = deferredFork()
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    act(() => { store.dispatch(dropActiveSlot()); void store.dispatch(switchSlot({ key: OTHER, automatic: true })) })
    const seen: (string | null)[] = []
    const unsubscribe = store.subscribe(() => { seen.push(activeSlot()) })
    await act(async () => { land(); await forked })
    unsubscribe()

    expect(seen).toContain(CHILD)
  })

  it('opens the child when the user stayed on the source', async () => {
    const land = deferredFork()
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    // Read at the switch itself: its pending step makes the child active, and
    // what the mocked detail fetch does afterwards is not this test's subject.
    const seen: (string | null)[] = []
    const unsubscribe = store.subscribe(() => { seen.push(activeSlot()) })
    await act(async () => { land(); await forked })
    unsubscribe()

    expect(listed(CHILD)).toBe(true)
    expect(seen).toContain(CHILD)
  })
})
