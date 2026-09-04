import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

beforeEach(() => { vi.mocked(copyToClipboard).mockClear() })
afterEach(() => { vi.useRealTimers() })

describe('InlineCode click-to-copy (non-path chips)', () => {
  it('copies the inline code text on click', async () => {
    render(<MarkdownRenderer content={'Run `npm test` please.'} />)
    const code = await screen.findByText('npm test')
    expect(code.tagName).toBe('CODE')
    expect(code).toHaveAttribute('role', 'button')

    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('copies on Enter keydown', async () => {
    render(<MarkdownRenderer content={'Try `curl -s https://example.com`.'} />)
    const code = await screen.findByText('curl -s https://example.com')
    fireEvent.keyDown(code, { key: 'Enter' })
    expect(copyToClipboard).toHaveBeenCalledWith('curl -s https://example.com')
  })

  it('copies on Space keydown', async () => {
    render(<MarkdownRenderer content={'Set `NODE_ENV=production`.'} />)
    const code = await screen.findByText('NODE_ENV=production')
    fireEvent.keyDown(code, { key: ' ' })
    expect(copyToClipboard).toHaveBeenCalledWith('NODE_ENV=production')
  })

  it('shows a "Click to copy" title before click and "Copied!" after', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    expect(code).toHaveAttribute('title', 'Click to copy')

    await act(async () => { fireEvent.click(code) })
    expect(code).toHaveAttribute('title', 'Copied!')

    // Title reverts after 1500ms
    act(() => { vi.advanceTimersByTime(1500) })
    expect(code).toHaveAttribute('title', 'Click to copy')
  })

  it('is focusable via tabIndex', async () => {
    render(<MarkdownRenderer content={'Check `FOO_BAR` env var.'} />)
    const code = await screen.findByText('FOO_BAR')
    expect(code).toHaveAttribute('tabindex', '0')
  })

  it('has cursor-pointer and hover:underline classes', async () => {
    render(<MarkdownRenderer content={'Run `ls -la`.'} />)
    const code = await screen.findByText('ls -la')
    expect(code.className).toContain('cursor-pointer')
    expect(code.className).toContain('hover:underline')
  })
})
