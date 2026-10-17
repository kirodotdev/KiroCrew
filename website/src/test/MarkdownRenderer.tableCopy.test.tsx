import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => true),
  copyCode: vi.fn(async () => true),
}))

beforeEach(() => { vi.mocked(copyToClipboard).mockClear(); vi.mocked(copyToClipboard).mockResolvedValue(true) })
afterEach(() => { vi.useRealTimers() })

/**
 * Every rendered markdown table carries a copy row beneath it, the way T3 Code
 * offers "Copy as Markdown" / "Copy as CSV" on its tables. Selecting a rendered
 * table by hand pastes as tab-separated words at best, so the copy must be a
 * one-click action -- and the Markdown target must round-trip alignment that
 * the DOM no longer carries, which is why it reads the hast node, not the
 * table element.
 */
describe('markdown table copy actions', () => {
  const TABLE = ['| Symbol | Price |', '| --- | ---: |', '| GOOGL | $344.82 |'].join('\n')

  it('renders a Markdown and a CSV copy button under every table', () => {
    render(<MarkdownRenderer content={`${TABLE}\n\nand another\n\n${TABLE}`} />)
    expect(screen.getAllByTestId('table-copy-markdown')).toHaveLength(2)
    expect(screen.getAllByTestId('table-copy-csv')).toHaveLength(2)
    expect(screen.getAllByTestId('table-copy-markdown')[0]).toHaveAttribute('aria-label', 'Copy table as Markdown')
    expect(screen.getAllByTestId('table-copy-csv')[0]).toHaveAttribute('aria-label', 'Copy table as CSV')
  })

  it('copies GFM markdown with alignment preserved', async () => {
    render(<MarkdownRenderer content={TABLE} />)
    await act(async () => { fireEvent.click(screen.getByTestId('table-copy-markdown')) })
    expect(copyToClipboard).toHaveBeenCalledWith(TABLE)
  })

  it('copies CSV', async () => {
    render(<MarkdownRenderer content={TABLE} />)
    await act(async () => { fireEvent.click(screen.getByTestId('table-copy-csv')) })
    expect(copyToClipboard).toHaveBeenCalledWith('Symbol,Price\nGOOGL,$344.82')
  })

  it('shows Copied on the pressed button only, then reverts', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={TABLE} />)
    const md = screen.getByTestId('table-copy-markdown')
    const csv = screen.getByTestId('table-copy-csv')
    await act(async () => { fireEvent.click(md) })
    expect(md).toHaveAttribute('aria-label', 'Copied!')
    expect(csv).toHaveAttribute('aria-label', 'Copy table as CSV')
    act(() => { vi.advanceTimersByTime(1500) })
    expect(md).toHaveAttribute('aria-label', 'Copy table as Markdown')
  })

  it('surfaces a failed copy instead of confirming it', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    render(<MarkdownRenderer content={TABLE} />)
    const md = screen.getByTestId('table-copy-markdown')
    await act(async () => { fireEvent.click(md) })
    expect(md).toHaveAttribute('aria-label', 'Copy table as Markdown')
    expect(screen.getByText('Copy failed')).toBeInTheDocument()
  })

  it('keeps the table inside its horizontal-scroll wrapper, with the actions as a sibling row', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const table = container.querySelector('table')!
    expect(table.parentElement?.className).toContain('overflow-x-auto')
    // The action row must not sit inside the scroll wrapper, or it would
    // scroll away with a wide table.
    expect(table.parentElement?.querySelector('[data-testid="table-copy-markdown"]')).toBeNull()
    expect(screen.getByTestId('markdown-table').contains(screen.getByTestId('table-copy-markdown'))).toBe(true)
  })

  it('reveals the row on hover/focus like the code block, and always on touch', () => {
    render(<MarkdownRenderer content={TABLE} />)
    const row = screen.getByTestId('table-copy-markdown').parentElement!
    const cls = row.className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/table:opacity-100')
    expect(cls).toContain('group-focus-within/table:opacity-100')
    // The touch escape hatch: hover:none media shows the row unconditionally.
    expect(cls).toContain('[@media(hover:none)]:opacity-100')
    expect(screen.getByTestId('markdown-table').className).toContain('group/table')
  })

  it('shows a visible verb label beside each glyph, which flips to Copied! on the pressed button', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={TABLE} />)
    const md = screen.getByTestId('table-copy-markdown')
    const csv = screen.getByTestId('table-copy-csv')
    expect(md).toHaveTextContent('Copy Markdown')
    expect(csv).toHaveTextContent('Copy CSV')
    await act(async () => { fireEvent.click(md) })
    expect(md).toHaveTextContent('Copied!')
    expect(csv).toHaveTextContent('Copy CSV')
    act(() => { vi.advanceTimersByTime(1500) })
    expect(md).toHaveTextContent('Copy Markdown')
  })

  it('lets the reader dismiss a failed-copy notice', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    render(<MarkdownRenderer content={TABLE} />)
    await act(async () => { fireEvent.click(screen.getByTestId('table-copy-markdown')) })
    expect(screen.getByText('Copy failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText('Copy failed')).toBeNull()
  })
})
