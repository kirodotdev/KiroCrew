/**
 * The scroll layout's arrows and edge fades are driven by a MEASUREMENT, and
 * that measurement is now the shared `useScrollEdges` hook rather than a
 * hand-rolled copy. These tests pin the behaviour the conversion has to
 * preserve, since the hand-rolled version shipped with none:
 *
 *   - a row that fits shows no arrow and no fade,
 *   - a clipped row offers the arrow pointing at the hidden side only,
 *   - the arrows follow the row as it is scrolled (which needs a listener bound
 *     to the node, not a one-shot read),
 *   - the wheel translation still rides on the same node after the binding moved
 *     into the ref callback.
 *
 * jsdom does no layout, so scroll geometry is stubbed — that stub is what makes
 * the derivation testable at all.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react'
import FollowUpBar, { FOLLOWUP_CHIP_DEBOUNCE_MS, LEFT_ARROW_CLICK_SHIELD_MS } from '../components/FollowUpBar'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const OPTIONS = ['Ship it', 'Explain the diff', 'Run the tests']

function renderScroll(options = OPTIONS) {
  return render(<FollowUpBar options={options} picked={new Set()} onSelect={() => {}} layout="scroll" />)
}

const leftArrow = () => screen.queryByRole('button', { name: /scroll suggestions left/i })
const rightArrow = () => screen.queryByRole('button', { name: /scroll suggestions right/i })

describe('FollowUpBar scroll layout edges', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('offers no arrow and no fade when every chip fits', () => {
    stubGeometry({ hidden: 0 })
    const { container } = renderScroll()
    expect(leftArrow()).toBeNull()
    expect(rightArrow()).toBeNull()
    expect(container.querySelector('.bg-gradient-to-l')).toBeNull()
  })

  it('offers only the arrow pointing at the hidden side', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderScroll()
    expect(rightArrow()).toBeTruthy()
    // Nothing is hidden to the left at offset 0, so an arrow there would scroll
    // to content that does not exist.
    expect(leftArrow()).toBeNull()
    expect(container.querySelector('.bg-gradient-to-l')).toBeTruthy()
  })

  it('follows the row as it scrolls', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderScroll()
    const scroller = container.querySelector('.overflow-x-auto') as HTMLElement
    expect(leftArrow()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scroller)
    expect(leftArrow()).toBeTruthy()
    expect(rightArrow()).toBeNull()
  })

  it('translates a vertical wheel into horizontal scrolling only while clipped', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderScroll()
    const scroller = container.querySelector('.overflow-x-auto') as HTMLElement
    // scrollLeft is stubbed as a getter, so observe the preventDefault contract
    // instead: the row claims the gesture exactly when it can act on it.
    const clipped = new WheelEvent('wheel', { deltaY: 40, deltaX: 0, cancelable: true })
    scroller.dispatchEvent(clipped)
    expect(clipped.defaultPrevented).toBe(true)

    vi.restoreAllMocks()
    stubGeometry({ hidden: 0 })
    const fits = new WheelEvent('wheel', { deltaY: 40, deltaX: 0, cancelable: true })
    scroller.dispatchEvent(fits)
    // A row that fits must leave the gesture to the page.
    expect(fits.defaultPrevented).toBe(false)
  })

  it('pads the far end of a scrolling row so a vanishing arrow never leaves a chip under the pointer', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderScroll()
    const scroller = container.querySelector('.overflow-x-auto') as HTMLElement
    const kids = Array.from(scroller.children) as HTMLElement[]
    // The right arrow's spot is a blank, not a chip: when the arrow goes away
    // at the end of the scroll, the next click hits nothing.
    const last = kids[kids.length - 1]
    expect(last.hasAttribute('data-arrow-gutter')).toBe(true)
    expect(last.getAttribute('aria-hidden')).toBe('true')
    // The leading end gets none: a blank there sits in front of the first chip
    // at rest and pushes it off the composer's left edge.
    expect(kids[0].hasAttribute('data-arrow-gutter')).toBe(false)
    // One gutter plus one element per chip.
    expect(kids.length).toBe(OPTIONS.length + 1)

    // Still there at the end of the scroll, which is when it matters.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scroller)
    expect(rightArrow()).toBeNull()
    expect((scroller.lastElementChild as HTMLElement).hasAttribute('data-arrow-gutter')).toBe(true)
  })

  it('keeps the left arrow spot inert for a moment after paging back to the start', () => {
    vi.useFakeTimers()
    try {
      const onSelect = vi.fn()
      stubGeometry({ hidden: 240, scrolled: 120 })
      const { container } = render(
        <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={onSelect} layout="scroll" />,
      )
      const scroller = container.querySelector('.overflow-x-auto') as HTMLElement
      expect(leftArrow()).toBeTruthy()
      // A row that has not reached the start shows the real arrow, no shield.
      expect(container.querySelector('[data-arrow-click-shield]')).toBeNull()

      // Paged back to offset 0: the arrow goes, its footprint stays and eats
      // the click that would otherwise reach the first chip under it.
      stubGeometry({ hidden: 240, scrolled: 0 })
      fireEvent.scroll(scroller)
      expect(leftArrow()).toBeNull()
      const shield = container.querySelector('[data-arrow-click-shield]') as HTMLElement
      expect(shield).toBeTruthy()
      expect(shield.getAttribute('aria-hidden')).toBe('true')
      expect(shield.className).toContain('left-0.5')
      expect(shield.className).toContain('z-20')
      const click = new MouseEvent('click', { bubbles: true, cancelable: true })
      shield.dispatchEvent(click)
      expect(click.defaultPrevented).toBe(true)
      expect(onSelect).not.toHaveBeenCalled()

      // Longer than the chip's single-click debounce, then gone.
      expect(LEFT_ARROW_CLICK_SHIELD_MS).toBeGreaterThan(FOLLOWUP_CHIP_DEBOUNCE_MS)
      act(() => { vi.advanceTimersByTime(LEFT_ARROW_CLICK_SHIELD_MS) })
      expect(container.querySelector('[data-arrow-click-shield]')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows no shield on a row that never scrolled', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderScroll()
    expect(container.querySelector('[data-arrow-click-shield]')).toBeNull()
  })

  it('adds no gutter to a row that fits, so its first chip stays flush', () => {    stubGeometry({ hidden: 0 })
    const { container } = renderScroll()
    expect(container.querySelector('[data-arrow-gutter]')).toBeNull()
  })

  it('drops the gutter when the row widens until only the gutter would overflow', () => {
    // Capture the observers watching the scroller so the test can deliver
    // the resize itself (the glass layers' observers expect real entries).
    const observers: Array<() => void> = []
    const RO = window.ResizeObserver
    window.ResizeObserver = class {
      cb: () => void
      constructor(cb: () => void) { this.cb = cb }
      observe(el: Element) { if (el.classList.contains('overflow-x-auto')) observers.push(this.cb) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      // The gutter is 24px wide in this stub; with no CSS the gap reads 0.
      vi.spyOn(window.HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(24)
      stubGeometry({ hidden: 240 })
      const { container } = renderScroll()
      expect(container.querySelectorAll('[data-arrow-gutter]').length).toBe(1)

      // Wider row: 20px still hidden, but the gutter accounts for 24 of the
      // content, so the chips alone fit.
      stubGeometry({ hidden: 20 })
      act(() => { observers.forEach(cb => cb()) })
      expect(container.querySelector('[data-arrow-gutter]')).toBeNull()
      // With the gutters gone the content shrinks to fit, and the arrow goes.
      stubGeometry({ hidden: 0 })
      act(() => { observers.forEach(cb => cb()) })
      expect(rightArrow()).toBeNull()
    } finally {
      window.ResizeObserver = RO
    }
  })
})
