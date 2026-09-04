import { describe, expect, it } from 'vitest'
import type { FileContents } from '@pierre/diffs'
import {
  PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE,
  PIERRE_FILE_PAIR_MAX_TOTAL_CODE_UNITS,
} from '../pierre/config'
import { isPierreFilePairWithinBudget } from '../pierre/renderBudget'

const file = (contents: string): FileContents => ({ name: 'generated.ts', contents })
const lines = (count: number) => Array.from({ length: count }, (_, i) => `const v${i} = ${i}`).join('\n')

describe('Pierre file-pair render budget', () => {
  it('includes the exact line boundary and rejects one line beyond it', () => {
    expect(isPierreFilePairWithinBudget(file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE)), file('x'))).toBe(true)
    expect(isPierreFilePairWithinBudget(file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)), file('x'))).toBe(false)
  })

  it('includes the exact combined character boundary and rejects one character beyond it', () => {
    expect(isPierreFilePairWithinBudget(null, file('x'.repeat(PIERRE_FILE_PAIR_MAX_TOTAL_CODE_UNITS)))).toBe(true)
    expect(isPierreFilePairWithinBudget(null, file('x'.repeat(PIERRE_FILE_PAIR_MAX_TOTAL_CODE_UNITS + 1)))).toBe(false)
  })

  it('bounds each side independently, including uneven and new-file pairs', () => {
    expect(isPierreFilePairWithinBudget(file('x'), file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)))).toBe(false)
    expect(isPierreFilePairWithinBudget(null, file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE)))).toBe(true)
  })
})
