/**
 * `dropUnconfirmedRow` guard coverage — the strip's exit splices a rendered row out, so a
 * mis-hit deletes a message that did reach the server and the user sees it vanish.
 *
 * The guard keys on `optimistic`, the flag every confirmation clears. A `mid` is stamped only
 * on some confirmations, so it cannot be the proof.
 */
import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, appendSlotMessage, confirmOptimisticSend, dropUnconfirmedRow, switchSlot } from '../store/chatSlice'
import { RECONCILE_WINDOW } from './chat/transcript'
import type { ChatMessage } from '../types'

const SLOT = 'slot-a'
const FIXED_TS = '2026-01-01T00:00:00.000Z'

function withBubble(sendId: string, meta: Record<string, unknown> = {}) {
  let state = reducer(undefined, { type: '@@INIT' })
  state = { ...state, activeSlot: SLOT }
  return reducer(state, appendMessage({
    role: 'user', content: 'did this land?', cls: 'msg msg-u',
    ts: FIXED_TS,
    meta: { optimistic: true, sendId, ...meta },
  }))
}

const userRows = (msgs: ChatMessage[]) => msgs.filter(m => m.role === 'user')

describe('dropUnconfirmedRow', () => {
  it('keeps a resend made after Remove during a session switch when the history lands', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = {
      ...state,
      activeSlot: 'other',
      slotMessages: {
        [SLOT]: [
          { role: 'assistant', content: 'hi', cls: '', ts: FIXED_TS, meta: { mid: 'm-hi' } },
          { role: 'user', content: 'did this land?', cls: 'msg msg-u', ts: FIXED_TS, meta: { optimistic: true, sendId: 'sw-1' } },
          { role: 'notice', content: 'in doubt', cls: '', ts: FIXED_TS, meta: { deliveryNoticeFor: 'sw-1' } },
        ],
      },
    }
    state = reducer(state, switchSlot.pending('req-sw', SLOT))
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'sw-1' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'did this land?', cls: 'msg msg-u', ts: FIXED_TS, meta: { optimistic: true, sendId: 'sw-2' } }))
    state = reducer(state, switchSlot.fulfilled({
      key: SLOT, messages: [{ role: 'assistant', content: 'hi', cls: '', ts: FIXED_TS, meta: { mid: 'm-hi' } }],
      hasMore: false, running: false, total: 1, queue: [],
    } as never, 'req-sw', SLOT))
    expect(userRows(state.messages).map(m => m.meta?.sendId)).toEqual(['sw-2'])
  })

  it('keeps a row removed during a session switch gone after switching away and back', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = {
      ...state,
      activeSlot: 'other',
      slotMessages: {
        [SLOT]: [
          { role: 'user', content: 'did this land?', cls: 'msg msg-u', ts: FIXED_TS, meta: { optimistic: true, sendId: 'sw-3' } },
          { role: 'notice', content: 'in doubt', cls: '', ts: FIXED_TS, meta: { deliveryNoticeFor: 'sw-3' } },
        ],
      },
    }
    state = reducer(state, switchSlot.pending('req-in', SLOT))
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'sw-3' }))
    expect(state.messages).toHaveLength(0)
    state = reducer(state, switchSlot.pending('req-out', 'other'))
    state = reducer(state, switchSlot.pending('req-back', SLOT))
    expect(userRows(state.messages)).toHaveLength(0)
    expect(state.messages.filter(m => m.meta?.deliveryNoticeFor === 'sw-3')).toHaveLength(0)
  })

  it('removes the row the send names', () => {
    let state = withBubble('drop-1')
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-1' }))
    expect(userRows(state.messages)).toHaveLength(0)
  })

  it('refuses a row confirmation has claimed, even without a mid', () => {
    let state = withBubble('drop-2')
    state = reducer(state, confirmOptimisticSend({ slot: SLOT, sendId: 'drop-2' }))
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-2' }))
    expect(userRows(state.messages)).toHaveLength(1)
  })

  it('still removes an unconfirmed row that happens to carry a mid', () => {
    let state = withBubble('drop-5', { mid: 'm-42' })
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-5' }))
    expect(userRows(state.messages)).toHaveLength(0)
  })

  it('takes its own in-doubt notice with it and leaves other notices', () => {
    let state = withBubble('drop-6')
    for (const [content, meta] of [['in doubt', { deliveryNoticeFor: 'drop-6' }], ['other', { deliveryNoticeFor: 'x' }], ['plain', undefined]] as const) {
      state = reducer(state, appendMessage({ role: 'notice', content, cls: '', ts: FIXED_TS, ...(meta ? { meta } : {}) }))
    }
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-6' }))
    expect(state.messages.filter(m => m.role === 'notice').map(m => m.content)).toEqual(['other', 'plain'])
  })

  it('keeps its notice when the row was confirmed and so is not removed', () => {
    let state = withBubble('drop-7')
    state = reducer(state, appendMessage({ role: 'notice', content: 'in doubt', cls: '', ts: FIXED_TS, meta: { deliveryNoticeFor: 'drop-7' } }))
    state = reducer(state, confirmOptimisticSend({ slot: SLOT, sendId: 'drop-7' }))
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-7' }))
    expect(state.messages.filter(m => m.role === 'notice')).toHaveLength(1)
  })

  it('reaches a row and its notice older than the reconcile window', () => {
    let state = withBubble('drop-8')
    state = reducer(state, appendMessage({ role: 'notice', content: 'in doubt', cls: '', ts: FIXED_TS, meta: { deliveryNoticeFor: 'drop-8' } }))
    for (let i = 0; i < RECONCILE_WINDOW + 5; i++) {
      state = reducer(state, appendMessage({ role: 'assistant', content: `reply ${i}`, cls: '', ts: FIXED_TS }))
    }
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-8' }))
    expect(userRows(state.messages)).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'notice')).toHaveLength(0)
  })

  it('leaves a different send alone', () => {
    let state = withBubble('drop-3')
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'someone-else' }))
    expect(userRows(state.messages)).toHaveLength(1)
  })

  /** The user can switch sessions inside the delivery deadline, which leaves the bubble in
   *  `slotMessages` rather than the active array — the strip still names its own slot. */
  it('reaches a bubble left behind by a session switch', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = { ...state, activeSlot: 'other' }
    state = reducer(state, appendSlotMessage({
      slot: SLOT,
      message: {
        role: 'user', content: 'sent before the switch', cls: 'msg msg-u',
        ts: FIXED_TS, meta: { optimistic: true, sendId: 'drop-4' },
      },
    }))
    expect(userRows(state.slotMessages[SLOT])).toHaveLength(1)
    state = reducer(state, dropUnconfirmedRow({ slot: SLOT, sendId: 'drop-4' }))
    expect(userRows(state.slotMessages[SLOT])).toHaveLength(0)
  })
})
