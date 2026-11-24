/**
 * Quote a whole user message: the row's seat + Copy + More shape once Quote is offered
 * (Edit & Resend follows Copy while the message can be edited),
 * the right-click / long-press menu, and the card a row carrying `meta.quote`
 * draws in place of its `>` block.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'
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

  it('an editable message shows Quote, Copy, Edit & Resend and More; Quote and Edit both work from the row', async () => {
    const onQuote = vi.fn()
    render(full(onQuote))
    expect(rowLabels()).toEqual(['Quote message', 'Copy', 'Edit & Resend', 'More actions'])
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledTimes(1)
    await act(async () => { fireEvent.click(screen.getByLabelText('Copy')) })
    expect(copyToClipboard).toHaveBeenCalledWith('hi')
    fireEvent.click(screen.getByTestId('edit-message'))
    expect(screen.getByRole('textbox')).toHaveValue('hi')
  })

  it('More then lists only what the row does not show: Copy link and Pin, no second Quote or Edit', () => {
    render(full(() => {}))
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Copy link to message', 'Pin message'])
  })

  it('a message that cannot be edited keeps Quote + Copy + More, and More does not list Quote again', async () => {
    const onQuote = vi.fn()
    render(full(onQuote, { canEdit: false }))
    expect(rowLabels()).toEqual(['Quote message', 'Copy', 'More actions'])
    fireEvent.click(screen.getByTestId('quote-message'))
    expect(onQuote).toHaveBeenCalledTimes(1)
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Copy link to message', 'Pin message'])
  })

  it('with Quote in the seat and nothing else to offer, the row shows no More', () => {
    render(<UserMessage content="hi" renderContent={renderContent} onQuoteMessage={() => {}} />)
    expect(screen.getByTestId('quote-message')).toBeInTheDocument()
    expect(screen.queryByTestId('user-more-actions')).not.toBeInTheDocument()
  })

  it('beside Reply in thread, Reply takes the seat, Copy follows, and Quote and Edit are in More', () => {
    const onQuote = vi.fn()
    render(full(onQuote, { onReplyInThread: () => {} }))
    expect(rowLabels()).toEqual(['Reply in thread', 'Copy', 'More actions'])
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Quote message', 'Copy link to message', 'Pin message', 'Edit & Resend'])
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
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Copy link to message', 'Pin message'])
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
  it('an editable bare carried quote reads Copy + Edit & Resend + More, with Quote nowhere', () => {
    const carried = { role: 'assistant' as const, text: 'older reply', ts: 't0' }
    render(<UserMessage content={prependQuote('', carried)} meta={{ quote: carried }} renderContent={renderContent} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} canEdit onEditResend={() => {}} onQuoteMessage={() => {}} />)
    const row = Array.from(screen.getByTestId('user-more-actions').parentElement!.querySelectorAll(':scope > button')).map(b => b.getAttribute('aria-label'))
    expect(row).toEqual(['Copy', 'Edit & Resend', 'More actions'])
    openMore()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Copy link to message', 'Pin message'])
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

  it('Copy copies the whole content, block included', async () => {
    render(<UserMessage content={content} meta={{ quote }} renderContent={renderContent} />)
    await act(async () => { fireEvent.click(screen.getByLabelText('Copy')) })
    expect(copyToClipboard).toHaveBeenCalledWith(content)
  })
})

describe('the sent quote card renders its quote as markdown, clamped', () => {
  const MD = [
    'We can drop the question.',
    '',
    '| Server | Now |',
    '|---|---|',
    '| builder | Asks you |',
    '',
    '- Only declared facts count.',
    '- It does not guess.',
    '',
    '<mcwidget title="Board"><div id="w">live</div></mcwidget>',
  ].join('\n')
  const quote = { role: 'assistant' as const, text: MD, ts: '2026-09-29T09:12:00Z', mid: 'm9' }
  // jsdom lays nothing out: give every element the scroll height a tall or a
  // short quote would have, so the card's overflow measure has something to read.
  const layOut = (px: number) => {
    const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get: () => px })
    // The real getter lives on Element.prototype, so with no own descriptor the
    // restore is a delete, which exposes it again.
    return () => { if (desc) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', desc); else Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight') }
  }

  it('draws the table and the list as markdown, not raw pipes or joined bullets, and no widget', () => {
    render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} />)
    const card = screen.getByTestId('quote-card-sent')
    expect(card.querySelector('table')).not.toBeNull()
    expect(card.querySelectorAll('li')).toHaveLength(2)
    expect(card.textContent).not.toContain('|---|')
    expect(card.textContent).not.toContain(' · ')
    expect(card.textContent).not.toContain('live')
    expect(card.querySelector('iframe')).toBeNull()
  })

  it('clamps a long quote with a fade, and Show more expands it in place', () => {
    const restore = layOut(400)
    try {
      render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} />)
      const body = screen.getByTestId('quote-card-body')
      expect(body).toHaveAttribute('data-clamped')
      expect(body.style.maskImage).toContain('linear-gradient')
      const toggle = screen.getByTestId('quote-card-toggle')
      expect(toggle).toHaveTextContent('Show more')
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      fireEvent.click(toggle)
      expect(toggle).toHaveTextContent('Show less')
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      expect(body).not.toHaveAttribute('data-clamped')
      expect(body.className).toContain('overflow-y-auto')
      fireEvent.click(toggle)
      expect(body).toHaveAttribute('data-clamped')
    } finally { restore() }
  })

  it('Show less scrolls the body back to the top, so the collapsed card starts at the quote\'s start', () => {
    const restore = layOut(400)
    try {
      render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} />)
      const body = screen.getByTestId('quote-card-body')
      let scrollTop = 0
      Object.defineProperty(body, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } })
      const toggle = screen.getByTestId('quote-card-toggle')
      fireEvent.click(toggle)
      scrollTop = 150
      fireEvent.click(toggle)
      expect(toggle).toHaveTextContent('Show more')
      expect(scrollTop).toBe(0)
    } finally { restore() }
  })

  it('a short quote shows whole, with no fade and no toggle', () => {
    const restore = layOut(40)
    try {
      render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} />)
      expect(screen.getByTestId('quote-card-body')).not.toHaveAttribute('data-clamped')
      expect(screen.queryByTestId('quote-card-toggle')).not.toBeInTheDocument()
    } finally { restore() }
  })

  it('expanded, the body keeps a bottom fade only while more sits below', () => {
    const restore = layOut(400)
    const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    const st = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')
    let clientHeight = 300
    let scrollTop = 0
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => clientHeight })
    Object.defineProperty(Element.prototype, 'scrollTop', { configurable: true, get: () => scrollTop, set: () => {} })
    try {
      render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} />)
      const body = screen.getByTestId('quote-card-body')
      fireEvent.click(screen.getByTestId('quote-card-toggle'))
      fireEvent.scroll(body)
      expect(body.className).toContain('mask-image')
      scrollTop = 100
      fireEvent.scroll(body)
      expect(body.className).not.toContain('mask-image')
      scrollTop = 0
      clientHeight = 400
      fireEvent.scroll(body)
      expect(body.className).not.toContain('mask-image')
    } finally {
      restore()
      if (ch) Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch)
      if (st) Object.defineProperty(Element.prototype, 'scrollTop', st)
    }
  })

  it('renders no live link inside the quote, only the link text', () => {
    const linked = { role: 'assistant' as const, text: 'See [the label](https://example.com/x) and https://example.com/y', ts: '2026-09-29T09:12:00Z' }
    render(<UserMessage content={prependQuote('Why?', linked)} meta={{ quote: linked }} renderContent={renderContent} />)
    const card = screen.getByTestId('quote-card-sent')
    expect(card.querySelector('a[href]')).toBeNull()
    expect(card).toHaveTextContent('the label')
    expect(card).toHaveTextContent('https://example.com/y')
  })

  const card = (text: string, role: 'user' | 'assistant' = 'assistant') => {
    const q = { role, text, ts: '2026-09-29T09:12:00Z' }
    render(<UserMessage content={prependQuote('Why?', q)} meta={{ quote: q }} renderContent={renderContent} />)
    return screen.getByTestId('quote-card-sent')
  }

  it('a widget tag named in inline code stays text, and the text after it is kept', () => {
    const c = card('Use the `<mcwidget title="x">` tag, then more text.')
    expect(c).toHaveTextContent('Use the <mcwidget title="x"> tag, then more text.')
  })

  it('a widget tag inside a fenced code block shows as code', () => {
    const c = card('Example:\n\n```html\n<mcwidget title="x"><div>hi</div></mcwidget>\n```\n\nafter')
    expect(c.querySelector('pre')?.textContent).toContain('<mcwidget title="x"><div>hi</div></mcwidget>')
    expect(c).toHaveTextContent('after')
  })

  it('an unclosed widget tag mounts no widget frame or placeholder', () => {
    const c = card('text before\n\n<mcwidget title="Cut"><div>cut')
    expect(c).toHaveTextContent('text before')
    expect(c.textContent).not.toContain('cut')
    expect(c.textContent).not.toContain('Cut')
    expect(c.querySelector('iframe')).toBeNull()
  })

  it('a quoted user message keeps its Shift+Enter line breaks; an assistant one does not', () => {
    expect(card('line one\nline two', 'user').querySelector('br')).not.toBeNull()
    cleanup()
    expect(card('line one\nline two', 'assistant').querySelector('br')).toBeNull()
  })

  it('an image in the quote never loads: no img, only its alt text', () => {
    const c = card('See ![diagram](https://example.com/a.png) and <img src="https://example.com/b.png" alt="raw shot">')
    expect(c.querySelector('img')).toBeNull()
    // Nor the click-to-load chip, one click from fetching the address.
    expect(c.querySelector('button[title^="https://example.com/"]')).toBeNull()
    expect(c.textContent).not.toContain('click to load')
    expect(c).toHaveTextContent('diagram')
    expect(c).toHaveTextContent('raw shot')
  })

  it('remote video and audio in the quote never offer to load: no media element, no load button', () => {
    const c = card('Clip <video src="https://example.com/a.mp4" title="demo clip"></video> and <audio src="https://example.com/b.mp3"></audio>')
    expect(c.querySelector('video, audio')).toBeNull()
    expect(c.querySelectorAll('button').length).toBe(c.querySelectorAll('[data-testid="quote-card-toggle"], [data-testid="quote-card-sent-jump"]').length)
    expect(c).toHaveTextContent('demo clip')
  })

  it('the header, not the whole card, is the jump control, so the toggle stays clickable', () => {
    const onJump = vi.fn()
    render(<UserMessage content={prependQuote('Why?', quote)} meta={{ quote }} renderContent={renderContent} onJumpToQuote={onJump} />)
    expect(screen.getByTestId('quote-card-sent').tagName).toBe('DIV')
    fireEvent.click(screen.getByTestId('quote-card-sent-jump'))
    expect(onJump).toHaveBeenCalledWith(quote)
  })
})
