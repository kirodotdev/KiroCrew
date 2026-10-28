/**
 * Surface 3 — cross-session messages in the main transcript: the receiver's
 * INCOMING card (from the gateway-set `meta.relay`) and the sender's OUTGOING
 * card (from its own `session_send` tool call), plus the pure helpers behind
 * both.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../../types'
import { formatTs, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from './transcriptRenderers'
import { isSessionSendTool, isUnsettledSteer, parseSessionSend, readRelay, readSessionSendResult, stripRelayPrefix } from './sessionRelay'

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

const sessions = new Map([['chat-1', 'Slack Code'], ['chat-2', 'Health card'], ['chat-9', 'Planner']])
const baseOpts = { slot: 'chat-2', sessions, activeSession: 'chat-2', onSessionOpen: vi.fn() }

const registry = (opts = baseOpts) => mergeRenderers(createTranscriptRenderers(opts))
const idFor = (m: ChatMessage, opts = baseOpts) => resolveRenderer(m, registry(opts))?.id

const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext =>
  ({
    index: 0,
    messages: [],
    running: false,
    key: 'k0',
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: (children: React.ReactNode) => children,
    row: (children: React.ReactNode) => children,
    ...over,
  }) as MessageRenderContext

describe('sessionRelay helpers', () => {
  it('readRelay accepts a well-formed meta.relay and rejects the rest', () => {
    expect(readRelay({ relay: { senderKey: 'chat-1', via: 'session_send' } })).toEqual({ senderKey: 'chat-1', via: 'session_send' })
    expect(readRelay({ relay: { senderKey: 'chat-1' } })).toEqual({ senderKey: 'chat-1', via: 'session_send' })
    expect(readRelay({ relay: { via: 'x' } })).toBeNull()
    expect(readRelay({})).toBeNull()
    expect(readRelay(undefined)).toBeNull()
  })

  it('stripRelayPrefix removes only the gateway opener', () => {
    expect(stripRelayPrefix('[sent by session chat-1 via session_send]\n\nthe real body')).toBe('the real body')
    expect(stripRelayPrefix('no prefix here')).toBe('no prefix here')
    // A lookalike mid-body is left alone.
    expect(stripRelayPrefix('hi [sent by session chat-9 via x]')).toBe('hi [sent by session chat-9 via x]')
  })

  it('isSessionSendTool / parseSessionSend read the trusted tool name, args and success result', () => {
    const send = msg('tool', {
      content: '🔧 Session send',
      meta: { tool_name: 'session_send', input: '{"target":"chat-1","message":"go"}', output: '📨 Delivered to `chat-1` — it started a turn on your message.' },
    })
    expect(isSessionSendTool(send)).toBe(true)
    expect(parseSessionSend(send)).toEqual({ target: 'chat-1', message: 'go', delivered: 'chat-1' })
    // Broadcast is left to the generic tool line.
    expect(isSessionSendTool(msg('tool', { content: '🔧 x', meta: { tool_name: 'session_broadcast', input: '{"message":"go"}' } }))).toBe(false)
    // A read tool named the same way in prose is not matched (trusted name only).
    expect(isSessionSendTool(msg('tool', { content: '🔧 session_send', meta: { tool_name: 'fs_read', input: '{}' } }))).toBe(false)
    expect(parseSessionSend(msg('tool', { content: '🔧', meta: { tool_name: 'session_send', input: 'not json' } }))).toBeNull()
  })
})

describe('Surface 3 — incoming (received cross-session message)', () => {
  const received = msg('user', {
    content: '[sent by session chat-1 via session_send]\n\nthe base moved, rebase first',
    meta: { relay: { senderKey: 'chat-1', via: 'session_send' } },
  })

  it('a relay user row resolves to the incoming card entry, a plain one does not', () => {
    expect(idFor(received)).toBe('session_relay_incoming')
    expect(idFor(msg('user', { content: 'hi there' }))).not.toBe('session_relay_incoming')
  })

  it('passes the row time through to the card meta line', () => {
    const timed = { ...received, ts: '2026-10-28T16:12:00Z' }
    const entry = resolveRenderer(timed, registry())!
    render(<>{entry.render(timed, ctx())}</>)
    expect(screen.getByTestId('session-card-meta').textContent).toBe(formatTs(timed.ts))
  })

  it('renders the incoming card with the sender title from the roster and the prefix stripped', () => {
    const entry = resolveRenderer(received, registry())!
    render(<>{entry.render(received, ctx())}</>)
    const card = screen.getByTestId('session-message-card')
    expect(card).toHaveAttribute('data-direction', 'incoming')
    expect(screen.getByTestId('session-card-title-link').textContent).toBe('Slack Code')
    expect(screen.getByTestId('session-card-body').textContent).toContain('the base moved, rebase first')
    expect(screen.getByTestId('session-card-body').textContent).not.toContain('sent by session')
    fireEvent.click(screen.getByTestId('session-card-title-link'))
    expect(baseOpts.onSessionOpen).toHaveBeenCalledWith('chat-1')
  })
})

describe('readSessionSendResult', () => {
  const withOutput = (output: unknown) => msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', output } })

  it('reads the resolved slot key from each success line, raw or in an MCP envelope', () => {
    expect(readSessionSendResult(withOutput('📨 Delivered to `chat-9` — it started a turn.'))).toBe('chat-9')
    expect(readSessionSendResult(withOutput('📨 Steered `chat-9` — your message went into the turn.'))).toBe('chat-9')
    expect(readSessionSendResult(withOutput('📨 Queued for `chat-9` — it is mid-turn.'))).toBe('chat-9')
    expect(readSessionSendResult(withOutput(JSON.stringify({ content: [{ type: 'text', text: '📨 Delivered to `chat-9` — ok' }] })))).toBe('chat-9')
  })

  it('reads nothing from a refusal, an empty result or no result', () => {
    expect(readSessionSendResult(withOutput('Error: could not send to that session: session_closed'))).toBeNull()
    expect(readSessionSendResult(withOutput(''))).toBeNull()
    expect(readSessionSendResult(withOutput(undefined))).toBeNull()
    // A success word later in the text is not the result's own opener.
    expect(readSessionSendResult(withOutput('Error: x\n📨 Delivered to `chat-9`'))).toBeNull()
  })
})

describe('isUnsettledSteer', () => {
  it('is true only for a steer still waiting, requeued or taken back', () => {
    expect(isUnsettledSteer({ steer: true, steerState: 'written' })).toBe(true)
    expect(isUnsettledSteer({ steer: true, steerState: 'requeued' })).toBe(true)
    expect(isUnsettledSteer({ steer: true, steerState: 'retracted' })).toBe(true)
    expect(isUnsettledSteer({ steer: true, steerState: 'consumed' })).toBe(false)
    expect(isUnsettledSteer({ steer: true })).toBe(false)
    expect(isUnsettledSteer({ steerState: 'written' })).toBe(false)
    expect(isUnsettledSteer(undefined)).toBe(false)
  })
})

describe('Surface 3 — a peer steer that is not delivered yet keeps the ordinary bubble', () => {
  const steer = (steerState: string) =>
    msg('user', {
      content: '[sent by session chat-1 via session_send]\n\nsteered in',
      meta: { steer: true, steerState, relay: { senderKey: 'chat-1', via: 'session_send' } },
    })

  it.each(['written', 'requeued', 'retracted'])('a %s peer steer is not drawn as the card', state => {
    expect(idFor(steer(state))).not.toBe('session_relay_incoming')
  })

  it('a consumed peer steer is drawn as the card', () => {
    expect(idFor(steer('consumed'))).toBe('session_relay_incoming')
  })
})

describe('Surface 3 — outgoing (this session sent a message)', () => {
  const sent = msg('tool', {
    content: '🔧 Session send',
    meta: { tool_name: 'session_send', input: '{"target":"chat-9","message":"plan is ready"}', output: '📨 Delivered to `chat-9` — it started a turn on your message.' },
  })

  it('a delivered session_send tool row resolves to the outgoing card entry', () => {
    expect(idFor(sent)).toBe('session_send_tool')
  })

  it('a call with no success result stays the generic tool line, which shows its state', () => {
    const base = { tool_name: 'session_send', input: '{"target":"chat-9","message":"plan is ready"}' }
    // Still running: no result yet.
    expect(idFor(msg('tool', { content: '🔧 Session send', meta: base }))).toBe('tool')
    // Refused by the gateway.
    expect(idFor(msg('tool', { content: '🔧 Session send', meta: { ...base, output: 'Error: could not send to that session: session_closed' } }))).toBe('tool')
  })

  it('a send addressed by title links to the slot the gateway resolved, not the alias', () => {
    const byTitle = msg('tool', {
      content: '🔧 Session send',
      meta: { tool_name: 'session_send', input: '{"target":"Planner","message":"plan is ready"}', output: '📨 Queued for `chat-9` — it is mid-turn.' },
    })
    const onOpen = vi.fn()
    const entry = resolveRenderer(byTitle, registry({ ...baseOpts, onSessionOpen: onOpen }))!
    render(<>{entry.render(byTitle, ctx())}</>)
    expect(screen.getByTestId('session-card-recipient').textContent).toBe('@Planner')
    fireEvent.click(screen.getByTestId('session-card-recipient'))
    expect(onOpen).toHaveBeenCalledWith('chat-9')
  })

  it('renders the outgoing card saying, in text, that this session sent it to the target', () => {
    const entry = resolveRenderer(sent, registry())!
    render(<>{entry.render(sent, ctx())}</>)
    const card = screen.getByTestId('session-message-card')
    expect(card).toHaveAttribute('data-direction', 'outgoing')
    expect(screen.getByTestId('session-card-direction').textContent).toBe('Sent to @Planner')
    expect(screen.getByTestId('session-card-recipient').textContent).toBe('@Planner')
    expect(screen.getByTestId('session-card-body').textContent).toContain('plan is ready')
    // The sender is named "This session", never by its own title, which in its
    // own chat reads as some other sender; the title stays on hover.
    expect(screen.getByTestId('session-card-title').textContent).toBe('This session')
    expect(screen.getByTestId('session-card-title')).toHaveAttribute('title', 'Health card')
  })

  it('passes the row time through to the card meta line', () => {
    const timed = { ...sent, ts: '2026-10-28T16:12:00Z' }
    const entry = resolveRenderer(timed, registry())!
    render(<>{entry.render(timed, ctx())}</>)
    expect(screen.getByTestId('session-card-meta').textContent).toBe(formatTs(timed.ts))
  })
})
