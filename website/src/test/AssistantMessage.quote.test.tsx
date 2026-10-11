/**
 * Quote a whole reply: the row reads seat + Copy + More on every reply shape
 * (the seat is Quote, or Regenerate / Fork / Reply in thread when one holds
 * it), and the bubble's right-click menu (pointer devices only).
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(true) }))
import { copySessionLink } from '../utils/shareUrl'
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))
import { copyToClipboard } from '../utils/clipboard'

beforeEach(() => { vi.useFakeTimers(); vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true) })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

const LONG = 'A completed reply that is comfortably longer than twenty characters.'
const openMore = () => fireEvent.pointerDown(screen.getByTestId('assistant-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })

describe('AssistantMessage row with Quote offered', () => {
  const rowLabels = () => Array.from(screen.getByTestId('assistant-more-actions').parentElement!.querySelectorAll(':scope > button')).map(b => b.getAttribute('aria-label'))

  it('seats Quote, keeps Copy right after it, and folds Link, Pin and Raw into More', () => {
    const onQuote = vi.fn()
    const { rerender } = render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} />)
    const before = screen.getAllByRole('button').length
    rerender(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onQuoteMessage={onQuote} />)
    expect(screen.getAllByRole('button').length).toBeLessThanOrEqual(before)
    // The everyday row is exactly Quote + Copy + More.
    expect(rowLabels()).toEqual(['Quote message', 'Copy', 'More actions'])
    expect(screen.queryByTestId('toggle-raw-view')).not.toBeInTheDocument()
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy as rich textKeeps formatting in email and documents', 'Copy link to message', 'Pin message', 'Show raw markdown'])
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledTimes(1)
  })

  it('on the main chat with Fork wired (the old menu context) Copy stays in the row after the Quote seat', () => {
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onFork={() => {}} forkIndex={1} forkMessageId="m1" onQuoteMessage={() => {}} />)
    expect(rowLabels()).toEqual(['Quote message', 'Copy', 'More actions'])
    openMore()
    expect(screen.queryByTestId('copy-message-menu-item')).not.toBeInTheDocument()
  })

  it('on the newest reply Regenerate takes the seat and Copy is still a visible row button right after it', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onSpeak={() => {}} onRegenerate={() => {}} onQuoteMessage={onQuote} />)
    expect(rowLabels()).toEqual(['Regenerate response', 'Copy', 'More actions'])
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('Reply text long enough for the raw toggle.')
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy as rich textKeeps formatting in email and documents', 'Copy link to message', 'Pin message', 'Show raw markdown', 'Read aloud'])
  })

  it('in a loaded window Fork takes the seat and Copy follows it', () => {
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} onSpeak={() => {}} onFork={() => {}} forkIndex={1} onQuoteMessage={() => {}} />)
    const row = Array.from(screen.getByTestId('assistant-more-actions').parentElement!.querySelectorAll(':scope > button'))
    expect(row[0]).toBe(screen.getByTestId('fork-from-here'))
    expect(rowLabels().slice(1)).toEqual(['Copy', 'More actions'])
  })

  it('on the newest reply of a loaded window Regenerate holds the seat, Copy is second, Fork follows', () => {
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} onRegenerate={() => {}} onFork={() => {}} forkIndex={1} onQuoteMessage={() => {}} />)
    const row = Array.from(screen.getByTestId('assistant-more-actions').parentElement!.querySelectorAll(':scope > button'))
    expect(rowLabels().slice(0, 2)).toEqual(['Regenerate response', 'Copy'])
    expect(row[2]).toBe(screen.getByTestId('fork-from-here'))
    expect(rowLabels()[3]).toBe('More actions')
  })

  it('beside Reply in thread (a crewmate chat) the row is Reply, Copy, More and Quote is in More', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} onReplyInThread={() => {}} onQuoteMessage={onQuote} />)
    expect(rowLabels()).toEqual(['Reply in thread', 'Copy', 'More actions'])
    openMore()
    expect(screen.getAllByRole('menuitem')[0]).toHaveTextContent('Quote message')
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledTimes(1)
  })

  it('without Quote the shipped row is unchanged: inline Copy, no More menu', () => {
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} />)
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
    expect(screen.queryByTestId('assistant-more-actions')).not.toBeInTheDocument()
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
  })

  it('a refused inline Copy link raises the row ErrorNotice, like the menu copy of the same action', async () => {
    // Without Quote the link action stays in the row; a refused write must not
    // end as a 1.5 s icon flash alone (errors-use-error-notice).
    vi.mocked(copySessionLink).mockResolvedValueOnce(false)
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" />)
    await act(async () => { fireEvent.click(screen.getByTitle('Copy link to message')); await Promise.resolve() })
    expect(screen.getByText('Copy failed. Select the text and copy it manually.')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByText('Copy failed. Select the text and copy it manually.')).not.toBeInTheDocument()
    vi.mocked(copySessionLink).mockRejectedValueOnce(new Error('denied'))
    await act(async () => { fireEvent.click(screen.getByTitle('Copy link to message')); await Promise.resolve() })
    expect(screen.getByText('Copy failed. Select the text and copy it manually.')).toBeInTheDocument()
  })
})

describe('AssistantMessage quotes what the reader sees', () => {
  it('hands the displayed variant, not the stored default, to onQuoteMessage', () => {
    const onQuote = vi.fn()
    const variants = [{ content: 'first answer, long enough to keep' }, { content: 'second answer, also long enough' }]
    render(<AssistantMessage content="second answer, also long enough" isStreaming={false} slotRunning={false} variants={variants} variantIdx={1} onQuoteMessage={onQuote} />)
    // Browse locally (no onSwitchVariant) back to the first variant.
    fireEvent.click(screen.getByLabelText('Previous version'))
    openMore()
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledWith('first answer, long enough to keep')
  })

  it('offers no Quote at all on a reply whose text parsing consumed entirely (options-only)', () => {
    render(<AssistantMessage content={'[OPTIONS: a | b]'} isStreaming={false} slotRunning={false} onQuoteMessage={() => {}} />)
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
    expect(screen.queryByTestId('quote-message-menu-item')).not.toBeInTheDocument()
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.queryByRole('menuitem', { name: 'Quote message' })).not.toBeInTheDocument()
  })

  it('strips the keep-visible marker from the quoted text, as Copy does', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={'Shown reply.\n\n<!-- keep-visible -->'} isStreaming={false} onQuoteMessage={onQuote} />)
    openMore()
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledWith('Shown reply.')
  })

  it('strips the steer ack marker from the quoted text', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={'[STEERING steer-1: noted]\nThe real answer that is long enough.'} isStreaming={false} slotRunning={false} onQuoteMessage={onQuote} />)
    openMore()
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote.mock.calls[0][0]).not.toContain('STEERING')
    expect(onQuote.mock.calls[0][0]).toContain('The real answer')
  })
})

describe('AssistantMessage context menu', () => {
  it('without Quote, is absent while the reply streams or has no footer', () => {
    const { rerender } = render(<AssistantMessage content={LONG} isStreaming slotRunning />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
    rerender(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} showFooter={false} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
  })

  it('opens on right-click: Quote first, then both copy formats, Copy link, Pin', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy as MarkdownKeeps ** and # symbols, for Markdown editors and chat apps', 'Copy as rich textKeeps formatting in email and documents', 'Copy link to message', 'Pin message', 'Show raw markdown'])
    fireEvent.click(screen.getByTestId('message-context-copy'))
    expect(copyToClipboard).toHaveBeenCalledWith(LONG)
  })
})

/**
 * On a touch device the bubble belongs to the platform's own long-press
 * selection: Radix's 700 ms long-press menu (which opened over the fresh
 * selection and collapsed it) is not drawn, and the selection actions dock
 * above the composer instead of floating over the platform's handles.
 */
describe('AssistantMessage on a touch device', () => {
  beforeEach(() => {
    vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
      matches: query === '(pointer: coarse)' || query === '(hover: none)',
      media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }) as MediaQueryList)
    if (!Range.prototype.getBoundingClientRect) {
      Range.prototype.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
    }
  })
  afterEach(() => { vi.mocked(window.matchMedia).mockRestore(); window.getSelection()?.removeAllRanges() })

  it('draws no bubble menu and leaves the platform callout enabled', () => {
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onQuoteMessage={() => {}} />)
    const bubble = screen.getByTestId('message-bubble')
    // The callout opt-out is the trigger's own inline style, so a bare bubble
    // carries no style attribute from it at all.
    expect(bubble.getAttribute('style') ?? '').not.toMatch(/touch-callout/i)
    fireEvent.contextMenu(bubble)
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
  })

  it('docks Quote / Ask above the composer instead of floating at a touch selection, and leaves Copy to the platform', () => {
    render(
      <>
        <AssistantMessage content={LONG} isStreaming={false} slotRunning={false} onQuote={() => {}} onAsk={() => {}} />
        <div data-testid="composer-area" className="input-area" />
      </>
    )
    const composer = screen.getByTestId('composer-area')
    composer.getBoundingClientRect = () => new DOMRect(0, 700, 400, 80)
    const md = screen.getByTestId('md')
    const range = document.createRange()
    range.selectNodeContents(md)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    act(() => { document.dispatchEvent(new Event('selectionchange')) })
    act(() => { vi.advanceTimersByTime(400) })
    const dock = screen.getByTestId('selection-dock')
    expect(screen.getByRole('button', { name: 'Ask about this' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
    // Copy is the platform callout's job on touch.
    expect(dock.querySelector('button[aria-label="Copy"]')).toBeNull()
    // Bottom edge above the composer's top (700), not hung off the selection rect (y 10..30).
    const box = dock.parentElement as HTMLElement
    expect(parseFloat(box.style.top)).toBeGreaterThan(30)
    expect(parseFloat(box.style.top)).toBeLessThan(700)
  })
})
