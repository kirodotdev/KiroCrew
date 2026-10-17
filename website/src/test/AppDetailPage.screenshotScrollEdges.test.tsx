/**
 * Regression for #4074: the app-detail screenshots strip hides its scrollbar
 * (`scrollbar-none`), so once thumbnails overflow there was NO signal that more
 * exist — the row simply ended. The fix wires the shared `useScrollEdges` hook
 * inline (the same treatment EmbedTabStrip, FollowUpBar and the side-panel tab
 * strip already ship) to paint a gradient over the clipped edge.
 *
 * These specs name what reverting the fix breaks:
 *   - a strip that fits shows no cue (a permanent fade would lie that
 *     thumbnails are hidden),
 *   - a clipped strip cues the hidden side only (no cue at all is the original
 *     defect),
 *   - the cues follow the strip as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read).
 *
 * jsdom does no layout, so scroll geometry is stubbed — mirroring
 * EmbedTabStrip.scrollEdges.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ScreenshotGallery } from '../pages/AppDetailPage'

const SHOTS = ['/shots/a.png', '/shots/b.png', '/shots/c.png']

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(300)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(300 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

function renderGallery() {
  return render(<MemoryRouter><ScreenshotGallery screenshots={SHOTS} /></MemoryRouter>)
}

const leftCue = () => document.querySelector('[data-testid="app-screenshots-cue-left"]')
const rightCue = () => document.querySelector('[data-testid="app-screenshots-cue-right"]')
const scroller = () => document.querySelector('.overflow-x-auto') as HTMLElement

describe('AppDetailPage screenshots scroll-edge cues (#4074)', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
  })

  it('shows no cue when the strip fits', () => {
    stubGeometry({ hidden: 0 })
    renderGallery()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the clipped (right) edge at the start of an overflowing strip', () => {
    stubGeometry({ hidden: 400, scrolled: 0 })
    renderGallery()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).not.toBeNull()
  })

  it('cues both edges when scrolled into the middle', () => {
    stubGeometry({ hidden: 400, scrolled: 200 })
    renderGallery()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).not.toBeNull()
    expect(rightCue()).not.toBeNull()
  })

  // Opus 5.5 review finding on a2c1fe51b1: a thumbnail going terminal unmounts
  // its button, shrinking scrollWidth with no onLoad / no box resize / (at
  // scrollLeft 0) no scroll event — so without a failure-keyed remeasure the
  // right-edge fade could stay on after the strip already fits. The fix adds
  // the failure-latch size to the remeasure effect's deps.
  it('clears the right cue after a failed thumbnail unmounts and the strip fits', () => {
    // Overflowing at first: 3 thumbnails, right cue painted.
    const proto = window.HTMLElement.prototype
    const widthSpy = vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(700)
    vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(300)
    vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(0)
    renderGallery()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(rightCue()).not.toBeNull()

    // One thumbnail (no fallback) errors -> its button unmounts and the strip
    // now fits. No scroll event fires; only the failure-driven remeasure can
    // clear the stale cue.
    widthSpy.mockReturnValue(300)
    const imgs = document.querySelectorAll('.overflow-x-auto img')
    expect(imgs.length).toBeGreaterThan(0)
    act(() => { imgs[0].dispatchEvent(new Event('error')) })
    expect(rightCue()).toBeNull()
  })
})
