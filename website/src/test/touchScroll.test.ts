import { describe, expect, it } from 'vitest'
import { touchScrollAction, scrollSequence, touchCellFromRect } from '../state/touchScroll'

const b = (s: string): number[] => Array.from(new TextEncoder().encode(s))
const seq = (
  a: 'arrows' | 'wheel',
  d: 'up' | 'down',
  n: number,
  appCursor = false,
): number[] => Array.from(scrollSequence(a, d, n, appCursor))
const seqAt = (
  a: 'arrows' | 'wheel',
  d: 'up' | 'down',
  n: number,
  col: number,
  row: number,
): number[] => Array.from(scrollSequence(a, d, n, false, col, row))

describe('touchScrollAction', () => {
  it('normal screen, no mouse tracking → native (browser pans xterm scrollback)', () => {
    expect(touchScrollAction(false, false)).toBe('native')
  })
  it('mouse tracking on → wheel, even if the buffer still reads normal', () => {
    // Mouse tracking wins over buffer type: xterm will not native-pan while the
    // app owns the pointer, and after a reconnect the mode can be re-emitted
    // before the alt-screen enter, so isAlt can read false for a real TUI.
    expect(touchScrollAction(false, true)).toBe('wheel')
    expect(touchScrollAction(true, true)).toBe('wheel')
  })
  it('alt screen, no mouse tracking → arrows (less/man/git log)', () => {
    expect(touchScrollAction(true, false)).toBe('arrows')
  })
})

describe('scrollSequence — arrows', () => {
  it('emits N up/down arrow keys (normal cursor mode → CSI)', () => {
    expect(seq('arrows', 'up', 1)).toEqual(b('\x1b[A'))
    expect(seq('arrows', 'down', 1)).toEqual(b('\x1b[B'))
    expect(seq('arrows', 'up', 3)).toEqual(b('\x1b[A\x1b[A\x1b[A'))
  })
  it('uses SS3 (ESC O) arrows in application-cursor mode (DECCKM)', () => {
    expect(seq('arrows', 'up', 1, true)).toEqual(b('\x1bOA'))
    expect(seq('arrows', 'down', 2, true)).toEqual(b('\x1bOB\x1bOB'))
  })
})

describe('scrollSequence — SGR mouse wheel reports', () => {
  it('emits wheel-up (64) / wheel-down (65) at the given cell', () => {
    expect(seqAt('wheel', 'up', 1, 10, 5)).toEqual(b('\x1b[<64;10;5M'))
    expect(seqAt('wheel', 'down', 1, 10, 5)).toEqual(b('\x1b[<65;10;5M'))
  })
  it('defaults the cell to 1;1 and repeats per step', () => {
    expect(seq('wheel', 'down', 2)).toEqual(b('\x1b[<65;1;1M\x1b[<65;1;1M'))
  })
})

describe('scrollSequence — clamping', () => {
  it('yields no bytes for a non-positive count', () => {
    expect(seq('arrows', 'up', 0)).toEqual([])
    expect(seq('wheel', 'down', -3)).toEqual([])
  })
  it('caps a fling at 6 steps', () => {
    expect(seq('arrows', 'down', 999)).toEqual(b('\x1b[B'.repeat(6)))
  })
})

describe('touchCellFromRect', () => {
  const rect = { left: 0, top: 0, width: 800, height: 400 } // 80x24 grid → 10x~16.7 px cells
  it('maps a point to its 1-based cell', () => {
    expect(touchCellFromRect(rect, 80, 24, 15, 20)).toEqual([2, 2])
  })
  it('clamps to [1,cols] / [1,rows] outside the box', () => {
    expect(touchCellFromRect(rect, 80, 24, -50, -50)).toEqual([1, 1])
    expect(touchCellFromRect(rect, 80, 24, 9999, 9999)).toEqual([80, 24])
  })
})
