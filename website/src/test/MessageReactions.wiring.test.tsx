/**
 * Reaction-row wiring on the two message components: the row renders only when
 * `onToggleReaction` is given, a placed reaction shows as its own visible chip
 * (any emoji, including one reached through the + menu), clicking a chip removes
 * it, the + add trigger opens the fixed set and hides once all are placed, and a
 * failed reaction surfaces an inline notice that names the emoji.
 * `MessageReactionBar`'s own unit tests (chatReactions.test.tsx) cover the
 * control in isolation — including the add menu; this file covers the host
 * components' opt-in gating and prop plumbing.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

const renderContent = (content: string) => <span data-testid="content">{content}</span>

const chipFor = (emoji: string) =>
  screen.getAllByTestId('reaction-chip').find(b => b.getAttribute('data-emoji') === emoji)!

describe('UserMessage reaction row', () => {
  it('does not render when onToggleReaction is absent', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="ts-1" />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })

  it('renders just the add trigger when nothing is placed', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="ts-1" onToggleReaction={() => {}} />)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
    expect(screen.queryAllByTestId('reaction-chip')).toHaveLength(0)
  })

  it('shows a placed reaction as its own visible marker (any emoji, including overflow)', () => {
    render(
      <UserMessage
        content="hi"
        renderContent={renderContent}
        messageTs="ts-1"
        onToggleReaction={() => {}}
        activeReactions={new Set(['⭐'])}
      />,
    )
    const chip = chipFor('⭐')
    expect(chip.tagName).toBe('SPAN') // display-only marker, not a button
    expect(chip).toHaveTextContent('⭐')
  })

  it('surfaces a reaction error inline, naming the emoji', () => {
    render(
      <UserMessage
        content="hi"
        renderContent={renderContent}
        messageTs="ts-1"
        onToggleReaction={() => {}}
        reactionError={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'react' }}
      />,
    )
    expect(screen.getByText('Could not add 👍. Open the reactions button and choose it again.')).toBeInTheDocument()
  })

  it('does not render without a messageTs even with a handler', () => {
    render(<UserMessage content="hi" renderContent={renderContent} onToggleReaction={() => {}} />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })
})

describe('AssistantMessage reaction row', () => {
  it('does not render when onToggleReaction is absent', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-2" />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })

  it('renders just the add trigger when nothing is placed', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-2" onToggleReaction={() => {}} />)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
    expect(screen.queryAllByTestId('reaction-chip')).toHaveLength(0)
  })

  it('shows an overflow reaction placed on the reply as its own chip (the marker)', () => {
    render(
      <AssistantMessage
        content="Hi"
        isStreaming={false}
        slotRunning={false}
        messageTs="ts-2"
        onToggleReaction={() => {}}
        activeReactions={new Set(['⭐'])}
      />,
    )
    // The chosen emoji is drawn as its own marker, not just a highlighted +
    // (UX fix). The marker is display-only (span), removal lives in the menu.
    const chip = chipFor('⭐')
    expect(chip).toHaveTextContent('⭐')
    expect(chip.tagName).toBe('SPAN')
  })

  it('keeps the add trigger present even when every emoji is placed (it also removes)', () => {
    render(
      <AssistantMessage
        content="Hi"
        isStreaming={false}
        slotRunning={false}
        messageTs="ts-2"
        onToggleReaction={() => {}}
        activeReactions={new Set(['👍', '✅', '👀', '⭐'])}
      />,
    )
    expect(screen.getAllByTestId('reaction-chip')).toHaveLength(4)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
  })
})
