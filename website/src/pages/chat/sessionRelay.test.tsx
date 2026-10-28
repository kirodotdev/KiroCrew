/**
 * Surface 3 — cross-session messages in the main transcript: the receiver's
 * INCOMING card (from the gateway-set `meta.relay`) and the sender's OUTGOING
 * card (from its own `session_send` tool call), plus the pure helpers behind
 * both.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from './transcriptRenderers'
import { isSessionSendTool, parseSessionSend, readRelay, stripRelayPrefix } from './sessionRelay'

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

  it('isSessionSendTool / parseSessionSend read the trusted tool name and args', () => {
    const send = msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', input: '{"target":"chat-1","message":"go"}' } })
    expect(isSessionSendTool(send)).toBe(true)
    expect(parseSessionSend(send)).toEqual({ target: 'chat-1', message: 'go' })
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

describe('Surface 3 — outgoing (this session sent a message)', () => {
  const sent = msg('tool', {
    content: '🔧 Session send',
    meta: { tool_name: 'session_send', input: '{"target":"chat-9","message":"plan is ready"}' },
  })

  it('a session_send tool row resolves to the outgoing card entry', () => {
    expect(idFor(sent)).toBe('session_send_tool')
  })

  it('renders the outgoing card with the target as the @tag', () => {
    const entry = resolveRenderer(sent, registry())!
    render(<>{entry.render(sent, ctx())}</>)
    const card = screen.getByTestId('session-message-card')
    expect(card).toHaveAttribute('data-direction', 'outgoing')
    expect(screen.getByTestId('session-card-recipient').textContent).toBe('@Planner')
    expect(screen.getByTestId('session-card-body').textContent).toContain('plan is ready')
    // The sender title is this session's own (from the roster).
    expect(screen.getByTestId('session-card-title').textContent).toBe('Health card')
  })
})
