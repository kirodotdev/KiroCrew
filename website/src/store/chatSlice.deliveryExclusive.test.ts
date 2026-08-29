/** Confirmation and doubt are mutually exclusive, from BOTH directions.
 *
 *  The markers are independent booleans, so the pair `deliveryConfirmed` + `deliveryUnknown` is
 *  representable -- and a reader resolving that on its own is how a bubble ends up muted while the
 *  composer beside it reads "delivered". Every confirm path already retired doubt; the gap was doubt
 *  arriving AFTER a confirmation, which no precedence function can tell from a live doubt.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot, appendMessage, markDeliveryUnknown, confirmOptimisticSend,
} from './chatSlice'
import { deliveryInDoubt } from '../utils/sendDelivery'

const SLOT = 'chat-1-exclusive'
const SEND = 's-excl-1'
const makeStore = () => configureStore({ reducer: { chat: chatReducer } })

const seeded = () => {
  const store = makeStore()
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(appendMessage({
    role: 'user', content: 'the prompt', cls: '', ts: '2026-09-08T00:00:00.000Z',
    meta: { sendId: SEND, optimistic: true },
  }))
  return store
}
const row = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.find(m => m.role === 'user')?.meta as Record<string, unknown>

describe('a row never holds confirmation and doubt at once', () => {
  beforeEach(() => vi.clearAllMocks())

  it('refuses to re-doubt a send the server already answered', () => {
    const store = seeded()
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND, mid: 'm-1' }))
    expect(row(store).deliveryConfirmed, 'premise: the send is confirmed').toBe(true)

    // A late unknown receipt for the SAME send, which the response/echo race makes reachable.
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND }))

    expect(row(store).deliveryUnknown, 'an answered send stays answered').toBeUndefined()
    expect(deliveryInDoubt(row(store)), 'and reads as settled').toBe(false)
  })

  it('still marks doubt on a send nothing has answered', () => {
    // Positive control: refusing every mark would lose the caption this PR exists to show.
    const store = seeded()

    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND }))

    expect(row(store).deliveryUnknown, 'an unanswered send is doubted').toBe(true)
    expect(deliveryInDoubt(row(store))).toBe(true)
  })

  it('retires doubt when the confirmation arrives second', () => {
    const store = seeded()
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND }))
    expect(row(store).deliveryUnknown).toBe(true)

    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND, mid: 'm-2' }))

    expect(row(store).deliveryUnknown, 'confirmation clears the doubt it answers').toBeUndefined()
    expect(row(store).deliveryConfirmed).toBe(true)
  })
})
