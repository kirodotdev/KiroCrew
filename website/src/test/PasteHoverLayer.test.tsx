import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, act } from '@testing-library/react'
import { createRef } from 'react'
import PasteHoverLayer, { type PasteHoverHandle } from '../components/PasteHoverLayer'
import type { PasteBlock } from '../utils/pasteTokens'
import { formatToken, findTokenRanges } from '../utils/pasteTokens'

// Mock isTouchDevice — same pattern as PastedChip.test.tsx.
const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

const block: PasteBlock = {
  id: 'hover-1',
  seq: 1,
  lines: 20,
  content: Array.from({ length: 20 }, (_, i) => `composer line ${i + 1}`).join('\n'),
}

const shortBlock: PasteBlock = {
  id: 'hover-2',
  seq: 2,
  lines: 3,
  content: 'short\ncontent\nhere',
}

/** Build a value string that contains the token for a block. */
function valueWith(...blocks: PasteBlock[]): string {
  return blocks.map(b => formatToken(b)).join('\nsome text between\n')
}

afterEach(() => { cleanup(); touchEnv.touch = false })

/** Mock getBoundingClientRect on [data-paste-seq] spans to simulate layout. */
function mockChipRects() {
  const origGetBCR = Element.prototype.getBoundingClientRect
  let callIdx = 0
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this.hasAttribute?.('data-paste-seq')) {
      // Each chip occupies a 200x20 region at y=100, stacked horizontally.
      const idx = callIdx++
      return new DOMRect(10 + idx * 220, 100, 200, 20)
    }
    return origGetBCR.call(this)
  })
  return () => { callIdx = 0 }
}

/**
 * Render the component and return a helper to trigger handleMouseMove at
 * coordinates that hit the first chip (based on our mocked rects).
 */
function setup(blocks: PasteBlock[], value: string) {
  const hoverRef = createRef<PasteHoverHandle>()
  const mirrorRef = createRef<HTMLDivElement>()

  const Wrapper = ({ blocks: b, value: v }: { blocks: PasteBlock[]; value: string }) => (
    <div>
      <div ref={mirrorRef} data-testid="mock-mirror">
        {b.map((bl, i) => (
          <span key={i} className="bg-accent-subtle" data-paste-seq={bl.seq} data-testid={`chip-${bl.seq}`}>
            {formatToken(bl)}
          </span>
        ))}
      </div>
      <PasteHoverLayer ref={hoverRef} value={v} blocks={b} mirrorRef={mirrorRef} />
    </div>
  )

  const result = render(<Wrapper blocks={blocks} value={value} />)

  const moveOver = (chipIndex = 0) => {
    hoverRef.current!.handleMouseMove({
      clientX: 15 + chipIndex * 220,
      clientY: 105,
    } as MouseEvent)
  }

  const moveOutside = () => {
    hoverRef.current!.handleMouseMove({ clientX: -100, clientY: -100 } as MouseEvent)
  }

  const leave = () => {
    hoverRef.current!.handleMouseLeave()
  }

  const rerender = (newBlocks: PasteBlock[], newValue: string) => {
    result.rerender(<Wrapper blocks={newBlocks} value={newValue} />)
  }

  return { ...result, moveOver, moveOutside, leave, hoverRef, rerender }
}

describe('PasteHoverLayer', () => {
  it('renders no visible DOM in the component tree (only a portal)', () => {
    const value = valueWith(block)
    const { container } = setup([block], value)
    // No tooltip in the component tree itself (it's in document.body via portal).
    expect(container.querySelector('[role="tooltip"]')).toBeNull()
  })
})

describe('PasteHoverLayer hover preview', () => {
  let resetIdx: () => void

  beforeEach(() => {
    vi.useFakeTimers()
    resetIdx = mockChipRects()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('shows a preview tooltip after hovering a token past the dwell delay', () => {
    const value = valueWith(block)
    const { moveOver } = setup([block], value)

    resetIdx()
    moveOver()

    // Before dwell delay — no preview.
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()
    expect(screen.getByText(/composer line 1/)).toBeInTheDocument()
  })

  it('caps the preview at PREVIEW_MAX_LINES and shows "+N more" footer', () => {
    const value = valueWith(block)
    const { moveOver } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })

    const panel = screen.getByTestId('composer-paste-preview-1')
    expect(panel.textContent).toContain('composer line 12')
    expect(panel.textContent).not.toContain('composer line 13')
    expect(panel.textContent).toContain('8 more lines')
  })

  it('shows full content without footer for short pastes', () => {
    const value = valueWith(shortBlock)
    const { moveOver } = setup([shortBlock], value)

    resetIdx()
    moveOver(0) // first (only) chip
    act(() => { vi.advanceTimersByTime(350) })

    const panel = screen.getByTestId('composer-paste-preview-2')
    expect(panel.textContent).toContain('here')
    expect(panel.textContent).not.toContain('more line')
  })

  it('dismisses the preview on handleMouseLeave', async () => {
    const value = valueWith(block)
    const { moveOver, leave } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()

    leave()
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument())
  })

  it('dismisses when cursor moves outside token area', async () => {
    const value = valueWith(block)
    const { moveOver, moveOutside } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()

    resetIdx()
    moveOutside()
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument())
  })

  it('does NOT show the preview on touch devices', () => {
    touchEnv.touch = true
    const value = valueWith(block)
    const { moveOver } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })

  it('dismisses the preview on Escape key', async () => {
    const value = valueWith(block)
    const { moveOver } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument())
  })

  it('renders nothing when blocks array is empty', () => {
    const { moveOver } = setup([], 'plain text')

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('dismisses the preview when value changes (typing)', async () => {
    const value = valueWith(block)
    const { moveOver, rerender } = setup([block], value)

    resetIdx()
    moveOver()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()

    // Re-render with a new value (simulating the user typing)
    rerender([block], value + ' extra text')
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument())
  })

  it('does NOT show preview when mouse buttons are pressed (drag-select)', () => {
    const value = valueWith(block)
    const { hoverRef } = setup([block], value)

    resetIdx()
    // Simulate mousemove with button pressed (text selection drag)
    hoverRef.current!.handleMouseMove({
      clientX: 15,
      clientY: 105,
      buttons: 1,
    } as MouseEvent)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })
})

/** Setup variant that wires onActivePanelChange so aria-describedby reporting
 *  can be asserted, and exposes findTokenRanges-derived caret offsets. */
function setupA11y(blocks: PasteBlock[], value: string) {
  const hoverRef = createRef<PasteHoverHandle>()
  const mirrorRef = createRef<HTMLDivElement>()
  const onPanel = vi.fn()

  const Wrapper = () => (
    <div>
      <div ref={mirrorRef} data-testid="mock-mirror">
        {blocks.map((bl, i) => (
          <span key={i} className="bg-accent-subtle" data-paste-seq={bl.seq}>
            {formatToken(bl)}
          </span>
        ))}
      </div>
      <PasteHoverLayer ref={hoverRef} value={value} blocks={blocks} mirrorRef={mirrorRef} onActivePanelChange={onPanel} />
    </div>
  )

  const result = render(<Wrapper />)
  const ranges = findTokenRanges(value, blocks)
  /** A caret offset that lands strictly inside the given block's token. */
  const caretInside = (seq: number) => {
    const r = ranges.find(x => x.block.seq === seq)!
    return Math.floor((r.start + r.end) / 2)
  }
  return { ...result, hoverRef, onPanel, caretInside }
}

describe('PasteHoverLayer keyboard / AT access (caret peek)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockChipRects()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('opens the preview when a collapsed caret lands inside a token', () => {
    const value = valueWith(block)
    const { hoverRef, caretInside } = setupA11y([block], value)
    const c = caretInside(1)

    act(() => { hoverRef.current!.handleCaret(c, c) })
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()
  })

  it('does NOT open on a non-collapsed selection (that is a drag, not a peek)', () => {
    const value = valueWith(block)
    const { hoverRef, caretInside } = setupA11y([block], value)
    const c = caretInside(1)

    act(() => { hoverRef.current!.handleCaret(c, c + 2) })
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })

  it('does NOT open when the caret is outside any token', () => {
    // Put literal text before the token so an early caret is clearly outside.
    const value = 'hello world\n' + valueWith(block)
    const { hoverRef } = setupA11y([block], value)

    act(() => { hoverRef.current!.handleCaret(3, 3) }) // inside "hello"
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })

  it('does NOT open on a touch device (caret peek is suppressed like hover)', () => {
    touchEnv.touch = true
    const value = valueWith(block)
    const { hoverRef, caretInside } = setupA11y([block], value)
    const c = caretInside(1)

    act(() => { hoverRef.current!.handleCaret(c, c) })
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })

  it('does NOT open when the caret rests on a token EDGE (post-paste resting position)', () => {
    const value = valueWith(block)
    const { hoverRef } = setupA11y([block], value)
    const ranges = findTokenRanges(value, [block])
    const end = ranges.find(r => r.block.seq === 1)!.end // caret parks here after a paste

    act(() => { hoverRef.current!.handleCaret(end, end) })
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument()
  })

  it('reports the open panel id via onActivePanelChange (aria-describedby wiring)', () => {
    const value = valueWith(block)
    const { hoverRef, onPanel, caretInside } = setupA11y([block], value)
    const c = caretInside(1)

    act(() => { hoverRef.current!.handleCaret(c, c) })
    act(() => { vi.advanceTimersByTime(350) })

    // Last call carries a non-null panel id matching the open tooltip's id.
    const openId = onPanel.mock.calls.at(-1)?.[0]
    expect(openId).toBeTruthy()
    expect(screen.getByTestId('composer-paste-preview-1').id).toBe(openId)
  })

  it('clears the reported panel id to null on blur', async () => {
    const value = valueWith(block)
    const { hoverRef, onPanel, caretInside } = setupA11y([block], value)
    const c = caretInside(1)

    act(() => { hoverRef.current!.handleCaret(c, c) })
    act(() => { vi.advanceTimersByTime(350) })
    act(() => { hoverRef.current!.handleMouseLeave() })

    vi.useRealTimers()
    await waitFor(() => expect(onPanel.mock.calls.at(-1)?.[0]).toBeNull())
  })
})

describe('PasteHoverLayer enter-once timing + block-change dismiss', () => {
  let resetIdx: () => void
  beforeEach(() => {
    vi.useFakeTimers()
    resetIdx = mockChipRects()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('does not restart the dwell timer on repeated mousemove over the same token', () => {
    const value = valueWith(block)
    const { moveOver } = setup([block], value)

    resetIdx()
    moveOver(0)
    // Jitter: keep moving over the same chip, each move short of the full dwell.
    act(() => { vi.advanceTimersByTime(200) })
    resetIdx()
    moveOver(0)
    act(() => { vi.advanceTimersByTime(200) })
    // Cumulative 400ms > 300ms dwell; if jitter had restarted the timer each
    // time (200ms < 300ms) the preview would never open. Enter-once means the
    // first schedule still fires.
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()
  })

  it('dismisses the stale tooltip immediately when the hovered block changes', async () => {
    const value = valueWith(block, shortBlock)
    const { moveOver } = setup([block, shortBlock], value)

    resetIdx()
    moveOver(0)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-1')).toBeInTheDocument()

    // Move onto the second chip: block-1's anchor is cleared synchronously
    // (it starts animating out at once — no waiting for the new dwell), and
    // block-2 opens after its own dwell.
    resetIdx()
    moveOver(1)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('composer-paste-preview-2')).toBeInTheDocument()

    // block-1's tooltip finishes exiting (framer-motion AnimatePresence).
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('composer-paste-preview-1')).not.toBeInTheDocument())
  })
})
