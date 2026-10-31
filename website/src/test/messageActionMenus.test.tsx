/**
 * App message actions sit in a message's More menu, by role, and a click hands
 * the layer that one message and nothing else.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'
import {
  resolveMessageActions,
  setMessageActionOpener,
  setMessageActions,
  setMessageActionsLoadError,
  type MessageActionRequest,
} from '../messageActions/messageActionRegistry'

vi.mock('../components/MarkdownRenderer', () => ({
  // Carries the content-root marker the real renderer's root carries.
  default: ({ content }: { content: string }) => <div data-testid="md" data-message-body="">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(true) }))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))

const actions = resolveMessageActions([{
  name: 'notes-app',
  displayName: 'Notes',
  enabled: true,
  manifest: { contributes: { messageActions: [
    { id: 'save', title: 'Save to notes', entry: 'f.mjs' },
    { id: 'mine', title: 'Clip my message', entry: 'f.mjs' },
  ] } },
  messageActionGrant: { actions: [
    { id: 'save', title: 'Save to notes', roles: ['assistant'], format: 'plain', maxChars: 32000 },
    { id: 'mine', title: 'Clip my message', roles: ['user'], format: 'plain', maxChars: 32000 },
  ] },
}])

const opened: MessageActionRequest[] = []
beforeEach(() => {
  vi.useFakeTimers()
  opened.length = 0
  setMessageActions(actions)
  setMessageActionOpener((req) => { opened.push(req) })
})
afterEach(() => {
  act(() => { vi.runAllTimers() })
  vi.useRealTimers()
  setMessageActions([])
  setMessageActionOpener(null)
})

const REPLY = 'A completed reply that is comfortably longer than twenty characters.'
const open = (testId: string) => fireEvent.pointerDown(screen.getByTestId(testId), { button: 0, ctrlKey: false, pointerType: 'mouse' })

describe('message action menu rows', () => {
  it('say so, with a retry, when the app list failed to load', () => {
    setMessageActions([])
    const retry = vi.fn()
    setMessageActionsLoadError(retry)
    try {
      render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
      open('assistant-more-actions')
      expect(screen.queryByTestId('message-action-item')).toBeNull()
      expect(screen.getByRole('alert').textContent).toContain("Couldn't load app actions")
      fireEvent.click(screen.getByTestId('message-actions-retry'))
      expect(retry).toHaveBeenCalledTimes(1)
    } finally {
      setMessageActionsLoadError(null)
    }
  })
  it('offer an assistant-role action on a reply, labelled with its app, and hand over that reply', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    open('assistant-more-actions')
    const items = screen.getAllByTestId('message-action-item')
    expect(items.map((i) => i.getAttribute('data-message-action'))).toEqual(['notes-app:save'])
    expect(items[0].textContent).toContain('Notes')
    expect(items[0].getAttribute('aria-label')).toContain('Notes')
    fireEvent.click(items[0])
    expect(opened).toHaveLength(1)
    // The text is read from the rendered bubble at the click.
    expect(opened[0].source).toEqual({ markdown: REPLY, text: REPLY, role: 'assistant' })
    expect(opened[0].anchor).toBe(screen.getByTestId('message-bubble'))
  })

  it('are not offered while a reply streams', () => {
    render(<AssistantMessage content={REPLY} isStreaming slotRunning />)
    expect(screen.queryByTestId('message-action-item')).toBeNull()
  })

  it('offer a user-role action only on the user\'s own message', () => {
    render(<UserMessage content="my own words" renderContent={(c: string, m?: Record<string, unknown>) => renderUserContent({ content: c, meta: m })} messageTs="t1" slotKey="chat-1" onQuoteMessage={() => {}} />)
    open('user-more-actions')
    const items = screen.getAllByTestId('message-action-item')
    expect(items.map((i) => i.getAttribute('data-message-action'))).toEqual(['notes-app:mine'])
    fireEvent.click(items[0])
    expect(opened[0].source).toEqual({ markdown: 'my own words', text: 'my own words', role: 'user' })
  })

  it('read only the message itself from a user bubble, never its knowledge chip or attachments', () => {
    const knowledge = { items: 1, tokens: 9, titles: ['Secret doc'], content: [{ title: 'Secret doc', text: 'confidential knowledge text' }] }
    render(<UserMessage content="my words" meta={{ knowledge }} renderContent={(c: string, m?: Record<string, unknown>) => renderUserContent({ content: c, meta: m })} messageTs="t1" slotKey="chat-1" onQuoteMessage={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /knowledge/i }))
    expect(document.body.textContent).toContain('confidential knowledge text')
    open('user-more-actions')
    fireEvent.click(screen.getAllByTestId('message-action-item')[0])
    expect(opened[0].source.text).toBe('my words')
  })

  it('read only the message itself from a user bubble, never its quoted card', () => {
    const quote = { text: 'someone else said this', role: 'assistant', ts: 't0' }
    render(<UserMessage content="my reply" meta={{ quote }} renderContent={(c: string, m?: Record<string, unknown>) => renderUserContent({ content: c, meta: m })} messageTs="t1" slotKey="chat-1" onQuoteMessage={() => {}} />)
    open('user-more-actions')
    expect(document.body.textContent).toContain('someone else said this')
    fireEvent.click(screen.getAllByTestId('message-action-item')[0])
    expect(opened[0].source.text).toBe('my reply')
  })

  it('keep the footer at two controls: a More only apps open takes Raw view into it', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    // Copy and More on the row; Raw view is not a third row control.
    expect(screen.getByTestId('assistant-more-actions')).toBeTruthy()
    expect(screen.queryByTestId('toggle-raw-view')).toBeNull()
    open('assistant-more-actions')
    expect(screen.getByTestId('toggle-raw-view')).toBeTruthy()
  })

  it('keep Raw view on the row when no app opens a More', () => {
    setMessageActions([])
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('toggle-raw-view')).toBeTruthy()
  })

  it('leave the menus as they were when no app declares one', () => {
    setMessageActions([])
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    // No other overflow item on this reply shape: no More at all.
    expect(screen.queryByTestId('assistant-more-actions')).toBeNull()
  })
})
