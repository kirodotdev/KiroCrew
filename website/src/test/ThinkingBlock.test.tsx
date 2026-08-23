/**
 * The one-line live preview on the collapsed reasoning row.
 *
 * Liveness is derived from the content growing, not from a slot flag, so these
 * cases pin the two edges that derivation has to get right: a mount is not a
 * stream event (the transcript is virtualised and recycles finished rows), and
 * the preview settles back off once chunks stop.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import ThinkingBlock from '../pages/chat/ThinkingBlock'
import { ROW_PILL_BUTTON_CLASS, ROW_PILL_WRAPPER_CLASS, ROW_RAIL_CLASS } from '../pages/chat/rowPill'

const liveLine = () => screen.queryByTestId('thinking-live-line')

describe('ThinkingBlock live preview', () => {
  afterEach(() => { vi.useRealTimers() })

  it('stays off for a finished block that merely mounts', () => {
    render(<ThinkingBlock content="settled reasoning" />)
    expect(liveLine()).toBeNull()
  })

  it('shows the tail of the trace as one line while chunks arrive', () => {
    const { rerender } = render(<ThinkingBlock content="checking the config" />)
    rerender(<ThinkingBlock content={'checking the config\nnow the handler'} />)
    expect(liveLine()?.textContent).toBe('checking the config now the handler')
  })

  it('bounds the preview to the newest slice of a long trace', () => {
    const { rerender } = render(<ThinkingBlock content="x" />)
    rerender(<ThinkingBlock content={'ab'.repeat(400)} />)
    expect(liveLine()?.textContent).toHaveLength(240)
  })

  it('settles off once chunks stop arriving', () => {
    vi.useFakeTimers()
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expect(liveLine()).not.toBeNull()

    act(() => { vi.advanceTimersByTime(1500) })

    expect(liveLine()).toBeNull()
  })

  it('holds the row scrolled to its end, so the newest words are the visible ones', () => {
    // Chrome leaves scrollLeft at 0 on an overflowing LTR box even with
    // text-align: right, which shows the OLDEST words -- the exact inversion
    // this pins. jsdom has no layout, so scrollWidth is stubbed and the write
    // is observed directly.
    vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(1200)
    const writes: number[] = []
    Object.defineProperty(HTMLElement.prototype, 'scrollLeft', {
      configurable: true,
      get: () => 0,
      set(v: number) { writes.push(v) },
    })
    try {
      const { rerender } = render(<ThinkingBlock content="first" />)
      rerender(<ThinkingBlock content="first second" />)
      expect(writes).toContain(1200)
    } finally {
      Reflect.deleteProperty(HTMLElement.prototype, 'scrollLeft')
      vi.restoreAllMocks()
    }
  })

  it('fades the clipped edge only when the preview actually overflows', () => {
    // A preview that FITS must not have its first glyphs faded -- that is the
    // opening state of every reasoning burst. jsdom has no layout and drops the
    // inline mask, so the widths are stubbed and the gate is read off the state
    // the mask is bound to.
    const widths = vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(100)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(400)
    const fits = render(<ThinkingBlock content="first" />)
    fits.rerender(<ThinkingBlock content="first second" />)
    expect(liveLine()?.getAttribute('data-clipped')).toBe('false')
    fits.unmount()

    widths.mockReturnValue(1200)
    const overflows = render(<ThinkingBlock content="first" />)
    overflows.rerender(<ThinkingBlock content="first second" />)
    expect(liveLine()?.getAttribute('data-clipped')).toBe('true')
    vi.restoreAllMocks()
  })

  it('keeps the settled row a content-sized click target', () => {
    // Widening the button unconditionally would make the empty space beside the
    // label toggle every finished block.
    const classes = () => screen.getByRole('button').className.split(/\s+/)
    const { rerender } = render(<ThinkingBlock content="settled reasoning" />)
    expect(classes()).toContain('inline-flex')
    // Exact token match: the header also carries `max-w-full`, which a
    // substring check would false-positive on.
    expect(classes()).not.toContain('w-full')

    rerender(<ThinkingBlock content="settled reasoning +" />)

    expect(classes()).toContain('w-full')
  })

  it('drops the preview while the full trace is expanded', () => {
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expect(liveLine()).not.toBeNull()

    fireEvent.click(screen.getByRole('button'))

    expect(liveLine()).toBeNull()
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true')
  })

  it('shares the tool row layout spec (unified header + rail geometry)', () => {
    // The thinking block and the tool call row are siblings in a turn; their
    // header and expanded-rail geometry must stay on one spec. Both components
    // render ROW_PILL_*_CLASS from rowPill.ts, and this test asserts against
    // THOSE constants — an equality guarantee, not a value pin: restyling the
    // pill means editing the shared string, and both rows plus this test move
    // together.
    render(<ThinkingBlock content="some reasoning" />)
    const header = screen.getByRole('button').className.split(/\s+/)
    for (const token of ROW_PILL_BUTTON_CLASS.split(/\s+/)) {
      expect(header).toContain(token)
    }
    const wrapper = screen.getByRole('button').parentElement!
    const wrapperClasses = wrapper.className.split(/\s+/)
    for (const token of ROW_PILL_WRAPPER_CLASS.split(/\s+/)) {
      expect(wrapperClasses).toContain(token)
    }

    fireEvent.click(screen.getByRole('button'))
    // The rail asserts against ROW_RAIL_CLASS itself — ToolDetails renders the
    // same constant, so this is an equality guarantee across both panels.
    const rail = document.querySelector('.border-l-2.pl-3')!
    expect(rail).not.toBeNull()
    const railClasses = rail.className.split(/\s+/)
    for (const token of ROW_RAIL_CLASS.split(/\s+/)) {
      expect(railClasses).toContain(token)
    }
    // The old 550px container cap made reasoning text mysteriously narrower
    // than the tool payload box beside it; the container must span the row.
    expect(document.querySelector('[class*="max-w-[550px]"]')).toBeNull()
  })
})
