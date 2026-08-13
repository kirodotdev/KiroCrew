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
    const { rerender } = render(<ThinkingBlock content="settled reasoning" />)
    expect(screen.getByRole('button').className).toContain('inline-flex')
    expect(screen.getByRole('button').className).not.toContain('w-full')

    rerender(<ThinkingBlock content="settled reasoning +" />)

    expect(screen.getByRole('button').className).toContain('w-full')
  })

  it('drops the preview while the full trace is expanded', () => {
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expect(liveLine()).not.toBeNull()

    fireEvent.click(screen.getByRole('button'))

    expect(liveLine()).toBeNull()
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true')
  })
})
