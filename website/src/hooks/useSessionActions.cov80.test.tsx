/**
 * useSessionActions — the surface-agnostic session actions shared by every menu.
 *
 * Each action reads its prior state from the store at CALL time and rolls back
 * on failure, so the tests drive the real global store (the hook reads
 * `store.getState()` directly) and assert both the optimistic write and the
 * rollback.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const apiMock = vi.hoisted(() => ({
  forkChatSlot: vi.fn(),
  setSlotPin: vi.fn(),
  setSlotQueuePriority: vi.fn(),
  chatSlots: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => ({
  ApiError: (await importOriginal<typeof import('../api/client')>()).ApiError,
  api: apiMock,
}))

const copySessionLink = vi.hoisted(() => vi.fn())
vi.mock('../utils/shareUrl', () => ({ copySessionLink }))

const moveSlotToFolder = vi.hoisted(() => vi.fn())
vi.mock('./useMoveSlotToFolder', () => ({ useMoveSlotToFolder: () => moveSlotToFolder }))

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: true }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

const deleteSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/deleteSlot', payload: key })))
const switchSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/switchSlot', payload: key })))
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSlot,
  switchSlot,
}))

import { store } from '../store'
import { sseSlotPatch, sseSlots, markSlotUnread, markSlotRead, setSidebarOrder, updateSlot, updateSlotPin } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { pinMutationKeysInFlight, useSessionActions } from './useSessionActions'

const KEY = 'zzq-slot-1'

function slots(patch: Partial<ChatSlot> = {}) {
  store.dispatch(sseSlots([{ key: KEY, messages: 0, running: false, ...patch } as ChatSlot]))
}

function harness(mode?: string, opts: { sessionSort?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  // The saved session sort is read from the shared config cache.
  if (opts.sessionSort !== undefined) client.setQueryData(['kirocrewConfig'], { dashboard: { session_sort: opts.sessionSort } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <Provider store={store}>{children}</Provider>
    </QueryClientProvider>
  )
  return { client, ...renderHook(() => useSessionActions(mode), { wrapper }) }
}

const slot = () => store.getState().dashboard.slots.find((s) => s.key === KEY)

beforeEach(() => {
  localStorage.clear()
  apiMock.forkChatSlot.mockReset().mockResolvedValue({ ok: true, key: 'zzq-forked' })
  apiMock.setSlotPin.mockReset().mockResolvedValue({ ok: true })
  apiMock.setSlotQueuePriority.mockReset().mockResolvedValue({ ok: true })
  apiMock.chatSlots.mockReset().mockImplementation(async () => store.getState().dashboard.slots)
  copySessionLink.mockClear()
  moveSlotToFolder.mockClear()
  deleteSlot.mockClear()
  switchSlot.mockClear()
  chatConfig.confirmCloseSession = true
  store.dispatch(sseSlots([]))
  store.dispatch(markSlotRead(KEY))
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('duplicate', () => {
  it('forks the slot and switches to the new one', async () => {
    slots()
    const { result } = harness()
    act(() => result.current.duplicate(KEY))
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalledWith(KEY))
    await waitFor(() => expect(switchSlot).toHaveBeenCalledWith('zzq-forked'))
  })

  it('switches nowhere when the fork is refused', async () => {
    apiMock.forkChatSlot.mockResolvedValue({ ok: false })
    slots()
    const { result } = harness()
    act(() => result.current.duplicate(KEY))
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalled())
    expect(switchSlot).not.toHaveBeenCalled()
  })
})

describe('toggleRead', () => {
  it('marks an unread session read', () => {
    slots()
    store.dispatch(markSlotUnread(KEY))
    const { result } = harness()
    act(() => result.current.toggleRead(KEY))
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
  })

  it('marks a read session unread', () => {
    slots()
    const { result } = harness()
    act(() => result.current.toggleRead(KEY))
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
  })
})

describe('togglePin', () => {
  it('pins optimistically and persists the new value', async () => {
    slots({ pinned: false })
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(true)
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
  })

  it('unpins a pinned session', async () => {
    store.dispatch(sseSlots([
      { key: 'before', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: 'after', messages: 0, running: false, pinned: true } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['before', KEY, 'after']))
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(false)
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, false))
  })

  it('rolls the pin back when the write fails', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('zzq offline'))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    slots({ pinned: false })
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(true)
    expect(pinMutationKeysInFlight()).toEqual([KEY])
    await waitFor(() => expect(slot()?.pinned).toBe(false))
    expect(pinMutationKeysInFlight()).toEqual([])
  })

  it('rolls back a rejected pin after an unrelated slot update', async () => {
    let rejectPin: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPin = reject }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    slots({ pinned: false, title: 'before' })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
    act(() => store.dispatch(updateSlot({ key: KEY, title: 'after' })))
    await act(async () => { rejectPin(new Error('zzq rejected')); await Promise.resolve() })

    await waitFor(() => expect(slot()?.pinned).toBe(false))
    expect(slot()?.title).toBe('after')
  })

  it('commits overlapping outcomes atomically when success arrives before failure', async () => {
    const other = 'zzq-slot-2'
    let rejectFirst: (reason?: unknown) => void = () => undefined
    let resolveSecond: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve }))
    store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: other, messages: 0, running: false, pinned: true } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder([KEY, other]))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: other, messages: 0, running: false, pinned: false } as ChatSlot,
    ])
    const { result } = harness()

    act(() => {
      result.current.togglePin(KEY)
      result.current.togglePin(other)
    })

    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    expect(apiMock.setSlotPin).toHaveBeenNthCalledWith(1, KEY, false)
    expect(apiMock.setSlotPin).toHaveBeenNthCalledWith(2, other, false)
    expect(store.getState().dashboard.slots.find(s => s.key === KEY)?.pinned).toBe(false)
    expect(store.getState().dashboard.slots.find(s => s.key === other)?.pinned).toBe(false)

    await act(async () => { resolveSecond({ ok: true }); await Promise.resolve() })

    await act(async () => { rejectFirst(new Error('zzq offline')); await Promise.resolve() })

    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === KEY)?.pinned).toBe(true))
    expect(store.getState().dashboard.slots.find(s => s.key === other)?.pinned).toBe(false)
  })

  it('appends an authoritative partial-success pin instead of reviving stale rank', async () => {
    apiMock.setSlotPin
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq unpin rejected'))
    apiMock.chatSlots.mockResolvedValue([
      { key: 'a', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
    ])
    store.dispatch(sseSlots([
      { key: 'a', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['a', KEY]))
    const { result } = harness()

    act(() => {
      result.current.togglePin(KEY)
      result.current.togglePin(KEY)
    })

    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(slot()?.pinned).toBe(true))
  })

  it('serializes overlapping same-key writes and restores membership when both fail', async () => {
    let rejectFirst: (reason?: unknown) => void = () => undefined
    let rejectSecond: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSecond = reject }))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ])
    slots({ pinned: false })
    const { result } = harness()

    act(() => {
      result.current.togglePin(KEY)
      result.current.togglePin(KEY)
    })

    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(1))
    expect(apiMock.setSlotPin).toHaveBeenNthCalledWith(1, KEY, true)
    expect(slot()?.pinned).toBe(false)

    await act(async () => { rejectFirst(new Error('zzq first')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    expect(apiMock.setSlotPin).toHaveBeenNthCalledWith(2, KEY, false)
    await act(async () => { rejectSecond(new Error('zzq second')); await Promise.resolve() })

    await waitFor(() => expect(slot()?.pinned).toBe(false))
  })

  it('keeps the newer successful toggle when its snapshot resolves first', async () => {
    let resolveOlderSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.chatSlots
      .mockImplementationOnce(() => new Promise(resolve => { resolveOlderSnapshot = resolve }))
      .mockResolvedValueOnce([
        { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      ])
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalledTimes(1))

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(slot()?.pinned).toBe(false))

    await act(async () => {
      resolveOlderSnapshot([
        { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      ])
      await Promise.resolve()
    })

    expect(slot()?.pinned).toBe(false)
  })

  it('retries reconciliation when an authoritative slots frame arrives in flight', async () => {
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())

    act(() => store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ])))
    await act(async () => {
      resolveSnapshot([{ key: KEY, messages: 0, running: false, pinned: true } as ChatSlot])
      await Promise.resolve()
    })

    expect(apiMock.chatSlots).toHaveBeenCalledTimes(2)
    expect(slot()?.pinned).toBe(false)
  })

  it('bounds snapshot retries under continuous authoritative slot frames', async () => {
    apiMock.chatSlots.mockImplementation(async () => {
      store.dispatch(sseSlots([
        { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      ]))
      return [{ key: KEY, messages: 0, running: false, pinned: true } as ChatSlot]
    })
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(slot()?.pinned).toBe(false))
  })

  it('uses snapshot membership after a delayed pre-mutation slots frame', async () => {
    let resolveUnpin: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise(resolve => { resolveUnpin = resolve }))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ])
    slots({ pinned: true })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, false))
    // A delayed frame captured before the mutation must not masquerade as a
    // newer writer and restore stale membership/rank.
    act(() => store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
    ])))
    await act(async () => { resolveUnpin({ ok: true }); await Promise.resolve() })

    await waitFor(() => expect(slot()?.pinned).toBe(false))
  })

  it('keeps a newer authoritative unpin when an older pin completion arrives later', async () => {
    let resolvePin: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise(resolve => { resolvePin = resolve }))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, title: 'stale title', messages: 0, running: false, pinned: false } as ChatSlot,
    ])
    slots({ pinned: false, title: 'original title' })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
    // A newer cross-tab/server broadcast wins before the older request completes.
    act(() => store.dispatch(sseSlots([
      { key: KEY, title: 'newer title', messages: 0, running: false, pinned: false } as ChatSlot,
      { key: 'zzq-newer-slot', messages: 0, running: false, pinned: false } as ChatSlot,
    ])))
    await act(async () => { resolvePin({ ok: true }); await Promise.resolve() })

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    expect(slot()?.pinned).toBe(false)
    expect(slot()?.title).toBe('newer title')
    expect(store.getState().dashboard.slots.some(slot => slot.key === 'zzq-newer-slot')).toBe(true)
  })

  it('keeps an authoritative matching broadcast when PATCH and snapshot responses fail', async () => {
    let rejectPin: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPin = reject }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
    // The server committed and broadcast the pin, but the PATCH response is lost.
    // The equal value is not enough to identify this as our optimistic object.
    act(() => store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
    ])))
    await act(async () => { rejectPin(new Error('zzq response lost')); await Promise.resolve() })

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    expect(slot()?.pinned).toBe(true)
  })

  it('rolls back an owned failure when another key was superseded', async () => {
    const other = 'zzq-slot-2'
    let resolveFirst: (value: { ok: boolean }) => void = () => undefined
    let rejectSecond: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSecond = reject }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      { key: other, messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    const { result } = harness()

    act(() => {
      result.current.togglePin(KEY)
      result.current.togglePin(other)
    })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    act(() => store.dispatch(updateSlotPin({ key: KEY, pinned: false })))
    await act(async () => {
      resolveFirst({ ok: true })
      rejectSecond(new Error('zzq rejected'))
      await Promise.resolve()
    })

    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === other)?.pinned).toBe(false))
    expect(slot()?.pinned).toBe(false)
  })

  it('does not fallback-roll back a newer broadcast when the snapshot refetch fails', async () => {
    let resolvePin: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise(resolve => { resolvePin = resolve }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
    act(() => store.dispatch(sseSlots([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ])))
    await act(async () => { resolvePin({ ok: true }); await Promise.resolve() })

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    expect(slot()?.pinned).toBe(false)
  })
})

  it('preserves manual rank when an optimistic unpin is rejected', async () => {
    const other = 'zzq-slot-2'
    apiMock.setSlotPin.mockRejectedValue(new Error('zzq offline'))
    apiMock.chatSlots.mockResolvedValue([
      { key: 'before', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: other, messages: 0, running: false, pinned: true } as ChatSlot,
    ])
    store.dispatch(sseSlots([
      { key: 'before', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: other, messages: 0, running: false, pinned: true } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['before', KEY, other]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(false)

    await waitFor(() => expect(slot()?.pinned).toBe(true))
  })
describe('setQueuePriority', () => {
  it('writes the tier optimistically and persists it', async () => {
    slots({})
    const { result } = harness()
    act(() => result.current.setQueuePriority(KEY, 'high'))
    expect(slot()?.queue_priority).toBe('high')
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledWith(KEY, 'high'))
  })

  it('rolls the tier back and shows the localized retry line, not the gateway text', async () => {
    const { ApiError } = await import('../api/client')
    apiMock.setSlotQueuePriority.mockRejectedValue(new ApiError(503, 'zzq failed to save agent-queue priority', '{}'))
    slots({ queue_priority: 'low' })
    const { result } = harness()
    act(() => result.current.setQueuePriority(KEY, 'high'))
    expect(slot()?.queue_priority).toBe('high')
    await waitFor(() => expect(slot()?.queue_priority).toBe('low'))
    expect(result.current.queuePriorityError?.key).toBe(KEY)
    expect(result.current.queuePriorityError?.message).not.toMatch(/zzq|agent-queue/)
    expect(result.current.queuePriorityError?.message).toMatch(/Try again/)
  })

  it('shows the localized line, not raw exception text, for a transport failure', async () => {
    apiMock.setSlotQueuePriority.mockRejectedValue(new TypeError('Failed to fetch'))
    slots({ queue_priority: 'low' })
    const { result } = harness()
    act(() => result.current.setQueuePriority(KEY, 'high'))
    await waitFor(() => expect(result.current.queuePriorityError?.key).toBe(KEY))
    expect(result.current.queuePriorityError?.message).not.toMatch(/Failed to fetch/)
  })

  it('reconciles a transport failure from the authoritative slot snapshot', async () => {
    apiMock.setSlotQueuePriority.mockRejectedValue(new TypeError('Failed to fetch'))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, messages: 0, running: false, queue_priority: 'high' } as ChatSlot,
    ])
    slots({ queue_priority: 'low' })
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'medium'))
    expect(slot()?.queue_priority).toBe('medium')

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(slot()?.queue_priority).toBe('high'))
    expect(result.current.queuePriorityError?.key).toBe(KEY)
  })

  it('keeps a newer slot patch that lands during transport reconciliation', async () => {
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.setSlotQueuePriority.mockRejectedValue(new TypeError('Failed to fetch'))
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    slots({ queue_priority: 'low' })
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'medium'))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    act(() => store.dispatch(sseSlotPatch({ slots: [{ key: KEY, queue_priority: 'high' }] })))
    await act(async () => {
      resolveSnapshot([
        { key: KEY, messages: 0, running: false, queue_priority: 'low' } as ChatSlot,
      ])
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.queuePriorityError?.key).toBe(KEY))
    expect(slot()?.queue_priority).toBe('high')
  })

  it('keeps the optimistic tier when transport reconciliation also fails', async () => {
    apiMock.setSlotQueuePriority.mockRejectedValue(new TypeError('Failed to fetch'))
    apiMock.chatSlots.mockRejectedValue(new TypeError('Failed to fetch snapshot'))
    slots({ queue_priority: 'low' })
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'high'))

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(result.current.queuePriorityError?.key).toBe(KEY))
    expect(slot()?.queue_priority).toBe('high')
  })

  it('sends rapid picks in invocation order', async () => {
    let resolveFirst: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotQueuePriority
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ ok: true })
    slots({})
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'high'))
    act(() => result.current.setQueuePriority(KEY, 'low'))
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(1))
    expect(apiMock.setSlotQueuePriority).toHaveBeenNthCalledWith(1, KEY, 'high')

    await act(async () => { resolveFirst({ ok: true }); await Promise.resolve() })
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(2))
    expect(apiMock.setSlotQueuePriority).toHaveBeenNthCalledWith(2, KEY, 'low')
    expect(slot()?.queue_priority).toBe('low')
  })

  it('restores the confirmed baseline when two consecutive picks are refused', async () => {
    const { ApiError } = await import('../api/client')
    let rejectHigh: (reason?: unknown) => void = () => undefined
    let rejectLow: (reason?: unknown) => void = () => undefined
    apiMock.setSlotQueuePriority
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectHigh = reject }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectLow = reject }))
    slots({ queue_priority: 'medium' })
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'high'))
    act(() => result.current.setQueuePriority(KEY, 'low'))
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(1))
    await act(async () => { rejectHigh(new ApiError(409, 'zzq high refused', '{}')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(2))
    await act(async () => { rejectLow(new ApiError(409, 'zzq low refused', '{}')); await Promise.resolve() })

    await waitFor(() => expect(slot()?.queue_priority).toBe('medium'))
  })

  it('rolls a refused pick back to the last successful pick', async () => {
    const { ApiError } = await import('../api/client')
    let resolveHigh: (value: { ok: boolean }) => void = () => undefined
    let rejectLow: (reason?: unknown) => void = () => undefined
    apiMock.setSlotQueuePriority
      .mockImplementationOnce(() => new Promise(resolve => { resolveHigh = resolve }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectLow = reject }))
    slots({ queue_priority: 'medium' })
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'high'))
    act(() => result.current.setQueuePriority(KEY, 'low'))
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(1))
    await act(async () => { resolveHigh({ ok: true }); await Promise.resolve() })
    await waitFor(() => expect(apiMock.setSlotQueuePriority).toHaveBeenCalledTimes(2))
    await act(async () => { rejectLow(new ApiError(409, 'zzq low refused', '{}')); await Promise.resolve() })

    await waitFor(() => expect(slot()?.queue_priority).toBe('high'))
  })

  it('clears a slot failure after its next successful pick', async () => {
    apiMock.setSlotQueuePriority
      .mockRejectedValueOnce(new Error('zzq offline'))
      .mockResolvedValueOnce({ ok: true })
    slots({})
    const { result } = harness()

    act(() => result.current.setQueuePriority(KEY, 'high'))
    await waitFor(() => expect(result.current.queuePriorityError?.key).toBe(KEY))
    act(() => result.current.setQueuePriority(KEY, 'low'))

    await waitFor(() => expect(result.current.queuePriorityError).toBeNull())
  })
})

describe('copyLink', () => {
  it('copies the link with the slot title and the caller mode', () => {
    slots({ title: 'zzq title' })
    const { result } = harness('zzq-mode')
    act(() => result.current.copyLink(KEY))
    expect(copySessionLink).toHaveBeenCalledWith(KEY, 'zzq title', undefined, 'zzq-mode')
  })

  it('copies a link for an unknown slot with no title', () => {
    const { result } = harness()
    act(() => result.current.copyLink('zzq-missing'))
    expect(copySessionLink).toHaveBeenCalledWith('zzq-missing', undefined, undefined, undefined)
  })
})

describe('move', () => {
  it('delegates to the shared optimistic move', () => {
    const { result } = harness()
    act(() => result.current.move(KEY, 'zzq-folder'))
    expect(moveSlotToFolder).toHaveBeenCalledWith(KEY, 'zzq-folder')
  })

  it('passes null through for a move to root', () => {
    const { result } = harness()
    act(() => result.current.move(KEY, null))
    expect(moveSlotToFolder).toHaveBeenCalledWith(KEY, null)
  })
})

describe('close', () => {
  it('closes without a prompt when the confirm preference is off', () => {
    chatConfig.confirmCloseSession = false
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(deleteSlot).toHaveBeenCalledWith(KEY)
  })

  it('closes after an accepted confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(deleteSlot).toHaveBeenCalledWith(KEY)
  })

  it('keeps the session on a declined confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(deleteSlot).not.toHaveBeenCalled()
  })
})
