/**
 * A turn the agent started on its own is headed by ONE "Started by the agent"
 * label, above its tool-call toggle, its tool rows and its reply, in the chat
 * page's turn and in the app-sdk pane alike, including a turn that wrote only
 * tool rows and a turn that is a lone reply.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import ChatMessageList from '../app-sdk/ChatMessageList'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))

let seq = 0
const at = () => `2026-11-05T10:00:${String(++seq).padStart(2, '0')}Z`
const OPENER = { kind: 'agent', turn_start: true }
const prompt = (): ChatMessage[] => [
  { role: 'user', content: 'run the build in the background', cls: '', ts: at() },
  { role: 'assistant', content: 'Started it.', cls: '', ts: at(), meta: { turn_stats: { elapsed_ms: 1000 } } },
]
const toolRow = (meta: Record<string, unknown> = {}): ChatMessage =>
  ({ role: 'tool', content: '🔧 Read the build log', cls: '', ts: at(), meta: { kind: 'read', ...meta } }) as ChatMessage
const reply = (origin: Record<string, unknown>): ChatMessage =>
  ({ role: 'assistant', content: 'The build passed.', cls: '', ts: at(), meta: { turn_stats: { elapsed_ms: 1000 }, origin } }) as ChatMessage

const withToolAndReply = (): ChatMessage[] => { seq = 0; return [...prompt(), toolRow({ origin: OPENER }), reply({ kind: 'agent' })] }
const toolOnly = (): ChatMessage[] => { seq = 0; return [...prompt(), toolRow({ origin: OPENER })] }
const loneReply = (): ChatMessage[] => { seq = 0; return [...prompt(), reply(OPENER)] }

/** The rendered order of the label, the tool-call toggle, the tool row and the agent's reply. */
function order(): string[] {
  const marks = [
    ...screen.queryAllByTestId('agent-started-label').map(el => ['label', el] as const),
    ...screen.queryAllByRole('button', { name: /tool call/i }).map(el => ['toggle', el] as const),
    ...screen.queryAllByText(/Read the build log/).map(el => ['tool', el] as const),
    ...screen.queryAllByText('The build passed.').map(el => ['reply', el] as const),
  ]
  marks.sort(([, a], [, b]) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
  return marks.map(([name]) => name)
}

const renderItem = (it: TurnItem): ReactNode =>
  it.kind === 'single' ? <div data-testid={`row-${it.msg.role}`}>{it.msg.content}</div> : null

function renderAgentTurn(messages: ChatMessage[]) {
  const items = applyRunningState(groupDisplayItems(messages), false)
  const turn = items.find((d): d is Extract<DisplayItem, { kind: 'turn' }> => d.kind === 'turn' && !!d.agentStarted)
  expect(turn).toBeTruthy()
  render(<TurnBlock turn={turn!} renderItem={renderItem} disclosureKey="t" />)
}

const openFold = () => fireEvent.click(screen.getByRole('button', { name: /tool call/i }))

describe('the chat page heads a turn the agent started with one label', () => {
  it('above its tool rows and its reply, fold closed and open', () => {
    renderAgentTurn(withToolAndReply())

    expect(order()).toEqual(['label', 'toggle', 'reply'])
    openFold()
    expect(order()).toEqual(['label', 'toggle', 'tool', 'reply'])
  })

  it('above a turn that wrote only tool rows', () => {
    renderAgentTurn(toolOnly())

    expect(order()).toEqual(['label', 'toggle'])
    openFold()
    expect(order()).toEqual(['label', 'toggle', 'tool'])
  })

  it('above a lone reply', () => {
    renderAgentTurn(loneReply())

    expect(order()).toEqual(['label', 'reply'])
  })
})

describe('the app-sdk pane heads a turn the agent started with one label', () => {
  it('above its tool rows and its reply, and not above the prompted turn before it', () => {
    render(<ChatMessageList messages={withToolAndReply()} running={false} />)

    expect(screen.getByText('Started it.').compareDocumentPosition(screen.getByTestId('agent-started-label'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(order()).toEqual(['label', 'toggle', 'reply'])
    openFold()
    expect(order()).toEqual(['label', 'toggle', 'tool', 'reply'])
  })

  it('above a turn that wrote only tool rows', () => {
    render(<ChatMessageList messages={toolOnly()} running={false} />)

    expect(order()).toEqual(['label', 'toggle'])
  })

  it('above a lone reply', () => {
    render(<ChatMessageList messages={loneReply()} running={false} />)

    expect(order()).toEqual(['label', 'reply'])
  })

  it('draws no label on a prompted turn', () => {
    seq = 0
    render(<ChatMessageList messages={[...prompt(), toolRow(), reply({})]} running={false} />)

    expect(screen.queryByTestId('agent-started-label')).toBeNull()
  })
})
