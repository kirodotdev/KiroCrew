/**
 * A fork that may open its child counts as pending (`pendingForkOpens`) from
 * the click until the child is active or the fork failed. The chat page reads
 * the count to hold its own auto-select while the source has left the list,
 * so the count must not drop before the switch to the child, and every
 * outcome must bring it back to zero. Both dashboard fork callers are
 * covered: the sidebar row's Fork (useSessionActions' duplicate) and a
 * transcript row's "Fork conversation from here" (useForkSession).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ forkChatSlot: vi.fn(), dashboardConfig: vi.fn(), chatSlotDetail: vi.fn() }))
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
import { dropActiveSlot, forkOpenSettled, setActiveSlot, switchSlot } from '../store/chatSlice'
import { useSessionActions } from '../hooks/useSessionActions'
import { useForkSession } from '../pages/useChatPageSlotSwitches'

const SOURCE = 'chat-pending-source'
const OTHER = 'chat-pending-other'
const CHILD = 'chat-pending-child'

function deferredFork() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  mocks.forkChatSlot.mockReturnValue(new Promise((res, rej) => { resolve = res; reject = rej }))
  return {
    land: () => resolve({ ok: true, key: CHILD, title: 'Fork of source' }),
    refuse: () => resolve({ ok: false, error: 'refused', code: 'fork_source_deleted' }),
    fail: () => reject(new Error('network down')),
  }
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  return <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
}

const pending = () => store.getState().chat.pendingForkOpens ?? 0
const activeSlot = () => store.getState().chat.activeSlot

/** Every store change while the fork lands: was the fork still pending
 *  whenever no chat was active? A gap would let the chat page auto-open
 *  another session between the response and the switch. */
function watchGap() {
  const gaps: string[] = []
  const unsubscribe = store.subscribe(() => {
    if (activeSlot() === null && pending() === 0) gaps.push('no chat and nothing pending')
  })
  return { gaps, unsubscribe }
}

beforeEach(() => {
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(SOURCE), slot(OTHER)]))
  store.dispatch(setActiveSlot(SOURCE))
  mocks.dashboardConfig.mockResolvedValue({})
  mocks.chatSlotDetail.mockResolvedValue([])
  vi.spyOn(window, 'alert').mockImplementation(() => {})
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  // The store is shared by every case: a case that failed before its fork
  // settled must not leave the next one starting with a raised count.
  while (pending() > 0) store.dispatch(forkOpenSettled())
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('sidebar Fork', () => {
  it('stays pending while the source is gone, until the child is active', async () => {
    const fork = deferredFork()
    const { result } = renderHook(() => useSessionActions('personal'), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    expect(pending()).toBe(1)
    // The chat page lost the source and cleared its selection.
    act(() => { store.dispatch(dropActiveSlot()) })
    const watch = watchGap()
    await act(async () => { fork.land() })
    watch.unsubscribe()

    await waitFor(() => expect(activeSlot()).toBe(CHILD))
    expect(pending()).toBe(0)
    expect(watch.gaps).toEqual([])
  })

  it('stops being pending when the fork fails', async () => {
    const fork = deferredFork()
    const { result } = renderHook(() => useSessionActions('personal'), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(pending()).toBe(1))
    await act(async () => { fork.fail() })

    await waitFor(() => expect(pending()).toBe(0))
  })

  it('stops being pending when the user opened another session meanwhile', async () => {
    const fork = deferredFork()
    const { result } = renderHook(() => useSessionActions('personal'), { wrapper })

    act(() => result.current.duplicate(SOURCE))
    await waitFor(() => expect(pending()).toBe(1))
    await act(async () => { await store.dispatch(switchSlot(OTHER)) })
    await act(async () => { fork.land() })

    await waitFor(() => expect(pending()).toBe(0))
    expect(activeSlot()).toBe(OTHER)
  })

  // The mutation's release lives in its one `onSettled`. A second
  // `onSettled` key added to the same options object (for another
  // per-mutation cleanup) silently replaces it, and the count then stays
  // raised for the life of the window, holding every later auto-select.
  // Any such merge must fold both bodies into one callback; this case fails
  // if the release stops running on any outcome.
  it('returns to zero after every outcome of a run of forks', async () => {
    const { result } = renderHook(() => useSessionActions('personal'), { wrapper })
    const outcomes: [string, (f: ReturnType<typeof deferredFork>) => void][] = [
      ['lands', f => f.land()],
      ['answers not ok', f => f.refuse()],
      ['fails', f => f.fail()],
      ['lands again', f => f.land()],
    ]
    for (const [label, settle] of outcomes) {
      store.dispatch(setActiveSlot(SOURCE))
      const fork = deferredFork()
      act(() => result.current.duplicate(SOURCE))
      await waitFor(() => expect(pending(), `${label}: raised`).toBe(1))
      await act(async () => { settle(fork) })
      await waitFor(() => expect(pending(), `${label}: released`).toBe(0))
    }
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

  it('stays pending while the source is gone, until the child is active', async () => {
    const fork = deferredFork()
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    expect(pending()).toBe(1)
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    act(() => { store.dispatch(dropActiveSlot()) })
    const watch = watchGap()
    await act(async () => { fork.land(); await forked })
    watch.unsubscribe()

    expect(activeSlot()).toBe(CHILD)
    expect(pending()).toBe(0)
    expect(watch.gaps).toEqual([])
  })

  it('stops being pending once the child is active, before its transcript loads', async () => {
    const fork = deferredFork()
    let loadTranscript!: (v: unknown) => void
    mocks.chatSlotDetail.mockReturnValue(new Promise(res => { loadTranscript = res }))
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    await act(async () => { fork.land() })

    await waitFor(() => expect(activeSlot()).toBe(CHILD))
    expect(mocks.chatSlotDetail).toHaveBeenCalled()
    expect(pending()).toBe(0)
    await act(async () => { loadTranscript([]); await forked })
    expect(pending()).toBe(0)
  })

  it('stops being pending when the fork fails', async () => {
    const fork = deferredFork()
    const { result, showActionError } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    await act(async () => { fork.fail(); await forked })

    expect(pending()).toBe(0)
    expect(showActionError).toHaveBeenCalled()
  })

  it('stops being pending when the user opened another session meanwhile', async () => {
    const fork = deferredFork()
    const { result } = renderFork()

    let forked!: Promise<void>
    act(() => { forked = result.current.handleFork(1) })
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalled())
    await act(async () => { await store.dispatch(switchSlot(OTHER)) })
    await act(async () => { fork.land(); await forked })

    expect(pending()).toBe(0)
    expect(activeSlot()).toBe(OTHER)
  })
})
