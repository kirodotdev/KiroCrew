import { describe, it, expect } from 'vitest'
import { sliceCodePoints, snapOffPair } from '../utils/codePoints'

describe('sliceCodePoints', () => {
  it('moves both edges off a surrogate pair', () => {
    expect(sliceCodePoints('a😀b', 0, 2)).toBe('a')
    expect(sliceCodePoints('a😀b', 2)).toBe('b')
    expect(sliceCodePoints('a😀b', 0, 3)).toBe('a😀')
    expect(sliceCodePoints('a😀b', 2, 2)).toBe('')
  })

  it('clamps out-of-range indices', () => {
    expect(sliceCodePoints('abc', -5, 99)).toBe('abc')
    expect(sliceCodePoints('abc', 5)).toBe('')
    expect(sliceCodePoints('abc', 2, 1)).toBe('')
    expect(sliceCodePoints('', 0, 3)).toBe('')
  })
})

describe('snapOffPair', () => {
  it('moves an index inside a pair in the given direction', () => {
    expect(snapOffPair('a😀b', 2, 1)).toBe(3)
    expect(snapOffPair('a😀b', 2, -1)).toBe(1)
  })

  it('leaves indices outside a pair and at the string edges alone', () => {
    expect(snapOffPair('a😀b', 1, 1)).toBe(1)
    expect(snapOffPair('a😀b', 3, -1)).toBe(3)
    expect(snapOffPair('a😀b', 0, -1)).toBe(0)
    expect(snapOffPair('a😀b', 4, 1)).toBe(4)
  })
})
