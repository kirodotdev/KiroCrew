/**
 * `fileAtScrollTop` — the pure mapping from a CodeView scroll offset to the
 * file whose content is at the top of the viewport. This is what keeps the PR
 * change-set tree's selection following the reader's own scrolling, so the
 * contract pinned here is positional: last item at-or-above the offset wins,
 * unmeasured items cannot answer, and the epsilon admits the exact boundary.
 */
import { describe, it, expect } from 'vitest'
import { fileAtScrollTop } from '../pierre/PierreCodeViewImpl'

const PATHS = ['a.ts', 'b.ts', 'c.ts'] as const
const TOPS: Record<string, number> = { 'a.ts': 0, 'b.ts': 500, 'c.ts': 1200 }
const topFor = (path: string) => TOPS[path]

describe('fileAtScrollTop', () => {
  it('answers the first file at the top of the change set', () => {
    expect(fileAtScrollTop(0, PATHS, topFor)).toBe('a.ts')
  })

  it('answers the file whose span the offset is inside, not the next one', () => {
    expect(fileAtScrollTop(499, PATHS, topFor)).toBe('a.ts')
    expect(fileAtScrollTop(501, PATHS, topFor)).toBe('b.ts')
  })

  it('treats an offset exactly on an item boundary as that item', () => {
    expect(fileAtScrollTop(500, PATHS, topFor)).toBe('b.ts')
  })

  it('answers the last file for any offset beyond it', () => {
    expect(fileAtScrollTop(99999, PATHS, topFor)).toBe('c.ts')
  })

  it('skips items the virtualizer has not measured instead of misplacing them', () => {
    const gappy = (path: string) => (path === 'b.ts' ? undefined : TOPS[path])
    // At 600 the measured answer is still a.ts: b.ts cannot be placed, and
    // guessing it would highlight a row the viewport may not be on.
    expect(fileAtScrollTop(600, PATHS, gappy)).toBe('a.ts')
    expect(fileAtScrollTop(1300, PATHS, gappy)).toBe('c.ts')
  })

  it('answers undefined when nothing is measured', () => {
    expect(fileAtScrollTop(100, PATHS, () => undefined)).toBeUndefined()
  })
})
