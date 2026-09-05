import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import type { ChatMessage } from '../../types'

// The reply bubble itself is AssistantMessage's contract; here only the
// registry wiring is under test, so it is reduced to a marker. The mock is
// what the SDK default entry renders too, so a stub in the output proves the
// chat profile DELEGATED to it rather than drawing its own bubble.
vi.mock('../chat/AssistantMessage', () => ({
  default: ({ content }: { content: string }) => <div data-testid="assistant-stub">{content}</div>,
}))

import ChatMessageList from '../../app-sdk/ChatMessageList'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer } from '../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../chat/transcriptRenderers'
import { createChatProfileRenderers } from './chatProfileRenderers'
import { projectChatView } from './chatProjection'

const LONG = 'A substantive reply that says something new about the work being done here. '.repeat(3)

function row(role: string, content = '', meta?: Record<string, unknown>): ChatMessage {
  return { role, content, cls: `msg msg-${role}`, meta }
}

function hostRenderers(onSend = vi.fn(), extra: Partial<Parameters<typeof createChatProfileRenderers>[0]> = {}) {
  const transcript = createTranscriptRenderers({ slot: 'member-oncall', toolDisclosure: {}, onToolDisclosureChange: () => {} })
  return [...transcript, ...createChatProfileRenderers({ memberName: 'oncall', onSend, ...extra })]
}

describe('createChatProfileRenderers with the backend escalation index', () => {
  const FUTURE = new Date(Date.now() + 3_600_000).toISOString()
  // The window the pane hydrated: it starts AFTER an older escalation (e0) that
  // is still pending on the server. The visible card e1 is followed by a
  // free-text human reply. The window simulation sees exactly one pending card
  // and would answer e1 with that reply; the backend index says both pending.
  const visible = row('escalation', 'Ship it?', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go', 'No-go'], deadline: FUTURE, mid: 'm1' })
  const freeText = row('user', 'how is it going?', { human_reply: true, mid: 'u1' })
  const entry = (id: string, state: string, more: Record<string, unknown> = {}) => ({ type: 'escalation', id, state, ...more })

  it('(a) an older pending escalation outside the window: the authoritative index keeps the visible card pending, controls intact', () => {
    const states = { e0: entry('e0', 'pending'), e1: entry('e1', 'pending', { deadline: FUTURE }) }
    render(<ChatMessageList messages={[visible, freeText]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: states })} />)
    const card = screen.getByTestId('escalation-card')
    expect(card).toHaveAttribute('data-state', 'pending')
    expect(screen.getByRole('radio', { name: 'Go' })).toBeEnabled()
    expect(screen.getByTestId('escalation-send')).toBeInTheDocument()
    expect(screen.getByTestId('escalation-deadline')).toBeInTheDocument()
  })

  it('(b) the authoritative index says answered: the card closes even with no reply row in the window', () => {
    const states = { e1: entry('e1', 'answered', { answered_ts: new Date().toISOString() }) }
    render(<ChatMessageList messages={[visible]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: states })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'answered')
    expect(screen.getByRole('radio', { name: 'Go' })).toBeDisabled()
    expect(screen.queryByTestId('escalation-send')).toBeNull()
  })

  it('(c) no index (null): today\u2019s window simulation decides — the free-text reply answers the only visible card', () => {
    render(<ChatMessageList messages={[visible, freeText]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: null })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'answered')
  })

  it('an id the index does not know falls back to the simulation as well', () => {
    const states = { other: entry('other', 'pending') }
    render(<ChatMessageList messages={[visible, freeText]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: states })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'answered')
  })

  it('retracted closes the card as "Withdrawn" with no default-applied line; defaulted / expired map to their closed shapes', () => {
    const withDefault = row('escalation', 'Ship it?', { kind: 'escalation', escalation_id: 'e1', options: ['Go'], deadline: FUTURE, default_action: 'Ship at 5pm', mid: 'm1' })
    const first = render(<ChatMessageList messages={[withDefault]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: { e1: entry('e1', 'retracted') } })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'retracted')
    expect(screen.getByTestId('escalation-state-badge')).toHaveTextContent('Withdrawn')
    expect(screen.queryByTestId('escalation-default')).toBeNull()
    expect(screen.queryByTestId('escalation-send')).toBeNull()
    expect(screen.getByRole('radio', { name: 'Go' })).toBeDisabled()
    first.unmount()
    const second = render(<ChatMessageList messages={[withDefault]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: { e1: entry('e1', 'defaulted') } })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'defaulted')
    expect(screen.getByTestId('escalation-default')).toHaveTextContent('Default applied: Ship at 5pm')
    second.unmount()
    render(<ChatMessageList messages={[withDefault]} running={false} renderers={hostRenderers(vi.fn(), { escalationStates: { e1: entry('e1', 'expired') } })} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'expired')
  })

  it('the card asks the index to refresh after a send the server accepted', async () => {
    const onSend = vi.fn().mockResolvedValue(true)
    const onEscalationRefresh = vi.fn()
    render(<ChatMessageList messages={[visible]} running={false} renderers={hostRenderers(onSend, { escalationStates: { e1: entry('e1', 'pending') }, onEscalationRefresh })} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Go' }))
    fireEvent.click(screen.getByTestId('escalation-send'))
    expect(onSend).toHaveBeenCalledWith('Go', { escalation_id: 'e1' })
    await screen.findByTestId('escalation-sent')
    expect(onEscalationRefresh).toHaveBeenCalledTimes(1)
  })
})

describe('createChatProfileRenderers', () => {
  it('its assistant entry wins over the SDK default once merged, and delegates the bubble to that default', () => {
    const merged = mergeRenderers(hostRenderers())
    const entry = resolveRenderer(row('assistant', 'hi'), merged)
    expect(entry?.id).toBe('assistant')
    // Exactly one `assistant` entry survives, and it is the chat profile's.
    const assistantEntries = merged.filter(r => r.id === 'assistant')
    expect(assistantEntries).toHaveLength(1)
    const sdkDefault = defaultMessageRenderers.find(r => r.id === 'assistant')!
    expect(assistantEntries[0]).not.toBe(sdkDefault)
    // Delegation, not a copy: the default's render runs and draws the bubble,
    // and its `null` for an invisible-only row is passed through untouched.
    const renderSpy = vi.spyOn(sdkDefault, 'render')
    const ctx = {
      index: 0, messages: [row('assistant', 'hi')], running: false, key: 'k', hideCardOwnedOAuth: false, autoDeniedIds: new Set<string>(),
      wrapper: (c: ReactNode) => <div data-testid="wrap">{c}</div>, row: (c: ReactNode) => <div>{c}</div>,
    }
    render(<>{assistantEntries[0].render(row('assistant', 'hi', { chat_process: [row('tool', '🔧 grep')] }), ctx)}</>)
    expect(renderSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('assistant-stub')).toHaveTextContent('hi')
    expect(screen.getByTestId('process-disclosure-toggle')).toBeInTheDocument()
    // An invisible-only row with NO process trail stays null (nothing to show).
    expect(assistantEntries[0].render(row('assistant', '\u200B'), { ...ctx, messages: [row('assistant', '\u200B')] })).toBeNull()
    renderSpy.mockRestore()
    expect(resolveRenderer(row('escalation', 'x', { kind: 'escalation' }), merged)?.id).toBe('escalation')
    expect(resolveRenderer(row('chat_fold', '', { kind: 'silent_rounds' }), merged)?.id).toBe('chat_fold')
  })

  it('an invisible-only reply that still carries a process trail draws the disclosure on its own row', () => {
    const merged = mergeRenderers(hostRenderers())
    const entry = merged.find(r => r.id === 'assistant')!
    const quiet = row('assistant', '\u200B', { chat_process: [row('tool', '🔧 check', { purpose: 'Poll the PR' })], mid: 'q1' })
    const rowFn = vi.fn((c: ReactNode, tight?: boolean) => <div data-testid="row" data-tight={String(!!tight)}>{c}</div>)
    const ctx = {
      index: 0, messages: [quiet], running: false, key: 'k', hideCardOwnedOAuth: false, autoDeniedIds: new Set<string>(),
      wrapper: (c: ReactNode) => <div data-testid="wrap">{c}</div>, row: rowFn,
    }
    render(<>{entry.render(quiet, ctx)}</>)
    // No bubble (the default returned null), but the hidden steps stay reachable.
    expect(screen.queryByTestId('assistant-stub')).toBeNull()
    expect(screen.queryByTestId('wrap')).toBeNull()
    expect(screen.getByTestId('row')).toHaveAttribute('data-tight', 'true')
    const toggle = screen.getByTestId('process-disclosure-toggle')
    expect(toggle).toHaveTextContent('View process (1 step)')
    fireEvent.click(toggle)
    expect(screen.getByTestId('process-disclosure-body')).toHaveTextContent('Poll the PR')
  })

  it('draws the reply with a process disclosure that opens to the hidden tool steps', () => {
    const src = [row('user', 'go'), row('tool', '🔧 grep', { purpose: 'Find the callers' }), row('tool', '🔧 read_file'), row('assistant', LONG, { mid: 'a1' })]
    const view = projectChatView(src, { running: false })
    render(<ChatMessageList messages={view} running={false} renderers={hostRenderers()} />)
    expect(screen.getByTestId('assistant-stub')).toHaveTextContent('A substantive reply')
    // No tool row of its own — the steps live behind the disclosure.
    expect(screen.queryByText('🔧 grep')).toBeNull()
    const toggle = screen.getByTestId('process-disclosure-toggle')
    expect(toggle).toHaveTextContent('View process (2 steps)')
    fireEvent.click(toggle)
    expect(screen.getByTestId('process-disclosure-toggle')).toHaveTextContent('Hide process')
    const body = screen.getByTestId('process-disclosure-body')
    expect(body).toHaveTextContent('Find the callers')
    expect(body).toHaveTextContent('read_file')
  })

  it('draws the silent-rounds fold row and expands it to the folded rows', () => {
    const src = [
      row('user', 'go'), row('assistant', LONG),
      row('nudge', '[auto-nudge cycle 1]\ncheck', { nudge: { cycle: 1 }, mid: 'n1' }), row('tool', '🔧 check'), row('assistant', 'Nothing new.'),
      row('nudge', '[auto-nudge cycle 2]\ncheck', { nudge: { cycle: 2 }, mid: 'n2' }), row('assistant', '\u200B'),
    ]
    const view = projectChatView(src, { running: false })
    render(<ChatMessageList messages={view} running={false} renderers={hostRenderers()} />)
    const fold = screen.getByTestId('chat-fold-row')
    expect(fold).toHaveTextContent('2 check-ins with nothing new')
    expect(screen.queryByTestId('chat-fold-body')).toBeNull()
    fireEvent.click(screen.getByTestId('chat-fold-toggle'))
    expect(screen.getByTestId('chat-fold-body')).toHaveTextContent('check')
  })

  it('escalation options send through the host handler with the escalation id, and the state comes from the transcript', () => {
    const onSend = vi.fn()
    const esc = row('escalation', 'Need a go/no-go', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go', 'No-go'], mid: 'm1' })
    const pending = [row('user', 'go'), esc]
    render(<ChatMessageList messages={pending} running={false} renderers={hostRenderers(onSend)} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'pending')
    expect(screen.getByText('oncall needs you')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Go' }))
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('escalation-send'))
    expect(onSend).toHaveBeenCalledWith('Go', { escalation_id: 'e1' })
  })

  it('an escalation followed by the person\u2019s reply (meta.human_reply) renders as answered', () => {
    const esc = row('escalation', 'Need a go/no-go', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go'], mid: 'm1' })
    render(<ChatMessageList messages={[esc, row('user', 'Go', { human_reply: true })]} running={false} renderers={hostRenderers()} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'answered')
    expect(screen.getByRole('radio', { name: 'Go' })).toBeDisabled()
    expect(screen.queryByTestId('escalation-send')).toBeNull()
  })

  it('an automated user row without meta.human_reply (heartbeat / cron prompt) leaves the card pending', () => {
    const esc = row('escalation', 'Need a go/no-go', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go'], mid: 'm1' })
    render(<ChatMessageList messages={[esc, row('user', '[cron] check the deploy')]} running={false} renderers={hostRenderers()} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'pending')
    expect(screen.getByTestId('escalation-send')).toBeInTheDocument()
  })

  it('the SDK default set alone still draws an escalation card (read-only, titled by the sending session, linking to /members)', () => {
    const esc = row('escalation', 'Need a go/no-go', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go'], mid: 'm1' })
    render(<MemoryRouter><ChatMessageList messages={[esc]} running={false} /></MemoryRouter>)
    expect(screen.getByTestId('escalation-card')).toBeInTheDocument()
    expect(screen.getByText('s1 needs you', { selector: 'span.font-semibold' })).toBeInTheDocument()
    // No dead click: the options are a plain list and the note is a real link to the Members page.
    expect(screen.getByTestId('escalation-options').querySelectorAll('button')).toHaveLength(0)
    expect(screen.getByTestId('escalation-options')).toHaveTextContent('Go')
    expect(screen.queryByTestId('escalation-reply-hint')).toBeNull()
    expect(within(screen.getByTestId('escalation-answer-in-thread')).getByRole('link')).toHaveAttribute('href', '/members')
  })

  it('an optimistic (unconfirmed) user row after the card leaves it pending; the confirmed row answers it', () => {
    const esc = row('escalation', 'Need a go/no-go', { kind: 'escalation', escalation_id: 'e1', from_session: 's1', options: ['Go'], mid: 'm1' })
    // Mirrors ChatPane.doSend's bubble meta: optimistic + the human stamp.
    const optimistic = row('user', 'Go', { sendId: 's-1', optimistic: true, human_reply: true })
    const first = render(<ChatMessageList messages={[esc, optimistic]} running={false} renderers={hostRenderers()} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'pending')
    first.unmount()
    render(<ChatMessageList messages={[esc, row('user', 'Go', { sendId: 's-1', human_reply: true })]} running={false} renderers={hostRenderers()} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'answered')
  })
})
