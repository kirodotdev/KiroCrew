/**
 * ChatMessageList's virtualized rows keep their identity across streaming flushes:
 * a flush that only grows the streaming row must not re-render the settled rows above
 * it, and a row appended below a settled row must still update that row (its footer
 * depends on what follows it).
 */
import { render } from '@testing-library/react'
import React from 'react'
import type { ChatMessage } from '../types'

const seen: { content: string; showFooter: boolean }[] = []
vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string; showFooter: boolean }) => {
    seen.push({ content: props.content, showFooter: props.showFooter })
    return <div data-testid="assistant-message">{props.content}</div>
  },
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string }) => <div data-testid="user-message">{props.content}</div>,
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))

import ChatMessageList from '../app-sdk/ChatMessageList'

function msg(role: string, content: string, ts: string): ChatMessage {
  return { role, content, cls: '', ts }
}

const q1 = msg('user', 'q1', '2026-01-01T00:00:01Z')
const a1 = msg('assistant', 'a1', '2026-01-01T00:00:02Z')
const q2 = msg('user', 'q2', '2026-01-01T00:00:03Z')
const rendersOf = (content: string) => seen.filter((s) => s.content === content).length
const transcript = { sessionId: 'test:row-identity' }

describe('ChatMessageList row identity across streaming flushes', () => {
  beforeEach(() => { seen.length = 0 })

  it('re-renders only the streaming row when a flush grows it', () => {
    const view = render(
      <ChatMessageList messages={[q1, a1, q2, msg('streaming', 'part', '')]} running transcript={transcript} />,
    )
    const settledBefore = rendersOf('a1')
    expect(settledBefore).toBeGreaterThan(0)
    // A flush: a NEW array, the settled messages are the same objects, only the
    // streaming row's content grew.
    view.rerender(
      <ChatMessageList messages={[q1, a1, q2, msg('streaming', 'part more', '')]} running transcript={transcript} />,
    )
    expect(rendersOf('part more')).toBeGreaterThan(0)
    expect(rendersOf('a1')).toBe(settledBefore)
  })

  it('still updates a settled row when a row is appended below it', () => {
    const view = render(<ChatMessageList messages={[q1, a1]} running transcript={transcript} />)
    // Nothing follows a1 yet and the turn is running, so no footer.
    expect(seen.filter((s) => s.content === 'a1').pop()?.showFooter).toBe(false)
    view.rerender(<ChatMessageList messages={[q1, a1, q2]} running transcript={transcript} />)
    // A user row now follows a1: its reply is finished and the footer shows.
    expect(seen.filter((s) => s.content === 'a1').pop()?.showFooter).toBe(true)
  })
})
