/**
 * A close whose DELETE fails without the server's own refusal code (a proxy
 * 502/504, a dropped connection) may still have worked. The "still open"
 * notice is decided from the reconciled slot list after the recovery read, so
 * a close that worked is never reported as still open.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { deleteSlot, setActiveSlot, sseChatMessage } from '../store/chatSlice'
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
