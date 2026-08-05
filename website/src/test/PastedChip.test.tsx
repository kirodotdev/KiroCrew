import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import PastedChip from '../components/PastedChip'
import type { PasteBlock } from '../utils/pasteTokens'

const block: PasteBlock = {
  id: 'abc',
  seq: 1,
  lines: 42,
  content: 'line one\nline two\nUNIQUE_CONTENT_MARKER',
}

afterEach(cleanup)

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
