/**
 * ChatMessageList's two mounting modes (chat-core P5-e): with `transcript` it
 * owns a virtualized scroller; without it, the original bare fragment.
 */
import { render, screen } from '@testing-library/react'
import React, { createRef, type ReactNode } from 'react'
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

import ChatMessageList, { type VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'

function msg(role: string, content: string, ts: string): ChatMessage {
  return { role, content, cls: '', ts }
}

const MESSAGES = [
  msg('user', 'q1', '2026-01-01T00:00:01Z'),
  msg('assistant', 'a1', '2026-01-01T00:00:02Z'),
  msg('user', 'q2', '2026-01-01T00:00:03Z'),
  msg('assistant', 'a2', '2026-01-01T00:00:04Z'),
]

describe('ChatMessageList transcript mount', () => {
  it('stays a bare fragment when the host owns the scroller', () => {
    const { container } = render(<ChatMessageList messages={MESSAGES} running={false} />)
    expect(container.querySelector('.chat-container')).toBeNull()
    expect(container.querySelectorAll('[data-display-index]')).toHaveLength(0)
    expect(screen.getAllByTestId('user-message')).toHaveLength(2)
  })

  it('owns a virtualized scroller when the host supplies the transcript wiring', () => {
    const ref = createRef<VirtualTranscriptHandle>()
    const { container } = render(
      <ChatMessageList
        ref={ref}
        messages={MESSAGES}
        running={false}
        transcript={{ sessionId: 'test:mount', aboveRows: <div data-testid="above" /> }}
      />,
    )
    const scroller = container.querySelector('.chat-container')
    expect(scroller).not.toBeNull()
    // Every display row is an indexed, measured block inside the scroller.
    const rows = scroller!.querySelectorAll('[data-display-index]')
    expect(rows.length).toBeGreaterThan(0)
    expect(scroller!.contains(screen.getByTestId('above'))).toBe(true)
    expect(screen.getAllByTestId('user-message').length + screen.getAllByTestId('assistant-message').length)
      .toBeGreaterThan(0)
    // The handle reaches the scroll control.
    expect(typeof ref.current?.scrollToBottom).toBe('function')
  })

  it('hides the pinned row by identity inside the virtualized mount', () => {
    const { container } = render(
      <ChatMessageList
        messages={MESSAGES}
        running={false}
        hiddenRow={{ ts: '2026-01-01T00:00:01Z', index: 0 }}
        transcript={{ sessionId: 'test:hidden' }}
      />,
    )
    const rows = [...container.querySelectorAll<HTMLDivElement>('[data-display-index]')]
    const hidden = rows.filter((r) => r.style.visibility === 'hidden')
    expect(hidden).toHaveLength(1)
    expect(hidden[0].textContent).toContain('q1')
  })

  it('reports the grouped display items to the host in both modes', () => {
    const onDisplayItems = vi.fn()
    render(
      <ChatMessageList
        messages={MESSAGES}
        running={false}
        onDisplayItems={onDisplayItems}
        transcript={{ sessionId: 'test:items' }}
      />,
    )
    expect(onDisplayItems).toHaveBeenCalled()
    const items = onDisplayItems.mock.calls.at(-1)![0]
    expect(Array.isArray(items)).toBe(true)
    expect(items.length).toBeGreaterThan(0)
  })
})
