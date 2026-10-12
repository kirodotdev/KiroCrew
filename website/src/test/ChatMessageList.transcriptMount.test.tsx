/**
 * ChatMessageList's two mounting modes (chat-core P5-e): with `transcript` it
 * owns a virtualized scroller; without it, the original bare fragment.
 */
import { render, screen } from '@testing-library/react'
import React, { createRef, type ReactNode } from 'react'
import type { ChatMessage } from '../types'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string }) => <div data-testid="assistant-message">{props.content}</div>,
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string }) => <div data-testid="user-message">{props.content}</div>,
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))
vi.mock('../pages/chat/NudgeCard', () => ({
  default: ({ message }: { message: ChatMessage }) => <div data-testid="nudge-message">{message.content}</div>,
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

  it('puts a split-pane nudge and its reply in the newest top-down turn', () => {
    const messages = [
      msg('user', 'q1', '2026-01-01T00:00:01Z'),
      msg('assistant', 'a1', '2026-01-01T00:00:02Z'),
      msg('nudge', 'n1', '2026-01-01T00:00:03Z'),
      msg('assistant', 'a2', '2026-01-01T00:00:04Z'),
    ]
    const { container } = render(
      <ChatMessageList
        messages={messages}
        running={false}
        transcript={{ sessionId: 'test:nudge-newest-first', order: 'newest-first' }}
      />,
    )
    const rows = [...container.querySelectorAll<HTMLElement>('[data-display-index]')]
      .map((row) => row.textContent)
    expect(rows).toEqual(['n1', 'a2', 'q1', 'a1'])
  })

  it('keeps the headless oldest top-down turn mounted when an older page grows its head', () => {
    // The headless turn is drawn last in newest-first order. Its key must stay
    // the chronological tail key: a lead key renames the row on each landing,
    // so React remounts it (a new DOM node) and its height cache entry is lost.
    const headless = [
      msg('assistant', 'h1', '2026-01-01T00:00:03Z'),
      msg('tool', 't1', '2026-01-01T00:00:04Z'),
      msg('assistant', 'h2', '2026-01-01T00:00:05Z'),
    ]
    const newest = [msg('user', 'q2', '2026-01-01T00:00:06Z'), msg('assistant', 'a2', '2026-01-01T00:00:07Z')]
    const olderPage = [msg('assistant', 'h0', '2026-01-01T00:00:01Z'), msg('tool', 't0', '2026-01-01T00:00:02Z')]
    const transcript = { sessionId: 'test:headless-newest-first', order: 'newest-first' as const }
    const headlessRow = (root: HTMLElement) => {
      const rows = [...root.querySelectorAll<HTMLElement>('[data-display-index]')]
      expect(rows.map((r) => r.textContent)).toEqual(['q2', 'a2', expect.stringContaining('h2')])
      return rows[2]
    }
    const { container, rerender } = render(
      <ChatMessageList messages={[...headless, ...newest]} running={false} transcript={transcript} />,
    )
    const before = headlessRow(container)
    expect(before.textContent).not.toContain('h0')
    rerender(<ChatMessageList messages={[...olderPage, ...headless, ...newest]} running={false} transcript={transcript} />)
    const after = headlessRow(container)
    expect(after.textContent).toContain('h0')
    expect(after).toBe(before)
  })

  it('keeps the chronological nudge grouping when newest-first is off', () => {
    const onDisplayItems = vi.fn()
    const messages = [
      msg('user', 'q1', '2026-01-01T00:00:01Z'),
      msg('assistant', 'a1', '2026-01-01T00:00:02Z'),
      msg('nudge', 'n1', '2026-01-01T00:00:03Z'),
      msg('assistant', 'a2', '2026-01-01T00:00:04Z'),
    ]
    render(
      <ChatMessageList
        messages={messages}
        running={false}
        onDisplayItems={onDisplayItems}
        transcript={{ sessionId: 'test:nudge-chronological' }}
      />,
    )
    const items = onDisplayItems.mock.calls.at(-1)![0] as DisplayItem[]
    expect(items).toHaveLength(2)
    expect(items[0]).toMatchObject({ kind: 'single', msg: { content: 'q1' } })
    expect(items[1]).toMatchObject({
      kind: 'turn',
      items: [
        { kind: 'single', msg: { content: 'a1' } },
        { kind: 'single', msg: { content: 'n1' } },
        { kind: 'single', msg: { content: 'a2' } },
      ],
    })
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
    // No strip flag: the stand-in marker is bare, so index.css leaves the row's
    // action strip hidden with the row (it has slid under the card).
    expect(hidden[0].getAttribute('data-pinned-standin')).toBe('')
    expect(rows.filter((r) => r.hasAttribute('data-pinned-standin'))).toHaveLength(1)
  })

  it('marks the hidden row `folding` while the host reports its strip still uncovered', () => {
    const { container } = render(
      <ChatMessageList
        messages={MESSAGES}
        running={false}
        hiddenRow={{ ts: '2026-01-01T00:00:01Z', index: 0, stripUncovered: true }}
        transcript={{ sessionId: 'test:folding' }}
      />,
    )
    const rows = [...container.querySelectorAll<HTMLDivElement>('[data-display-index]')]
    const hidden = rows.filter((r) => r.style.visibility === 'hidden')
    expect(hidden).toHaveLength(1)
    // The value index.css keys the re-shown strip on: written while any of the
    // strip is still on screen below the card standing in for the bubble.
    expect(hidden[0].getAttribute('data-pinned-standin')).toBe('folding')
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
