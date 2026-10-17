/**
 * A wide markdown table on a phone scrolls sideways through a wrapper whose
 * scrollbar is hidden, so the row simply ends and nothing says columns sit
 * off-screen. The scroller carries a `mask-image` that fades its own content to
 * transparent over whichever edge still hides content, driven by the shared
 * `useScrollEdges` measurement. These tests pin that behaviour:
 *
 *   - a table that fits shows no fade on either edge,
 *   - a clipped table fades only the side that hides content,
 *   - the fade follows the table as it is scrolled (a listener bound to the
 *     node, not a one-shot read),
 *   - a width change the SCROLLER's own box never reports — an auto-layout
 *     table growing wider — still updates the fade, because the hook observes
 *     the table through `attachTable` (deleting that ref regresses this),
 *   - the mask never intercepts the scroll gesture or the copy controls
 *     (`pointer-events` is untouched; the mask is `mask-image` only).
 *
 * jsdom does no layout and drops an inline `mask-image`, so scroll geometry is
 * stubbed and the live state is read from the scroller's `data-overflow` mirror
 * (as ThinkingBlock mirrors its fade with `data-clipped`) — that stub and that
 * mirror are what make the derivation testable at all.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const MD = [
  '| Symbol | Price | MACD Hist | Overall |',
  '| --- | --- | --- | --- |',
  '| GOOGL | $344.82 | -0.57 | STRONG SELL |',
].join('\n')

const renderTable = () => render(<MarkdownRenderer content={MD} />)
const overflow = () => screen.getByTestId('table-scroller').getAttribute('data-overflow')

describe('markdown table overflow cue', () => {
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

  it('shows no fade when the table fits', () => {
    stubGeometry({ hidden: 0 })
    renderTable()
    expect(overflow()).toBe('')
    // No mask style on a scroller with nothing clipped.
    expect(screen.getByTestId('table-scroller').getAttribute('style')).toBeNull()
  })

  it('fades only the side that hides content', () => {
    stubGeometry({ hidden: 240 })
    renderTable()
    // Nothing is hidden to the left at offset 0, so a fade there would point at
    // content that does not exist.
    expect(overflow()).toBe('right')
  })

  it('follows the table as it is scrolled', () => {
    stubGeometry({ hidden: 240 })
    renderTable()
    const scroller = screen.getByTestId('table-scroller')
    expect(overflow()).toBe('right')

    // Scrolled to the far end: the clipped side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scroller)
    expect(overflow()).toBe('left')

    // Scrolled to the middle: both edges clip.
    stubGeometry({ hidden: 240, scrolled: 120 })
    fireEvent.scroll(scroller)
    expect(overflow()).toBe('both')
  })

  it('never intercepts the scroll gesture or the copy controls', () => {
    stubGeometry({ hidden: 240 })
    renderTable()
    const scroller = screen.getByTestId('table-scroller')
    // The fade is a mask only — it adds no pointer-events-eating overlay node,
    // and the scroller itself keeps default pointer-events.
    expect(scroller.querySelector('[data-testid^="table-overflow-cue"]')).toBeNull()
    expect(scroller.className).not.toContain('pointer-events')
  })

  // Point of this test: an auto-layout table whose border-box grows wider does
  // NOT resize the scroller's own box and fires no scroll event, so the fade
  // would freeze unless the hook observes the TABLE. The component wires that
  // through `ref={attachTable}`; a functional ResizeObserver here fires the
  // table's observer callback, and the fade must appear. Deleting
  // `ref={attachTable}` from MarkdownTable makes this assertion fail — the
  // inert stub in the other tests cannot catch that, this can.
  it('updates the fade when the table grows wider with no scroll or scroller resize', () => {
    const observers: Array<{ cb: ResizeObserverCallback; targets: Element[] }> = []
    const RealRO = window.ResizeObserver
    window.ResizeObserver = class {
      cb: ResizeObserverCallback
      targets: Element[] = []
      constructor(cb: ResizeObserverCallback) { this.cb = cb; observers.push(this) }
      observe(el: Element) { this.targets.push(el) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      // Table starts fitting: no fade.
      stubGeometry({ hidden: 0 })
      renderTable()
      const scroller = screen.getByTestId('table-scroller')
      const table = scroller.querySelector('table') as HTMLElement
      expect(overflow()).toBe('')

      // An observer must be watching the TABLE node (that is `attachTable`'s
      // whole job). Without `ref={attachTable}` no observer targets the table
      // and this find returns undefined, failing the test.
      const tableObserver = observers.find(o => o.targets.includes(table))
      expect(tableObserver, 'expected a ResizeObserver observing the <table> (ref={attachTable})').toBeDefined()

      // The table re-renders wider (locale relabel / webfont) with no scroll and
      // no scroller-box resize; only the table's observer can report it.
      stubGeometry({ hidden: 240 })
      act(() => { tableObserver!.cb([], tableObserver as unknown as ResizeObserver) })
      expect(overflow()).toBe('right')
    } finally {
      window.ResizeObserver = RealRO
    }
  })
})
