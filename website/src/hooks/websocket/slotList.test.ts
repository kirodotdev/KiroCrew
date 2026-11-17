/** `applySlotsDiff` rebuilds the list a `slots_diff` frame describes from the
 *  list the tab holds, or refuses when the frame was built on another one. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import type { ChatSlot } from '../../types'
import { store } from '../../store'
import { api } from '../../api/client'
import { clearSlotState, setActiveSlot } from '../../store/chatSlice'
import { slotMessagesQueryKey } from '../../api/slotMessagesQuery'
import type { StreamBuffers } from './streamBuffers'
import { applySlotsDiff, useSlotListSync, type SlotListSync } from './slotList'
import { takeIdleRowRefresh } from './idleRowRefresh'

vi.mock('../../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),
  },
}))

const rowA = { key: 'slot-a', title: 'A', agent: 'kirocrew', running: false } as ChatSlot
const rowB = { key: 'slot-b', title: 'B', agent: 'kirocrew', running: false } as ChatSlot
const rowC = { key: 'slot-c', title: 'C', agent: 'kirocrew', running: false } as ChatSlot

describe('applySlotsDiff', () => {
  const held = { seq: 4, rows: [rowA, rowB, rowC], envelope: {} }

  it('replaces changed rows whole, keeps unchanged rows as held, and drops removed rows', () => {
    const rowA2 = { ...rowA, title: 'A2' }
    const rows = applySlotsDiff(held, {
      base: 4, seq: 5,
      rows: [{ key: 'slot-a', row: rowA2 }, { key: 'slot-c', row: { key: 'slot-c', title: 'C2' } as ChatSlot }],
      removed: ['slot-b'],
    })
    expect(JSON.stringify(rows)).toBe(JSON.stringify([rowA2, { key: 'slot-c', title: 'C2' }]))
    expect(applySlotsDiff(held, { base: 4, seq: 5, rows: [] })![1]).toBe(rowB)
  })

  it('refuses a delta on another base or naming a row it does not hold', () => {
    expect(applySlotsDiff(held, { base: 3, seq: 5, rows: [] })).toBeNull()
    expect(applySlotsDiff(held, { base: 4, seq: 5, rows: [{ key: 'slot-z', row: rowA }] })).toBeNull()
  })
})

/** A split-view member pane or a Crew Members thread is on screen but is not
 *  the active slot. When its turn's frames and `chat_done` are lost, the idle
 *  `slots` row that ends the turn must warm that pane (#38010). */
describe('useSlotListSync: an on-screen pane that is not the active slot', () => {
  const SLOT = 'slot-active'
  const row = (over: Partial<ChatSlot> = {}) =>
    ({ key: SLOT, title: 'T', agent: 'kirocrew', running: false, messages: 2, last_message: 'first reply', ...over }) as ChatSlot
  const buffers = { hasPendingChunks: () => false, flushChunks: () => {} } as unknown as StreamBuffers
  let queryClient: QueryClient

  beforeEach(() => {
    vi.mocked(api.chatSlotDetail).mockClear()
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(SLOT))
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })

  afterEach(() => {
    queryClient.clear()
    store.dispatch(setActiveSlot(null))
  })

  const deliver = (sync: SlotListSync, rows: ChatSlot[]) => {
    const msg = { type: 'slots', data: rows }
    act(() => { sync.onSlots(msg, rows, JSON.stringify(msg)) })
  }

  const mountSync = () =>
    renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current


  it('warms a non-active pane on screen from the idle row of a turn whose _done was lost (#38010)', async () => {
    const MEMBER = 'slot-member'
    const memberObserver = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(MEMBER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    })
    const stop = memberObserver.subscribe(() => {})
    const member = (over: Partial<ChatSlot> = {}) =>
      row({ key: MEMBER, last_message: 'member reply', turn: 1, turn_gen: 'gen-a', ...over } as Partial<ChatSlot>)
    const sync = mountSync()
    deliver(sync, [row(), member()])
    // The connection's first list warms the pane once; count from here.
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    vi.mocked(api.chatSlotDetail).mockClear()
    // The member's turn ran and ended; no run, row or `_done` frame arrived.
    deliver(sync, [row(), member({ messages: 4, last_message: 'patrol report', turn: 2 } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.chatSlotDetail).mock.calls[0][0]).toBe(MEMBER)
    // The active pane is untouched, and the same row again reads nothing.
    deliver(sync, [row(), member({ messages: 4, last_message: 'patrol report', turn: 2, title: 'x' } as Partial<ChatSlot>)])
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    stop()
  })

  it('warms a non-active pane for a newer turn that kept the same row count and preview (regenerate)', async () => {
    const MEMBER = 'slot-member-regen'
    const stop = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(MEMBER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    }).subscribe(() => {})
    const member = (over: Partial<ChatSlot> = {}) =>
      row({ key: MEMBER, messages: 4, last_message: 'same preview', turn: 1, turn_gen: 'gen-a', ...over } as Partial<ChatSlot>)
    const sync = mountSync()
    deliver(sync, [row(), member()])
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    vi.mocked(api.chatSlotDetail).mockClear()
    deliver(sync, [row(), member({ turn: 2 } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.chatSlotDetail).mock.calls[0][0]).toBe(MEMBER)
    stop()
  })

  it('drops the record of a failed warm of a non-active pane, so its _done warms again', async () => {
    const MEMBER = 'slot-member-retry'
    const memberObserver = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(MEMBER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    })
    const stop = memberObserver.subscribe(() => {})
    const member = (over: Partial<ChatSlot> = {}) =>
      row({ key: MEMBER, last_message: 'member reply', turn: 1, turn_gen: 'gen-a', ...over } as Partial<ChatSlot>)
    const sync = mountSync()
    deliver(sync, [row(), member()])
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    vi.mocked(api.chatSlotDetail).mockClear()
    vi.mocked(api.chatSlotDetail).mockRejectedValueOnce(new Error('gateway down'))
    vi.useFakeTimers()
    try {
      deliver(sync, [row(), member({ messages: 4, last_message: 'patrol report', turn: 2 } as Partial<ChatSlot>)])
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(0)
      // The record is gone, so the `_done` for the same turn would warm again.
      expect(takeIdleRowRefresh(MEMBER, { turn: 2, turn_gen: 'gen-a' })).toBe(false)
      // No timer of its own retries the warm.
      await vi.advanceTimersByTimeAsync(60_000)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
      stop()
    }
  })
})

/** A queued entry's held verdict rides only the queue a slot read returns, so
 *  a `slots` frame whose held depth moved for an open pane re-reads that pane. */
describe('useSlotListSync: held depth moved on an open pane', () => {
  const SLOT = 'slot-held'
  const row = (over: Partial<ChatSlot> = {}) =>
    ({ key: SLOT, title: 'T', agent: 'kirocrew', running: false, messages: 2, last_message: 'x', queue_depth: 1, ...over }) as ChatSlot
  const buffers = { hasPendingChunks: () => false, flushChunks: () => {} } as unknown as StreamBuffers
  let queryClient: QueryClient

  beforeEach(() => {
    vi.mocked(api.chatSlotDetail).mockClear()
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(SLOT))
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })

  afterEach(() => {
    queryClient.clear()
    store.dispatch(setActiveSlot(null))
  })

  const deliver = (sync: SlotListSync, rows: ChatSlot[]) => {
    const msg = { type: 'slots', data: rows }
    act(() => { sync.onSlots(msg, rows, JSON.stringify(msg)) })
  }

  it('re-reads the active pane when its held depth changes, and not on a repeat', () => {
    const sync = renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current
    deliver(sync, [row({ queue_held_depth: 0 })])
    vi.mocked(api.chatSlotDetail).mockClear()
    deliver(sync, [row({ queue_held_depth: 1, last_message: 'y' })])
    expect(vi.mocked(api.chatSlotDetail).mock.calls.some(c => c[0] === SLOT)).toBe(true)
    vi.mocked(api.chatSlotDetail).mockClear()
    deliver(sync, [row({ queue_held_depth: 1, last_message: 'z' })])
    expect(vi.mocked(api.chatSlotDetail).mock.calls.some(c => c[0] === SLOT)).toBe(false)
  })

  it('warms another open pane once when its held depth changes with no turn ending there', async () => {
    const OTHER = 'slot-held-other'
    const observer = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(OTHER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    })
    const stop = observer.subscribe(() => {})
    const other = (over: Partial<ChatSlot> = {}) => row({ key: OTHER, turn: 1, turn_gen: 'g', ...over } as Partial<ChatSlot>)
    const sync = renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current
    deliver(sync, [row(), other({ queue_held_depth: 0 })])
    await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    vi.mocked(api.chatSlotDetail).mockClear()
    deliver(sync, [row(), other({ queue_held_depth: 1 })])
    await vi.waitFor(() => expect(vi.mocked(api.chatSlotDetail).mock.calls.filter(c => c[0] === OTHER)).toHaveLength(1))
    stop()
  })
})
