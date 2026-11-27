/**
 * Settings -> Chat display choices are read by the message rows themselves, so
 * every surface that draws them (the Sessions chat, a Bot's chat, a split-view
 * pane, an embed) follows the user's choice whether or not its host threads a
 * prop. The SDK row registry, which a Bot's chat and a pane draw through,
 * passes a timestamp and the turn stats on every row, so the rows' own read is
 * what hides them there.
 */
import { act, render, screen } from '@testing-library/react'
import type { ChatMessage } from '../types'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../components/FileChangeChips', () => ({
  default: ({ style }: { style?: string }) => <div data-testid="file-chips" data-style={style ?? ''} />,
}))

import AssistantMessage from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import ChatMessageList from '../app-sdk/ChatMessageList'
import { formatTs } from '../app-sdk/messageRenderers'

const seed = (cfg: Record<string, unknown>) => localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
const TIME = '9:41 AM'

describe('Show Timestamps', () => {
  it('draws the time the host passes while the setting is on (the default)', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} timestamp={TIME} />)
    expect(screen.getByText(TIME)).toBeInTheDocument()
  })

  it('hides a reply time the host still passes once the setting is off', () => {
    seed({ showTimestamps: false })
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} timestamp={TIME} />)
    expect(screen.queryByText(TIME)).toBeNull()
  })

  it('hides a user bubble time the host still passes once the setting is off', () => {
    seed({ showTimestamps: false })
    render(<UserMessage content="hello" timestamp={TIME} renderContent={(c) => <span>{c}</span>} />)
    expect(screen.getByText('hello')).toBeInTheDocument()
    expect(screen.queryByText(TIME)).toBeNull()
  })

  it('follows a change made while the row is on screen', () => {
    render(<UserMessage content="hello" timestamp={TIME} renderContent={(c) => <span>{c}</span>} />)
    expect(screen.getByText(TIME)).toBeInTheDocument()
    act(() => {
      seed({ showTimestamps: false })
      window.dispatchEvent(new Event('mc-config-changed'))
    })
    expect(screen.queryByText(TIME)).toBeNull()
  })

  // The SDK registry's own rows, which a Bot's chat and a split pane draw
  // through: they pass `formatTs(m.ts)` on every row, setting or not.
  const rows: ChatMessage[] = [
    { role: 'user', content: 'ping', cls: '', ts: '2026-01-01T17:41:00Z' },
    { role: 'assistant', content: 'pong', cls: '', ts: '2026-01-01T17:42:00Z' },
  ]
  const times = rows.map(r => formatTs(r.ts)!)

  it('draws both times on the SDK rows while the setting is on', () => {
    render(<ChatMessageList messages={rows} running={false} />)
    for (const time of times) expect(screen.getByText(time)).toBeInTheDocument()
  })

  it('hides both times on the SDK rows once the setting is off', () => {
    seed({ showTimestamps: false })
    render(<ChatMessageList messages={rows} running={false} />)
    expect(screen.getByText('ping')).toBeInTheDocument()
    expect(screen.getByText('pong')).toBeInTheDocument()
    for (const time of times) expect(screen.queryByText(time)).toBeNull()
  })
})

describe('turn stats', () => {
  const stats = { elapsed_ms: 84_000, credits: 2.5 }

  it('draws the footer by default', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={stats} />)
    expect(screen.getByTestId('turn-stats')).toBeInTheDocument()
  })

  it('hides it when the stored setting is off', () => {
    seed({ showTurnStats: false })
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={stats} />)
    expect(screen.queryByTestId('turn-stats')).toBeNull()
  })
})

describe('File Change Chips', () => {
  const fileChanges = [{ path: 'a.ts', before: 'x', after: 'y', added: 1, removed: 1 }] as never

  it('uses the stored style when the host passes none', () => {
    seed({ fileChipStyle: 'minimal' })
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} fileChanges={fileChanges} />)
    expect(screen.getByTestId('file-chips')).toHaveAttribute('data-style', 'minimal')
  })

  it("keeps the host's explicit style", () => {
    seed({ fileChipStyle: 'minimal' })
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} fileChanges={fileChanges} fileChipStyle="expanded" />)
    expect(screen.getByTestId('file-chips')).toHaveAttribute('data-style', 'expanded')
  })
})
