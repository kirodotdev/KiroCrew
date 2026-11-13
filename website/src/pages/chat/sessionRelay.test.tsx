/**
 * Surface 3 — cross-session messages in the main transcript: the receiver's row
 * (the ordinary user bubble, which names the sender from `meta.relay`) and the
 * sender's OUTGOING card (from its own `session_send` tool call), plus the pure
 * helpers behind both.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../../types'
import { formatTs, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from './transcriptRenderers'
import { isSessionSendTool, parseSessionSend, readRelay, readSessionSendResult, withoutRelayEnvelope } from './sessionRelay'

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

  it('readRelay keeps only a well-formed envelope span list', () => {
    expect(readRelay({ relay: { senderKey: 'chat-1', via: 'session_send', envelope: [[0, 43]] } })?.envelope).toEqual([[0, 43]])
    expect(readRelay({ relay: { senderKey: 'chat-1', envelope: [[0, 5], [9, 14]] } })?.envelope).toEqual([[0, 5], [9, 14]])
    // Overlapping, descending, empty, fractional or non-pair spans are ignored.
    for (const envelope of [[[0, 5], [4, 9]], [[5, 0]], [], [[0.5, 3]], [[0]], 'x', [[-1, 3]]]) {
      expect(readRelay({ relay: { senderKey: 'chat-1', envelope } })?.envelope).toBeUndefined()
    }
  })

  it('withoutRelayEnvelope removes exactly the recorded spans and nothing else', () => {
    const line = '[sent by session chat-1 via session_send]\n\n'
    const content = `${line}keep this: [sent by session chat-1 via session_send]\n\nliteral`
    const relay = readRelay({ relay: { senderKey: 'chat-1', envelope: [[0, line.length]] } })
    // Only the recorded span goes; a lookalike later in the text is the body's.
    expect(withoutRelayEnvelope(content, relay)).toBe('keep this: [sent by session chat-1 via session_send]\n\nliteral')
    // No spans, or spans past the end of the text: nothing to drop by position.
    expect(withoutRelayEnvelope(content, readRelay({ relay: { senderKey: 'chat-1' } }))).toBeNull()
    expect(withoutRelayEnvelope('short', relay)).toBeNull()
    expect(withoutRelayEnvelope(content, null)).toBeNull()
  })

  it('isSessionSendTool / parseSessionSend read the trusted tool name, args and success result', () => {
    const send = msg('tool', {
      content: '🔧 Session send',
      meta: { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: '{"target":"chat-1","message":"go"}', sessionSend: { outcome: 'delivered', target: 'chat-1' } },
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
  it('a relay user row draws through the ordinary user bubble, never a second renderer', () => {
    const received = msg('user', {
      content: '[sent by session chat-1 via session_send]\n\nthe base moved, rebase first',
      meta: { relay: { senderKey: 'chat-1', via: 'session_send' } },
    })
    expect(idFor(received)).toBe(idFor(msg('user', { content: 'hi there' })))
  })
})

describe('readSessionSendResult', () => {
  const withMeta = (meta: Record<string, unknown>) => msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', ...meta } })

  it('reads the outcome and the resolved slot key from the gateway receipt', () => {
    expect(readSessionSendResult(withMeta({ sessionSend: { outcome: 'delivered', target: 'chat-9' } }))).toEqual({ outcome: 'delivered', target: 'chat-9' })
    expect(readSessionSendResult(withMeta({ sessionSend: { outcome: 'steered', target: 'chat-9' } }))).toEqual({ outcome: 'steered', target: 'chat-9' })
    expect(readSessionSendResult(withMeta({ sessionSend: { outcome: 'queued', target: 'chat-9' } }))).toEqual({ outcome: 'queued', target: 'chat-9' })
  })

  it('reads nothing from the result text, however it is worded', () => {
    // The success line the tool returns is prose for the model, not the record.
    expect(readSessionSendResult(withMeta({ output: '📨 Delivered to `chat-9` — it started a turn.' }))).toBeNull()
    expect(readSessionSendResult(withMeta({ output: JSON.stringify({ content: [{ type: 'text', text: '📨 Delivered to `chat-9` — ok' }] }) }))).toBeNull()
  })

  it('reads nothing from a malformed receipt or no receipt', () => {
    expect(readSessionSendResult(withMeta({ sessionSend: { outcome: 'sent', target: 'chat-9' } }))).toBeNull()
    expect(readSessionSendResult(withMeta({ sessionSend: { outcome: 'delivered', target: '' } }))).toBeNull()
    expect(readSessionSendResult(withMeta({ sessionSend: 'chat-9' }))).toBeNull()
    expect(readSessionSendResult(withMeta({}))).toBeNull()
  })
})

describe('Surface 3 — outgoing (this session sent a message)', () => {
  const sent = msg('tool', {
    content: '🔧 Session send',
    meta: { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: '{"target":"chat-9","message":"plan is ready"}', sessionSend: { outcome: 'delivered', target: 'chat-9' } },
  })

  it('a delivered session_send tool row resolves to the outgoing card entry', () => {
    expect(idFor(sent)).toBe('session_send_tool')
  })

  it('a call with no receipt stays the generic tool line, which shows its state', () => {
    const base = { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: '{"target":"chat-9","message":"plan is ready"}' }
    // Still running: no result yet.
    expect(idFor(msg('tool', { content: '🔧 Session send', meta: base }))).toBe('tool')
    // Refused by the gateway.
    expect(idFor(msg('tool', { content: '🔧 Session send', meta: { ...base, output: 'Error: could not send to that session: session_closed' } }))).toBe('tool')
    // A row from before the receipt existed, with only the success prose.
    expect(idFor(msg('tool', { content: '🔧 Session send', meta: { ...base, output: '📨 Delivered to `chat-9` — it started a turn on your message.' } }))).toBe('tool')
  })

  it("another MCP server's `session_send`, or one naming no server, is never the delivery card", () => {
    const delivered = { input: '{"target":"chat-9","message":"request accepted"}', sessionSend: { outcome: 'delivered', target: 'chat-9' } }
    const foreign = msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', mcp_server: 'evil-server', ...delivered } })
    const anonymous = msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', ...delivered } })
    expect(parseSessionSend(foreign)).toBeNull()
    expect(parseSessionSend(anonymous)).toBeNull()
    expect(idFor(foreign)).toBe('tool')
    expect(idFor(anonymous)).toBe('tool')
  })

  it('reads the arguments out of a wrapper only when it names the gateway session_send', () => {
    const receipt = { outcome: 'delivered', target: 'chat-9' }
    const row = (input: unknown) => msg('tool', { content: '🔧 Session send', meta: { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: JSON.stringify(input), sessionSend: receipt } })
    const args = { target: 'chat-9', message: 'plan is ready' }
    // Codex records the call with its server and tool around the arguments.
    expect(parseSessionSend(row({ server: 'kirocrew-dashboard', tool: 'session_send', arguments: args }))).toEqual({ ...args, delivered: 'chat-9' })
    // A Tool Search deferred call names it as `<server>::<tool>`.
    expect(parseSessionSend(row({ tool_id: 'kirocrew-dashboard::session_send', arguments: args }))).toEqual({ ...args, delivered: 'chat-9' })
    // A wrapper naming another tool is not unwrapped.
    expect(parseSessionSend(row({ tool_id: 'evil::session_send', arguments: args }))).toBeNull()
  })

  it('a send addressed by title links to the slot the gateway resolved, not the alias', () => {
    const byTitle = msg('tool', {
      content: '🔧 Session send',
      meta: { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: '{"target":"Planner","message":"plan is ready"}', sessionSend: { outcome: 'queued', target: 'chat-9' } },
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

  it('a host that withholds the session roster (offline) gets a plain @tag, not a dead link', () => {
    const entry = resolveRenderer(sent, registry({ ...baseOpts, sessions: undefined as unknown as Map<string, string> }))!
    render(<>{entry.render(sent, ctx())}</>)
    expect(screen.getByTestId('session-card-recipient').tagName).toBe('SPAN')
  })

  it('passes the row time through to the card meta line', () => {
    const timed = { ...sent, ts: '2026-10-28T16:12:00Z' }
    const entry = resolveRenderer(timed, registry())!
    render(<>{entry.render(timed, ctx())}</>)
    expect(screen.getByTestId('session-card-meta').textContent).toBe(formatTs(timed.ts))
  })
})
