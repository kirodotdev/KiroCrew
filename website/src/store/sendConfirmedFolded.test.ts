/** A send FOLDED into a merged drain echo is still delivered.
 *
 *  The drain merges several sends into one row that carries `sendIds: [X, Y]` while its scalar
 *  `sendId` names only the last. Judging confirmation on the scalar reported the
 *  earlier send unconfirmed, so a later transport error re-armed its text and invited the user to
 *  resend a turn the server had already taken.
 *
 *  Rows arrive through `replaceMessages`, the server-row path: `appendMessage` is the OPTIMISTIC
 *  append and stamps `optimistic: true`, which no server-confirmed row carries.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { replaceMessages, setActiveSlot, selectSendConfirmed } from './chatSlice'
import type { RootState } from './index'
import type { ChatMessage } from './chatSlice'

const SLOT = 'slot-folded-send'

const makeStore = (rows: ChatMessage[]) => {
  const store = configureStore({
    reducer: { chat: chatReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(replaceMessages(rows))
  return store.getState() as unknown as RootState
}

const row = (meta: Record<string, unknown>): ChatMessage => ({
  role: 'user', content: 'delivered', cls: '', ts: '2026-09-12T10:00:00.000Z', meta,
} as ChatMessage)

describe('a send folded into a merged drain row counts as confirmed', () => {
  it('confirms the EARLIER send whose id survives only in the folded set', () => {
    const state = makeStore([row({ sendId: 'send-Y', sendIds: ['send-X', 'send-Y'] })])

    expect(selectSendConfirmed(state, SLOT, 'send-Y'),
      'premise: the scalar id is confirmed either way').toBe(true)
    expect(selectSendConfirmed(state, SLOT, 'send-X'),
      'the folded send was delivered by the same row, so re-arming its text would duplicate the turn')
      .toBe(true)
  })

  it('confirms a send whose id reconciliation rewrote as confirmedSendId', () => {
    // Reconciliation DELETES `sendId` and rewrites it here, so no identity list carries it.
    const state = makeStore([row({ confirmedSendId: 'send-R' })])

    expect(selectSendConfirmed(state, SLOT, 'send-R'),
      'a reconciled row proves delivery as much as the row it replaced').toBe(true)
  })

  it('still refuses an OPTIMISTIC row, which proves nothing', () => {
    // Positive control: confirming on identity alone would report every un-sent bubble delivered.
    const state = makeStore([row({ sendId: 'send-O', sendIds: ['send-O'], optimistic: true })])

    expect(selectSendConfirmed(state, SLOT, 'send-O'),
      'an optimistic bubble is not a server confirmation').toBe(false)
  })

  it('refuses a send no row stands for', () => {
    // Second positive control: a selector that answered true for everything would pass the rest.
    const state = makeStore([row({ sendId: 'send-Y', sendIds: ['send-X', 'send-Y'] })])

    expect(selectSendConfirmed(state, SLOT, 'send-absent'),
      'an unrelated id must not be reported delivered').toBe(false)
  })
})
