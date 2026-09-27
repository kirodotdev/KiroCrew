/**
 * The reasoning-effort popover is portaled to <body> by its two hosts
 * (ChatPage, ChatPane), which position it from the composer chip's rect. The
 * bound that keeps it on screen has to come from the popover's real width:
 * ChatPage clamped against a hand-written 220 while the popover renders at 240,
 * so a chip near the right edge pushed the slider's max notch, the Default
 * toggle and the help tip off the viewport, where no pointer could reach them.
 *
 * `effortPopoverLeft` owns that bound for both hosts. A viewport-width anchor
 * sweep is the assertion: whatever the chip's position, the popover stays
 * between the two gutters.
 */
import { describe, it, expect } from 'vitest'
import { EFFORT_POPOVER_WIDTH_PX, effortPopoverLeft } from '../lib/effort'

const GUTTER = 8

describe('effortPopoverLeft', () => {
  it('leaves a chip with room where it is', () => {
    expect(effortPopoverLeft(400, 1440)).toBe(400)
  })

  it('keeps the right edge inside the viewport for a chip near the right edge', () => {
    const viewport = 1440
    const left = effortPopoverLeft(viewport - 90, viewport)
    expect(left + EFFORT_POPOVER_WIDTH_PX).toBeLessThanOrEqual(viewport - GUTTER)
  })

  it('never crosses the left gutter', () => {
    expect(effortPopoverLeft(-50, 1440)).toBe(GUTTER)
  })

  it('follows the popover own max-width shrink on a narrow viewport', () => {
    // Narrower than the popover: it renders at calc(100vw - 16px), so the two
    // gutters are all that is left and the only valid left is the gutter.
    expect(effortPopoverLeft(200, 220)).toBe(GUTTER)
  })

  it('holds at every anchor position across a range of viewports', () => {
    for (const viewport of [320, 768, 1024, 1440, 1920]) {
      const width = Math.min(EFFORT_POPOVER_WIDTH_PX, viewport - 2 * GUTTER)
      for (let anchor = -20; anchor <= viewport + 20; anchor += 17) {
        const left = effortPopoverLeft(anchor, viewport)
        expect(left).toBeGreaterThanOrEqual(GUTTER)
        expect(left + width).toBeLessThanOrEqual(viewport - GUTTER)
      }
    }
  })
})
