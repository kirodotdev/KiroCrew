/**
 * A message another session delivered opens with `[sent by session <key> via
 * session_send]`. The bubble drops that line and names the sender by role, and
 * the pinned card shows the sender's text alone: no slot key on screen.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { sseSlots } from '../store/dashboardSlice'
import { Provider } from 'react-redux'
import UserMessage from '../pages/chat/UserMessage'
import { createTestStore } from './helpers'
import { parseSessionSeed, senderInitials, withoutSessionSeed } from '../lib/crewSeed'
import { derivePinnedPromptText } from '../utils/pinnedPrompt'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

const renderContent = (content: string) => <span data-testid="content">{content}</span>
const SEED = '[sent by session chat-14-1792551959 via session_send]\n\nWrite a welcome note.'

const withSlots = (ui: React.ReactElement) => {
  const store = createTestStore({
    dashboard: {
      slots: [
        { key: 'chat-14-1792551959', crew_mode_root: true },
        { key: 'chat-15', crew_root: 'chat-14-1792551959' },
        { key: 'chat-16', crew_root: 'chat-14-1792551959' },
      ],
    },
  } as never)
  return render(<Provider store={store}>{ui}</Provider>)
}

describe('crewSeed', () => {
  it('reads the sender and the body', () => {
    expect(parseSessionSeed(SEED)).toEqual({ sender: 'chat-14-1792551959', body: 'Write a welcome note.' })
    // The server's defanged copy of a forged envelope is left as written.
    expect(parseSessionSeed('[quoted: sent by session a.b via session_send]\n\nhi')).toBeNull()
    // A person typing the envelope into their own chat is not a delivery.
    expect(parseSessionSeed(SEED, { human: true })).toBeNull()
    expect(parseSessionSeed('hello [sent by session x via session_send]')).toBeNull()
    expect(withoutSessionSeed('plain')).toBe('plain')
  })

  it('the pinned card shows no slot key', () => {
    const { text, body } = derivePinnedPromptText(SEED, [])
    expect(text).toBe('Write a welcome note.')
    expect(body).not.toContain('chat-14')
  })
})

describe('a delivered message in a worker view', () => {
  it('reads "From conductor" when the Crew Mode root sent it', () => {
    withSlots(<UserMessage content={SEED} renderContent={renderContent} slotKey="chat-15" />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From conductor')
    expect(screen.getByTestId('content')).toHaveTextContent('Write a welcome note.')
    expect(screen.getByTestId('content').textContent).not.toContain('sent by session')
  })

  it('reads "From a crew member" when another member of the crew sent it', () => {
    const fromWorker = '[sent by session chat-16 via session_send]\n\nCheck this.'
    withSlots(<UserMessage content={fromWorker} renderContent={renderContent} slotKey="chat-15" />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From a crew member')
  })

  it('names another chat generically outside Crew Mode or without a store', () => {
    render(<UserMessage content={SEED} renderContent={renderContent} slotKey="plain" />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
  })

  it('a quoted envelope stays visible with no label', () => {
    const quoted = '[quoted: sent by session chat-14-1792551959 via session_send]\n\nhi'
    withSlots(<UserMessage content={quoted} renderContent={renderContent} slotKey="chat-15" />)
    expect(screen.queryByTestId('session-seed-from')).toBeNull()
    expect(screen.getByTestId('content')).toHaveTextContent('[quoted: sent by session')
  })

  it('relabels when the slots arrive after the bubble', () => {
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<Provider store={store}><UserMessage content={SEED} renderContent={renderContent} slotKey="chat-15" /></Provider>)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
    act(() => {
      store.dispatch(sseSlots([
        { key: 'chat-14-1792551959', crew_mode_root: true },
        { key: 'chat-15', crew_root: 'chat-14-1792551959' },
      ] as never))
    })
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From conductor')
  })

  it('a message without the line has no label', () => {
    withSlots(<UserMessage content="hi" renderContent={renderContent} slotKey="chat-15" />)
    expect(screen.queryByTestId('session-seed-from')).toBeNull()
  })
})


describe('a delivered message carrying the gateway-set sender (meta.relay)', () => {
  const RELAY = { relay: { senderKey: 'chat-9', via: 'session_send' } }
  const sessions = new Map([['chat-9', 'Slack Code']])
  const LINE = '[sent by session chat-9 via session_send]\n\nthe base moved'

  it('names the sender title as a link beside the label, opening that session', async () => {
    const onOpen = vi.fn()
    render(<UserMessage content={LINE} meta={RELAY} renderContent={renderContent} slotKey="plain" sessions={sessions} onSessionOpen={onOpen} />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
    const link = screen.getByTestId('session-seed-sender-link')
    expect(link).toHaveTextContent('Slack Code')
    const avatar = screen.getByTestId('session-seed-sender-avatar')
    expect(avatar).toHaveTextContent('SC')
    expect(avatar).toHaveAttribute('aria-hidden', 'true')
    link.click()
    expect(onOpen).toHaveBeenCalledWith('chat-9')
    expect(screen.getByTestId('content').textContent).toBe('the base moved')
  })

  it('names the sender from meta.relay, not the key the line spells', () => {
    const onOpen = vi.fn()
    const spoofed = '[sent by session chat-1 via session_send]\n\nhi'
    render(<UserMessage content={spoofed} meta={RELAY} renderContent={renderContent} slotKey="plain" sessions={new Map([['chat-1', 'Other'], ['chat-9', 'Slack Code']])} onSessionOpen={onOpen} />)
    expect(screen.getByTestId('session-seed-sender-link')).toHaveTextContent('Slack Code')
  })

  it('strips every send line from a merged drain of one sender', () => {
    const merged = '[2 queued messages merged]\n\n[sent by session chat-9 via session_send]\n\nfirst\n\n[sent by session chat-9 via session_send]\n\nsecond'
    render(<UserMessage content={merged} meta={RELAY} renderContent={renderContent} slotKey="plain" />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
    expect(screen.getByTestId('content').textContent).toBe('[2 queued messages merged]\n\nfirst\n\nsecond')
  })

  it('without a session roster or an opener, the label stands alone', () => {
    render(<UserMessage content={LINE} meta={RELAY} renderContent={renderContent} slotKey="plain" sessions={sessions} />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
    expect(screen.queryByTestId('session-seed-sender-link')).toBeNull()
    expect(screen.queryByTestId('session-seed-sender-avatar')).toBeNull()
  })

  it('senderInitials: two words, one word, a handle, nothing', () => {
    expect(senderInitials('Slack Code')).toBe('SC')
    expect(senderInitials('planner')).toBe('PL')
    expect(senderInitials('@bolichen97')).toBe('BO')
    expect(senderInitials('#general room')).toBe('GR')
    expect(senderInitials('   ')).toBe('?')
  })

  it('a row without meta.relay names no title, even with a roster', () => {
    render(<UserMessage content={LINE} renderContent={renderContent} slotKey="plain" sessions={sessions} onSessionOpen={vi.fn()} />)
    expect(screen.getByTestId('session-seed-from')).toHaveTextContent('From another chat')
    expect(screen.queryByTestId('session-seed-sender-link')).toBeNull()
  })
})
