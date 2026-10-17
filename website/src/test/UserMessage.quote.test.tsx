/**
 * Quote a whole user message: the row's seat + Copy + More shape once Quote is offered,
 * the right-click / long-press menu, and the card a row carrying `meta.quote`
 * draws in place of its `>` block.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import { prependQuote } from '../chat-core/composer/messageQuote'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(true) }))
import { copyToClipboard } from '../utils/clipboard'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

const renderContent = (content: string) => <span data-testid="content">{content}</span>
const openMore = () => fireEvent.pointerDown(screen.getByTestId('user-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })

describe('UserMessage action row without Quote', () => {
  it('keeps the shipped inline row: Copy, Copy link, Pin, Edit and no More menu', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} canEdit onEditResend={() => {}} />)
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
    expect(screen.getByLabelText('Copy link to message')).toBeInTheDocument()
    expect(screen.getByLabelText('Pin message')).toBeInTheDocument()
    expect(screen.getByLabelText('Edit & Resend')).toBeInTheDocument()
    expect(screen.queryByTestId('user-more-actions')).not.toBeInTheDocument()
    expect(screen.queryByTestId('quote-message')).not.toBeInTheDocument()
  })
})

describe('UserMessage action row with Quote offered', () => {
  const full = (onQuoteMessage: () => void, extra: Record<string, unknown> = {}) => (
    <UserMessage content="hi" renderContent={renderContent} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} canEdit onEditResend={() => {}} onQuoteMessage={onQuoteMessage} {...extra} />
  )

  const rowLabels = () => Array.from(screen.getByTestId('user-more-actions').parentElement!.querySelectorAll(':scope > button')).map(b => b.getAttribute('aria-label'))

  it('shows Quote, Copy and More; Quote and Copy both work from the row', () => {
    const onQuote = vi.fn()
    render(full(onQuote))
    expect(rowLabels()).toEqual(['Quote message', 'Copy', 'More actions'])
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByLabelText('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('hi')
  })

  it('More lists Quote first (matching the bubble menu), then Copy link, Pin, Edit; Quote fires the host callback', () => {
    const onQuote = vi.fn()
    render(full(onQuote))
    openMore()
    const items = screen.getAllByRole('menuitem').map(i => i.textContent)
    expect(items).toEqual(['Quote message', 'Copy link to message', 'Pin message', 'Edit & Resend'])
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledTimes(1)
  })

  it('beside Reply in thread, Reply takes the seat, Copy follows, and Quote is in More', () => {
    const onQuote = vi.fn()
    render(full(onQuote, { onReplyInThread: () => {} }))
    expect(rowLabels()).toEqual(['Reply in thread', 'Copy', 'More actions'])
    openMore()
    expect(screen.getAllByRole('menuitem')[0]).toHaveTextContent('Quote message')
    fireEvent.click(screen.getByTestId('quote-message-menu-item'))
    expect(onQuote).toHaveBeenCalledTimes(1)
  })
})

describe('UserMessage menus while the row is the pinned stand-in', () => {
  it('withhold Edit & resend (the editor would mount inside a visibility:hidden row)', () => {
    render(
      <div data-pinned-standin="folding">
        <UserMessage content="hi" renderContent={renderContent} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} canEdit onEditResend={() => {}} onQuoteMessage={() => {}} />
      </div>,
    )
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy link to message', 'Pin message'])
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    fireEvent.contextMenu(screen.getByTestId('content'))
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy text', 'Copy link to message', 'Pin message'])
  })
})

describe('UserMessage context menu', () => {
  it('is absent when Quote is not offered', () => {
    render(<UserMessage content="hi" renderContent={renderContent} />)
    fireEvent.contextMenu(screen.getByTestId('content'))
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
  })

  it('opens on right-click with Quote first, then the row actions', () => {
    const onQuote = vi.fn()
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} canEdit onEditResend={() => {}} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('content'))
    const items = screen.getAllByRole('menuitem').map(i => i.textContent)
    expect(items).toEqual(['Quote message', 'Copy text', 'Copy link to message', 'Pin message', 'Edit & Resend'])
    fireEvent.click(screen.getByTestId('message-context-quote'))
    expect(onQuote).toHaveBeenCalledWith('hi')
  })
})

describe('UserMessage quotes what the bubble shows', () => {
  it('expands collapsed pastes from meta.pastes before quoting', () => {
    const onQuote = vi.fn()
    const block = { id: 'p1', seq: 1, lines: 3, content: 'line a\nline b\nline c' }
    render(<UserMessage content="see [ Paste #1 · 3 lines ] please" meta={{ pastes: [block] }} renderContent={renderContent} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('content'))
    fireEvent.click(screen.getByTestId('message-context-quote'))
    expect(onQuote).toHaveBeenCalledWith('see line a\nline b\nline c please')
  })
  it('ignores a malformed meta.pastes entry', () => {
    const onQuote = vi.fn()
    render(<UserMessage content="see [ Paste #1 · 3 lines ]" meta={{ pastes: [{ id: 'x' }, null, 'junk'] }} renderContent={renderContent} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('content'))
    fireEvent.click(screen.getByTestId('message-context-quote'))
    expect(onQuote).toHaveBeenCalledWith('see [ Paste #1 · 3 lines ]')
  })
  it('quoting a row that itself carries a quote quotes only its own words, never the old block', () => {
    const onQuote = vi.fn()
    const carried = { role: 'assistant' as const, text: 'older reply', ts: 't0' }
    render(<UserMessage content={prependQuote('my follow-up', carried)} meta={{ quote: carried }} renderContent={renderContent} onQuoteMessage={onQuote} />)
    fireEvent.contextMenu(screen.getByTestId('content'))
    fireEvent.click(screen.getByTestId('message-context-quote'))
    expect(onQuote).toHaveBeenCalledWith('my follow-up')
  })
  it('a row that is ONLY a carried quote withholds Quote from the row and both menus (nothing of its own to quote)', () => {
    const carried = { role: 'assistant' as const, text: 'older reply', ts: 't0' }
    render(<UserMessage content={prependQuote('', carried)} meta={{ quote: carried }} renderContent={renderContent} messageTs="t1" slotKey="chat-1" onQuoteMessage={() => {}} />)
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
    openMore()
    expect(screen.queryByTestId('quote-message-menu-item')).not.toBeInTheDocument()
    fireEvent.contextMenu(screen.getByTestId('quote-card-sent'))
    expect(screen.queryByRole('menuitem', { name: 'Quote message' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Copy text' })).toBeInTheDocument()
  })
  it('a bare carried quote with nothing else to offer shows Copy and no empty More', () => {
    const carried = { role: 'assistant' as const, text: 'older reply', ts: 't0' }
    render(<UserMessage content={prependQuote('', carried)} meta={{ quote: carried }} renderContent={renderContent} onQuoteMessage={() => {}} />)
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
    expect(screen.queryByTestId('user-more-actions')).not.toBeInTheDocument()
  })
  it('Edit on a row carrying a quote opens on the shown body, and the block goes back on the head when resent', () => {
    const quote = { role: 'assistant' as const, text: 'older reply', ts: 't0' }
    const onEditResend = vi.fn()
    render(<UserMessage content={prependQuote('my follow-up', quote)} meta={{ quote }} renderContent={renderContent} messageTs="t1" slotKey="chat-1" canEdit onEditResend={onEditResend} messageIndex={3} />)
    fireEvent.click(screen.getByLabelText('Edit & Resend'))
    const box = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(box.value).toBe('my follow-up')
    fireEvent.change(box, { target: { value: 'my edited follow-up' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onEditResend).toHaveBeenCalledWith(3, 't1', prependQuote('my edited follow-up', quote))
  })
  it('a ts-less carried quote draws a plain card, not a jump control', () => {
    const onJump = vi.fn()
    render(<UserMessage content="x" meta={{ quote: { role: 'assistant', text: 'q' } }} renderContent={renderContent} onJumpToQuote={onJump} />)
    expect(screen.queryByRole('button', { name: 'Jump to the quoted message' })).not.toBeInTheDocument()
  })
  it('a refused clipboard write shows an ErrorNotice under the row', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    render(<UserMessage content="hi" renderContent={renderContent} />)
    fireEvent.click(screen.getByLabelText('Copy'))
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('alert')).toHaveTextContent('Copy failed. Select the text and copy it manually.')
  })
})

describe('a row carrying meta.quote', () => {
  const quote = { role: 'assistant' as const, text: 'Staged files now show as chips.', ts: '2026-09-29T09:12:00Z', mid: 'm9' }
  const content = prependQuote('Why chips?', quote)

  it('draws the card and renders the body without the block', () => {
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} />)
    const card = screen.getByTestId('quote-card-sent')
    expect(card).toHaveTextContent('Kiro Crew')
    expect(card).toHaveTextContent('Staged files now show as chips.')
    expect(screen.getByTestId('content')).toHaveTextContent('Why chips?')
    expect(screen.getByTestId('content').textContent).not.toContain('>')
  })

  it('the card is the jump control when the host can jump, and hands over the record', () => {
    const onJump = vi.fn()
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} onJumpToQuote={onJump} />)
    fireEvent.click(screen.getByRole('button', { name: 'Jump to the quoted message' }))
    expect(onJump).toHaveBeenCalledWith(quote)
  })

  it('is a plain box, not a button, when the host cannot jump', () => {
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} />)
    expect(screen.queryByRole('button', { name: 'Jump to the quoted message' })).not.toBeInTheDocument()
    expect(screen.getByTestId('quote-card-sent').tagName).toBe('DIV')
  })

  it('keeps the card AND the full text when the content does not begin with the block', () => {
    render(<UserMessage content="edited by hand" meta={{ quote }} renderContent={renderContent} />)
    expect(screen.getByTestId('quote-card-sent')).toBeInTheDocument()
    expect(screen.getByTestId('content')).toHaveTextContent('edited by hand')
  })

  it('a malformed meta.quote draws no card and leaves the content alone', () => {
    render(<UserMessage content={content} meta={{ quote: { role: 'system', text: 'x' } }} renderContent={renderContent} />)
    expect(screen.queryByTestId('quote-card-sent')).not.toBeInTheDocument()
    expect(screen.getByTestId('content').textContent).toBe(content)
  })

  it('the card takes no intrinsic width, so the bubble stays sized by the message text (with a floor)', () => {
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} />)
    const card = screen.getByTestId('quote-card-sent')
    expect(card.className).toMatch(/\bw-0\b/)
    expect(card.className).toMatch(/\bmin-w-full\b/)
    expect(card.className).not.toMatch(/(^|\s)w-full(\s|$)/)
    const bubble = card.closest('.message-bubble')!
    expect(bubble.className).toContain('w-fit')
    expect(bubble.className).toContain('max-w-full')
    expect(bubble.className).toContain('min-w-64')
  })

  it('a row without a quote gets no width floor', () => {
    render(<UserMessage content="hi" renderContent={renderContent} />)
    expect(screen.getByTestId('content').closest('.message-bubble')!.className).not.toContain('min-w-64')
  })

  it('Copy copies the whole content, block included', () => {
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} />)
    fireEvent.click(screen.getByLabelText('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith(content)
  })
})
