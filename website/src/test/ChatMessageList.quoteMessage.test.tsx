/**
 * `onQuoteMessage` is a host capability threaded from ChatMessageList through
 * the renderer context to every bubble (`quoteMessageFor`): present, each
 * final row hands its own fields back; absent, no row is offered Quote.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../types'

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string; onQuoteMessage?: () => void }) => (
    <div data-testid="assistant-message">
      {props.content}
      {props.onQuoteMessage && <button data-testid="a-quote" onClick={props.onQuoteMessage}>quote</button>}
    </div>
  ),
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string; onQuoteMessage?: () => void }) => (
    <div data-testid="user-message">
      {props.content}
      {props.onQuoteMessage && <button data-testid="u-quote" onClick={props.onQuoteMessage}>quote</button>}
    </div>
  ),
}))

import ChatMessageList from '../app-sdk/ChatMessageList'
import { quoteMessageFor, type MessageRenderContext } from '../app-sdk/messageRenderers'

const msg = (role: ChatMessage['role'], content: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content, cls: '', ts: `2026-09-29T09:${role === 'user' ? '12' : '13'}:00Z`, ...extra }) as ChatMessage

describe('ChatMessageList onQuoteMessage', () => {
  it('offers Quote on no row when the host passes nothing', () => {
    render(<ChatMessageList messages={[msg('user', 'q'), msg('assistant', 'a')]} running={false} />)
    expect(screen.queryByTestId('u-quote')).not.toBeInTheDocument()
    expect(screen.queryByTestId('a-quote')).not.toBeInTheDocument()
  })

  it('hands each row its own role, content, ts and mid', () => {
    const onQuoteMessage = vi.fn()
    render(<ChatMessageList messages={[msg('user', 'q', { meta: { mid: 'm1' } }), msg('assistant', 'a', { meta: { mid: 'm2' } })]} running={false} onQuoteMessage={onQuoteMessage} />)
    fireEvent.click(screen.getByTestId('u-quote'))
    expect(onQuoteMessage).toHaveBeenLastCalledWith('user', 'q', '2026-09-29T09:12:00Z', 'm1')
    fireEvent.click(screen.getByTestId('a-quote'))
    expect(onQuoteMessage).toHaveBeenLastCalledWith('assistant', 'a', '2026-09-29T09:13:00Z', 'm2')
  })
})

describe('quoteMessageFor', () => {
  const ctx = (onQuoteMessage?: MessageRenderContext['onQuoteMessage']) => ({ onQuoteMessage } as unknown as MessageRenderContext)

  it('is undefined while the row streams, when it is blank, or when the host offers nothing', () => {
    expect(quoteMessageFor(msg('streaming', 'partial'), ctx(vi.fn()), 'assistant')).toBeUndefined()
    expect(quoteMessageFor(msg('assistant', '   '), ctx(vi.fn()), 'assistant')).toBeUndefined()
    expect(quoteMessageFor(msg('assistant', 'a'), ctx(undefined), 'assistant')).toBeUndefined()
  })

  it('quotes the text the caller says is shown, else the row content', () => {
    const fn = vi.fn()
    const h = quoteMessageFor(msg('assistant', 'stored'), ctx(fn), 'assistant')!
    h('shown variant')
    expect(fn).toHaveBeenLastCalledWith('assistant', 'shown variant', '2026-09-29T09:13:00Z', undefined)
    h()
    expect(fn).toHaveBeenLastCalledWith('assistant', 'stored', '2026-09-29T09:13:00Z', undefined)
  })

  it('ignores a click event handed over as the first argument (user rows wire onClick directly)', () => {
    const fn = vi.fn()
    quoteMessageFor(msg('user', 'q'), ctx(fn), 'user')!({ type: 'click' } as unknown as string)
    expect(fn).toHaveBeenLastCalledWith('user', 'q', '2026-09-29T09:12:00Z', undefined)
  })

  it('drops a non-string mid', () => {
    const fn = vi.fn()
    quoteMessageFor(msg('user', 'q', { meta: { mid: 7 } }), ctx(fn), 'user')!()
    expect(fn).toHaveBeenCalledWith('user', 'q', '2026-09-29T09:12:00Z', undefined)
  })
})
