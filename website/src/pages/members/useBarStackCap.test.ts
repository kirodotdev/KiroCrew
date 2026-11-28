import { describe, it, expect } from 'vitest'
import { BAR_TOUCH_TARGET_PX, barStackMaxWidth, type Span } from './useBarStackCap'

// Control boxes measured on the Bots page phone bar (B035 repro, Chromium,
// coarse pointer). The drawer toggle is 34px wide; with a remote crew the
// icon-only crew chip and the switcher caret sit before it. The trail's first
// control is the history search opener.
const BARS = {
  390: { toggle: 8, toggleWithCrew: 66, search: 276, panel: 310 },
  360: { toggle: 8, toggleWithCrew: 66, search: 246, panel: 280 },
} as const
const BUTTON = 34
/** The chip's `min-w-[7rem]` floor. */
const CHIP_MIN = 112

const box = (left: number, width = BUTTON): Span => ({ left, right: left + width })
const hit = (s: Span): Span => {
  const w = Math.max(s.right - s.left, BAR_TOUCH_TARGET_PX)
  const mid = (s.left + s.right) / 2
  return { left: mid - w / 2, right: mid + w / 2 }
}
const overlaps = (a: Span, b: Span) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0

describe('barStackMaxWidth', () => {
  for (const width of [390, 360] as const) {
    for (const remote of [false, true]) {
      it(`keeps the name chip off both side groups' touch targets at ${width}px ${remote ? 'with' : 'without'} a remote crew`, () => {
        const bar = BARS[width]
        const toggle = box(remote ? bar.toggleWithCrew : bar.toggle)
        const trail = [box(bar.search), box(bar.panel)]
        const cap = barStackMaxWidth(width / 2, { left: 0, right: width }, [toggle], trail)
        // A long name fills the cap; a short one sits at the chip's floor.
        for (const chip of [Math.max(CHIP_MIN, cap), CHIP_MIN]) {
          const stack = { left: width / 2 - chip / 2, right: width / 2 + chip / 2 }
          expect(overlaps(stack, hit(toggle))).toBe(false)
          for (const control of trail) expect(overlaps(stack, hit(control))).toBe(false)
        }
        // The chip's floor still fits, so the cap never has to give way to it here.
        expect(cap).toBeGreaterThanOrEqual(CHIP_MIN)
      })
    }
  }

  it('is set by the nearer side, so a wide lead narrows the stack on both sides', () => {
    // The 12rem cap assumed 96px a side; the crew switcher's full "Local" label
    // put the drawer toggle at 103-137 at 360px.
    expect(barStackMaxWidth(180, { left: 0, right: 360 }, [box(103)], [box(246)])).toBe(68)
    expect(barStackMaxWidth(180, { left: 0, right: 360 }, [box(66)], [box(246)])).toBe(114)
  })

  it('falls back to the header edges when a side has no control', () => {
    expect(barStackMaxWidth(195, { left: 40, right: 350 }, [], [])).toBe(302)
  })

  it('never goes negative', () => {
    expect(barStackMaxWidth(100, { left: 0, right: 200 }, [box(90)], [])).toBe(0)
  })
})
