/**
 * Integration tests for MarkdownPanel comment/copy selection flow.
 * Tests the interaction between SelectionToolbar and CommentPopover within the panel.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import MarkdownPanel from '../src/components/MarkdownPanel'

// Mock framer-motion (SelectionToolbar uses it)
vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => children,
  motion: { div: ({ children, ...props }: any) => <div {...props}>{children}</div> },
}))

// Mock monaco editor (heavy dependency)
vi.mock('@monaco-editor/react', () => ({ default: () => <div data-testid="monaco" /> }))

// Mock useFileWatch (requires backend)
vi.mock('../src/hooks/useFileWatch', () => ({
  useFileWatch: () => ({ status: 'closed' }),
}))

// Mock clipboard
const writeText = vi.fn().mockResolvedValue(undefined)
Object.defineProperty(navigator, 'clipboard', {
  value: { writeText },
  writable: true,
  configurable: true,
})

// jsdom doesn't implement Range.getBoundingClientRect
if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function () {
    return new DOMRect(10, 10, 100, 20)
  }
}

function mockSelectionInContainer(container: HTMLElement, text: string) {
  // Find the first text node that contains our target text
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
  let node: Node | null
  while ((node = walker.nextNode())) {
    const idx = (node as Text).data.indexOf(text)
    if (idx >= 0) {
      const range = document.createRange()
      range.setStart(node, idx)
      range.setEnd(node, idx + text.length)
      const sel = window.getSelection()!
      sel.removeAllRanges()
      sel.addRange(range)
      return
    }
  }
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
function Providers({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const defaultProps = {
  filePath: '/tmp/test.md',
  content: '# Hello\n\nThis is a test paragraph with some text.',
  onContentChange: vi.fn(),
  onSave: vi.fn().mockResolvedValue(undefined),
  onClose: vi.fn(),
  onSubmitComments: vi.fn(),
}

describe('MarkdownPanel comment/copy flow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    writeText.mockClear()
    defaultProps.onSubmitComments.mockClear()
    defaultProps.onClose.mockClear()
    localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
  })
  afterEach(() => {
    vi.useRealTimers()
    window.getSelection()?.removeAllRanges()
  })

  it('shows Comment and Copy buttons when text is selected', () => {
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('only shows Copy button when onSubmitComments is not provided', () => {
    render(<Providers><MarkdownPanel {...defaultProps} onSubmitComments={undefined} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Comment' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('copies text to clipboard when Copy is clicked', () => {
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('test paragraph')
  })

  it('shows comment popover when Comment is clicked', () => {
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })
    act(() => { vi.advanceTimersByTime(60) })

    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    expect(screen.getByPlaceholderText('Write a comment…')).toBeInTheDocument()
  })

  it('adds comment and shows pending list after submitting comment text', async () => {
    vi.useRealTimers()
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Write a comment…')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByPlaceholderText('Write a comment…'), { target: { value: 'Fix this section' } })
    fireEvent.keyDown(screen.getByPlaceholderText('Write a comment…'), { key: 'Enter' })
    await waitFor(() => {
      expect(screen.getByText('1 comment pending')).toBeInTheDocument()
    })
  })

  it('submits all comments to chat when Submit All is clicked', async () => {
    vi.useRealTimers()
    const user = userEvent.setup()
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    const preview = document.querySelector('.msg-content')!
    mockSelectionInContainer(preview as HTMLElement, 'test paragraph')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Write a comment…')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByPlaceholderText('Write a comment…'), { target: { value: 'Needs revision' } })
    fireEvent.keyDown(screen.getByPlaceholderText('Write a comment…'), { key: 'Enter' })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /submit all/i })).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: /submit all/i }))
    expect(defaultProps.onSubmitComments).toHaveBeenCalledWith(
      expect.stringContaining('test paragraph')
    )
  })

  it('dismisses comment hint when Got it is clicked', () => {
    localStorage.removeItem('kirocrew:comment-hint-dismissed')
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    expect(screen.getByText('Got it')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Got it'))
    expect(screen.queryByText('Got it')).not.toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:comment-hint-dismissed')).toBe('1')
  })

  it('does not show selection toolbar in edit mode', () => {
    render(<Providers><MarkdownPanel {...defaultProps} /></Providers>)
    fireEvent.click(screen.getByText('Edit'))
    // Attempt to trigger toolbar — preview container won't exist in edit mode
    fireEvent.mouseUp(document, { clientX: 100, clientY: 80 })
    act(() => { vi.advanceTimersByTime(60) })
    expect(screen.queryByRole('button', { name: 'Comment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })
})
