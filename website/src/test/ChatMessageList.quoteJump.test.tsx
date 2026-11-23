/**
 * `onJumpToQuote` is a host capability threaded from ChatMessageList through the
 * renderer context to the sent quote card: absent, the card is a plain card;
 * present, it is a button handing back the quote. The host's JumpRevealContext
 * rings the one row its jump landed on.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../types'
import ChatMessageList from '../app-sdk/ChatMessageList'
import JumpRevealContext, { NO_JUMP_REVEAL } from '../hooks/JumpRevealContext'
import type { MessageRenderer } from '../app-sdk/messageRenderers'

vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))

const QUOTE = { role: 'assistant' as const, text: 'the excerpt', ts: 't1', mid: 'm1' }
const revealing = (ts: string, mid: string | null, list: React.ReactNode) => <JumpRevealContext.Provider value={{ ...NO_JUMP_REVEAL, ts, mid }}>{list}</JumpRevealContext.Provider>
const MESSAGES: ChatMessage[] = [
  { role: 'assistant', content: 'the quoted reply', cls: '', ts: 't1', meta: { mid: 'm1' } },
  { role: 'user', content: 'remind the team', cls: '', ts: 't2', meta: { mid: 'm2', quote: QUOTE } },
]

describe('ChatMessageList onJumpToQuote', () => {
  it('draws the sent quote card as a plain card when the host offers no jump', () => {
    render(<ChatMessageList messages={MESSAGES} running={false} />)
    expect(screen.getByTestId('quote-card-sent').tagName).toBe('DIV')
    expect(document.querySelector('.animate-msg-highlight')).toBeNull()
  })

  it('makes the card a button that hands the host its quote', () => {
    const onJumpToQuote = vi.fn()
    render(<ChatMessageList messages={MESSAGES} running={false} onJumpToQuote={onJumpToQuote} />)
    const card = screen.getByTestId('quote-card-sent')
    expect(card.tagName).toBe('BUTTON')
    fireEvent.click(card)
    expect(onJumpToQuote).toHaveBeenCalledWith(QUOTE)
  })

  it('rings only the row whose ts the host names', () => {
    render(revealing('t1', null, <ChatMessageList messages={MESSAGES} running={false} />))
    expect(screen.getByText('the quoted reply').closest('.animate-msg-highlight')).not.toBeNull()
    expect(screen.getByText('remind the team').closest('.animate-msg-highlight')).toBeNull()
  })

  it('marks only the row of a shared ts whose mid the host names', () => {
    const messages: ChatMessage[] = [
      { role: 'assistant', content: 'an earlier reply', cls: '', ts: 't1', meta: { mid: 'm1' } },
      { role: 'assistant', content: 'the quoted reply', cls: '', ts: 't1', meta: { mid: 'm1b' } },
    ]
    render(revealing('t1', 'm1b', <ChatMessageList messages={messages} running={false} />))
    const quoted = screen.getByText('the quoted reply')
    expect(quoted.closest('.animate-msg-highlight')).not.toBeNull()
    expect(quoted.closest('[data-jump-target="t1"]')).not.toBeNull()
    const earlier = screen.getByText('an earlier reply')
    expect(earlier.closest('.animate-msg-highlight')).toBeNull()
    expect(earlier.closest('[data-jump-target]')).toBeNull()
  })

  it('rings a landed row a renderer draws through ctx.row', () => {
    const viaRow: MessageRenderer[] = [{ id: 'assistant', roles: ['assistant'], render: (m, ctx) => ctx.row(<span>{m.content}</span>, true) }]
    const messages: ChatMessage[] = [
      { role: 'assistant', content: 'the quoted reply', cls: '', ts: 't1', meta: { mid: 'm1' } },
      { role: 'assistant', content: 'a later reply', cls: '', ts: 't3', meta: { mid: 'm3' } },
    ]
    render(revealing('t1', null, <ChatMessageList messages={messages} running={false} renderers={viaRow} />))
    expect(screen.getByText('the quoted reply').closest('.animate-msg-highlight')).not.toBeNull()
    expect(screen.getByText('a later reply').closest('.animate-msg-highlight')).toBeNull()
  })
})
