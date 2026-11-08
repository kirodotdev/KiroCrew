import { describe, it, expect, vi } from 'vitest'
import reducer, { sseSlots, touchSlotActivity, markSlotUnread } from '../store/dashboardSlice'
import { selectSlotsWithLiveLastTs, slotLastTsOf, withLiveLastTs } from '../store/slotLastTs'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * Mid-turn `last_ts` bumps land in `dashboard.slotLastTs`, not on the row, so
 * the slot list keeps its identity while sessions stream. These cases pin that
 * the list stays still, and that every reader still gets the newest value.
 */

const T0 = '2026-09-01T00:00:00.000Z'
const T1 = '2026-09-01T00:00:01.000Z'
const T2 = '2026-09-01T00:00:02.000Z'
const T3 = '2026-09-01T00:00:03.000Z'

const mk = (key: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: true, ...over,
})

const loaded = (rows: ChatSlot[]) => reducer(undefined, sseSlots(rows))

describe('touchSlotActivity with the last_ts overlay', () => {
  it('keeps the slots array and row when a row with last_turn_ts streams', () => {
    const before = loaded([mk('a', { last_turn_ts: T0, last_ts: T0 }), mk('b', { last_turn_ts: T0, last_ts: T0 })])
    const after = reducer(before, touchSlotActivity({ key: 'a', ts: T1 }))
    expect(after.slots).toBe(before.slots)
    expect(after.slots[0].last_ts).toBe(T0)
    expect(slotLastTsOf(after, 'a')).toBe(T1)
    expect(slotLastTsOf(after, 'b')).toBe(T0)
  })

  it('writes the row when it has no last_turn_ts, since the list sorts by last_ts there', () => {
    const before = loaded([mk('a', { last_ts: T0 })])
    const after = reducer(before, touchSlotActivity({ key: 'a', ts: T1 }))
    expect(after.slots).not.toBe(before.slots)
    expect(after.slots[0].last_ts).toBe(T1)
    expect(after.slotLastTs).toEqual({})
  })

  it('never moves backwards and treats an equal time as no write', () => {
    const s1 = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 })]), touchSlotActivity({ key: 'a', ts: T2 }))
    const s2 = reducer(s1, touchSlotActivity({ key: 'a', ts: T1 }))
    expect(slotLastTsOf(s2, 'a')).toBe(T2)
    expect(reducer(s2, touchSlotActivity({ key: 'a', ts: T2 }))).toBe(s2)
  })

  it('a settled bump writes the row and drops the entry it caught up with', () => {
    const s1 = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 })]), touchSlotActivity({ key: 'a', ts: T1 }))
    const s2 = reducer(s1, touchSlotActivity({ key: 'a', ts: T2, settled: true }))
    expect(s2.slots[0].last_ts).toBe(T2)
    expect(s2.slots[0].last_turn_ts).toBe(T2)
    expect(s2.slotLastTs).toEqual({})
  })

  it('a slots list that caught up drops the entry; an older one keeps it', () => {
    const s1 = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 })]), touchSlotActivity({ key: 'a', ts: T2 }))
    const stale = reducer(s1, sseSlots([mk('a', { last_turn_ts: T0, last_ts: T1 })]))
    expect(slotLastTsOf(stale, 'a')).toBe(T2)
    const fresh = reducer(stale, sseSlots([mk('a', { last_turn_ts: T0, last_ts: T3 })]))
    expect(fresh.slotLastTs).toEqual({})
    expect(slotLastTsOf(fresh, 'a')).toBe(T3)
  })

  it('a slots list without the key drops its entry', () => {
    const s1 = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 }), mk('b')]), touchSlotActivity({ key: 'a', ts: T1 }))
    const gone = reducer(s1, sseSlots([mk('b')]))
    expect(gone.slotLastTs).toEqual({})
  })

  it('markSlotUnread falls back to the live last_ts as its watermark', () => {
    const s1 = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 })]), touchSlotActivity({ key: 'a', ts: T1 }))
    const s2 = reducer(s1, markSlotUnread({ slot: 'a' }))
    expect(s2.unreadSince?.a).toBe(T1)
  })
})

describe('live last_ts rows', () => {
  it('replaces only the rows the overlay moves, and keeps the array when none moved', () => {
    const rows = [mk('a', { last_ts: T0 }), mk('b', { last_ts: T2 })]
    expect(withLiveLastTs(rows, {})).toBe(rows)
    expect(withLiveLastTs(rows, { 'k:b': T1 })).toBe(rows)
    const live = withLiveLastTs(rows, { 'k:a': T1 })
    expect(live[0]).toEqual({ ...rows[0], last_ts: T1 })
    expect(live[1]).toBe(rows[1])
  })

  it('selectSlotsWithLiveLastTs merges the overlay into the list', () => {
    const dashboard = reducer(loaded([mk('a', { last_turn_ts: T0, last_ts: T0 })]), touchSlotActivity({ key: 'a', ts: T1 }))
    const state = { dashboard } as unknown as RootState
    expect(selectSlotsWithLiveLastTs(state)[0].last_ts).toBe(T1)
  })
})
