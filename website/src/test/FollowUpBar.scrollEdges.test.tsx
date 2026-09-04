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
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

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
})
