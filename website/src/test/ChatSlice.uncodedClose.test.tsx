/**
 * A close whose DELETE fails without the server's own refusal code (a proxy
 * 502/504, a dropped connection) may still have worked. The "still open"
 * notice is decided from the reconciled slot list after the recovery read, so
 * a close that worked is never reported as still open.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { armCloseRestoreWatch, deleteSlot, reconcileCloseRestoreWatch, resumeFromHistory, setActiveSlot, sseChatMessage } from '../store/chatSlice'
import dashboardReducer, { fetchSlots, sseSlotPatch, sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const apiMock = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  chatSlots: vi.fn(),
  deleteChatSlot: vi.fn(),
  sessions: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

function makeStore() {
  return configureStore({
    reducer: {
      chat: chatReducer,
      dashboard: dashboardReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

type Store = ReturnType<typeof makeStore>
const root = (store: Store): RootState => store.getState() as unknown as RootState
const chat = (store: Store) => store.getState().chat

const slotRow = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 0,
  running: false,
  ...extra,
} as ChatSlot)

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  apiMock.chatSlots.mockResolvedValue([])
})

describe('an uncoded close failure', () => {
  it('an uncoded close failure the recovery list shows worked keeps the row gone, its view state, and no notice', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    store.dispatch(sseChatMessage({ slot: 'doomed', role: 'assistant', content: 'cached' }))

    const outcome = await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(outcome.type).toBe('chat/deleteSlot/rejected')
    expect(apiMock.chatSlots).toHaveBeenCalledTimes(1)
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
    expect(chat(store).closeRefused).toBeNull()
    expect(chat(store).slotMessages.doomed?.map(m => m.content)).toEqual(['cached'])
  })

  it('an uncoded close failure the recovery list shows did not work brings the row back with a notice', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(new TypeError('Failed to fetch'))
    apiMock.chatSlots.mockResolvedValue([slotRow('doomed'), slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed', reason: 'failed' })
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')
    expect(root(store).dashboard.closingSlots).toEqual({})
  })

  it('reports an uncoded close failure whose recovery list cannot be read', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockRejectedValueOnce(new Error('offline'))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(chat(store).closeRefused?.reason).toBe('failed')
    expect(root(store).dashboard.closingSlots).toEqual({})
  })

  it('settles an uncoded close failure without waiting on its recovery list, which a pre-close list cannot pre-empt', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    let answerStale: (v: unknown) => void = () => {}
    let answerRecovery: (v: unknown) => void = () => {}
    apiMock.chatSlots
      .mockReturnValueOnce(new Promise(resolve => { answerStale = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { answerRecovery = resolve }))
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    store.dispatch(setActiveSlot('doomed'))

    const stale = store.dispatch(fetchSlots())
    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    expect(apiMock.chatSlots).toHaveBeenCalledTimes(2)
    answerStale([slotRow('doomed'), slotRow('peer')])
    await stale
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])

    answerRecovery([slotRow('peer')])
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
    expect(chat(store).closeRefused).toBeNull()
  })

  it('a late recovery list from a failed close does not report a newer close attempt as refused', async () => {
    let answerRecovery: (v: unknown) => void = () => {}
    let answerRetry: (v: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise(resolve => { answerRecovery = resolve }))
    apiMock.deleteChatSlot
      .mockRejectedValueOnce(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      .mockReturnValueOnce(new Promise(resolve => { answerRetry = resolve }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    const retry = store.dispatch(deleteSlot('doomed'))
    answerRecovery([slotRow('doomed'), slotRow('peer')])
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toBeNull()

    answerRetry({})
    expect((await retry).type).toBe('chat/deleteSlot/fulfilled')
    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
  })

  it('a rejected recovery read does not report a newer server-confirmed close as refused', async () => {
    let rejectRecovery: (reason?: unknown) => void = () => {}
    let answerRetry: (v: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRecovery = reject }))
    apiMock.deleteChatSlot
      .mockRejectedValueOnce(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      .mockReturnValueOnce(new Promise(resolve => { answerRetry = resolve }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')

    const retry = store.dispatch(deleteSlot('doomed'))
    answerRetry({})
    expect((await retry).type).toBe('chat/deleteSlot/fulfilled')
    expect(root(store).dashboard.connected).toBe(false)
    expect(root(store).dashboard.durablyRemoved.doomed).toBeUndefined()
    expect(root(store).dashboard.closingSlots.doomed?.confirmedUntil).toEqual(expect.any(Number))

    rejectRecovery(new Error('offline'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
  })

  it('a removed frame that lands before the recovery list answers is not reported as still open', async () => {
    let answerRecovery: (v: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise(resolve => { answerRecovery = resolve }))
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
    answerRecovery([slotRow('doomed'), slotRow('peer')])
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
  })

  it('a removed frame keeps a rejected recovery list from restoring the notice', async () => {
    let rejectRecovery: (reason?: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRecovery = reject }))
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
    rejectRecovery(new Error('offline'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(root(store).dashboard.durablyRemoved.doomed).toBe(true)
    expect(chat(store).closeRefused).toBeNull()
  })

  it('a 404 grace-frame straggler the dashboard drops neither fires the notice nor consumes the watch', async () => {
    // On a 404 the awaiting close hold keeps grace frames so pre-pop straggler
    // frames stay hidden: such a frame is dropped from dashboard.slots. The
    // reconcile reads the accepted list, so a straggler listing the key does
    // not fire the notice, and the watch stays armed for a real later re-add.
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toBeNull()

    // A straggler frame from before the pop still lists the key. The 404 hold
    // drops it from the accepted list, so it is not in dashboard.slots.
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    expect(root(store).dashboard.slots.some(s => s.key === 'doomed')).toBe(false)
    await store.dispatch(reconcileCloseRestoreWatch())
    // No notice, and the watch was not consumed by the dropped straggler.
    expect(chat(store).closeRefused).toBeNull()
    expect(chat(store).closeRestoreWatch.doomed).toBeDefined()
  })

  it('a watched key absent from the accepted list is neither fired nor consumed', async () => {
    // A stale pre-close fetchSlots reply that still names the closed key is
    // filtered out of dashboard.slots by the dashboard reconciliation. The
    // reconcile reads only the accepted list, so the key's absence means no
    // notice and the watch is preserved for a real later re-add.
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('peer')]))
    store.dispatch(armCloseRestoreWatch({ key: 'doomed', title: 'doomed' }))

    // doomed is not in the accepted dashboard.slots (the stale row was filtered).
    expect(root(store).dashboard.slots.some(s => s.key === 'doomed')).toBe(false)
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toBeNull()
    expect(chat(store).closeRestoreWatch.doomed).toBeDefined()

    // A genuine later re-add that the dashboard accepts fires the notice.
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed', reason: 'failed' })
  })

  it('a slot the server re-adds after a recovery list showed it gone raises the notice once', async () => {
    // The server pops the slot from its list BEFORE its slow archive save. The
    // recovery read catches that gap and shows the row gone, so no notice. When
    // the save then fails the server re-adds the slot and publishes a list; the
    // row comes back and must carry the "close didn't go through" notice.
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))
    // Recovery list showed the row gone: no notice yet, row gone.
    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])

    // The archive save fails: the server re-adds the slot and publishes a list.
    // The slotList hook reconciles the watch against the accepted list after
    // each frame; the test drives that reconcile explicitly.
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed', reason: 'failed' })
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')

    // A second server list for the same row does not re-raise a notice the
    // user may have dismissed.
    store.dispatch({ type: 'chat/setCloseRefused', payload: null })
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toBeNull()
  })

  it('a 404 close the server then re-adds raises the notice', async () => {
    // The 404 path: another close popped the key first; this close waits on its
    // outcome with a 3-frame grace window that hides pre-pop stragglers. Only
    // once that grace is spent does a list naming the key mean a genuine
    // restore the dashboard accepts; the notice fires then, not on a straggler.
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toBeNull()

    // Send straggler frames until the hold's grace is spent and the dashboard
    // accepts the row. While the row is hidden, no notice; once accepted, the
    // notice fires. (The exact grace count is the dashboard's business.)
    let accepted = false
    for (let i = 0; i < 6 && !accepted; i++) {
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      accepted = root(store).dashboard.slots.some(s => s.key === 'doomed')
      await store.dispatch(reconcileCloseRestoreWatch())
      if (!accepted) expect(chat(store).closeRefused).toBeNull()
    }
    expect(accepted).toBe(true)
    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed', reason: 'failed' })
  })

  it('a slot that stays gone after a recovery list showed it gone never raises a notice', async () => {
    // The close genuinely worked behind a proxy timeout: the row stays gone and
    // the server never re-adds it, so the watch set by the recovery read must
    // never fire.
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toBeNull()

    // Later server lists keep omitting the row; no notice ever.
    store.dispatch(sseSlots([slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    store.dispatch(sseSlots([slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
  })

  it('a new close press retires an outstanding re-add watch', async () => {
    // The recovery list showed the row gone (watch armed, no notice). Before any
    // re-add the user presses close again and it succeeds. That press owns the
    // outcome now, so a later server re-add must not raise the stale watch.
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    apiMock.deleteChatSlot
      .mockRejectedValueOnce(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      .mockResolvedValueOnce({ ok: true })
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toBeNull()

    // Second press on the same key, before any re-add: it succeeds.
    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    await new Promise(resolve => setTimeout(resolve, 0))

    // A later server list naming the key must not raise the retired watch.
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())
    expect(chat(store).closeRefused).toBeNull()
  })

  it('a watched key the user resumes from History raises no notice', async () => {
    // A code-less close can miss the `removed` frame (a dropped connection is
    // exactly that), so the watch can outlive a close that worked. When the
    // user later reopens the session from History, the re-added row is their
    // resume, not a failed close, and must not carry the still-open notice.
    const store = makeStore()
    store.dispatch(armCloseRestoreWatch({ key: 'doomed', title: 'doomed' }))
    // The resume supersedes the watch before any list re-adds the row.
    store.dispatch(resumeFromHistory.pending('req-1', { key: 'doomed', title: 'doomed' }, { startedAt: 1_000 }))
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())

    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')
  })

  it('a History resume passing a dashboard_ stem retires the watch on the canonical key', async () => {
    // History passes the transcript stem (`dashboard_chat-1-123`) while the
    // watch and the published row use the bare key (`chat-1-123`). Normalizing
    // the stem is what makes the resume retire the right watch, so the row the
    // resume publishes carries no false notice.
    const store = makeStore()
    store.dispatch(armCloseRestoreWatch({ key: 'chat-1-123', title: 'chat-1-123' }))
    store.dispatch(resumeFromHistory.pending('req-1', { key: 'dashboard_chat-1-123', title: 'x' }, { startedAt: 1_000 }))
    // The resume publishes the bare slot key; without the stem normalization
    // the watch would survive and fire here.
    store.dispatch(sseSlots([slotRow('chat-1-123'), slotRow('peer')]))
    await store.dispatch(reconcileCloseRestoreWatch())

    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('chat-1-123')
  })

  it('a watch older than the close-hold cap raises no notice when the row re-adds', async () => {
    // The tab missed the `removed` frame for a close that worked. The watch
    // must not linger forever: past the close-hold wall-clock cap it is stale,
    // so a much later re-add (or a resume) fires nothing.
    vi.useFakeTimers()
    try {
      const store = makeStore()
      store.dispatch(armCloseRestoreWatch({ key: 'doomed', title: 'doomed' }))
      // Well past CLOSE_IN_FLIGHT_MAX_MS (30s).
      vi.advanceTimersByTime(45_000)
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      await store.dispatch(reconcileCloseRestoreWatch())
      expect(chat(store).closeRefused).toBeNull()
      expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a removed frame after the notice clears it', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('doomed'), slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed', reason: 'failed' })

    store.dispatch(sseSlotPatch({ removed: ['peer'] }))
    expect(chat(store).closeRefused).toMatchObject({ key: 'doomed' })
    store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual([])
  })
})
