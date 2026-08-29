/** A queue edit that COMMITS while its HTTP response is lost must leave no failure banner.
 *
 *  The server commits and broadcasts on a channel separate from the response, so `onError` and the
 *  confirming `queue_edit` echo race. Either order has to end with no banner: an error raised over a
 *  correctly-applied card contradicts the screen, and QueueStack renders on the error alone, so it
 *  outlives the card once the entry drains.
 *
 *  Driven through the reducer that publishes the signal rather than a hand-written state object, so
 *  the test cannot pass against a shape the reducer does not actually write.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { editQueuedMessage, setActiveSlot, replaceMessages } from './chatSlice'
import type { ChatMessage } from './chatSlice'

const SLOT = 'slot-edit-settled'
const QID = 'q-lost-response'

const makeStore = () => {
  const store = configureStore({
    reducer: { chat: chatReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(replaceMessages([{
    role: 'queued', content: 'the original text', cls: '', ts: '2026-09-12T10:00:00.000Z',
    meta: { queueId: QID },
  } as ChatMessage]))
  return store
}

describe('a confirmed queue edit publishes a settlement a failing surface can read', () => {
  it('names the entry and advances the sequence when the server confirms', () => {
    const store = makeStore()
    expect(store.getState().chat.queueEditSettled, 'premise: nothing is settled yet').toBeNull()

    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: QID, content: 'the committed text', editId: 'edit-1',
    }))

    const settled = store.getState().chat.queueEditSettled
    expect(settled?.queueId, 'the failing surface keys on the entry it holds an error for').toBe(QID)
    expect(settled?.editId).toBe('edit-1')
    expect(settled?.seq, 'a sequence is what dates the confirmation to one edit').toBeGreaterThan(0)
  })

  it('does NOT publish a settlement for an unconfirmed optimistic edit', () => {
    // Positive control: publishing on every edit would retire a banner for a failure that stands.
    const store = makeStore()

    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: QID, content: 'typed but not taken', confirmed: false,
    }))

    expect(store.getState().chat.queueEditSettled,
      'an edit the server has not taken settles nothing').toBeNull()
  })

  it('advances the sequence again on a LATER confirmation of the same entry', () => {
    // A second edit of one card must be distinguishable, or its genuine failure would be masked by
    // the settlement the FIRST edit published.
    const store = makeStore()
    store.dispatch(editQueuedMessage({ slot: SLOT, queue_id: QID, content: 'first' }))
    const first = store.getState().chat.queueEditSettled?.seq ?? 0

    store.dispatch(editQueuedMessage({ slot: SLOT, queue_id: QID, content: 'second' }))

    expect(store.getState().chat.queueEditSettled?.seq).toBeGreaterThan(first)
  })
})
