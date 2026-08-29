/** A merged echo folds SEVERAL sends into ONE canonical message, so it carries exactly one `mid`.
 *
 *  `mid` is the row identity that deep links and pins resolve on, so spreading one echo's meta
 *  across every matched optimistic row hands two rows the same identity, and a deep link then
 *  lands on whichever of them is found first.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, appendMessage, sseChatMessage } from './chatSlice'

const makeStore = () => configureStore({
  reducer: { chat: chatReducer },
  middleware: (getDefault) => getDefault({ immutableCheck: false, serializableCheck: false }),
})

const SLOT = 'chat-merged-echo-identity'
const userRows = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.filter(m => m.role === 'user')

const optimistic = (content: string, sendId: string, ts: string) =>
  appendMessage({ role: 'user', content, cls: '', ts, meta: { sendId, optimistic: true } })

describe('a merged echo never gives two rows one identity', () => {
  it('leaves no two user rows sharing a mid', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(optimistic('first thing', 's-1', '2026-09-07T10:00:00.000Z'))
    store.dispatch(optimistic('second thing', 's-2', '2026-09-07T10:00:01.000Z'))
    expect(userRows(store), 'premise: two optimistic sends are pending').toHaveLength(2)

    // The server folded BOTH sends into one persisted message, naming both in `sendIds`.
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'first thing\nsecond thing',
      cls: '',
      ts: '2026-09-07T10:00:02.000Z',
      meta: { sendId: 's-2', sendIds: ['s-1', 's-2'], mid: 'm-canonical' },
    }))

    const mids = userRows(store).map(r => r.meta?.mid).filter(Boolean)
    expect(mids.length, 'premise: the canonical identity reached the transcript').toBeGreaterThan(0)
    expect(new Set(mids).size,
      'each row must carry its OWN identity, or a deep link resolves the wrong bubble')
      .toBe(mids.length)
  })

  it('still reconciles a SINGLE-send echo in place instead of appending a duplicate', () => {
    // Positive control: retiring every matched row unconditionally would double the transcript on
    // the ordinary one-send path, which is the case this helper exists to collapse.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(optimistic('just the one', 's-solo', '2026-09-07T11:00:00.000Z'))

    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'just the one',
      cls: '',
      ts: '2026-09-07T11:00:01.000Z',
      meta: { sendId: 's-solo', mid: 'm-solo' },
    }))

    const rows = userRows(store)
    expect(rows, 'one send is one row').toHaveLength(1)
    expect(rows[0].meta?.mid).toBe('m-solo')
  })
})
