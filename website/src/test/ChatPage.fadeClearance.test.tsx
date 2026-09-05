import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The transcript's bottom mask is four numbers that only work as a set. Each
 * invariant below is a defect that actually shipped, so none of them is theoretical.
 *
 *  1. clearance >= the part ABOVE the scrollport edge. The clearance used to be
 *     `2vh + 8px` against a fixed 24px band, so it tracked the viewport and cleared
 *     by exactly 1px at 844px tall. Measured at 390px wide, scrolled to the bottom,
 *     the last line's bottom relative to the band's top: +1.0px at 844, then −1.0 at
 *     740, −2.0 at 700, −3.0 at 660, −5.0 at 560 — every phone viewport sliced the
 *     last line while every desktop one looked fine, which is why it read as
 *     mobile-only and a desktop reviewer could not see it.
 *  2. The clearance is stated in px, never viewport units — that unit IS defect 1.
 *  3. Layout neutrality: height == above + overshoot, and the two negative margins
 *     cancel the box exactly. A positive residual pushes the composer down.
 *  4. The overshoot equals ChatInput's own gap to the composer box (`pt-1` 4px plus
 *     the `h-[6px]` top spacer). Short, and a strip is left unmasked where the
 *     reported hairline shows; long, and the `z-[1]` mask paints over the composer
 *     box's top border and dims it.
 *
 * Asserted against SOURCE TEXT rather than a render: the numbers live in two files,
 * one of them as a Tailwind class jsdom cannot resolve into a height, and the
 * invariant is the arithmetic BETWEEN them.
 */
const CHAT_PAGE_VIEW = readFileSync(resolve(__dirname, '../pages/chat/ChatPageView.tsx'), 'utf8')
const CHAT_INPUT = readFileSync(resolve(__dirname, '../components/ChatInput.tsx'), 'utf8')

const num = (re: RegExp, src: string): number => {
  const m = re.exec(src)
  expect(m, `pattern not found: ${re}`).not.toBeNull()
  return Number(m![1])
}

describe('transcript bottom mask geometry', () => {
  const above = () => num(/const TRANSCRIPT_MASK_ABOVE_PX = (\d+)/, CHAT_PAGE_VIEW)
  const overshoot = () => num(/const COMPOSER_MASK_OVERSHOOT_PX = (\d+)/, CHAT_PAGE_VIEW)
  const tail = () => num(/const TRANSCRIPT_TAIL_SPACER_PX = (\d+)/, CHAT_PAGE_VIEW)
  const pad = () => num(/paddingBottom: (\d+)/, CHAT_PAGE_VIEW)

  it('keeps tail spacer + scroller padding >= the mask above the scrollport edge', () => {
    expect(tail() + pad()).toBeGreaterThanOrEqual(above())
  })

  it('states the clearance in px, never in viewport units', () => {
    // A tail spacer sized in vh/dvh/svh/lvh passes the arithmetic above by accident
    // (its number reads as px) while still shrinking on a phone.
    expect(CHAT_PAGE_VIEW).not.toMatch(/height:\s*['"]?\d+(\.\d+)?(vh|dvh|svh|lvh)/)
  })

  it('consumes zero layout: height == above + overshoot, margins cancel it', () => {
    const decl = /height: (TRANSCRIPT_MASK_ABOVE_PX \+ COMPOSER_MASK_OVERSHOOT_PX),\s*marginTop: (-TRANSCRIPT_MASK_ABOVE_PX),\s*marginBottom: (-COMPOSER_MASK_OVERSHOOT_PX),/
    // Pinned symbolically rather than numerically: this is the one invariant that
    // must hold for ANY values the two constants take.
    expect(CHAT_PAGE_VIEW).toMatch(decl)
  })

  it('overshoots exactly to the composer box, never past its top border', () => {
    // ChatInput's contribution: the input-area's own top padding plus the spacer box
    // that stands in for the pointer-only drag handle.
    expect(CHAT_INPUT).toMatch(/input-area px-4 pb-1 \$\{hasApproval \? 'pt-0' : 'pt-1'\}/)
    const spacer = num(/data-testid="composer-top-gap" className="h-\[(\d+)px\] shrink-0"/, CHAT_INPUT)
    const INPUT_AREA_PT_1_PX = 4
    expect(overshoot()).toBe(INPUT_AREA_PT_1_PX + spacer)
  })

  it('keeps the solid stop above the scrollport edge, not merely at it', () => {
    const stop = num(/from-bg from-\[(\d+)%\]/, CHAT_PAGE_VIEW)
    const solidPx = (stop / 100) * (above() + overshoot())
    // Solid must cover the whole overshoot AND reach some way past the edge, or the
    // gradient's topmost rows sit just shy of opaque and clipped glyphs bleed through.
    expect(solidPx).toBeGreaterThan(overshoot())
    // ...but not so far that the feather disappears entirely.
    expect(solidPx).toBeLessThan(overshoot() + above())
  })
})
