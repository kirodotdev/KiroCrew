import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import PastedChip from '../components/PastedChip'
import type { PasteBlock } from '../utils/pasteTokens'

// Mock isTouchDevice — mirror ChatInput.test.tsx pattern.
const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

const block: PasteBlock = {
  id: 'abc',
  seq: 1,
  lines: 42,
  content: 'line one\nline two\nUNIQUE_CONTENT_MARKER',
}

afterEach(() => { cleanup(); touchEnv.touch = false })

describe('PastedChip', () => {
  it('renders the collapsed label with line count', () => {
    render(<PastedChip block={block} />)
    expect(screen.getByText(/Paste #1 · 42 lines/)).toBeInTheDocument()
  })

  it('starts collapsed: aria-expanded is false and content is hidden', () => {
    render(<PastedChip block={block} />)
    const btn = screen.getByRole('button')
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/UNIQUE_CONTENT_MARKER/)).not.toBeInTheDocument()
  })

  it('expands on click: flips aria-expanded and reveals content', () => {
    render(<PastedChip block={block} />)
    const btn = screen.getByRole('button')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText(/UNIQUE_CONTENT_MARKER/)).toBeInTheDocument()
  })

  it('collapses again on a second click', async () => {
    render(<PastedChip block={block} />)
    const btn = screen.getByRole('button')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    await waitFor(() =>
      expect(screen.queryByText(/UNIQUE_CONTENT_MARKER/)).not.toBeInTheDocument(),
    )
  })

  it('uses singular "line" for a single-line paste', () => {
    cleanup()
    render(<PastedChip block={{ ...block, lines: 1 }} />)
    expect(screen.getByText(/Paste #1 · 1 line\b/)).toBeInTheDocument()
    expect(screen.getByRole('button')).toHaveAttribute('aria-label', 'Expand pasted 1 line')
  })

  it('exposes an accessible expand/collapse label', () => {
    render(<PastedChip block={block} />)
    const btn = screen.getByRole('button')
    expect(btn).toHaveAttribute('aria-label', 'Expand pasted 42 lines')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-label', 'Collapse pasted 42 lines')
  })
})

describe('PastedChip hover preview', () => {
  const longBlock: PasteBlock = {
    id: 'long',
    seq: 2,
    lines: 20,
    content: Array.from({ length: 20 }, (_, i) => `preview line ${i + 1}`).join('\n'),
  }

  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const hover = (btn: HTMLElement) => {
    fireEvent.mouseEnter(btn)
    act(() => { vi.advanceTimersByTime(350) })
  }

  it('shows a preview after hovering the collapsed chip past the dwell delay', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    fireEvent.mouseEnter(btn)
    // Before the dwell delay elapses, nothing shows (no flicker on pass-through).
    expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('paste-preview-2')).toBeInTheDocument()
    expect(screen.getByText(/preview line 1/)).toBeInTheDocument()
  })

  it('caps the preview and shows a "+N more lines" footer for long pastes', () => {
    render(<PastedChip block={longBlock} />)
    hover(screen.getByRole('button'))
    const panel = screen.getByTestId('paste-preview-2')
    expect(panel.textContent).toContain('preview line 12')
    expect(panel.textContent).not.toContain('preview line 13')
    expect(panel.textContent).toContain('8 more lines')
  })

  it('shows the full content without a footer when under the cap', () => {
    render(<PastedChip block={{ ...longBlock, content: 'a\nb\nc', lines: 3 }} />)
    hover(screen.getByRole('button'))
    const panel = screen.getByTestId('paste-preview-2')
    expect(panel.textContent).toContain('c')
    expect(panel.textContent).not.toContain('more line')
  })

  it('dismisses the preview on mouse leave', async () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    hover(btn)
    expect(screen.getByTestId('paste-preview-2')).toBeInTheDocument()
    fireEvent.mouseLeave(btn)
    vi.useRealTimers()
    await waitFor(() => expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument())
  })

  it('opens on keyboard focus for accessibility', () => {
    render(<PastedChip block={longBlock} />)
    fireEvent.focus(screen.getByRole('button'))
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByTestId('paste-preview-2')).toBeInTheDocument()
  })

  it('never shows the preview while the block is expanded', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    fireEvent.mouseEnter(btn)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument()
  })

  it('clicking to expand cancels a pending preview', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    fireEvent.mouseEnter(btn)
    fireEvent.click(btn) // expand before the dwell timer fires
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument()
  })

  it('does NOT open the preview on touch devices', () => {
    touchEnv.touch = true
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    fireEvent.mouseEnter(btn)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument()
    // Focus should also not open it
    fireEvent.focus(btn)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByTestId('paste-preview-2')).not.toBeInTheDocument()
  })

  it('dismisses the preview on Escape key', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    hover(btn)
    expect(btn).toHaveAttribute('aria-describedby')
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(btn).not.toHaveAttribute('aria-describedby')
  })

  it('dismisses the preview on pointerdown outside', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    hover(btn)
    expect(btn).toHaveAttribute('aria-describedby')
    act(() => { document.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    expect(btn).not.toHaveAttribute('aria-describedby')
  })

  it('sets aria-describedby on trigger when preview is open', () => {
    render(<PastedChip block={longBlock} />)
    const btn = screen.getByRole('button')
    expect(btn).not.toHaveAttribute('aria-describedby')
    hover(btn)
    const panel = screen.getByTestId('paste-preview-2')
    expect(btn).toHaveAttribute('aria-describedby', panel.id)
  })
})
