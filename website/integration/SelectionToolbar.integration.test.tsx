import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { useSelectionActions, type SelectionAction } from '../src/components/SelectionToolbar'

// isTouchDevice gates the touch-only `selectionchange` trigger (mobile selection
// via long-press + drag handles never fires `mouseup`). Default false so the
// existing mouse/keyboard tests exercise the desktop path; flip per-test.
const touchEnv = { touch: false }
vi.mock('../src/utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

// Mock framer-motion to skip animations (immediate mount/unmount).
// motion.div forwards its ref so toolbarRef.current resolves to the rendered
// node — the position-clamp layout effect measures it via offsetWidth, so a
// dropped ref would silently no-op the clamp (and mask the bug it guards).
vi.mock('framer-motion', async () => {
  const { forwardRef } = await import('react')
  return {
    AnimatePresence: ({ children }: any) => children,
    motion: {
      div: forwardRef(({ children, ...props }: any, ref: any) => (
        <div ref={ref} {...props}>{children}</div>
      )),
    },
  }
})

// jsdom doesn't implement Range.getBoundingClientRect
if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function () {
    return new DOMRect(10, 10, 100, 20)
  }
}

function mockSelection(container: HTMLElement, text: string) {
  const textNode = container.firstChild as Text
  const range = document.createRange()
  range.setStart(textNode, 0)
  range.setEnd(textNode, text.length)

  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function Wrapper({ actions, children }: { actions: SelectionAction[]; children: string }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div>
      <div ref={ref} data-testid="container">{children}</div>
      <SelectionToolbar containerRef={ref} actions={actions} />
    </div>
  )
}

describe('SelectionToolbar', () => {
  beforeEach(() => { vi.useFakeTimers(); touchEnv.touch = false })
  afterEach(() => { vi.useRealTimers(); window.getSelection()?.removeAllRanges() })

  it('shows toolbar after mouseup with text selected inside container', async () => {
    const onClick = vi.fn()
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when selection is empty', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    window.getSelection()?.removeAllRanges()
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('calls action onClick with selected text when button is clicked', () => {
    const onClick = vi.fn()
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(onClick).toHaveBeenCalledWith('Hello World', expect.any(DOMRect))
  })

  it('stays visible after copy action (does not dismiss)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('hides after non-copy action (e.g. comment)', () => {
    const actions: SelectionAction[] = [{ id: 'comment', icon: null, label: 'Comment', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    expect(screen.queryByRole('button', { name: 'Comment' })).not.toBeInTheDocument()
  })

  it('hides on Escape key', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.keyUp(document, { key: 'Escape' })
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('hides on mousedown outside container and toolbar', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('does not hide on mousedown inside container (new selection)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.mouseDown(container)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not reposition on mouseup inside toolbar (copy click)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    const btn = screen.getByRole('button', { name: 'Copy' })
    // mouseup on toolbar button should not re-trigger checkSelection
    fireEvent.mouseUp(btn, { clientX: 200, clientY: 200 })
    act(() => { vi.advanceTimersByTime(60) })

    // Toolbar still there, not repositioned (if it had run checkSelection
    // with the new mouse position, it would have set pos to {200, 208})
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('prevents default on button mousedown to preserve selection', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    const btn = screen.getByRole('button', { name: 'Copy' })
    const event = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    const prevented = !btn.dispatchEvent(event)
    expect(prevented).toBe(true)
  })

  it('renders multiple actions', () => {
    const actions: SelectionAction[] = [
      { id: 'comment', icon: null, label: 'Comment', onClick: vi.fn() },
      { id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() },
    ]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when selection is outside the container', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(
      <div>
        <Wrapper actions={actions}>Inside</Wrapper>
        <div data-testid="outside">Outside text</div>
      </div>
    )

    const outside = screen.getByTestId('outside')
    // Select text in the outside element
    const textNode = outside.firstChild as Text
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 7)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)

    fireEvent.mouseUp(document, { clientX: 200, clientY: 100 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('positions toolbar using selection rect center for keyboard selection', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    // Trigger via keyboard (Shift key up) instead of mouse
    fireEvent.keyUp(document, { key: 'ArrowRight', shiftKey: true })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when container ref is null', () => {
    function NullRefWrapper({ actions }: { actions: SelectionAction[] }) {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div>
          <div data-testid="container">Some text</div>
          <SelectionToolbar containerRef={ref} actions={actions} />
        </div>
      )
    }
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<NullRefWrapper actions={actions} />)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Some text')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('calls copyToClipboard via useSelectionActions copy action', () => {
    // Mock clipboard. happy-dom defines navigator.clipboard as getter-only, so
    // a plain assignment throws; defineProperty (configurable) replaces it.
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    function UseActionsWrapper() {
      const ref = useRef<HTMLDivElement>(null)
      const actions = useSelectionActions()
      return (
        <div>
          <div ref={ref} data-testid="container">Hello World</div>
          <SelectionToolbar containerRef={ref} actions={actions} />
        </div>
      )
    }
    render(<UseActionsWrapper />)
    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('Hello World')
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('clamps left so the toolbar never hangs off the right edge', () => {
    // jsdom reports offsetWidth/offsetHeight as 0; stub a realistic toolbar
    // footprint so the clamp math (left = min(x - w/2, vw - w - margin)) has a
    // non-zero width to work with.
    const W = 220
    const H = 36
    const widthSpy = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(W)
    const heightSpy = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(H)
    const origInnerWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 })

    try {
      const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
      render(<Wrapper actions={actions}>Hello World</Wrapper>)

      const container = screen.getByTestId('container')
      mockSelection(container, 'Hello World')
      // Mouse near the right edge — unclamped left would be 390 - 220/2 = 280,
      // overflowing the 400px viewport. Clamp ceiling is vw - W - margin = 172.
      fireEvent.mouseUp(document, { clientX: 390, clientY: 50 })
      act(() => { vi.advanceTimersByTime(60) })

      const button = screen.getByRole('button', { name: 'Copy' })
      // The portal renders the toolbar as the motion.div ancestor of the button.
      const toolbar = button.closest('.fixed') as HTMLElement
      const left = parseFloat(toolbar.style.left)
      const margin = 8
      // Fully inside the viewport: left within [margin, vw - W - margin].
      expect(left).toBeGreaterThanOrEqual(margin)
      expect(left).toBeLessThanOrEqual(window.innerWidth - W - margin) // <= 172
      // And no CSS transform is used for positioning (framer-motion owns it).
      expect(toolbar.style.transform).toBe('')
    } finally {
      widthSpy.mockRestore()
      heightSpy.mockRestore()
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: origInnerWidth })
    }
  })

  // --- Touch / mobile: selection made via long-press + drag handles fires
  // `selectionchange`, never `mouseup`. These cover the mobile quote-menu path. ---

  it('shows toolbar on selectionchange for touch devices (no mouseup)', () => {
    touchEnv.touch = true
    const actions: SelectionAction[] = [{ id: 'quote', icon: null, label: 'Quote', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    // Nothing yet — the trigger is debounced.
    expect(screen.queryByRole('button', { name: 'Quote' })).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(400) })

    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
  })

  it('ignores selectionchange on non-touch devices (desktop uses mouseup)', () => {
    touchEnv.touch = false
    const actions: SelectionAction[] = [{ id: 'quote', icon: null, label: 'Quote', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(400) })

    expect(screen.queryByRole('button', { name: 'Quote' })).not.toBeInTheDocument()
  })

  it('debounces rapid selectionchange (handle drag) into a single show', () => {
    touchEnv.touch = true
    const actions: SelectionAction[] = [{ id: 'quote', icon: null, label: 'Quote', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    // Simulate a handle drag: several changes in quick succession, each within
    // the debounce window — the toolbar must not appear until the user settles.
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(200) })
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByRole('button', { name: 'Quote' })).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(350) })

    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
  })

  it('hides on touch when the selection collapses', () => {
    touchEnv.touch = true
    const actions: SelectionAction[] = [{ id: 'quote', icon: null, label: 'Quote', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(400) })
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()

    // Tapping elsewhere clears the selection → selectionchange with no range.
    window.getSelection()?.removeAllRanges()
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(400) })

    expect(screen.queryByRole('button', { name: 'Quote' })).not.toBeInTheDocument()
  })
})

describe('useSelectionActions', () => {
  function HookWrapper({ onQuote }: { onQuote?: (text: string, rect: DOMRect) => void }) {
    const actions = useSelectionActions(onQuote)
    return (
      <div data-testid="actions">
        {actions.map(a => <span key={a.id} data-testid={a.id}>{a.label}</span>)}
      </div>
    )
  }

  it('returns only Copy when no onQuote provided', () => {
    render(<HookWrapper />)
    expect(screen.getByTestId('copy')).toBeInTheDocument()
    expect(screen.queryByTestId('quote')).not.toBeInTheDocument()
  })

  it('returns Quote and Copy when onQuote provided', () => {
    render(<HookWrapper onQuote={vi.fn()} />)
    expect(screen.getByTestId('quote')).toBeInTheDocument()
    expect(screen.getByTestId('copy')).toBeInTheDocument()
  })
})
