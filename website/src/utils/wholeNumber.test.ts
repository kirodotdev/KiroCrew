import { describe, expect, it } from 'vitest'

import { parseWholeNumber } from './wholeNumber'

describe('parseWholeNumber', () => {
  it.each([
    ['50', 50],
    [' 50 ', 50],
    ['0', 0],
    ['007', 7],
    ['50.0', 50],
    ['50.', 50],
  ])('reads %j as %d', (raw, want) => {
    expect(parseWholeNumber(raw)).toBe(want)
  })

  // `parseInt` reads a number out of most of these (50, 8, 1, -5, 6, 5, 0).
  it.each(['', '  ', '50.9', '8O', '1e3', '-5', '6,000', 'abc', '.5', '+5', '0x10', '9007199254740993'])(
    'refuses %j',
    (raw) => {
      expect(parseWholeNumber(raw)).toBeNull()
    },
  )
})
