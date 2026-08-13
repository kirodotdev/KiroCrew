/** Context meter seeding from the slot-detail HTTP payload.
 *
 *  `context_usage` WS frames are turn-scoped, so reopening a session in a fresh
 *  tab left `slotContextPct`/`slotContextTokens` unseeded and the bar rendered
 *  at 0%. These guard the seeding path and — more importantly — the absent-only
 *  rule that keeps it from clobbering live measurements: the server broadcasts
 *  over WS before the HTTP response lands, so a turn's frame can arrive while
 *  the fetch is still in flight.
 */

import { describe, expect, it } from 'vitest'

import reducer, { sseContextUsage } from '../store/chatSlice'

const initial = reducer(undefined, { type: '@@INIT' })

const CTX = { pct: 31.5, used: 63000, window: 200000 }

function detailPayload(key: string, context?: { pct: number; used?: number; window?: number }) {
  return {
    key,
    messages: [{ role: 'user' as const, content: 'hi', cls: '' }],
    running: false,
    stopping: false,
    hasMore: false,
    total: 1,
    queue: [],
    context,
  }
}

function switchTo(state: typeof initial, key: string, context?: { pct: number; used?: number; window?: number }) {
  let s = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: key, requestId: 'r1', requestStatus: 'pending' } })
  s = reducer(s, {
    type: 'chat/switchSlot/fulfilled',
    meta: { arg: key, requestId: 'r1', requestStatus: 'fulfilled' },
    payload: detailPayload(key, context),
  })
  return s
}

describe('slot-detail context seeding', () => {
  it('switchSlot seeds the meter for a reopened session', () => {
    const state = switchTo({ ...initial, activeSlot: 'old' }, 'A', CTX)
    expect(state.slotContextPct['A']).toBe(31.5)
    expect(state.slotContextTokens['A']).toEqual({ used: 63000, window: 200000 })
  })

  it('leaves the meter unseeded when the payload carries no reading', () => {
    // A fresh session, or a cold one whose model changed: the backend omits the
    // fields entirely rather than sending zeros, so the frontend keeps falling
    // back to its model-derived window instead of claiming "0 of 0 tokens".
    const state = switchTo({ ...initial, activeSlot: 'old' }, 'A', undefined)
    expect(state.slotContextPct['A']).toBeUndefined()
    expect(state.slotContextTokens['A']).toBeUndefined()
  })

  it('does NOT overwrite a live WS reading that landed during the fetch', () => {
    // The regression this rule exists for: the WS frame is newer than the
    // snapshot the HTTP response was built from, so seeding must lose.
    let state: typeof initial = { ...initial, activeSlot: 'old' }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'A', requestId: 'r1', requestStatus: 'pending' } })
    state = reducer(state, sseContextUsage({ slot: 'A', pct: 72, used_tokens: 144000, window_tokens: 200000 }))
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'A', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: detailPayload('A', CTX),
    })
    expect(state.slotContextPct['A']).toBe(72)
    expect(state.slotContextTokens['A']).toEqual({ used: 144000, window: 200000 })
  })

  it('does not resurrect counts a compaction reset just deleted', () => {
    // A pct-only reset (compaction) deletes the token entry but leaves pct at
    // 0. That slot still HAS an entry, so a late seed must not refill it with
    // the pre-compaction numbers.
    let state: typeof initial = { ...initial, activeSlot: 'old' }
    state = reducer(state, sseContextUsage({ slot: 'A', pct: 0, reset: true }))
    state = switchTo(state, 'A', CTX)
    expect(state.slotContextPct['A']).toBe(0)
    expect(state.slotContextTokens['A']).toBeUndefined()
  })

  it('warmSlotCache seeds a background slot', () => {
    const state = reducer({ ...initial, activeSlot: 'other' }, {
      type: 'chat/warmSlotCache/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: detailPayload('B', CTX),
    })
    expect(state.slotContextPct['B']).toBe(31.5)
    expect(state.slotContextTokens['B']).toEqual({ used: 63000, window: 200000 })
  })

  it('refreshSlot seeds the active slot', () => {
    const state = reducer({ ...initial, activeSlot: 'A' }, {
      type: 'chat/refreshSlot/fulfilled',
      meta: { arg: 'A', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: detailPayload('A', CTX),
    })
    expect(state.slotContextPct['A']).toBe(31.5)
  })

  it('seeds the window when used is undefined', () => {
    // The shape a cold-session recovery arrives in: the server omits the count
    // it never measured, so `used` is undefined while the window is known. That
    // is what makes the tooltip render its `~` approximation from pct instead
    // of asserting a figure.
    const state = switchTo({ ...initial, activeSlot: 'old' }, 'A', { pct: 31.5, used: undefined, window: 200000 })
    expect(state.slotContextPct['A']).toBe(31.5)
    expect(state.slotContextTokens['A']?.window).toBe(200000)
    expect(state.slotContextTokens['A']?.used).toBeUndefined()
  })

  it('a pct-only reading seeds the bar and stores no token entry', () => {
    // The common shape: kiro-cli reports a percentage without absolute counts.
    // Writing no token entry is what keeps the meter on its model-derived
    // window rather than a fabricated one.
    const state = switchTo({ ...initial, activeSlot: 'old' }, 'A', { pct: 11.4 })
    expect(state.slotContextPct['A']).toBe(11.4)
    expect(state.slotContextTokens['A']).toBeUndefined()
  })
})
