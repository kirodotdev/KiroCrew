import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, {
  addSlotOptimistic,
  removeSlotOptimistic,
  slotCloseStarted,
  slotCloseRetireRead,
} from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

/** A close whose DELETE response was LOST must stay tombstoned until the outcome is
 *  PROVEN. Its bound read is issued after the request but can outrun `close_slot`, so a
 *  reply that STILL LISTS the key says nothing about whether the pop happened. Retiring
 *  on the read's identity alone restored the row, a new turn could start on it, and the
 *  continuing close then popped the slot and cancelled that turn. */

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })

const makeStore = () => {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  store.dispatch(addSlotOptimistic(slot('chat-1')))
  store.dispatch(addSlotOptimistic(slot('chat-2')))
  return store
}

/** Begin a close the way the thunk does, then bind its confirming read. */
const beginClose = (store: ReturnType<typeof makeStore>, key: string, readId: string) => {
  store.dispatch(slotCloseStarted(key))
  store.dispatch(removeSlotOptimistic(key))
  store.dispatch(slotCloseRetireRead({ key, readId }))
}

const applyRead = (
  store: ReturnType<typeof makeStore>,
  keys: string[],
  readId: string,
) => {
  store.dispatch({
    type: 'dashboard/fetchSlots/fulfilled',
    payload: keys.map(slot),
    meta: { requestId: readId },
  })
}

const state = (store: ReturnType<typeof makeStore>) => store.getState().dashboard
const rows = (store: ReturnType<typeof makeStore>) => state(store).slots.map(s => s.key)

describe('a close tombstone retires only on proof of the pop', () => {
  it('does NOT retire when its own read still lists the key', () => {
    const store = makeStore()
    beginClose(store, 'chat-2', 'read-A')
    expect(rows(store)).toEqual(['chat-1'])

    // The bound reply arrives BEFORE the server finished popping, so it still lists it.
    applyRead(store, ['chat-1', 'chat-2'], 'read-A')

    expect(state(store).closingSlots?.['chat-2']).toBeDefined()
    expect(rows(store)).toEqual(['chat-1'])
  })

  it('retires once an accepted read OMITS the key', () => {
    const store = makeStore()
    beginClose(store, 'chat-2', 'read-A')

    applyRead(store, ['chat-1'], 'read-A')

    expect(state(store).closingSlots?.['chat-2']).toBeUndefined()
    expect(rows(store)).toEqual(['chat-1'])
  })

  it('keeps withholding across a later still-listing read, then settles on omission', () => {
    const store = makeStore()
    beginClose(store, 'chat-2', 'read-A')

    // A poll that outran the close: still listed, so still no proof.
    applyRead(store, ['chat-1', 'chat-2'], 'read-A')
    expect(state(store).closingSlots?.['chat-2']).toBeDefined()
    expect(rows(store)).toEqual(['chat-1'])

    // `reconcileSlots` never gives up; the read that finally omits it settles it.
    store.dispatch(slotCloseRetireRead({ key: 'chat-2', readId: 'read-B' }))
    applyRead(store, ['chat-1'], 'read-B')
    expect(state(store).closingSlots?.['chat-2']).toBeUndefined()
    expect(rows(store)).toEqual(['chat-1'])
  })

  /** Control: an UNRELATED read must not retire someone else's tombstone just by
   *  omitting the key, or the id binding would be doing no work at all. */
  it('ignores an omission from a read the close never bound', () => {
    const store = makeStore()
    beginClose(store, 'chat-2', 'read-A')

    applyRead(store, ['chat-1'], 'read-OTHER')

    expect(state(store).closingSlots?.['chat-2']).toBeDefined()
  })
})
