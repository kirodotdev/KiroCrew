import { describe, it, expect, vi } from 'vitest'
import reducer, { sseSlots, touchSlotActivity, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * Referential stability of `dashboard.slots` under the two authoritative
 * writers.
 *
 * The sidebar renders each row as a Framer `motion.div` inside one
 * `LayoutGroup`, and its filter/sort hangs off a `useMemo` keyed on the array.
 * A writer that replaces the array wholesale therefore re-renders and
 * re-measures every row on every frame — several times a second while any
 * session is active — for what is usually one slot's change. These cases pin
 * the identity contract that keeps that cost proportional to the change.
 */

const mk = (key: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 1,
  running: false,
  pending_approval: false,
  waiting_for_input: false,
  last_activity_ts: undefined,
  ...over,
})

// Fresh objects per frame: the real payload is parsed from the wire, so a
// reducer that merely happened to receive the same reference would pass
// vacuously.
const frame = (...slots: ChatSlot[]): ChatSlot[] => slots.map(s => JSON.parse(JSON.stringify(s)) as ChatSlot)

describe('dashboard.slots referential stability', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const loaded = reducer(initial, sseSlots(frame(mk('a'), mk('b'), mk('c'))))

  it('leaves the array untouched when a frame repeats the same content', () => {
    const again = reducer(loaded, sseSlots(frame(mk('a'), mk('b'), mk('c'))))
    expect(again.slots).toBe(loaded.slots)
  })

  it('ignores key order in the incoming payload', () => {
    // Guards against a serialization-based comparator: a row an in-place
    // reducer has patched can spell its keys in a different order than the
    // server does, and calling that unequal would defeat the merge silently.
    const reordered = loaded.slots.map(s => {
      const flipped: Record<string, unknown> = {}
      for (const k of Object.keys(s).reverse()) flipped[k] = (s as unknown as Record<string, unknown>)[k]
      return flipped as unknown as ChatSlot
    })
    const again = reducer(loaded, sseSlots(reordered))
    expect(again.slots).toBe(loaded.slots)
  })

  it('replaces only the row that changed', () => {
    const next = reducer(loaded, sseSlots(frame(mk('a'), mk('b', { running: true }), mk('c'))))
    expect(next.slots).not.toBe(loaded.slots)
    expect(next.slots[0]).toBe(loaded.slots[0])
    expect(next.slots[2]).toBe(loaded.slots[2])
    expect(next.slots[1]).not.toBe(loaded.slots[1])
    expect(next.slots[1].running).toBe(true)
  })

  it('reuses every row across a pure reorder while still publishing a new array', () => {
    // The array must change so the sidebar's sort re-runs; the rows must not, so
    // only the moved rows animate.
    const next = reducer(loaded, sseSlots(frame(mk('c'), mk('a'), mk('b'))))
    expect(next.slots).not.toBe(loaded.slots)
    expect(next.slots.map(s => s.key)).toEqual(['c', 'a', 'b'])
    expect(next.slots[0]).toBe(loaded.slots[2])
    expect(next.slots[1]).toBe(loaded.slots[0])
    expect(next.slots[2]).toBe(loaded.slots[1])
  })

  it('keeps surviving rows across an addition and a removal', () => {
    const added = reducer(loaded, sseSlots(frame(mk('a'), mk('b'), mk('c'), mk('d'))))
    expect(added.slots).toHaveLength(4)
    expect(added.slots[0]).toBe(loaded.slots[0])

    const removed = reducer(loaded, sseSlots(frame(mk('a'), mk('c'))))
    expect(removed.slots.map(s => s.key)).toEqual(['a', 'c'])
    expect(removed.slots[0]).toBe(loaded.slots[0])
    expect(removed.slots[1]).toBe(loaded.slots[2])
  })

  it('settles a locally patched row without churning the array', () => {
    // `touchSlotActivity` bumps recency off the finer-grained message stream, so
    // the authoritative frame that follows usually agrees with what the row
    // already holds. That agreement must cost nothing.
    const ts = '2026-01-01T00:00:00Z'
    const patched = reducer(loaded, touchSlotActivity({ key: 'b', ts, settled: true }))
    expect(patched.slots).not.toBe(loaded.slots)

    const confirmed = reducer(patched, sseSlots(frame(
      mk('a'), mk('b', { last_ts: ts, last_turn_ts: ts }), mk('c'),
    )))
    expect(confirmed.slots).toBe(patched.slots)
  })

  it('applies the same contract to the HTTP refetch', () => {
    const same = reducer(loaded, {
      type: fetchSlots.fulfilled.type,
      payload: frame(mk('a'), mk('b'), mk('c')),
    })
    expect(same.slots).toBe(loaded.slots)
  })

  it('still ignores an empty frame that arrives before the first snapshot', () => {
    const early = reducer(initial, sseSlots([]))
    expect(early.slotsLoaded).toBe(false)
    expect(early.slots).toEqual([])
  })

  it('still tears down on an empty frame once loaded', () => {
    const emptied = reducer(loaded, sseSlots([]))
    expect(emptied.slots).toEqual([])
    expect(emptied.slotsLoaded).toBe(true)
  })
})
