/**
 * The chip rectangle is derived from a writable scrollLeft, so assertions check
 * final geometry. The 100px scrollport spans viewport x 0..100 over 400px of
 * content, giving a 0..300 scroll range.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { revealChip } from '../pages/chat/sidePanelReveal'

const V = 100

afterEach(() => {
  vi.restoreAllMocks()
})

function rect(left: number, width: number): DOMRect {
  return {
    x: left, y: 0, left, top: 0, width, height: 32, right: left + width, bottom: 32,
    toJSON: () => ({}),
  } as DOMRect
}

interface Fixture {
  scroller: HTMLElement
  chip: HTMLElement
  left: () => number
  writes: () => number
  chipRect: () => DOMRect
}

/**
 * @param x     chip's leading border, in content coordinates
 * @param width chip border-box width
 * @param scrollLeft initial scroll offset
 */
function fixture(x: number, width: number, scrollLeft: number, scrollWidth = 400): Fixture {
  const scroller = document.createElement('div')
  const chip = document.createElement('div')
  scroller.appendChild(chip)
  let left = scrollLeft
  let writes = 0
  Object.defineProperty(scroller, 'clientWidth', { configurable: true, get: () => V })
  Object.defineProperty(scroller, 'scrollWidth', { configurable: true, get: () => scrollWidth })
  Object.defineProperty(scroller, 'scrollLeft', {
    configurable: true,
    get: () => left,
    set: (v: number) => { writes += 1; left = v },
  })
  scroller.getBoundingClientRect = () => rect(0, V)
  const chipRect = () => rect(x - left, width)
  chip.getBoundingClientRect = chipRect
  return { scroller, chip, left: () => left, writes: () => writes, chipRect }
}

function expectInside(f: Fixture) {
  const c = f.chipRect()
  expect(c.left).toBeGreaterThanOrEqual(0)
  expect(c.right).toBeLessThanOrEqual(V)
}

describe('revealChip, chip that fits with room (V - 10)', () => {
  it('scrolls back the minimum when clipped on the left', () => {
    const f = fixture(50, V - 10, 120)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(50)
    expect(f.chipRect().left).toBe(0)
    expectInside(f)
  })

  it('scrolls forward the minimum when clipped on the right', () => {
    const f = fixture(150, V - 10, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(140)
    expect(f.chipRect().right).toBe(V)
    expectInside(f)
  })

  it('does not write scrollLeft when already visible', () => {
    const f = fixture(105, V - 10, 100)
    revealChip(f.scroller, f.chip, true)
    expect(f.writes()).toBe(0)
    expect(f.left()).toBe(100)
  })
})

describe('revealChip, chip that exactly fits (V)', () => {
  it('ends with both borders inside when clipped on the right', () => {
    const f = fixture(150, V, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(150)
    expect(f.chipRect().left).toBe(0)
    expect(f.chipRect().right).toBe(V)
  })
})

describe('revealChip, oversized chip (V + 1)', () => {
  it('aligns a closable chip trailing border with the trailing edge', () => {
    const f = fixture(150, V + 1, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(151)
    expect(f.chipRect().right).toBe(V)
  })

  it('aligns a closable chip trailing border even when it was leading-aligned', () => {
    const f = fixture(150, V + 1, 150)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(151)
    expect(f.chipRect().right).toBe(V)
  })

  it('aligns a non-closable chip leading border with the leading edge', () => {
    const f = fixture(150, V + 1, 0)
    revealChip(f.scroller, f.chip, false)
    expect(f.left()).toBe(150)
    expect(f.chipRect().left).toBe(0)
  })
})

describe('revealChip, clamping', () => {
  it('keeps the first chip at scrollLeft 0 with its box visible', () => {
    const f = fixture(0, 60, 10)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(0)
    expectInside(f)
  })

  it('clamps a negative target to 0', () => {
    const f = fixture(-5, 60, 10)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(0)
  })

  it('ends a chip near the end at max with its box visible', () => {
    const f = fixture(340, 60, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(300)
    expectInside(f)
  })

  it('clamps a target past max to max', () => {
    const f = fixture(350, 60, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(300)
  })

  it('clamps an oversized closable chip past max to max', () => {
    const f = fixture(310, V + 1, 0)
    revealChip(f.scroller, f.chip, true)
    expect(f.left()).toBe(300)
  })

  it('does nothing when the scroller does not overflow', () => {
    const f = fixture(150, 60, 0, V)
    revealChip(f.scroller, f.chip, true)
    expect(f.writes()).toBe(0)
    expect(f.left()).toBe(0)
  })
})

describe('revealChip, write style', () => {
  it('never uses scrollIntoView or smooth scrolling', () => {
    const intoView = typeof Element.prototype.scrollIntoView === 'function'
      ? vi.spyOn(Element.prototype, 'scrollIntoView')
      : null
    const f = fixture(150, V - 10, 0)
    const scrollTo = vi.fn()
    const scroll = vi.fn()
    f.scroller.scrollTo = scrollTo
    f.scroller.scroll = scroll
    revealChip(f.scroller, f.chip, true)
    expect(f.writes()).toBe(1)
    if (intoView) expect(intoView).not.toHaveBeenCalled()
    expect(scrollTo).not.toHaveBeenCalled()
    expect(scroll).not.toHaveBeenCalled()
  })
})
