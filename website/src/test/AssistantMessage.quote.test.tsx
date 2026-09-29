/**
 * Quote a whole reply: the row seat Quote takes (and what moves into More to
 * pay for it), the crewmate-chat case beside Reply, and the bubble's
 * right-click / long-press menu.
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
  it('seats Quote in the row and moves Copy + the raw toggle into More, so the row does not grow', () => {
    const onQuote = vi.fn()
    const { rerender } = render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} />)
    const before = screen.getAllByRole('button').length
    rerender(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onQuoteMessage={onQuote} />)
    // Quote in, Copy + raw toggle out, More in: one fewer peer control than before.
    expect(screen.getAllByRole('button').length).toBeLessThanOrEqual(before)
    expect(screen.getByTestId('quote-message')).toBeInTheDocument()
    expect(screen.queryByLabelText('Copy')).not.toBeInTheDocument()
    expect(screen.queryByTestId('toggle-raw-view')).not.toBeInTheDocument()
    // The everyday row is exactly Quote + More.
    const row = screen.getByTestId('quote-message').parentElement!
    expect(Array.from(row.querySelectorAll(':scope > button')).map(b => b.getAttribute('aria-label'))).toEqual(['Quote message', 'More actions'])
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledTimes(1)
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy text', 'Copy link to message', 'Pin message', 'Show raw markdown'])
  })

  it('on the main chat with Fork wired (the old menu context) Copy still folds into More beside Quote', () => {
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onFork={() => {}} forkIndex={1} forkMessageId="m1" onQuoteMessage={() => {}} />)
    expect(screen.getByTestId('quote-message')).toBeInTheDocument()
    expect(screen.queryByLabelText('Copy')).not.toBeInTheDocument()
    fireEvent.pointerDown(screen.getByTestId('assistant-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toContain('Copy text')
  })

  it('beside Regenerate (the newest reply) Quote joins More, so the row keeps two controls', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content="Reply text long enough for the raw toggle." isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onRegenerate={() => {}} onQuoteMessage={onQuote} />)
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Regenerate response')).toBeInTheDocument()
    expect(screen.queryByLabelText('Copy link to message')).not.toBeInTheDocument()
    fireEvent.pointerDown(screen.getByTestId('assistant-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy text', 'Copy link to message', 'Pin message', 'Show raw markdown'])
  })

  it('beside Reply in thread (a crewmate chat) Quote joins More instead of taking a third seat', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} onReplyInThread={() => {}} onQuoteMessage={onQuote} />)
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
    expect(screen.getByTestId('reply-in-thread')).toBeInTheDocument()
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
    fireEvent.click(screen.getByTestId('quote-message'))
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
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledWith('Shown reply.')
  })

  it('strips the steer ack marker from the quoted text', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={'[STEERING steer-1: noted]\nThe real answer that is long enough.'} isStreaming={false} slotRunning={false} onQuoteMessage={onQuote} />)
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote.mock.calls[0][0]).not.toContain('STEERING')
    expect(onQuote.mock.calls[0][0]).toContain('The real answer')
  })
})

describe('AssistantMessage context menu', () => {
  it('is absent when Quote is not offered', () => {
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
  })

  it('opens on right-click: Quote first, then Copy, Copy link, Pin', () => {
    const onQuote = vi.fn()
    render(<AssistantMessage content={LONG} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy text', 'Copy link to message', 'Pin message', 'Show raw markdown'])
    fireEvent.click(screen.getByTestId('message-context-copy'))
    expect(copyToClipboard).toHaveBeenCalledWith(LONG)
  })
})
