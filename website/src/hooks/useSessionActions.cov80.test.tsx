/**
 * useSessionActions — the surface-agnostic session actions shared by every menu.
 *
 * Each action reads its prior state from the store at CALL time and rolls back
 * on failure, so the tests drive the real global store (the hook reads
 * `store.getState()` directly) and assert both the optimistic write and the
 * rollback. The guarded mode rollback — which must NOT clobber a superseding
 * toggle — is covered explicitly, since that is the branch a naive rollback
 * gets wrong.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const apiMock = vi.hoisted(() => ({
  forkChatSlot: vi.fn(),
  setSlotPin: vi.fn(),
  setSlotMode: vi.fn(),
  chatSlots: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: apiMock }))

const copySessionLink = vi.hoisted(() => vi.fn())
vi.mock('../utils/shareUrl', () => ({ copySessionLink }))

const moveSlotToFolder = vi.hoisted(() => vi.fn())
vi.mock('./useMoveSlotToFolder', () => ({ useMoveSlotToFolder: () => moveSlotToFolder }))

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: true }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

/** `close` reads the close's outcome, so the stub must be thunk-shaped: a plain
 *  action has no `.unwrap()`. A factory, so a rejection is built on consumption
 *  rather than at setup, where it would land as an unhandled rejection. */
const deleteSlotOutcome = vi.hoisted(() => ({ current: (): Promise<unknown> => Promise.resolve('ok') }))
const deleteSlot = vi.hoisted(() => vi.fn((key: string) => () => Object.assign(
  Promise.resolve({ type: 'zzq/deleteSlot', payload: key }),
  { unwrap: () => deleteSlotOutcome.current() },
)))
const switchSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/switchSlot', payload: key })))
const closeSlotWithNotice = vi.hoisted(() => vi.fn())
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSlot,
  switchSlot,
  closeSlotWithNotice,
}))

import { store } from '../store'
import { sseSlots, markSlotUnread, markSlotRead, setSidebarOrder, updateSlot, updateSlotPin } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import {
  PINNED_SESSION_ORDER_KEY,
  movePinnedSession,
  persistPinnedSessionOrder,
  readPinnedSessionOrder,
  reconcilePinnedSessionOrder,
} from '../utils/pinnedSessionOrder'
import { pinMutationKeysInFlight, useSessionActions } from './useSessionActions'

const KEY = 'zzq-slot-1'

function slots(patch: Partial<ChatSlot> = {}) {
  store.dispatch(sseSlots([{ key: KEY, messages: 0, running: false, ...patch } as ChatSlot]))
}

function harness(mode?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
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
  apiMock.setSlotMode.mockReset().mockResolvedValue({ ok: true })
  apiMock.chatSlots.mockReset().mockImplementation(async () => store.getState().dashboard.slots)
  copySessionLink.mockClear()
  moveSlotToFolder.mockClear()
  deleteSlot.mockClear()
  closeSlotWithNotice.mockClear()
  deleteSlotOutcome.current = () => Promise.resolve('ok')
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
    await waitFor(() => expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([KEY]))
  })

  it('unpins a pinned session', async () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['before', KEY, 'after']))
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
    await waitFor(() => expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['before', 'after']))
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

  it('appends the first new pin after the complete natural baseline', async () => {
    store.dispatch(sseSlots([
      { key: 'pin-b', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: 'pin-a', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['pin-a', 'pin-b', KEY]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))

    await waitFor(() => expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!))
      .toEqual(['pin-b', 'pin-a', KEY]))
  })

  it('sorts the first baseline by saved preference when no sidebar order exists', async () => {
    localStorage.setItem('mc-session-sort', 'name-desc')
    store.dispatch(sseSlots([
      { key: 'pin-a', title: 'Alpha', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: 'pin-z', title: 'Zulu', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, title: 'Middle', messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder([]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))

    await waitFor(() => expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!))
      .toEqual(['pin-z', 'pin-a', KEY]))
  })

  it('ignores a partial sidebar order when seeding the first pinned rank', async () => {
    localStorage.setItem('mc-session-sort', 'name-desc')
    store.dispatch(sseSlots([
      { key: 'pin-a', title: 'Alpha', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: 'pin-z', title: 'Zulu', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, title: 'Middle', messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    // A filter projects only one existing pin into the rendered order.
    store.dispatch(setSidebarOrder(['pin-a', KEY]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))

    await waitFor(() => expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!))
      .toEqual(['pin-z', 'pin-a', KEY]))
  })

  it('sorts concurrent authoritative new pins before appending them', async () => {
    const other = 'zzq-slot-z'
    localStorage.setItem('mc-session-sort', 'name-desc')
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['base']))
    store.dispatch(sseSlots([
      { key: 'base', title: 'Base', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, title: 'Alpha', messages: 0, running: false, pinned: false } as ChatSlot,
      { key: other, title: 'Zulu', messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['base', KEY, other]))
    const { result } = harness()

    act(() => {
      result.current.togglePin(KEY)
      result.current.togglePin(other)
    })

    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(readPinnedSessionOrder()).toEqual(['base', other, KEY]))
  })

  it('commits overlapping outcomes atomically when success arrives before failure', async () => {
    const other = 'zzq-slot-2'
    let rejectFirst: (reason?: unknown) => void = () => undefined
    let resolveSecond: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([KEY, other]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([KEY, other])

    await act(async () => { rejectFirst(new Error('zzq offline')); await Promise.resolve() })

    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === KEY)?.pinned).toBe(true))
    expect(store.getState().dashboard.slots.find(s => s.key === other)?.pinned).toBe(false)
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([KEY])
  })

  it('appends an authoritative partial-success pin instead of reviving stale rank', async () => {
    apiMock.setSlotPin
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq unpin rejected'))
    apiMock.chatSlots.mockResolvedValue([
      { key: 'a', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: true } as ChatSlot,
    ])
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([KEY, 'a']))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['a', KEY])
  })

  it('serializes overlapping same-key writes and restores membership when both fail', async () => {
    let rejectFirst: (reason?: unknown) => void = () => undefined
    let rejectSecond: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSecond = reject }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })

  it('retains a pending unpin key during a concurrent reorder', async () => {
    const a = 'zzq-slot-a'
    const b = KEY
    const c = 'zzq-slot-c'
    let rejectUnpin: (reason?: unknown) => void = () => undefined
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectUnpin = reject }))
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([a, b, c]))
    store.dispatch(sseSlots([
      { key: a, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: b, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: c, messages: 0, running: false, pinned: true } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder([a, b, c]))
    const { result } = harness()

    act(() => result.current.togglePin(b))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(b, false))
    const natural = [a, c]
    const naturalSet = new Set(natural)
    const pending = pinMutationKeysInFlight().filter(key => !naturalSet.has(key))
    const reordered = movePinnedSession(
      reconcilePinnedSessionOrder(readPinnedSessionOrder(), [...natural, ...pending]),
      a,
      c,
    )
    persistPinnedSessionOrder(reordered)
    expect(reordered).toEqual([b, c, a])

    await act(async () => { rejectUnpin(new Error('zzq rejected')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await act(async () => {
      resolveSnapshot([
        { key: a, messages: 0, running: false, pinned: true } as ChatSlot,
        { key: b, messages: 0, running: false, pinned: true } as ChatSlot,
        { key: c, messages: 0, running: false, pinned: true } as ChatSlot,
      ])
      await Promise.resolve()
    })

    await waitFor(() => expect(slot()?.pinned).toBe(true))
    expect(readPinnedSessionOrder()).toEqual([b, c, a])
  })

  it('preserves an in-flight manual reorder while rolling back a rejected pin', async () => {
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.setSlotPin.mockRejectedValue(new Error('zzq rejected'))
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    store.dispatch(sseSlots([
      { key: 'a', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: 'b', messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder(['a', 'b', KEY]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    act(() => persistPinnedSessionOrder(['b', 'a']))
    await act(async () => {
      resolveSnapshot([
        { key: 'a', messages: 0, running: false, pinned: true } as ChatSlot,
        { key: 'b', messages: 0, running: false, pinned: true } as ChatSlot,
        { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      ])
      await Promise.resolve()
    })

    await waitFor(() => expect(slot()?.pinned).toBe(false))
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['b', 'a'])
  })

  it('retries reconciliation when an authoritative slots frame arrives in flight', async () => {
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })

  it('bounds snapshot retries under continuous authoritative slot frames', async () => {
    apiMock.chatSlots.mockImplementation(async () => {
      store.dispatch(sseSlots([
        { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      ]))
      return [{ key: KEY, messages: 0, running: false, pinned: true } as ChatSlot]
    })
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))

    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(slot()?.pinned).toBe(false))
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })

  it('discards a snapshot when cross-tab pinned order changes in flight', async () => {
    let resolveSnapshot: (slots: ChatSlot[]) => void = () => undefined
    apiMock.chatSlots.mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve }))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
    slots({ pinned: false })
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())

    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: PINNED_SESSION_ORDER_KEY }))
      store.dispatch(sseSlots([
        { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
      ]))
    })
    await act(async () => {
      resolveSnapshot([{ key: KEY, messages: 0, running: false, pinned: true } as ChatSlot])
      await Promise.resolve()
    })

    expect(slot()?.pinned).toBe(false)
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })

  it('uses snapshot membership after a delayed pre-mutation slots frame', async () => {
    let resolveUnpin: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise(resolve => { resolveUnpin = resolve }))
    apiMock.chatSlots.mockResolvedValue([
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ])
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([KEY]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })


  it('keeps an authoritative matching broadcast when PATCH and snapshot responses fail', async () => {
    let rejectPin: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPin = reject }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
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

  it('removes a rejected optimistic pin from concurrently reordered storage', async () => {
    const a = 'zzq-slot-a'
    const c = 'zzq-slot-c'
    let rejectPin: (reason?: unknown) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPin = reject }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([a, c]))
    store.dispatch(sseSlots([
      { key: a, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: c, messages: 0, running: false, pinned: true } as ChatSlot,
      { key: KEY, messages: 0, running: false, pinned: false } as ChatSlot,
    ]))
    store.dispatch(setSidebarOrder([a, c, KEY]))
    const { result } = harness()

    act(() => result.current.togglePin(KEY))
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
    const natural = [a, c]
    const pending = pinMutationKeysInFlight()
    persistPinnedSessionOrder(movePinnedSession(
      reconcilePinnedSessionOrder(readPinnedSessionOrder(), [...natural, ...pending]),
      a,
      c,
    ))
    expect(readPinnedSessionOrder()).toEqual([c, a, KEY])

    await act(async () => { rejectPin(new Error('zzq rejected')); await Promise.resolve() })

    await waitFor(() => expect(slot()?.pinned).toBe(false))
    expect(readPinnedSessionOrder()).toEqual([c, a])
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY) ?? '[]')).toEqual([])
  })

  it('does not fallback-roll back a newer broadcast when the snapshot refetch fails', async () => {
    let resolvePin: (value: { ok: boolean }) => void = () => undefined
    apiMock.setSlotPin.mockImplementationOnce(() => new Promise(resolve => { resolvePin = resolve }))
    apiMock.chatSlots.mockRejectedValue(new Error('zzq snapshot offline'))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify([]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual([])
  })
})


  it('preserves manual rank when an optimistic unpin is rejected', async () => {
    const other = 'zzq-slot-2'
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['before', KEY, other]))
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
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['before', KEY, other])

    await waitFor(() => expect(slot()?.pinned).toBe(true))
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['before', KEY, other])
  })
describe('toggleMode', () => {
  it('switches to orchestrator once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('orchestrator')
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledWith(KEY, 'orchestrator'))
  })

  it('switches back to normal chat once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    slots({ mode: 'orchestrator' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledWith(KEY, ''))
  })

  it('changes nothing when the confirm is declined', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(apiMock.setSlotMode).not.toHaveBeenCalled()
    expect(slot()?.mode).toBe('')
  })

  it('rolls the mode back when the write fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiMock.setSlotMode.mockRejectedValue(new Error('zzq offline'))
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('orchestrator')
    await waitFor(() => expect(slot()?.mode).toBe(''))
  })

  it('does not clobber a superseding toggle when the write fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let release: (() => void) | undefined
    apiMock.setSlotMode.mockImplementation(
      () => new Promise((_res, rej) => { release = () => rej(new Error('zzq offline')) }),
    )
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    await waitFor(() => expect(release).toBeTypeOf('function'))
    // A second toggle lands while the first write is still in flight.
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('')
    act(() => release?.())
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledTimes(2))
    // The stale rollback must not restore '' over the newer value.
    expect(slot()?.mode).toBe('')
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
    expect(closeSlotWithNotice).toHaveBeenCalledWith(expect.anything(), KEY, undefined)
  })

  it('closes after an accepted confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(closeSlotWithNotice).toHaveBeenCalledWith(expect.anything(), KEY, undefined)
  })

  it('keeps the session on a declined confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(closeSlotWithNotice).not.toHaveBeenCalled()
  })

  /** The gesture's whole obligation is now REACHING the shared helper: the helper
   *  owns the notice, so this asserts the wiring and not a duplicate of it. */
  it('routes the gesture through the helper that owns the notice', () => {
    chatConfig.confirmCloseSession = false
    const { result } = harness()
    act(() => result.current.close(KEY))
    expect(closeSlotWithNotice).toHaveBeenCalledTimes(1)
  })
})
