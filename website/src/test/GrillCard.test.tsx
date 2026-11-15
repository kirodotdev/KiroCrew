import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import GrillCard, { COMPOSE_SEND_EVENT, type ComposeSendDetail, type ComposeSendResult } from '../components/chat/GrillCard'
import AssistantMessage from '../pages/chat/AssistantMessage'
import type { GrillProposal } from '../app-sdk/protocol/grillMarker'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

const GRILL: GrillProposal = {
  q: 'Who writes the facts?',
  must: 'No wrong PR state',
  skip: 'Board layout',
  who: [{ name: 'A', stance: 'for A' }, { name: 'B', stance: 'for B' }],
  minutes: 10,
}

let sent: string[]
let slots: string[]
let take: ComposeSendResult | null
const listener = (e: Event) => {
  const d = (e as CustomEvent<ComposeSendDetail>).detail
  sent.push(d.text)
  slots.push(d.slot)
  d.result = take
}

beforeEach(() => {
  localStorage.clear()
  sent = []
  slots = []
  take = 'sent'
  window.addEventListener(COMPOSE_SEND_EVENT, listener)
})
afterEach(() => window.removeEventListener(COMPOSE_SEND_EVENT, listener))

describe('GrillCard', () => {
  it('shows the proposal: three editable lines, who talks, and the time', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who writes the facts?')
    expect(screen.getByLabelText('Must')).toHaveValue('No wrong PR state')
    expect(screen.getByLabelText('Not discussing')).toHaveValue('Board layout')
    expect(screen.getByTestId('grill-card-who').textContent).toBe('A: for AB: for B')
    expect(screen.getByTestId('grill-card-minutes').textContent).toBe('About 10 minutes')
  })

  it('sends the edited lines on Start, then locks', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Who writes the welcome facts?' } })
    fireEvent.change(screen.getByLabelText('Not discussing'), { target: { value: 'colors and fonts' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(sent).toEqual(['Start the discussion\nQuestion: Who writes the welcome facts?\nMust: No wrong PR state\nNot discussing: colors and fonts'])
    expect(slots).toEqual(['s1'])
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
    expect(screen.getByLabelText('Question')).toBeDisabled()
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
  })

  it('sends the decline text, locks on this page only, and writes nothing to storage', () => {
    const { unmount } = render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual(['No discussion needed'])
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    expect(screen.queryByRole('button', { name: 'No thanks' })).toBeNull()
    expect(localStorage.length).toBe(0)
    unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    expect(screen.getByRole('button', { name: 'No thanks' })).toBeEnabled()
  })

  it('stays open and says so when no chat box takes the text', () => {
    take = null
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(screen.getByTestId('grill-card-unhandled').textContent).toContain('This view has no chat box to send to.')
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled()
  })

  it('sends nothing without a chat to send to', () => {
    render(<GrillCard grill={GRILL} />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual([])
    expect(screen.getByTestId('grill-card-unhandled')).toBeInTheDocument()
  })

  it('says the text only reached the chat box', () => {
    take = 'staged'
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Put in the chat box. Send it from there.')
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
  })

  it('keeps its edits and lock when its row scrolls out and back', () => {
    const first = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Edited?' } })
    first.unmount()
    const second = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Edited?')
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    second.unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    expect(localStorage.length).toBe(0)
  })

  it('holds Start while the question is empty', () => {
    render(<GrillCard grill={GRILL} />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: '  ' } })
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
  })
})

describe('AssistantMessage with a [GRILL:] marker', () => {
  const content = `Two paths.\n[GRILL: ${JSON.stringify(GRILL)}]\n[OPTIONS: Go A | Go B]`

  it('draws the card and keeps the marker out of the prose', () => {
    render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} messageTs="t4" />)
    expect(screen.getByTestId('md').textContent).toBe('Two paths.')
    expect(screen.getByTestId('grill-card')).toBeInTheDocument()
  })

  it('starts each reply version with its own proposal', () => {
    const other = { ...GRILL, q: 'Who reads the facts?' }
    const v = (g: GrillProposal) => ({ content: `Two paths.\n[GRILL: ${JSON.stringify(g)}]` })
    const { rerender } = render(<AssistantMessage content={v(GRILL).content} variants={[v(GRILL), v(other)]} variantIdx={0} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} messageTs="t5" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who writes the facts?')
    rerender(<AssistantMessage content={v(other).content} variants={[v(GRILL), v(other)]} variantIdx={1} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} messageTs="t5" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who reads the facts?')
  })

  it('keeps edits when a refresh gives the row its timestamp', () => {
    const { rerender } = render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="client-1" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Kept?' } })
    rerender(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" messageTs="2026-11-15T00:00:00Z" grillRowId="client-1" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Kept?')
  })

  it('draws no card while the reply streams', () => {
    render(<AssistantMessage content={'Two paths.\n[GRILL: {"q":"Wh'} isStreaming slotRunning />)
    expect(screen.getByTestId('md').textContent).toBe('Two paths.')
    expect(screen.queryByTestId('grill-card')).toBeNull()
  })

  it('shows a bad marker as text, with no card', () => {
    const bad = 'Two paths.\n[GRILL: {"q": oops}]'
    render(<AssistantMessage content={bad} isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md').textContent).toBe(bad)
    expect(screen.queryByTestId('grill-card')).toBeNull()
  })
})
