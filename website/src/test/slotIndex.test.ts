import { describe, expect, it } from 'vitest'
import { slotRowByKey } from '../store/slotIndex'
import type { ChatSlot } from '../types'

const row = (key: string, title = key) => ({ key, title }) as unknown as ChatSlot

describe('slotRowByKey', () => {
  it('returns the row Array.prototype.find would return', () => {
    const slots = [row('a'), row('b'), row('c')]
    for (const key of ['a', 'b', 'c', 'missing']) {
      expect(slotRowByKey(slots, key)).toBe(slots.find(s => s.key === key))
    }
  })

  it('keeps the first row when a key repeats, as find does', () => {
    const first = row('dup', 'first')
    const slots = [first, row('dup', 'second')]
    expect(slotRowByKey(slots, 'dup')).toBe(first)
  })

  it('matches an empty key as find does, and no row for a null or undefined key', () => {
    const empty = row('')
    const slots = [row('a'), empty]
    expect(slotRowByKey(slots, '')).toBe(slots.find(s => s.key === ''))
    expect(slotRowByKey(slots, '')).toBe(empty)
    expect(slotRowByKey([row('a')], '')).toBeUndefined()
    expect(slotRowByKey(slots, null)).toBeUndefined()
    expect(slotRowByKey(slots, undefined)).toBeUndefined()
  })

  it('reads a new slots array afresh, so a replaced row is never stale', () => {
    const before = [row('a', 'old')]
    expect(slotRowByKey(before, 'a')?.title).toBe('old')
    const after = [row('a', 'new')]
    expect(slotRowByKey(after, 'a')?.title).toBe('new')
    expect(slotRowByKey(before, 'a')?.title).toBe('old')
  })
})
