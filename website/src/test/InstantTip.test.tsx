import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { InstantTip, useInstantTip, OPEN_DELAY_MS, scrollMovesAnchor } from '../components/InstantTip'

/** Minimal consumer: one anchor button + the shared bubble. */
function Harness() {
  const { tip, tipHandlers, tipId } = useInstantTip()
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

/** Same consumer inside a [data-tip-boundary] wrapper, anchor NOT in the first
 *  row: the bubble must lift to the boundary's top, not the anchor's. */
function BoundaryHarness() {
  const { tip, tipHandlers, tipId } = useInstantTip()
  return (
    <div data-tip-boundary data-testid="boundary">
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </div>
  )
}

/** A consumer whose anchor sits at the top of the viewport (the top bar), so
 *  the bubble opens under it instead. */
function BelowHarness() {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement: 'below' })
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

// The gesture semantics live in the shared module, so they are pinned here
// once rather than per consumer. FollowUpBar / ChatInput tests assert only
// their own tooltip CONTENT, via keyboard focus (the synchronous path).
describe('InstantTip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows synchronously on keyboard focus — a tab stop is deliberate', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('shows after the hover-intent delay on pointer enter, not immediately', () => {
    render(<Harness />)
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('paints nothing for a pointer passing through inside the intent window', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    fireEvent.mouseLeave(anchor)
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('hides on mouse leave', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseLeave(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('Escape dismisses while open, without requiring blur', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a page scroll dismisses — the captured rect is stale once the window moves', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a scroll of a container the anchor sits in dismisses', () => {
    render(<BoundaryHarness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    // Scroll events do not bubble; the window listener is capture-phase, and
    // fireEvent dispatches on the target itself just as a real strip would.
    fireEvent.scroll(screen.getByTestId('boundary'))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a scroll elsewhere in the document leaves the bubble open — the anchor did not move', () => {
    // The transcript re-pinning, a sidebar lane re-sorting, a side panel
    // following its tail: all fire `scroll` on elements the anchor is not in.
    // Without the ancestor check every one of them closes the bubble under a
    // resting pointer.
    const elsewhere = document.createElement('div')
    document.body.appendChild(elsewhere)
    try {
      render(<Harness />)
      fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
      expect(screen.getByRole('tooltip')).toBeInTheDocument()
      fireEvent.scroll(elsewhere)
      expect(screen.getByRole('tooltip')).toBeInTheDocument()
    } finally {
      elsewhere.remove()
    }
  })

  it('scrollMovesAnchor: window, document and a detached anchor count; an unrelated node does not; no anchor fails closed', () => {
    const anchor = document.createElement('button')
    const parent = document.createElement('div')
    const sibling = document.createElement('div')
    parent.appendChild(anchor)
    document.body.append(parent, sibling)
    try {
      expect(scrollMovesAnchor(window, anchor)).toBe(true)
      expect(scrollMovesAnchor(document, anchor)).toBe(true)
      expect(scrollMovesAnchor(parent, anchor)).toBe(true)
      expect(scrollMovesAnchor(sibling, anchor)).toBe(false)
      expect(scrollMovesAnchor(anchor, anchor)).toBe(false)
      expect(scrollMovesAnchor(sibling, null)).toBe(true)
      // The anchor's element was replaced while the bubble stayed open (a chip
      // changing shape on a pick): nothing contains a detached node, so the
      // ancestor test alone would keep a stranded bubble open on every scroll.
      const detached = document.createElement('button')
      expect(scrollMovesAnchor(parent, detached)).toBe(true)
    } finally {
      parent.remove(); sibling.remove()
    }
  })

  it('blur hides the focus-shown bubble', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.blur(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('links the anchor to the bubble via aria-describedby', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    const described = anchor.getAttribute('aria-describedby')
    expect(described).toBeTruthy()
    expect(screen.getByRole('tooltip').id).toBe(described)
  })

  it('clamps the bubble inside the right viewport edge', () => {
    // jsdom has no layout: give every element a measured width for this test.
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, value: 300 })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      render(<Harness />)
      const anchor = screen.getByRole('button', { name: 'anchor' })
      // Anchor near the right edge: 1000 + 300 would overflow 1024.
      anchor.getBoundingClientRect = () => ({ top: 200, left: 1000, right: 1010, bottom: 210, width: 10, height: 10, x: 1000, y: 200, toJSON: () => ({}) }) as DOMRect
      fireEvent.focus(anchor)
      const left = parseFloat(screen.getByRole('tooltip').style.left)
      expect(left + 300).toBeLessThanOrEqual(1024 - 8)
      expect(left).toBeGreaterThanOrEqual(8)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })

  it('clamps a scrolled-off-screen anchor back to the left viewport edge', () => {
    // A horizontally scrolled strip can hand us a partially visible anchor
    // whose left is already negative; the bubble must come back on-screen.
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, value: 300 })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      render(<Harness />)
      const anchor = screen.getByRole('button', { name: 'anchor' })
      anchor.getBoundingClientRect = () => ({ top: 200, left: -40, right: 20, bottom: 210, width: 60, height: 10, x: -40, y: 200, toJSON: () => ({}) }) as DOMRect
      fireEvent.focus(anchor)
      const left = parseFloat(screen.getByRole('tooltip').style.left)
      expect(left).toBeGreaterThanOrEqual(8)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })

  it('lifts above a [data-tip-boundary] ancestor so wrapped rows are never covered', () => {
    render(<BoundaryHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    // Anchor sits in a second wrapped row (top 300); the strip starts at 240.
    anchor.getBoundingClientRect = () => ({ top: 300, left: 60, right: 160, bottom: 328, width: 100, height: 28, x: 60, y: 300, toJSON: () => ({}) }) as DOMRect
    screen.getByTestId('boundary').getBoundingClientRect = () => ({ top: 240, left: 8, right: 900, bottom: 340, width: 892, height: 100, x: 8, y: 240, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    // Boundary top (240) - 8, not anchor top (300) - 8.
    expect(parseFloat(screen.getByRole('tooltip').style.top)).toBe(232)
  })

  it('keeps the anchor position when no boundary ancestor exists', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    anchor.getBoundingClientRect = () => ({ top: 300, left: 60, right: 160, bottom: 328, width: 100, height: 28, x: 60, y: 300, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    expect(parseFloat(screen.getByRole('tooltip').style.top)).toBe(292)
    // The default is the bubble's BOTTOM edge at `top`: pulled up its own height.
    expect(screen.getByRole('tooltip').className).toMatch(/-translate-y-full/)
    expect(screen.getByRole('tooltip')).toHaveAttribute('data-placement', 'above')
  })

  it('opens under the anchor for placement: below (a top-bar anchor has no room above)', () => {
    render(<BelowHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    anchor.getBoundingClientRect = () => ({ top: 8, left: 60, right: 160, bottom: 36, width: 100, height: 28, x: 60, y: 8, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    const tip = screen.getByRole('tooltip')
    // Anchor bottom (36) + 8, and the bubble's TOP edge sits there: no translate.
    expect(parseFloat(tip.style.top)).toBe(44)
    expect(tip.className).not.toMatch(/-translate-y-full/)
    expect(tip).toHaveAttribute('data-placement', 'below')
    expect(tip.id).toBe(anchor.getAttribute('aria-describedby'))
  })
})
