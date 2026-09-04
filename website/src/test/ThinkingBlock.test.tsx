/**
 * The one-line live preview on the collapsed reasoning row.
 *
 * Liveness is derived from the content growing, not from a slot flag, so these
 * cases pin the two edges that derivation has to get right: a mount is not a
 * stream event (the transcript is virtualised and recycles finished rows), and
 * the preview settles back off once chunks stop.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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

  it('labels a settled block as a finished thought, not an in-progress one', () => {
    // Several locales translate `thinking` as an explicitly in-progress form
    // ("思考中"), so a finished block that keeps that label reads as if the
    // model were still reasoning. A block that merely mounts (history restore,
    // virtualizer recycle) is settled by definition.
    render(<ThinkingBlock content="settled reasoning" />)
    expect(screen.getByRole('button').textContent).toContain('Thought process')
    expect(screen.getByRole('button').textContent).not.toContain('Thinking')
  })

  it('labels the row as thinking while chunks arrive, then settles the label', () => {
    vi.useFakeTimers()
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expect(screen.getByRole('button').textContent).toContain('Thinking')

    act(() => { vi.advanceTimersByTime(1500) })

    expect(screen.getByRole('button').textContent).toContain('Thought process')
    expect(screen.getByRole('button').textContent).not.toContain('Thinking')
  })

  it('keeps the label static while live — icon pulse is the only motion, and it drops once settled', () => {
    // The folded turn collapses every reasoning burst into ONE row (TurnBlock),
    // so that single row is the only place a running turn can signal "still
    // thinking". Liveness is carried by the pulsing Sparkles icon (plus the
    // live preview tail) — the label itself must NOT wear `.streaming-glow`:
    // that shimmer is sized for a full streaming sentence, and on a short
    // label (zh-CN "思考中" is 3 glyphs) its background chip + sweep read as a
    // smeared highlight. The pulse must still clear the moment the burst
    // settles so a finished block is visually quiet.
    vi.useFakeTimers()
    const { rerender, container } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expect(container.querySelector('.streaming-glow')).toBeNull()
    expect(screen.getByRole('button').textContent).toContain('Thinking')
    expect(container.querySelector('svg.animate-pulse')).not.toBeNull()

    act(() => { vi.advanceTimersByTime(1500) })

    expect(container.querySelector('.streaming-glow')).toBeNull()
    expect(container.querySelector('svg.animate-pulse')).toBeNull()
  })

  it('a settled block that merely mounts carries no streaming shimmer', () => {
    // History restore / virtualizer recycle must not paint any liveness motion
    // on an already-finished block.
    const { container } = render(<ThinkingBlock content="settled reasoning" />)
    expect(container.querySelector('.streaming-glow')).toBeNull()
    expect(container.querySelector('svg.animate-pulse')).toBeNull()
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

/**
 * Follow-tail behaviour of the EXPANDED reasoning panel.
 *
 * Reasoning is appended, so the newest text is at the BOTTOM. These cases pin the
 * contract: pinned to the end WHILE LIVE, top-down once settled, released when the
 * reader scrolls up, re-armed on return.
 *
 * jsdom has no layout, so the geometry is stubbed and scrollTop is backed by a
 * real value the component can read back -- as the scrollLeft case above does.
 */
describe('ThinkingBlock expanded follow-tail', () => {
  const CONTENT_H = 1000
  const VIEWPORT_H = 360        // matches the panel's max-h-[360px]
  const BOTTOM = CONTENT_H - VIEWPORT_H

  let scrollTop = 0
  let writes: number[] = []

  const body = () => screen.getByTestId('thinking-body')
  const expand = () => fireEvent.click(screen.getByRole('button'))
  /** Move the panel as a READER would, then let the component observe it. */
  const readerScrollsTo = (px: number) => {
    body().scrollTop = px
    fireEvent.scroll(body())
    writes = []
  }

  beforeEach(() => {
    scrollTop = 0
    writes = []
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(CONTENT_H)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(VIEWPORT_H)
    Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set(v: number) { scrollTop = v; writes.push(v) },
    })
  })

  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, 'scrollTop')
    vi.restoreAllMocks()
  })

  it('opens at the END of the trace, not the beginning', () => {
    // The defect this pins: an untouched container shows the OLDEST reasoning,
    // so opening a LIVE trace lands the reader away from the newest words.
    const { rerender } = render(<ThinkingBlock content="a long reasoning" />)
    rerender(<ThinkingBlock content="a long reasoning trace" />)

    expand()

    expect(writes).toContain(CONTENT_H)
  })

  it('stays at the end as new reasoning arrives', () => {
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expand()
    writes = []

    rerender(<ThinkingBlock content="first second third" />)

    expect(writes).toContain(CONTENT_H)
  })

  it('stops following once the reader scrolls up', () => {
    // Following must yield to the reader: being yanked back down mid-sentence is
    // the same class of annoyance as never following at all.
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expand()
    // Assert following is ACTIVE first. Without this the test passes against a
    // component that never scrolls at all -- no writes either way.
    expect(writes).toContain(CONTENT_H)

    readerScrollsTo(100)
    rerender(<ThinkingBlock content="first second third" />)

    expect(writes).toEqual([])
  })

  it('resumes following when the reader returns to the end', () => {
    const { rerender } = render(<ThinkingBlock content="first" />)
    expand()
    readerScrollsTo(100)
    rerender(<ThinkingBlock content="first second" />)
    expect(writes).toEqual([])

    readerScrollsTo(BOTTOM)

    rerender(<ThinkingBlock content="first second third" />)
    expect(writes).toContain(CONTENT_H)
  })

  it('counts a sub-pixel shortfall as still at the end', () => {
    // Without TAIL_SLACK_PX an exact test never re-arms, stranding a reader who
    // scrolled back down for the rest of the turn -- silent, not visible.
    const { rerender } = render(<ThinkingBlock content="first" />)
    expand()
    readerScrollsTo(100)

    readerScrollsTo(BOTTOM - 10)

    rerender(<ThinkingBlock content="first second" />)
    expect(writes).toContain(CONTENT_H)
  })

  it('re-pins to the end when a scrolled-up panel is closed and reopened', () => {
    // Re-opening is a fresh "show me this" gesture, so a LIVE trace should land
    // at the newest reasoning regardless of where the reader left the panel.
    const { rerender } = render(<ThinkingBlock content="first" />)
    rerender(<ThinkingBlock content="first second" />)
    expand()
    readerScrollsTo(100)

    expand()   // collapse
    expand()   // reopen

    expect(writes).toContain(CONTENT_H)
  })

  it('opens a SETTLED trace at the TOP, so finished prose reads top-down', () => {
    // Tail-pinning is a LOG contract. Reasoning is prose, so a finished trace
    // opened to be read must not start at its last paragraph.
    render(<ThinkingBlock content="a long finished reasoning trace" />)

    expand()

    expect(writes).toEqual([])
    expect(body().scrollTop).toBe(0)
  })

  it('starts following a settled trace once it resumes streaming', () => {
    // The reader opted in by returning to the end, so a resume should follow --
    // also the positive control that the component CAN write in this setup.
    const { rerender } = render(<ThinkingBlock content="first" />)
    expand()
    expect(writes).toEqual([])
    readerScrollsTo(BOTTOM)

    rerender(<ThinkingBlock content="first second" />)

    expect(writes).toContain(CONTENT_H)
  })

  it('does NOT yank a reader who opened a settled trace and stayed at the top', () => {
    // The regression this pins: pinning on the settled->live edge jumps a reader
    // mid-sentence, with no scroll of their own to release it.
    const { rerender } = render(<ThinkingBlock content="first" />)
    expand()
    expect(writes).toEqual([])

    rerender(<ThinkingBlock content="first second" />)

    expect(writes).toEqual([])
    expect(body().scrollTop).toBe(0)
  })

  it('leaves the reader alone when a live trace settles', () => {
    // Liveness is a dependency of the pin effect, so its falling edge re-runs
    // the effect -- that must not be a jump.
    vi.useFakeTimers()
    try {
      const { rerender } = render(<ThinkingBlock content="first" />)
      rerender(<ThinkingBlock content="first second" />)
      expand()
      expect(writes).toContain(CONTENT_H)
      writes = []

      act(() => { vi.advanceTimersByTime(1500) })

      expect(writes).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps following when a paused trace resumes with a chunk taller than the slack', () => {
    // The suite stubs scrollHeight to a CONSTANT, so an append never grows the box
    // and this case cannot arise; give this one a height that actually grows.
    const CHUNK_H = 200          // comfortably more than the 20px tail slack
    let height = CONTENT_H
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height)
    const { rerender } = render(<ThinkingBlock content="first" />)
    expand()
    readerScrollsTo(height - VIEWPORT_H)   // legitimately AT the end
    height = CONTENT_H + CHUNK_H           // the append lands before liveness flips

    rerender(<ThinkingBlock content="first second" />)

    expect(writes).toContain(height)
  })
})
