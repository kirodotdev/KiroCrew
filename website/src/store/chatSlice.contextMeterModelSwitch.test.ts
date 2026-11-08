// The dashboard context meter must stay correct when the model is switched
// mid-conversation. On a switch the transcript is unchanged but the new model's
// window differs, so the reducer keeps the `used` token count and drops the
// stale percentage and window: ContextUsageControl then derives the percentage
// from `used` against the new model's window, instead of pairing the old
// percentage with a window that does not describe it. A compaction / clear /
// session reset carries `pct: 0` and clears the counts, so the meter reads 0.
import { describe, expect, it } from 'vitest'
import reducer, { sseContextUsage } from './chatSlice'

const SLOT = 'slot-a'

// A conversation already in progress: 120k used of a 1M window (12%).
function midConversation() {
  let state = reducer(undefined, { type: '@@init' })
  state = reducer(state, sseContextUsage({ slot: SLOT, pct: 12, used_tokens: 120_000, window_tokens: 1_000_000 }))
  expect(state.slotContextPct[SLOT]).toBe(12)
  expect(state.slotContextTokens[SLOT]).toEqual({ used: 120_000, window: 1_000_000 })
  return state
}

describe('context meter on a model switch', () => {
  it('keeps used and drops the stale pct + window on a switch frame', () => {
    let state = midConversation()
    // Backend switch frame: window not known for the new model, carries only
    // `used_tokens` + `reset`. `used` must survive; the stale pct and window
    // must be dropped so the control derives the percentage against the new
    // window (120k / 200k ~= 60%, not 12%).
    state = reducer(state, sseContextUsage({ slot: SLOT, used_tokens: 120_000, reset: true }))
    expect(state.slotContextPct[SLOT]).toBeUndefined()
    expect(state.slotContextTokens[SLOT]).toEqual({ used: 120_000 })
  })

  it('keeps the stored used when the switch frame carries no counts (cold reset)', () => {
    let state = midConversation()
    // The no-provider cold-reset path sends only `{slot, reset}`; the reducer
    // keeps the `used` it already holds.
    state = reducer(state, sseContextUsage({ slot: SLOT, reset: true }))
    expect(state.slotContextPct[SLOT]).toBeUndefined()
    expect(state.slotContextTokens[SLOT]).toEqual({ used: 120_000 })
  })

  it('switching back and forth keeps the used count, never zeroes it', () => {
    let state = midConversation()
    state = reducer(state, sseContextUsage({ slot: SLOT, used_tokens: 120_000, reset: true })) // away
    state = reducer(state, sseContextUsage({ slot: SLOT, used_tokens: 120_000, reset: true })) // back
    expect(state.slotContextTokens[SLOT]).toEqual({ used: 120_000 })
    expect(state.slotContextPct[SLOT]).toBeUndefined()
  })

  it('clears the reading on a percentage-only switch (no used to measure)', () => {
    // A percentage-only backend never set a `used` count: seed pct-only, then a
    // switch frame with no counts must clear rather than guess a wrong number.
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 45 }))
    expect(state.slotContextPct[SLOT]).toBe(45)
    state = reducer(state, sseContextUsage({ slot: SLOT, reset: true }))
    expect(state.slotContextPct[SLOT]).toBeUndefined()
    expect(state.slotContextTokens[SLOT]).toBeUndefined()
  })

  it('reads 0 on compaction / clear (reset frame carries pct: 0)', () => {
    let state = midConversation()
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 0, reset: true }))
    expect(state.slotContextPct[SLOT]).toBe(0)
    expect(state.slotContextTokens[SLOT]).toBeUndefined()
  })

  it('a later real telemetry frame replaces the kept counts', () => {
    let state = midConversation()
    state = reducer(state, sseContextUsage({ slot: SLOT, used_tokens: 120_000, reset: true }))
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 60, used_tokens: 120_000, window_tokens: 200_000 }))
    expect(state.slotContextPct[SLOT]).toBe(60)
    expect(state.slotContextTokens[SLOT]).toEqual({ used: 120_000, window: 200_000 })
  })
})
