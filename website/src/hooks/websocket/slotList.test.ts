/** `applySlotsDiff` rebuilds the list a `slots_diff` frame describes from the
 *  list the tab holds, or refuses when the frame was built on another one. */
import { describe, it, expect } from 'vitest'
import type { ChatSlot } from '../../types'
import { applySlotsDiff } from './slotList'

const rowA = { key: 'slot-a', title: 'A', agent: 'kirocrew', running: false } as ChatSlot
const rowB = { key: 'slot-b', title: 'B', agent: 'kirocrew', running: false } as ChatSlot
const rowC = { key: 'slot-c', title: 'C', agent: 'kirocrew', running: false } as ChatSlot

describe('applySlotsDiff', () => {
  const held = { seq: 4, rows: [rowA, rowB, rowC], envelope: {} }

  it('replaces changed rows whole, keeps unchanged rows as held, and drops removed rows', () => {
    const rowA2 = { ...rowA, title: 'A2' }
    const rows = applySlotsDiff(held, {
      base: 4, seq: 5,
      rows: [{ key: 'slot-a', row: rowA2 }, { key: 'slot-c', row: { key: 'slot-c', title: 'C2' } as ChatSlot }],
      removed: ['slot-b'],
    })
    expect(JSON.stringify(rows)).toBe(JSON.stringify([rowA2, { key: 'slot-c', title: 'C2' }]))
    expect(applySlotsDiff(held, { base: 4, seq: 5, rows: [] })![1]).toBe(rowB)
  })

  it('refuses a delta on another base or naming a row it does not hold', () => {
    expect(applySlotsDiff(held, { base: 3, seq: 5, rows: [] })).toBeNull()
    expect(applySlotsDiff(held, { base: 4, seq: 5, rows: [{ key: 'slot-z', row: rowA }] })).toBeNull()
  })
})
