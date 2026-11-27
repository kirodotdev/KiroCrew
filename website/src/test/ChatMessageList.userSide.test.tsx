/**
 * The SDK list aligns every user row to the end side and an agent reply to the
 * start side. A user row someone else wrote marks itself `data-user-side=start`
 * (UserMessage, by the byline's rule; MessageByline.runs.test.tsx pins that
 * half), and the row's two wrapper boxes follow it to the start side through
 * `has-[[data-user-side=start]]:items-start`. happy-dom resolves no CSS, so the
 * pin is the class contract on the wrappers.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import type { ChatMessage } from '../types'

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string }) => <div data-testid="assistant-message">{props.content}</div>,
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string; meta?: { side?: string } }) => (
    <div data-role="user" data-user-side={props.meta?.side}>{props.content}</div>
  ),
}))

import ChatMessageList from '../app-sdk/ChatMessageList'

const FOLLOWS = 'has-[[data-user-side=start]]:items-start'

const msg = (role: ChatMessage['role'], content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: content, ...(meta ? { meta } : {}) }) as ChatMessage

/** The two flex boxes between the row and its content, outermost first. */
function wrappers(el: HTMLElement): HTMLElement[] {
  const body = el.closest('.chat-message-body') as HTMLElement
  return [body.parentElement as HTMLElement, body]
}

describe('ChatMessageList user row side', () => {
  it('a user row is on the end side unless it marks itself start; an agent reply is on the start side', () => {
    const { getByText } = render(
      <ChatMessageList messages={[msg('user', 'mine'), msg('assistant', 'agent'), msg('user', 'theirs', { side: 'start' })]} running={false} />,
    )
    for (const box of wrappers(getByText('mine'))) {
      expect(box.classList.contains('items-end')).toBe(true)
      expect(box.classList.contains(FOLLOWS)).toBe(true)
    }
    expect(getByText('mine').dataset.userSide).toBeUndefined()
    expect(getByText('theirs').dataset.userSide).toBe('start')
    // The agent's row is never pushed to the end side.
    const agent = getByText('agent')
    expect(agent.closest('.items-end')).toBeNull()
  })
})
