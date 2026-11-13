/**
 * A close whose DELETE fails without the server's own refusal code (a proxy
 * 502/504, a dropped connection) may still have worked. The "still open"
 * notice is decided from the reconciled slot list after the recovery read, so
 * a close that worked is never reported as still open.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { deleteSlot, resumeFromHistory, setActiveSlot, setCloseRefused, sseChatMessage } from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic, fetchSlots, sseSlotPatch, sseSlots } from '../store/dashboardSlice'
import { selectCloseNotice } from '../store/dashboard/closeWatch'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const apiMock = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  chatSlots: vi.fn(),
  deleteChatSlot: vi.fn(),
  resumeChatSlot: vi.fn(),
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

  it('a late uncoded failure does not report a retry confirmed after the first hold expired', async () => {
    vi.useFakeTimers()
    onTestFinished(() => vi.useRealTimers())
    let rejectFirst: (reason?: unknown) => void = () => {}
    apiMock.chatSlots.mockRejectedValueOnce(new Error('offline'))
    apiMock.deleteChatSlot
      .mockReturnValueOnce(new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockResolvedValueOnce({})
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    const first = store.dispatch(deleteSlot('doomed'))
    vi.setSystemTime(Date.now() + 30_001)
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    expect(root(store).dashboard.closeConfirmations.doomed).toBe(1)

    rejectFirst(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    expect((await first).type).toBe('chat/deleteSlot/rejected')
    await Promise.resolve()

    expect(chat(store).closeRefused).toBeNull()
  })

  it('a rejected recovery read does not report a confirmed retry as refused after its list budget is spent', async () => {
    let rejectRecovery: (reason?: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRecovery = reject }))
    apiMock.deleteChatSlot
      .mockRejectedValueOnce(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      .mockResolvedValueOnce({})
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    for (let i = 0; i < 3; i++) store.dispatch(sseSlots([slotRow('peer')]))
    expect(root(store).dashboard.closingSlots.doomed?.graceFrames).toBe(0)

    rejectRecovery(new Error('offline'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(root(store).dashboard.closingSlots.doomed).toBeUndefined()
    expect(chat(store).closeRefused).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
  })

  it('a pre-close recovery list does not report a confirmed retry as refused after its list budget is spent', async () => {
    let answerRecovery: (v: unknown) => void = () => {}
    apiMock.chatSlots.mockReturnValueOnce(new Promise(resolve => { answerRecovery = resolve }))
    apiMock.deleteChatSlot
      .mockRejectedValueOnce(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      .mockResolvedValueOnce({})
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/rejected')
    expect((await store.dispatch(deleteSlot('doomed'))).type).toBe('chat/deleteSlot/fulfilled')
    for (let i = 0; i < 3; i++) store.dispatch(sseSlots([slotRow('peer')]))

    answerRecovery([slotRow('doomed'), slotRow('peer')])
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

  it('a removed frame before a delayed 404 keeps a later listing quiet', async () => {
    let rejectClose: (reason?: unknown) => void = () => {}
    apiMock.deleteChatSlot.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectClose = reject }))
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      const store = makeStore()
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

      const close = store.dispatch(deleteSlot('doomed'))
      expect(apiMock.deleteChatSlot).toHaveBeenCalledWith('doomed', undefined)
      store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
      expect(root(store).dashboard.durablyRemoved.doomed).toBe(true)

      const confirmedUntil = root(store).dashboard.closingSlots.doomed?.confirmedUntil
      expect(confirmedUntil).toEqual(expect.any(Number))
      clock.mockReturnValue(confirmedUntil as number)
      store.dispatch(sseSlots([slotRow('peer')]))
      expect(root(store).dashboard.closingSlots.doomed).toBeUndefined()

      rejectClose(Object.assign(new Error('not found'), { status: 404 }))
      expect((await close).type).toBe('chat/deleteSlot/fulfilled')

      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      expect(selectCloseNotice(root(store))).toBeNull()
    } finally {
      clock.mockRestore()
    }
  })

  it('a timed-out close the server later puts back raises the notice, and dismissing it clears it (#31168)', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed', { title: 'Doomed notes' }), slotRow('peer')]))
    await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(selectCloseNotice(root(store))).toBeNull()

    store.dispatch(sseSlots([slotRow('doomed', { title: 'Doomed notes' }), slotRow('peer')]))
    expect(selectCloseNotice(root(store))).toEqual({ key: 'doomed', title: 'Doomed notes', reason: 'failed' })

    store.dispatch(setCloseRefused(null))
    expect(selectCloseNotice(root(store))).toBeNull()
  })

  it('a removed frame after a timed-out close keeps a later listing of that key quiet (#31168)', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))

    store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    expect(selectCloseNotice(root(store))).toBeNull()
  })

  it('this tab reopening the key after a timed-out close is not reported as the close undone (#31168)', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))

    store.dispatch(addSlotOptimistic(slotRow('doomed')))
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    expect(selectCloseNotice(root(store))).toBeNull()
  })

  it('resuming the session from History after a timed-out close is not reported as the close undone (#31168)', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))

    let noticeWhenListed: unknown = 'unset'
    apiMock.resumeChatSlot.mockImplementation(async () => {
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      noticeWhenListed = selectCloseNotice(root(store))
      return { ok: true, key: 'doomed', mode: 'default', messages: [] }
    })
    await store.dispatch(resumeFromHistory({ key: 'dashboard_doomed', title: 'doomed' }))
    expect(noticeWhenListed).toBeNull()
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('doomed')
    expect(selectCloseNotice(root(store))).toBeNull()
  })

  it('the session reopened elsewhere after a timed-out close is not reported, the same session put back is (#31537)', async () => {
    for (const [listed, expected] of [
      ['inc-reopened', null],
      ['inc-closed', { key: 'doomed', title: null, reason: 'failed' }],
    ] as const) {
      apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
      apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
      const store = makeStore()
      store.dispatch(sseSlots([slotRow('doomed', { incarnation: 'inc-closed' }), slotRow('peer')]))
      await store.dispatch(deleteSlot('doomed'))
      await vi.waitFor(() => expect(root(store).dashboard.closeWatch?.doomed).toBeDefined())

      store.dispatch(sseSlots([slotRow('doomed', { incarnation: listed }), slotRow('peer')]))
      expect(selectCloseNotice(root(store))).toEqual(expected)
      expect(root(store).dashboard.closeWatch?.doomed).toBeUndefined()
    }
  })

  it('a listing after the watch window is not tied to the timed-out close (#31168)', async () => {
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      const store = makeStore()
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      await store.dispatch(deleteSlot('doomed'))
      await new Promise(resolve => setTimeout(resolve, 0))

      clock.mockReturnValue(1_700_000_000_000 + 10 * 60_000)
      store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
      expect(selectCloseNotice(root(store))).toBeNull()
    } finally {
      clock.mockRestore()
    }
  })

  it('a list fetched before a timed-out close does not count as the server putting it back (#31168)', async () => {
    let answerStale: (v: unknown) => void = () => {}
    apiMock.chatSlots
      .mockReturnValueOnce(new Promise(resolve => { answerStale = resolve }))
      .mockResolvedValueOnce([slotRow('peer')])
    apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('Gateway Timeout'), { status: 504 }))
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))

    const stale = store.dispatch(fetchSlots())
    await store.dispatch(deleteSlot('doomed'))
    await new Promise(resolve => setTimeout(resolve, 0))
    answerStale([slotRow('doomed'), slotRow('peer')])
    await stale

    expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
    expect(selectCloseNotice(root(store))).toBeNull()
  })

  it('a 404 close whose popping sibling is still saving past the in-flight cap raises no false notice (#31538)', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      // This tab's DELETE answers 404: another close popped the key first and is
      // still slowly saving the long-context session. The 404 completed THIS close.
      apiMock.deleteChatSlot.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
      apiMock.chatSlots.mockResolvedValue([slotRow('peer')])
      const store = makeStore()
      store.dispatch(sseSlots([slotRow('doomed', { title: 'Doomed notes' }), slotRow('peer')]))

      await store.dispatch(deleteSlot('doomed'))
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(selectCloseNotice(root(store))).toBeNull()

      // Past the 30s in-flight cap the hold lifts and the next list re-shows the
      // row because the sibling save has not landed yet. That re-show is the slow
      // save, not the server undoing the close, so it must stay quiet.
      clock.mockReturnValue(1_700_000_000_000 + 30_001)
      store.dispatch(sseSlots([slotRow('doomed', { title: 'Doomed notes' }), slotRow('peer')]))
      expect(selectCloseNotice(root(store))).toBeNull()

      // When the slow save finally lands, the row is removed for good.
      store.dispatch(sseSlotPatch({ removed: ['doomed'] }))
      expect(selectCloseNotice(root(store))).toBeNull()
      expect(root(store).dashboard.slots.map(s => s.key)).toEqual(['peer'])
    } finally {
      clock.mockRestore()
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
