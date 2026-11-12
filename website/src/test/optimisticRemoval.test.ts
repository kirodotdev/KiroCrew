import { describe, it, expect } from 'vitest'
import { putRowBack, takeRow } from '../lib/optimisticRemoval'

const isB = (s: string) => s === 'b'

describe('takeRow / putRowBack', () => {
  it('puts the row back at its old position', () => {
    const removed = takeRow(['a', 'b', 'c'], isB)
    expect(removed).toEqual({ row: 'b', index: 1 })
    expect(putRowBack(['a', 'c'], removed, isB)).toEqual(['a', 'b', 'c'])
  })

  it('keeps every other change made to the list meanwhile', () => {
    const removed = takeRow(['a', 'b', 'c'], isB)
    // `c` was removed by another delete while this one was in flight.
    expect(putRowBack(['a'], removed, isB)).toEqual(['a', 'b'])
  })

  it('clamps the position to the current length', () => {
    const removed = takeRow(['a', 'c', 'b'], isB)
    expect(putRowBack(['a'], removed, isB)).toEqual(['a', 'b'])
  })

  it('does not add the row twice when the list already holds it', () => {
    const removed = takeRow(['a', 'b'], isB)
    const list = ['a', 'b']
    expect(putRowBack(list, removed, isB)).toBe(list)
  })

  it('leaves an uncached list uncached, and needs a recorded row', () => {
    expect(takeRow(undefined, isB)).toBeUndefined()
    expect(takeRow(['a'], isB)).toBeUndefined()
    expect(putRowBack(undefined, { row: 'b', index: 0 }, isB)).toBeUndefined()
    const list = ['a']
    expect(putRowBack(list, undefined, isB)).toBe(list)
  })
})
