import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act, fireEvent } from '@testing-library/react'
import DetailPanel from '../components/DetailPanel'
import { safeSetItem } from '../utils/safeStorage'

// The panel's pixel width lives as an inline style on the inner div that also
// carries the left border (`div.border-l`). Read it back to assert clamping.
function panelWidth(container: HTMLElement): number {
  const el = container.querySelector('div.border-l') as HTMLElement | null
  if (!el) throw new Error('panel element not found')
  return parseInt(el.style.width, 10)
}

const setViewport = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { value: w, configurable: true, writable: true })
}

// jsdom does no layout, so getBoundingClientRect().width is 0 everywhere. Stub
// it to a fixed value so DetailPanel can measure its flex row (the panel's
// parent element) the way it does in a real browser. Returns a restore fn.
function stubRowWidth(w: number) {
  const orig = HTMLElement.prototype.getBoundingClientRect
  HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: w, height: 0, top: 0, left: 0, right: w, bottom: 0, x: 0, y: 0, toJSON: () => {} } as DOMRect
  }
  return () => { HTMLElement.prototype.getBoundingClientRect = orig }
}

// Regression: a persisted width sized on a wide monitor must not
// push the panel (shrink-0, in an overflow-hidden row) past a smaller viewport,
// which clipped the right-edge header actions (diff toggle / Edit·Preview).
describe('DetailPanel width clamp', () => {
  const ORIG = window.innerWidth
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); setViewport(ORIG) })

  it('clamps a persisted width wider than the viewport to 60% of the viewport', () => {
    setViewport(1000) // cap = 600
    localStorage.setItem('mc-test-w', '2000')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(600)
  })

  it('clamps an oversized initialWidth to 60% of the viewport', () => {
    setViewport(1000) // cap = 600
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={5000} minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(600)
  })

  it('leaves a width that already fits unchanged', () => {
    setViewport(1600) // cap = 960
    localStorage.setItem('mc-test-w', '480')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(480)
  })

  it('re-clamps down when the viewport shrinks', () => {
    setViewport(2000) // cap = 1200
    localStorage.setItem('mc-test-w', '1100')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(1100) // fits initially
    act(() => { setViewport(800); window.dispatchEvent(new Event('resize')) }) // cap = 480
    expect(panelWidth(container)).toBe(480)
  })

  // Regression: a resize firing *during* a drag must not clobber
  // the width the user dragged to. The resize listener is suppressed mid-drag,
  // and on mouseup the dragged (preferred) width is persisted while the live
  // render is clamped down to the (now smaller) viewport.
  it('preserves the dragged preferred width across a resize mid-drag', () => {
    setViewport(2000) // cap = 1200
    localStorage.setItem('mc-test-w', '400')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" initialWidth={400} minWidth={300}>x</DetailPanel>,
    )
    const handle = container.querySelector('div.cursor-col-resize') as HTMLElement
    // start dragging from x=1000, then move left to x=300 → intended width 1100 (fits cap 1200)
    fireEvent.pointerDown(handle, { clientX: 1000, pointerId: 1 })
    act(() => { fireEvent.pointerMove(handle, { clientX: 300, pointerId: 1 }) })
    expect(panelWidth(container)).toBe(1100)
    // viewport shrinks mid-drag (DevTools toggle / OS tile / zoom) → cap drops to 480.
    // The resize listener must be suppressed, so the in-flight width is untouched.
    act(() => { setViewport(800); window.dispatchEvent(new Event('resize')) })
    expect(panelWidth(container)).toBe(1100)
    // release: preferred width (1100) persists, live render clamps to fit (480).
    act(() => { fireEvent.pointerUp(handle, { clientX: 300, pointerId: 1 }) })
    expect(localStorage.getItem('mc-test-w')).toBe('1100')
    expect(panelWidth(container)).toBe(480)
  })

  // Regression: a sub-threshold tap on the 6px handle must still reset the drag
  // suppression flag. usePointerDrag fires onEnd on every pointer-up (committed
  // or not), so a stray click can't wedge draggingRef=true and make later resize
  // re-clamps early-return forever (which would re-expose the overflow).
  it('resets the drag guard after a sub-threshold click so later resizes still clamp', () => {
    setViewport(2000) // cap = 1200
    localStorage.setItem('mc-test-w', '1100')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" initialWidth={1100} minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(1100)
    const handle = container.querySelector('div.cursor-col-resize') as HTMLElement
    // Stray click: down then up moving only 2px (< 6px threshold) → never commits.
    fireEvent.pointerDown(handle, { clientX: 500, pointerId: 1 })
    act(() => { fireEvent.pointerUp(handle, { clientX: 502, pointerId: 1 }) })
    // Viewport shrinks: the re-clamp guard must NOT be wedged → width clamps to fit.
    act(() => { setViewport(800); window.dispatchEvent(new Event('resize')) }) // cap = 480
    expect(panelWidth(container)).toBe(480)
  })
})

// Regression: the width cap must be the panel's room in its flex
// ROW (rowWidth − reserveWidth), not a fraction of the whole window. A window
// fraction let the shrink-0 panel exceed its row, collapse the chat pane, and
// overflow off-screen with content reflowing past the viewport edge. Here the
// row is measured (stubbed) and always narrower than the 60% viewport bound, so
// the row-minus-reserve cap is the one that binds.
describe('DetailPanel row-aware width clamp', () => {
  const ORIG = window.innerWidth
  let restoreRow: (() => void) | undefined
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); setViewport(ORIG); restoreRow?.(); restoreRow = undefined })

  it('caps a drag to rowWidth − reserveWidth, not innerWidth * 0.6', () => {
    setViewport(2000)           // viewport-only cap would be 1200
    restoreRow = stubRowWidth(1000) // row is only 1000 wide; reserve 400 → cap 600
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={480} minWidth={300} reserveWidth={400}>x</DetailPanel>,
    )
    const handle = container.querySelector('div.cursor-col-resize') as HTMLElement
    // Drag far left (start 900 → move to 0) would ask for 480 + 900 = 1380.
    fireEvent.pointerDown(handle, { clientX: 900, pointerId: 1 })
    act(() => { fireEvent.pointerMove(handle, { clientX: 0, pointerId: 1 }) })
    // Capped to row(1000) − reserve(400) = 600, NOT the viewport 1200.
    expect(panelWidth(container)).toBe(600)
    act(() => { fireEvent.pointerUp(handle, { clientX: 0, pointerId: 1 }) })
  })

  it('never lets the panel exceed rowWidth − reserveWidth however far you drag', () => {
    setViewport(3000)
    restoreRow = stubRowWidth(1200) // reserve 500 → cap 700
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={400} minWidth={300} reserveWidth={500}>x</DetailPanel>,
    )
    const handle = container.querySelector('div.cursor-col-resize') as HTMLElement
    fireEvent.pointerDown(handle, { clientX: 1000, pointerId: 1 })
    act(() => { fireEvent.pointerMove(handle, { clientX: -5000, pointerId: 1 }) }) // absurd overshoot
    expect(panelWidth(container)).toBe(700)
    expect(panelWidth(container)).toBeLessThanOrEqual(1200 - 500)
    act(() => { fireEvent.pointerUp(handle, { clientX: -5000, pointerId: 1 }) })
  })

  it('re-clamps a fitting width down when reserveWidth grows (sidebar widened)', () => {
    setViewport(2000)
    restoreRow = stubRowWidth(1000)
    // reserve 200 → cap 800; a stored 700 fits.
    safeSetItem('mc-test-w', '700')
    const { container, rerender } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300} reserveWidth={200}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(700)
    // Sidebar drags wider → reserve 500 → cap 500. No window resize fires, so the
    // reserveWidth-change effect must re-clamp 700 down to 500.
    rerender(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300} reserveWidth={500}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(500)
  })

  it('ignores the row and keeps the viewport-only cap when reserveWidth is omitted (opt-in)', () => {
    setViewport(2000)                // viewport-only cap = 1200
    restoreRow = stubRowWidth(1000)  // row is measurable at 1000, but must NOT bind without a reserve
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={400} minWidth={300}>x</DetailPanel>,
    )
    const handle = container.querySelector('div.cursor-col-resize') as HTMLElement
    fireEvent.pointerDown(handle, { clientX: 1000, pointerId: 1 })
    act(() => { fireEvent.pointerMove(handle, { clientX: -3000, pointerId: 1 }) })
    // No reserveWidth → row term drops out → capped at viewport 1200, not row(1000)-anything.
    expect(panelWidth(container)).toBe(1200)
    act(() => { fireEvent.pointerUp(handle, { clientX: -3000, pointerId: 1 }) })
  })
})
