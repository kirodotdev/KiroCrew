import { fireEvent, render, screen } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import QuoteCard from '../pages/chat/QuoteCard'
import { prependQuote, quoteFromMessage } from '../chat-core/composer/messageQuote'
import { SEND_PROVENANCE_META_KEY } from '../pages/chat/sendProvenance'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))
import { copyToClipboard } from '../utils/clipboard'

const KEY = 'chat-316-1792176321'
const ENV = `[sent by session ${KEY} via session_send]`
const renderContent = (content: string) => <span data-testid="content">{content}</span>
const meta = (extra: Record<string, unknown> = {}) => ({
  mid: 'm-1',
  [SEND_PROVENANCE_META_KEY]: { from: KEY, title: 'Release conductor', via: 'session_send', mode: 'queued', ...extra },
})

describe('UserMessage: the "From <chat>" label on a row another chat sent', () => {
  it('draws the label above an unchanged bubble and hides the envelope line', () => {
    const open = vi.fn()
    render(
      <UserMessage
        content={`${ENV}\n\nrebase onto main`}
        meta={meta()}
        renderContent={renderContent}
        senderSessions={new Map([[KEY, 'Release conductor']])}
        onOpenSender={open}
      />,
    )
    const label = screen.getByTestId('send-provenance')
    expect(label.textContent).toBe('From Release conductor · queued')
    expect(screen.getByTestId('content').textContent).toBe('rebase onto main')
    expect(document.body.textContent).not.toContain('[sent by session')
    // The label precedes the bubble in the row.
    const row = label.closest('[data-role="user"]')!
    expect(row.firstElementChild).toBe(label)
    const sender = screen.getByTestId('send-provenance-sender')
    expect(sender.tagName).toBe('BUTTON')
    fireEvent.click(sender)
    expect(open).toHaveBeenCalledWith(KEY)
  })

  it('says a confirmed (consumed) peer steer interrupted the turn', () => {
    const steerMeta = { ...meta({ mode: 'steered' }), steer: true, steerState: 'consumed' }
    render(<UserMessage content={`${ENV}\n\nstop`} meta={steerMeta} renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From Release conductor · interrupted the reply')
  })

  it.each([
    ['written (no consumption echo yet)', 'written'],
    ['requeued (the turn ended without taking it)', 'requeued'],
    ['missing', undefined],
  ])('an unconfirmed peer steer, steerState %s, names the sender without claiming an interruption', (_label, steerState) => {
    const steerMeta = { ...meta({ mode: 'steered' }), steer: true, ...(steerState ? { steerState } : {}) }
    render(<UserMessage content={`${ENV}\n\nstop`} meta={steerMeta} renderContent={renderContent} slotRunning />)
    const label = screen.getByTestId('send-provenance')
    expect(label.textContent).toBe('From Release conductor')
    expect(label.getAttribute('data-mode')).toBe('')
    expect(screen.getByTestId('content').textContent).toBe('stop')
  })

  it('a confirmed peer steer shows its label once, without the duplicate steer badge', () => {
    const steerMeta = { ...meta({ mode: 'steered' }), steer: true, steerState: 'consumed' }
    render(<UserMessage content={`${ENV}\n\nstop`} meta={steerMeta} renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From Release conductor · interrupted the reply')
    expect(screen.queryByText('Steered into the running turn')).toBeNull()
  })

  it("the person's own confirmed steer keeps the steer badge", () => {
    render(<UserMessage content="stop" meta={{ mid: 'm', human: true, steer: true, steerState: 'consumed' }} renderContent={renderContent} />)
    expect(screen.getByText('Steered into the running turn')).toBeTruthy()
  })

  it('says how many chats a broadcast reached, and handles one', () => {
    const b = (count: number) => ({ mid: 'm', [SEND_PROVENANCE_META_KEY]: { from: KEY, title: 'Conductor', via: 'session_broadcast', mode: 'queued', count } })
    const bEnv = `[sent by session ${KEY} via session_broadcast]\n\nthe base moved`
    const { unmount } = render(<UserMessage content={bEnv} meta={b(3)} renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From Conductor · addressed to 3 chats')
    unmount()
    render(<UserMessage content={bEnv} meta={b(1)} renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From Conductor · addressed to 1 chat')
  })

  it('a started send carries no mode suffix', () => {
    render(<UserMessage content={`${ENV}\n\ngo`} meta={meta({ mode: 'started' })} renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From Release conductor')
  })

  it('names a closed sender by its short key, not a link', () => {
    render(
      <UserMessage
        content={`${ENV}\n\nx`}
        meta={meta()}
        renderContent={renderContent}
        senderSessions={new Map([['chat-1', 'Other']])}
        onOpenSender={vi.fn()}
      />,
    )
    const sender = screen.getByTestId('send-provenance-sender')
    expect(sender.tagName).toBe('SPAN')
    expect(sender.textContent).toBe('chat-316 (closed)')
    expect(sender.getAttribute('data-sender-state')).toBe('closed')
    expect(sender.getAttribute('title')).toBe('Closed chat: Release conductor')
  })

  it('a closed sender whose record has no title (sent before its title landed) reads as its short key', () => {
    const untitled = { mid: 'm-1', [SEND_PROVENANCE_META_KEY]: { from: KEY, via: 'session_send', mode: 'queued' } }
    render(<UserMessage content={`${ENV}\n\nx`} meta={untitled} renderContent={renderContent} senderSessions={new Map()} onOpenSender={vi.fn()} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From chat-316 (closed) · queued')
    expect(screen.getByTestId('send-provenance-sender').getAttribute('title')).toBeNull()
  })

  it('an unknown key with no recorded title falls back to its short form', () => {
    render(
      <UserMessage
        content={'[sent by session unknown via session_send]\n\nx'}
        meta={{ mid: 'm' }}
        messageTs="2026-10-01T12:00:00Z"
        renderContent={renderContent}
        senderSessions={new Map()}
        onOpenSender={vi.fn()}
      />,
    )
    expect(screen.getByTestId('send-provenance').textContent).toBe('From unknown (closed)')
    expect(screen.getByTestId('send-provenance-sender').tagName).toBe('SPAN')
  })

  it('renders the same label for a legacy row with only the text prefix', () => {
    render(<UserMessage content={`${ENV}\n\nold\nmulti-line`} meta={{ mid: 'm' }} messageTs="2026-10-01T12:00:00Z" renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From chat-316')
    expect(screen.getByTestId('content').textContent).toBe('old\nmulti-line')
  })

  it('shows a placeholder when nothing follows the envelope', () => {
    render(<UserMessage content={ENV} meta={{ mid: 'm' }} messageTs="2026-10-01T12:00:00Z" renderContent={renderContent} />)
    expect(screen.getByTestId('send-provenance')).toBeTruthy()
    expect(screen.queryByTestId('content')).toBeNull()
    expect(screen.getByText('No message text')).toBeTruthy()
  })

  it('quoting a peer message carries its sender into the outgoing turn, not just the shown body', () => {
    const onQuote = vi.fn()
    render(<UserMessage content={`${ENV}\n\nrebase onto main`} meta={meta()} renderContent={renderContent} onQuoteMessage={onQuote} />)
    fireEvent.click(screen.getByRole('button', { name: 'Quote message' }))
    expect(onQuote).toHaveBeenCalledWith(`${ENV}\n\nrebase onto main`, 'Release conductor')
    // What the next send hands the model: the envelope sits inside the quote block.
    const [shown, author] = onQuote.mock.calls[0] as [string, string]
    const q = quoteFromMessage('user', shown, undefined, 'm-1', author)!
    const outgoing = prependQuote('agreed?', q)
    expect(outgoing.startsWith(`> ${ENV}\n> \n> rebase onto main\n> `)).toBe(true)
    expect(outgoing.endsWith('\n\nagreed?')).toBe(true)
    // The card a person reads names the sender and leaves the envelope out.
    const card = render(<QuoteCard quote={q} variant="composer" />).container
    expect(card.textContent).toContain('Release conductor')
    expect(card.textContent).toContain('rebase onto main')
    expect(card.textContent).not.toContain('[sent by session')
  })

  it("quoting the person's own message is unchanged", () => {
    const onQuote = vi.fn()
    render(<UserMessage content="hello there" meta={{ mid: 'm', human: true }} renderContent={renderContent} onQuoteMessage={onQuote} />)
    fireEvent.click(screen.getByRole('button', { name: 'Quote message' }))
    expect(onQuote).toHaveBeenCalledWith('hello there')
  })

  it('copies the body without the envelope', () => {
    render(<UserMessage content={`${ENV}\n\ncopy me`} meta={meta()} renderContent={renderContent} />)
    fireEvent.click(screen.getByTitle('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('copy me')
  })

  it('gives typed text that looks like an envelope no label', () => {
    // A person's own send (the gateway marks it) and the quoted form a gateway
    // writes for an envelope found inside a body: neither is a delivery.
    const { unmount } = render(<UserMessage content={`${ENV}\n\nhi`} meta={{ mid: 'm', human: true }} renderContent={renderContent} />)
    expect(screen.queryByTestId('send-provenance')).toBeNull()
    expect(screen.getByTestId('content').textContent).toBe(`${ENV}\n\nhi`)
    unmount()
    render(<UserMessage content={'[quoted: sent by session chat-316-1792176321 via session_send]\n\nhi'} renderContent={renderContent} />)
    expect(screen.queryByTestId('send-provenance')).toBeNull()
  })

  it('an ordinary message gets no label', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.queryByTestId('send-provenance')).toBeNull()
  })
})
