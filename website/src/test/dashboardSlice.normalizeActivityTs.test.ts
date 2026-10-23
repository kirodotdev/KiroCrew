/**
 * `last_activity_ts` is normalized on the backend before it reaches the store.
 * The frontend's only remaining write that can advance it is `touchSlotActivity`,
 * which does so only for **settled** events (user-sent prompts) — not for the
 * un-settled streaming bumps that advance `last_ts` mid-turn — so the session
 * grid does not re-rank a running session on every streamed tool call.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseSlots, touchSlotActivity } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

const initial = reducer(undefined, { type: '@@INIT' })

const mkSlot = (overrides: Partial<ChatSlot>): ChatSlot => ({
  key: 'test',
  title: 'Test',
  messages: 1,
  running: false,
  pending_approval: false,
  waiting_for_input: false,
  ...overrides,
} as ChatSlot)

describe('touchSlotActivity — last_activity_ts advanced only on settled events', () => {
  const seedState = reducer(initial, sseSlots([
    mkSlot({ last_activity_ts: '2026-09-22T01:00:00+00:00', last_ts: '2026-09-22T01:00:00+00:00' }),
  ]))

  it('advances last_activity_ts when a newer settled event arrives', () => {
    const after = reducer(
      seedState,
      touchSlotActivity({ key: 'test', ts: '2026-09-22T02:00:00+00:00', settled: true }),
    )
    expect(after.slots[0].last_activity_ts).toBe('2026-09-22T02:00:00+00:00')
  })

  it('does not advance last_activity_ts for an unsettled bump (streaming tool call)', () => {
    // An unsettled bump only moves last_ts; advancing last_activity_ts too would
    // re-rank the session in the grid on every streamed event mid-turn.
    const after = reducer(
      seedState,
      touchSlotActivity({ key: 'test', ts: '2026-09-22T02:00:00+00:00' }),
    )
    expect(after.slots[0].last_activity_ts).toBe('2026-09-22T01:00:00+00:00')
    expect(after.slots[0].last_ts).toBe('2026-09-22T02:00:00+00:00')
  })

  it('does not move last_activity_ts backward', () => {
    const after = reducer(
      seedState,
      touchSlotActivity({ key: 'test', ts: '2026-09-22T00:30:00+00:00', settled: true }),
    )
    expect(after.slots[0].last_activity_ts).toBe('2026-09-22T01:00:00+00:00')
  })
})
