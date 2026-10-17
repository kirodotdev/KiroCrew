import { describe, it, expect } from 'vitest'
import {
  PAD_SIDES,
  ROW_SELECTOR,
  isGrid,
  judgeFrame,
  offGrid,
  rowName,
} from '../../scripts/lib/transcript-grid-floor.mjs'

/**
 * The 4px-grid floor the transcript row harness enforces in CI.
 *
 * Tested as a pure function: the harness feeds it the DOM probe for each frame,
 * and reaching these branches through a browser would need a deliberately broken
 * component per case. The case that matters most is the empty frame, because a
 * selector that matches nothing leaves every per-row check with nothing to fail on.
 */

const row = (over: Record<string, unknown> = {}) => ({
  index: 0,
  tag: 'div',
  testid: 'notice-card',
  dx: 0,
  padTop: 8,
  padRight: 12,
  padBottom: 8,
  padLeft: 12,
  ...over,
})

const frame = (rows: object[], over: Record<string, unknown> = {}) => ({
  rows,
  gutterClass: 'px-4',
  candidateCount: rows.length,
  ...over,
})

describe('judgeFrame', () => {
  it('fails a frame whose selector matched zero rows, naming the selector', () => {
    const failures = judgeFrame('dark', frame([], { candidateCount: 0, gutterClass: '' }))
    expect(failures).toHaveLength(1)
    expect(failures[0]).toContain('ZERO transcript rows')
    expect(failures[0]).toContain(ROW_SELECTOR)
    expect(failures[0]).toContain('0 candidate(s)')
  })

  it('fails an empty frame even when the selector found candidates the gutter filter dropped', () => {
    const failures = judgeFrame('light', frame([], { candidateCount: 15, gutterClass: 'px-5' }))
    expect(failures).toHaveLength(1)
    expect(failures[0]).toContain('15 candidate(s)')
    expect(failures[0]).toContain("gutter class 'px-5'")
  })

  it('passes a frame where every row is on the text edge with on-grid padding', () => {
    const rows = [row(), row({ index: 1, testid: '', padTop: 12, padRight: 16, padBottom: 12, padLeft: 16 })]
    expect(judgeFrame('dark', frame(rows))).toEqual([])
  })

  it('treats zero padding as on the grid', () => {
    expect(judgeFrame('dark', frame([row({ padTop: 0, padRight: 0, padBottom: 0, padLeft: 0 })]))).toEqual([])
  })

  it('fails a row whose card padding is off the grid on any side, naming each side', () => {
    const failures = judgeFrame('dark', frame([row({ padTop: 6, padBottom: 6 })]))
    expect(failures).toEqual([
      'dark: row notice-card card padding is off the 4px grid: padTop=6 (off by 2), padBottom=6 (off by 2)',
    ])
  })

  it.each(PAD_SIDES)('checks %s', side => {
    expect(judgeFrame('dark', frame([row({ [side]: 10 })]))).toHaveLength(1)
  })

  it('fails a fractional padding the grid test would round away', () => {
    expect(judgeFrame('dark', frame([row({ padLeft: 12.5 })]))[0]).toContain('padLeft=12.5 (off by 0.5)')
  })

  it('fails a row whose box does not start on the column text edge', () => {
    const failures = judgeFrame('dark', frame([row({ dx: -4 })]))
    expect(failures).toEqual(['dark: row notice-card box starts at -4px, not on the column text edge'])
  })

  it('does not fail on pitch or internal edges, which the harness only reports', () => {
    const r = row({ pitch: 82, height: 82, hEdges: [{ off: 13.5, who: 'svg.R' }], vEdges: [{ off: 3, who: 'span.B' }] })
    expect(judgeFrame('dark', frame([r]))).toEqual([])
  })

  it('fails on page errors', () => {
    const failures = judgeFrame('dark', frame([row()]), ['TypeError: x is undefined'])
    expect(failures).toHaveLength(1)
    expect(failures[0]).toContain('1 page error(s)')
    expect(failures[0]).toContain('TypeError: x is undefined')
  })
})

describe('grid helpers', () => {
  it('measures distance to the nearest 4px line', () => {
    expect(offGrid(0)).toBe(0)
    expect(offGrid(6)).toBe(2)
    expect(offGrid(13)).toBe(1)
    expect(offGrid(15)).toBe(1)
    expect(offGrid(-2)).toBe(2)
    expect(isGrid(16)).toBe(true)
    expect(isGrid(12.5)).toBe(false)
  })

  it('names a row by its test id, else its tag and position', () => {
    expect(rowName(row())).toBe('notice-card')
    expect(rowName(row({ testid: '', tag: 'a', index: 7 }))).toBe('a.7')
  })
})
