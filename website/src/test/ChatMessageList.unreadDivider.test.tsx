// Feature: the "N new messages" divider above the first unseen message.
//
// ChatMessageList draws the divider INSIDE the first unseen message's own
// output (ahead of it), so a turn row holding both seen and unseen messages is
// split at the right message and the row's measured height carries the line.
// The host decides whether a run exists; without one the list is unchanged.
import { render, screen } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { describe, it, expect, vi } from 'vitest'
import type { ChatMessage } from '../types'
import type { TurnItem } from '../pages/chat/types'

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string }) => <div data-testid="assistant-message">{props.content}</div>,
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string }) => <div data-testid="user-message">{props.content}</div>,
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))
vi.mock('../pages/chat/TurnBlock', () => ({
  default: ({ turn, renderItem }: {
    turn: { items: TurnItem[] }; renderItem: (item: TurnItem, i: number) => ReactNode
  }) => (
    <div data-testid="turn-block">
      {turn.items.map((item: TurnItem, i: number) => <div key={i}>{renderItem(item, i)}</div>)}
    </div>
  ),
}))

import ChatMessageList from '../app-sdk/ChatMessageList'
import { UNREAD_DIVIDER_ATTR } from '../app-sdk/NewMessagesDivider'

const msg = (role: string, content: string, ts: string): ChatMessage => ({ role, content, cls: '', ts })

// One user prompt, then a run of crewmate replies: the replies group into ONE
// turn row, which is exactly the shape a crewmate's unattended run takes.
const MESSAGES = [
  msg('user', 'status?', '2026-10-23T09:00:00Z'),
  msg('assistant', 'seen reply', '2026-10-23T09:01:00Z'),
  msg('assistant', 'first new', '2026-10-23T09:02:00Z'),
  msg('assistant', 'second new', '2026-10-23T09:03:00Z'),
  msg('assistant', 'third new', '2026-10-23T09:04:00Z'),
]

describe('ChatMessageList unread divider', () => {
  it('draws the divider directly above the first unseen message, with the count', () => {
    const { container } = render(
      <ChatMessageList messages={MESSAGES} running={false} unreadRun={{ startId: '2026-10-23T09:02:00Z', count: 3 }} />,
    )
    const divider = container.querySelector(`[${UNREAD_DIVIDER_ATTR}]`)
    expect(divider).not.toBeNull()
    expect(divider).toHaveAttribute('role', 'separator')
    expect(divider).toHaveTextContent('3 new messages')
    // The element right after the divider in document order is the first new
    // message; the seen reply sits before it.
    const texts = Array.from(container.querySelectorAll(`[${UNREAD_DIVIDER_ATTR}], [data-testid="assistant-message"]`))
      .map((el) => (el.hasAttribute(UNREAD_DIVIDER_ATTR) ? '<divider>' : el.textContent))
    expect(texts).toEqual(['seen reply', '<divider>', 'first new', 'second new', 'third new'])
  })

  it('reads singular for one message', () => {
    render(
      <ChatMessageList messages={MESSAGES.slice(0, 3)} running={false} unreadRun={{ startId: '2026-10-23T09:02:00Z', count: 1 }} />,
    )
    expect(screen.getByRole('separator')).toHaveTextContent('1 new message')
  })

  it('draws nothing without a run, and nothing for a start no row carries', () => {
    const { container, rerender } = render(<ChatMessageList messages={MESSAGES} running={false} />)
    expect(container.querySelector(`[${UNREAD_DIVIDER_ATTR}]`)).toBeNull()
    rerender(<ChatMessageList messages={MESSAGES} running={false} unreadRun={{ startId: 'not-a-row', count: 2 }} />)
    expect(container.querySelector(`[${UNREAD_DIVIDER_ATTR}]`)).toBeNull()
    expect(screen.getAllByTestId('assistant-message')).toHaveLength(4)
  })
})
