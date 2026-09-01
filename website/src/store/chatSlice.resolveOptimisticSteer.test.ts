/**
 * `resolveOptimisticSteer` guard coverage — the branches a single steer send
 * cannot reach.
 *
 * The reducer rewrites or DELETES a rendered row, so a mis-hit destroys a
 * message the user typed. Three guards carry that weight: the `optimistic` gate
 * (a row the server already claimed is off limits), `sendId` identity, and the
 * `slotMessages` array the bubble sits in after a session switch.
 */
import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, appendSlotMessage, resolveOptimisticSteer } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'slot-a'

/** A store slice holding one optimistic steer bubble in the active array. */
function withSteerBubble(sendId: string, meta: Record<string, unknown> = {}) {
  let state = reducer(undefined, { type: '@@INIT' })
  state = { ...state, activeSlot: SLOT }
  return reducer(state, appendMessage({
    role: 'user', content: 'change course', cls: 'msg msg-u',
    ts: new Date().toISOString(),
    meta: { steer: true, optimistic: true, sendId, ...meta },
  }))
}

const userRows = (msgs: ChatMessage[]) => msgs.filter(m => m.role === 'user')

describe('resolveOptimisticSteer', () => {
  /** Every arm that answers `queued: true` has already broadcast a `queue_push`
   *  — including the turn teardown, which is why the requeued arm does not
   *  re-broadcast — so that card owns the text and the bubble is a duplicate. */
  it('removes a queued bubble and leaves the rest of the transcript intact', () => {
    let state = withSteerBubble('drain-1')
    const before = state.messages.length
    state = reducer(state, resolveOptimisticSteer({ slot: SLOT, sendId: 'drain-1', outcome: 'queued' }))
    expect(userRows(state.messages)).toHaveLength(0)
    expect(state.messages).toHaveLength(before - 1)
  })

  it('strips only the steer flag on the new-turn outcome, preserving the row and its sendId', () => {
    let state = withSteerBubble('s1')
    state = reducer(state, resolveOptimisticSteer({ slot: SLOT, sendId: 's1', outcome: 'turn' }))
    const rows = userRows(state.messages)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBeUndefined()
    // The sendId is the reconciliation key for any later echo, so demotion must
    // not cost the row its identity.
    expect(rows[0].meta?.sendId).toBe('s1')
    expect(rows[0].content).toBe('change course')
  })

  it('leaves a row the server has already claimed untouched', () => {
    // No `optimistic` flag: the server already owns this row, so deleting it
    // would erase a message the backend has persisted.
    let state = withSteerBubble('s1', { optimistic: undefined })
    state = reducer(state, resolveOptimisticSteer({ slot: SLOT, sendId: 's1', outcome: 'queued' }))
    const rows = userRows(state.messages)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBe(true)
  })

  it('ignores a receipt whose sendId matches no bubble', () => {
    let state = withSteerBubble('s1')
    state = reducer(state, resolveOptimisticSteer({ slot: SLOT, sendId: 'other', outcome: 'queued' }))
    expect(userRows(state.messages)).toHaveLength(1)
  })

  it('resolves a bubble that lives in slotMessages after a session switch', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = { ...state, activeSlot: 'slot-b' }
    state = reducer(state, appendSlotMessage({
      slot: SLOT,
      message: {
        role: 'user', content: 'change course', cls: 'msg msg-u',
        ts: new Date().toISOString(),
        meta: { steer: true, optimistic: true, sendId: 's1' },
      },
    }))
    expect(userRows(state.slotMessages[SLOT])).toHaveLength(1)
    state = reducer(state, resolveOptimisticSteer({ slot: SLOT, sendId: 's1', outcome: 'queued' }))
    expect(userRows(state.slotMessages[SLOT])).toHaveLength(0)
  })

  it('refuses an unsafe slot key', () => {
    let state = withSteerBubble('s1')
    state = reducer(state, resolveOptimisticSteer({ slot: '__proto__', sendId: 's1', outcome: 'queued' }))
    expect(userRows(state.messages)).toHaveLength(1)
  })
})
