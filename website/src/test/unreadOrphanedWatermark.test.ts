/**
 * A read must retire a dot whose saved watermark names a row the gateway does
 * not have, and must never erase one that names a row it does.
 *
 * The scenario: the shared unread record holds a watermark taken from a turn's
 * `done` row, which is never written to the transcript, so a window that boots
 * restores the dot above the restored slot's `last_ts` (the transcript tail).
 * No read can cover it: every open clears the dot, the window's own slot_read
 * echo lights it again from the shared record, and every reload restores it.
 * The rule under test: retire only when a gateway page fetched after the
 * gesture captured the watermark has no row as new as it, and only while the
 * shared record still holds that same value.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { bindSlotReadSender, noteUnsavedRowTs, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { unreadWatermarkTs } from '../hooks/unreadOnAttention'
import { createTestStore } from './helpers'
import dashboardReducer, {
  fetchSlots,
  MANUAL_UNREAD,
  markSlotRead,
  markSlotUnread,
  newestRowTs,
  orphanedWatermark,
  readSlot,
  remoteSlotRead,
  removeSlotOptimistic,
  restoreUnreadBadges,
  restoreUnreadSince,
  retireSlotUnread,
  sseSlotPatch,
  sseSlots,
  UNREAD_PROOF_TIMEOUT_MS,
  updateSlot,
  watermarkDisprovedBy,
} from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlotDetail: vi.fn() },
}))

const KEY = 'chat-326-1791528142'
const OTHER_KEY = 'chat-326-1791528143'
const TRANSCRIPT_TAIL = '2026-10-09T22:33:35.542839+00:00'
const ORPHAN = '2026-10-09T22:33:35.611204+00:00'
const NEWER = '2026-10-09T22:33:36.000000+00:00'
const LATEST = '2026-10-09T22:33:37.000000+00:00'

const row = (lastTs = TRANSCRIPT_TAIL, key = KEY): ChatSlot => ({
  key,
  title: 'Unread test',
  messages: 2,
  running: false,
  last_ts: lastTs,
})
const page = (...timestamps: string[]) => ({
  messages: timestamps.map((ts) => ({ role: 'assistant', content: ts, cls: '', ts })),
  running: false,
  has_more: false,
  total: timestamps.length,
  queue: [],
})
/** A bounded read keeps a turn still streaming, collapsed into one row with no ts. */
const streamingRows = [{ role: 'streaming', content: 'writing', cls: 'msg msg-a' }]
const shared = () => JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>
const orphan = (key = KEY) => localStorage.getItem('mc-unread-orphaned:' + key) ?? undefined
const arrival = (key = KEY) => localStorage.getItem('mc-unread-arrival:' + key) ?? undefined
const retireArrival = (key = KEY) => {
  const marker = 'held-arrival:' + key
  localStorage.setItem('mc-unread-arrival:' + key, marker)
  return marker
}
const boot = (watermark = ORPHAN, lastTs = TRANSCRIPT_TAIL, key = KEY) => {
  localStorage.setItem('mc-unread-shared', JSON.stringify({ ...shared(), [key]: watermark }))
  const since = restoreUnreadSince()
  const initial = dashboardReducer(undefined, { type: '@@test/init' })
  return createTestStore({
    dashboard: {
      ...initial,
      slots: [row(lastTs, key)],
      unreadSince: since,
      unreadSlots: restoreUnreadBadges(since),
    } as RootState['dashboard'],
  })
}

describe('orphaned unread watermark retirement', () => {
  let relayed: Array<{ slot: string; readTs?: string }>
  const detail = vi.mocked(api.chatSlotDetail)

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    relayed = []
    detail.mockReset()
    detail.mockResolvedValue(page(TRANSCRIPT_TAIL))
    _resetSlotReadRelayForTest()
    bindSlotReadSender((slot, readTs) => { relayed.push({ slot, readTs }) })
  })

  afterEach(() => { vi.useRealTimers(); _resetSlotReadRelayForTest() })

  it('arms the arrival marker before clearing the local unread badge', async () => {
    const events: string[] = []
    const store = boot()
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-arrival:' + KEY) events.push('arrival')
      original.call(this, key, value)
    })
    try {
      await readSlot(KEY)(
        (action) => {
          if (action.type === markSlotRead.type) events.push('read')
          return store.dispatch(action)
        },
        store.getState,
      )
    } finally {
      set.mockRestore()
    }
    expect(events.slice(0, 2)).toEqual(['arrival', 'read'])
  })

  it('a row that arrives while the proof is in flight stays out of the delayed relay', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    noteUnsavedRowTs(KEY, LATEST)
    store.dispatch(markSlotUnread({
      slot: KEY,
      ts: unreadWatermarkTs('permission', LATEST),
      localTs: LATEST,
    }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
    store.dispatch(remoteSlotRead({ slot: KEY, readTs: relayed[0].readTs }))
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
  })

  it('a row that arrives while the proof is in flight keeps its dot across a reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    noteUnsavedRowTs(KEY, LATEST)
    store.dispatch(markSlotUnread({
      slot: KEY,
      ts: unreadWatermarkTs('permission', LATEST),
      localTs: LATEST,
    }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a row another window records while the proof is in flight keeps its dot across a reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    const secondWindow = createTestStore()
    secondWindow.dispatch(markSlotUnread({
      slot: KEY,
      ts: unreadWatermarkTs('permission', LATEST),
      localTs: LATEST,
    }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it("a read that overlaps another read of the session keeps the other read's retire", async () => {
    let resolveFirstPage!: (value: ReturnType<typeof page>) => void
    let resolveSecondPage!: (value: ReturnType<typeof page>) => void
    detail
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirstPage = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecondPage = resolve }))
    const firstWindow = boot()
    const firstRead = firstWindow.dispatch(readSlot(KEY))
    const secondWindow = boot()
    const secondRead = secondWindow.dispatch(readSlot(KEY))

    resolveSecondPage(page(TRANSCRIPT_TAIL))
    await secondRead
    expect(orphan()).toBe(ORPHAN)

    resolveFirstPage(page(TRANSCRIPT_TAIL))
    await firstRead
    expect(orphan()).toBe(ORPHAN)
    expect(restoreUnreadBadges(restoreUnreadSince())).not.toContain(KEY)
  })

  it('an arrival that lands while the retire writes its tombstone keeps its dot across a reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    const secondWindow = createTestStore()
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      original.call(this, key, value)
      if (key === 'mc-unread-orphaned:' + KEY) {
        secondWindow.dispatch(markSlotUnread({ slot: KEY, ts: TRANSCRIPT_TAIL, localTs: LATEST }))
      }
    })
    try {
      resolvePage(page(TRANSCRIPT_TAIL))
      await pending
    } finally {
      set.mockRestore()
    }
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('an arrival that finishes before the retire writes its tombstone keeps its dot across a reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    const secondWindow = createTestStore()
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-orphaned:' + KEY) {
        secondWindow.dispatch(markSlotUnread({ slot: KEY, ts: TRANSCRIPT_TAIL, localTs: LATEST }))
      }
      original.call(this, key, value)
    })
    try {
      resolvePage(page(TRANSCRIPT_TAIL))
      await pending
    } finally {
      set.mockRestore()
    }
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('an arrival clears a tombstone written after its marker is removed', () => {
    const store = boot()
    localStorage.setItem('mc-unread-arrival:' + KEY, 'held-arrival')
    const originalRemove = Storage.prototype.removeItem
    const originalSet = Storage.prototype.setItem
    let tombstoneWritten = false
    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key: string) {
      originalRemove.call(this, key)
      if (!tombstoneWritten && key === 'mc-unread-arrival:' + KEY) {
        tombstoneWritten = true
        originalSet.call(this, 'mc-unread-orphaned:' + KEY, ORPHAN)
      }
    })
    try {
      store.dispatch(markSlotUnread({ slot: KEY, ts: TRANSCRIPT_TAIL, localTs: LATEST }))
    } finally {
      remove.mockRestore()
    }
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('an absorbed arrival during a read keeps its dot across a reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    store.dispatch(markSlotUnread({ slot: KEY, ts: TRANSCRIPT_TAIL, localTs: LATEST }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(orphan()).toBeUndefined()
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a read whose marker cannot be written retires nothing and keeps its dot', async () => {
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-arrival:' + KEY) throw new DOMException('quota exceeded', 'QuotaExceededError')
      original.call(this, key, value)
    })
    try {
      const store = boot()
      await store.dispatch(readSlot(KEY))
    } finally {
      set.mockRestore()
    }
    expect(orphan()).toBeUndefined()
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a read cannot reuse an earlier arrival marker when its own marker write fails', async () => {
    const store = boot()
    localStorage.setItem('mc-unread-arrival:' + KEY, 'earlier-arrival')
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-arrival:' + KEY) throw new DOMException('quota exceeded', 'QuotaExceededError')
      original.call(this, key, value)
    })
    try {
      await store.dispatch(readSlot(KEY))
    } finally {
      set.mockRestore()
    }
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a finished read leaves none of its arrival markers behind', async () => {
    const retired = boot()
    await retired.dispatch(readSlot(KEY))
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()

    detail.mockResolvedValue(page(NEWER))
    const kept = boot(ORPHAN, TRANSCRIPT_TAIL, OTHER_KEY)
    await kept.dispatch(readSlot(OTHER_KEY))
    expect(localStorage.getItem('mc-unread-arrival:' + OTHER_KEY)).toBeNull()
  })

  it('a failed proof request leaves none of its arrival markers behind', async () => {
    detail.mockRejectedValue(new Error('offline'))
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
  })

  it('a proof request that never settles still relays the read after the deadline', async () => {
    vi.useFakeTimers()
    let aborted = false
    // Like fetch, the request settles only when its signal aborts.
    detail.mockImplementation((_slot, _limit, _before, signal) => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => { aborted = true; reject(signal.reason) })
    }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    await vi.advanceTimersByTimeAsync(UNREAD_PROOF_TIMEOUT_MS - 1)
    expect(relayed).toEqual([])
    await vi.advanceTimersByTimeAsync(1)
    await pending
    expect(aborted).toBe(true)
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
    expect(localStorage.getItem('mc-unread-orphaned:' + KEY)).toBeNull()
  })

  it('an arrival the shared record does not outrank leaves no arrival marker', () => {
    const store = createTestStore()
    store.dispatch(markSlotUnread({ slot: KEY, ts: LATEST, localTs: LATEST }))
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
  })

  it('the delayed relay still covers an unsaved row received before the click', async () => {
    noteUnsavedRowTs(KEY, NEWER)
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(detail).toHaveBeenCalledWith(KEY, 1, undefined, expect.any(AbortSignal))
    expect(relayed).toEqual([{ slot: KEY, readTs: NEWER }])
  })

  it('retires when a post-gesture gateway page is older and relays the click-time last_ts', async () => {
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    store.dispatch(updateSlot({ key: KEY, last_ts: LATEST }))
    expect(relayed).toEqual([])  // nothing goes out before the gateway answers
    await pending
    expect(detail).toHaveBeenCalledWith(KEY, 1, undefined, expect.any(AbortSignal))
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(orphan()).toBe(ORPHAN)
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
  })

  it('keeps the guard when the gateway page covers a held watermark despite a stale slot row', async () => {
    detail.mockResolvedValue(page(TRANSCRIPT_TAIL, NEWER))
    const store = boot(NEWER, TRANSCRIPT_TAIL)
    await store.dispatch(readSlot(KEY))
    expect(shared()[KEY]).toBe(NEWER)
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
  })

  it('keeps the guard when the gateway proof request rejects', async () => {
    detail.mockRejectedValue(new Error('offline'))
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
  })

  it('keeps the guard when the gateway page holds only a streaming turn', async () => {
    detail.mockResolvedValue({ messages: streamingRows, queue: [] } as never)
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(orphan()).toBeUndefined()
    expect(shared()[KEY]).toBe(ORPHAN)
    const since = restoreUnreadSince()
    expect(since[KEY]).toBe(ORPHAN)
    expect(restoreUnreadBadges(since)).toContain(KEY)
    expect(relayed).toEqual([{ slot: KEY, readTs: TRANSCRIPT_TAIL }])
  })

  it('fetches nothing when this window last_ts already covers the held watermark', () => {
    const store = boot(ORPHAN, NEWER)
    const result = store.dispatch(readSlot(KEY))
    expect(result).toBeUndefined()
    expect(detail).not.toHaveBeenCalled()
    expect(shared()[KEY]).toBeUndefined()
    expect(localStorage.getItem('mc-unread-arrival:' + KEY)).toBeNull()
    expect(relayed).toEqual([{ slot: KEY, readTs: NEWER }])
  })

  it('keeps a sibling watermark published after capture and restores its dot on reload', async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: NEWER }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(shared()[KEY]).toBe(NEWER)
    expect(restoreUnreadSince()[KEY]).toBe(NEWER)
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('restores a newer watermark written after the retire', () => {
    const store = boot()
    store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: NEWER }))
    expect(shared()[KEY]).toBe(NEWER)
    expect(restoreUnreadSince()[KEY]).toBe(NEWER)
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a window that loaded the dot before the proof keeps it through a relay that cannot cover it', () => {
    const sibling = boot()
    boot().dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    boot().dispatch(markSlotUnread({ slot: KEY, ts: NEWER }))
    sibling.dispatch(remoteSlotRead({ slot: KEY, readTs: TRANSCRIPT_TAIL }))
    expect(sibling.getState().dashboard.unreadSlots).toContain(KEY)
    expect(shared()[KEY]).toBe(NEWER)
    const restored = restoreUnreadSince()
    expect(restored[KEY]).toBe(NEWER)
    expect(restoreUnreadBadges(restored)).toContain(KEY)
  })

  it('an arrival whose write fails over a tombstoned value keeps a dot across a reload', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(orphan()).toBe(ORPHAN)

    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-shared') throw new DOMException('quota exceeded', 'QuotaExceededError')
      original.call(this, key, value)
    })
    try {
      store.dispatch(markSlotUnread({ slot: KEY, ts: LATEST, localTs: LATEST }))
    } finally {
      set.mockRestore()
    }

    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a stale write that restores a retired value after a new message still shows a dot', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(orphan()).toBe(ORPHAN)
    store.dispatch(markSlotUnread({ slot: KEY, ts: LATEST, localTs: LATEST }))
    expect(orphan()).toBeUndefined()
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: ORPHAN }))
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it("a stale write that restores the retired value while a read waits still shows the new message's dot", async () => {
    let resolvePage!: (value: ReturnType<typeof page>) => void
    detail.mockImplementation(() => new Promise((resolve) => { resolvePage = resolve }))
    const store = boot()
    const pending = store.dispatch(readSlot(KEY))
    store.dispatch(markSlotUnread({ slot: KEY, ts: LATEST, localTs: LATEST }))
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: ORPHAN }))
    resolvePage(page(TRANSCRIPT_TAIL))
    await pending
    expect(orphan()).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('a new older arrival replaces a tombstoned shared value and restores its dot', () => {
    const store = boot()
    store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    store.dispatch(markSlotUnread({ slot: KEY, ts: TRANSCRIPT_TAIL }))
    expect(shared()[KEY]).toBe(TRANSCRIPT_TAIL)
    sessionStorage.clear()
    const since = restoreUnreadSince()
    expect(since[KEY]).toBe(TRANSCRIPT_TAIL)
    expect(restoreUnreadBadges(since)).toContain(KEY)
  })

  it('two windows retiring different slots keep independent tombstones across reload', () => {
    const firstWindow = boot(ORPHAN, TRANSCRIPT_TAIL, KEY)
    const secondWindow = boot(NEWER, TRANSCRIPT_TAIL, OTHER_KEY)
    firstWindow.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    secondWindow.dispatch(retireSlotUnread({ slot: OTHER_KEY, watermark: NEWER, arrival: retireArrival(OTHER_KEY) }))
    expect(orphan(KEY)).toBe(ORPHAN)
    expect(orphan(OTHER_KEY)).toBe(NEWER)
    sessionStorage.clear()
    const since = restoreUnreadSince()
    expect(since[KEY]).toBeUndefined()
    expect(since[OTHER_KEY]).toBeUndefined()
    expect(restoreUnreadBadges(since)).not.toContain(KEY)
    expect(restoreUnreadBadges(since)).not.toContain(OTHER_KEY)
  })

  it('keeps an adopted sibling watermark when the gateway page contains that row', async () => {
    const store = boot(ORPHAN, TRANSCRIPT_TAIL)
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: NEWER }))
    store.dispatch(remoteSlotRead({ slot: KEY, readTs: ORPHAN }))
    expect(store.getState().dashboard.unreadSince[KEY]).toBe(NEWER)
    detail.mockResolvedValue(page(TRANSCRIPT_TAIL, NEWER))
    await store.dispatch(readSlot(KEY))
    expect(shared()[KEY]).toBe(NEWER)
  })

  it('leaves a changed shared record alone when retiring the captured value', () => {
    const store = boot()
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: NEWER }))
    store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    expect(shared()[KEY]).toBe(NEWER)
    expect(store.getState().dashboard.unreadSince[KEY]).toBeUndefined()
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
    expect(restoreUnreadSince()[KEY]).toBe(NEWER)
    expect(restoreUnreadBadges(restoreUnreadSince())).toContain(KEY)
  })

  it('records a tombstone without writing the shared unread record', () => {
    const store = boot()
    const set = vi.spyOn(Storage.prototype, 'setItem')
    set.mockClear()
    store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    expect(orphan()).toBe(ORPHAN)
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(set.mock.calls.filter(([key]) => key === 'mc-unread-shared')).toHaveLength(0)
    set.mockRestore()
  })

  it('keeps the local dot lit when the tombstone write fails', () => {
    const store = boot()
    const original = Storage.prototype.setItem
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === 'mc-unread-orphaned:' + KEY) throw new DOMException('denied', 'SecurityError')
      original.call(this, key, value)
    })
    try {
      store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
      expect(orphan()).toBeUndefined()
      expect(store.getState().dashboard.unreadSince[KEY]).toBe(ORPHAN)
      expect(store.getState().dashboard.unreadSlots).toContain(KEY)
      expect(shared()[KEY]).toBe(ORPHAN)
      expect(set.mock.calls.filter(([key]) => key === 'mc-unread-shared')).toHaveLength(0)
    } finally {
      set.mockRestore()
    }
  })

  it('a window that loaded the dot before the proof drops it on reload and on its own read', async () => {
    const sibling = boot()
    boot().dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    sibling.dispatch(remoteSlotRead({ slot: KEY, readTs: TRANSCRIPT_TAIL }))
    expect(sibling.getState().dashboard.unreadSlots).toContain(KEY)
    const restored = restoreUnreadSince()
    expect(restoreUnreadBadges(restored)).not.toContain(KEY)
    await sibling.dispatch(readSlot(KEY))
    expect(sibling.getState().dashboard.unreadSlots).not.toContain(KEY)
    expect(shared()[KEY]).toBe(ORPHAN)
  })

  it('keeps the local dot cleared through its own older slot_read echo', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    store.dispatch(remoteSlotRead({ slot: KEY, readTs: TRANSCRIPT_TAIL }))
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
  })

  it('reload restores neither watermark nor badge while the shared orphan remains', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(orphan()).toBe(ORPHAN)
    const since = restoreUnreadSince()
    expect(since[KEY]).toBeUndefined()
    expect(restoreUnreadBadges(since)).not.toContain(KEY)
  })

  it('posts the lowered unread count to the embedding hub', async () => {
    const store = boot()
    const posted: number[] = []
    const parent = vi.spyOn(window, 'parent', 'get').mockReturnValue({
      postMessage: (message: unknown) => { posted.push((message as { count: number }).count) },
    } as unknown as Window)
    try {
      await store.dispatch(readSlot(KEY))
    } finally {
      parent.mockRestore()
    }
    expect(posted).toContain(0)
  })

  it('a sibling keeps a newer dot through an older relay until its own covering read', () => {
    const sibling = boot(NEWER, NEWER)
    sibling.dispatch(remoteSlotRead({ slot: KEY, readTs: ORPHAN }))
    expect(sibling.getState().dashboard.unreadSlots).toContain(KEY)
    sibling.dispatch(readSlot(KEY))
    expect(sibling.getState().dashboard.unreadSlots).not.toContain(KEY)
  })

  it('a read of a manual reminder clears it and leaves the shared record alone', () => {
    const store = boot()
    store.dispatch(markSlotUnread(KEY))
    store.dispatch(readSlot(KEY))
    expect(detail).not.toHaveBeenCalled()
    expect(store.getState().dashboard.unreadSince[KEY]).toBeUndefined()
    expect(shared()[KEY]).toBe(ORPHAN)
  })

  it('retiring shared state keeps a manual reminder this window set', () => {
    const store = boot()
    store.dispatch(markSlotUnread(KEY))
    store.dispatch(retireSlotUnread({ slot: KEY, watermark: ORPHAN, arrival: retireArrival(KEY) }))
    expect(shared()[KEY]).toBe(ORPHAN)
    expect(orphan()).toBe(ORPHAN)
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
    expect(store.getState().dashboard.unreadSince[KEY]).toBe(MANUAL_UNREAD)
  })

  it('excludes manual and empty values from orphan classification', () => {
    expect(orphanedWatermark(undefined, TRANSCRIPT_TAIL)).toBeUndefined()
    expect(orphanedWatermark('', TRANSCRIPT_TAIL)).toBeUndefined()
    expect(orphanedWatermark(MANUAL_UNREAD, TRANSCRIPT_TAIL)).toBeUndefined()
  })

  it('classifies only uncovered message watermarks as orphaned', () => {
    expect(orphanedWatermark(ORPHAN, TRANSCRIPT_TAIL)).toBe(ORPHAN)
    expect(orphanedWatermark(ORPHAN, NEWER)).toBeUndefined()
  })

  it('a page whose rows carry no time, or no rows array, disproves nothing', () => {
    expect(watermarkDisprovedBy(ORPHAN, streamingRows)).toBeUndefined()
    expect(watermarkDisprovedBy(ORPHAN, undefined)).toBeUndefined()
  })

  it('an empty page, or one with no row as new as the watermark, disproves it', () => {
    expect(watermarkDisprovedBy(ORPHAN, [])).toBe(ORPHAN)
    expect(watermarkDisprovedBy(ORPHAN, [{ ts: TRANSCRIPT_TAIL }])).toBe(ORPHAN)
    expect(watermarkDisprovedBy(ORPHAN, [{ ts: ORPHAN }])).toBeUndefined()
    expect(watermarkDisprovedBy(ORPHAN, [{ ts: NEWER }])).toBeUndefined()
  })

  it('selects the newest parseable row timestamp regardless of row order', () => {
    expect(newestRowTs([
      { ts: NEWER },
      { ts: 'not-a-time' },
      { ts: TRANSCRIPT_TAIL },
      { ts: LATEST },
    ])).toBe(LATEST)
    expect(newestRowTs([{ ts: 'invalid' }, {}])).toBeUndefined()
  })

  it('removing a session drops its tombstone and arrival marker', () => {
    const store = boot()
    localStorage.setItem('mc-unread-orphaned:' + KEY, ORPHAN)
    localStorage.setItem('mc-unread-arrival:' + KEY, 'read-in-flight')
    localStorage.setItem('mc-unread-orphaned:' + OTHER_KEY, NEWER)
    localStorage.setItem('mc-unread-arrival:' + OTHER_KEY, 'sibling-read')
    store.dispatch(removeSlotOptimistic(KEY))
    expect(orphan(KEY)).toBeUndefined()
    expect(arrival(KEY)).toBeUndefined()
    expect(orphan(OTHER_KEY)).toBe(NEWER)
    expect(arrival(OTHER_KEY)).toBe('sibling-read')
  })

  it('a slot list without the session drops its tombstone and arrival marker', () => {
    const store = boot()
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
    localStorage.setItem('mc-unread-orphaned:' + KEY, ORPHAN)
    localStorage.setItem('mc-unread-arrival:' + KEY, 'read-in-flight')
    localStorage.setItem('mc-unread-orphaned:' + OTHER_KEY, NEWER)
    localStorage.setItem('mc-unread-arrival:' + OTHER_KEY, 'sibling-read')
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    expect(orphan(KEY)).toBeUndefined()
    expect(arrival(KEY)).toBeUndefined()
    expect(orphan(OTHER_KEY)).toBe(NEWER)
    expect(arrival(OTHER_KEY)).toBe('sibling-read')
  })

  it('a retired session another window deletes leaves no saved dot, and a stale reply removes nothing', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    expect(orphan()).toBe(ORPHAN)
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
    retireArrival(KEY)
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, KEY), row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    store.dispatch(fetchSlots.fulfilled([row(TRANSCRIPT_TAIL, OTHER_KEY)], 'req-stale', undefined as never))
    expect(orphan()).toBe(ORPHAN)
    expect(arrival()).toBeDefined()
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, KEY), row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    expect(orphan()).toBeUndefined()
    expect(arrival()).toBeUndefined()
    expect(shared()[KEY]).toBeUndefined()
    expect(restoreUnreadBadges(restoreUnreadSince())).not.toContain(KEY)
  })

  it('a removed frame for a retired session leaves no saved dot', async () => {
    const store = boot()
    await store.dispatch(readSlot(KEY))
    retireArrival(KEY)
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, KEY), row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    store.dispatch(sseSlotPatch({ slots: [], removed: [KEY] }))
    expect(orphan()).toBeUndefined()
    expect(arrival()).toBeUndefined()
    expect(shared()[KEY]).toBeUndefined()
  })

  it('a first slot list without an unread session drops its tombstone and arrival marker', () => {
    localStorage.setItem('mc-unread-shared', JSON.stringify({ [KEY]: NEWER }))
    localStorage.setItem('mc-unread-orphaned:' + KEY, ORPHAN)
    localStorage.setItem('mc-unread-arrival:' + KEY, 'read-in-flight')
    const since = restoreUnreadSince()
    const initial = dashboardReducer(undefined, { type: '@@test/init' })
    const store = createTestStore({
      dashboard: { ...initial, unreadSince: since, unreadSlots: restoreUnreadBadges(since) } as RootState['dashboard'],
    })
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
    store.dispatch(sseSlots([row(TRANSCRIPT_TAIL, OTHER_KEY)]))
    expect(orphan()).toBeUndefined()
    expect(arrival()).toBeUndefined()
  })
})
