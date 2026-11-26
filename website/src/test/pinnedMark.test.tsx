/**
 * A pinned message keeps a visible "Pinned" mark outside the hover-revealed
 * action row, on user and assistant rows alike, and the mark opens the
 * session's Pins list when the host wires that reveal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ComponentProps } from 'react'
import UserMessage from '../pages/chat/UserMessage'
import AssistantMessage from '../pages/chat/AssistantMessage'
import { i18nT } from '../i18n/t'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

const userRow = (props: Partial<ComponentProps<typeof UserMessage>> = {}) =>
  render(<UserMessage content="Remember this" messageTs="t1" renderContent={c => <p>{c}</p>} onTogglePin={vi.fn()} {...props} />)
const assistantRow = (props: Partial<ComponentProps<typeof AssistantMessage>> = {}) =>
  render(<AssistantMessage content="An answer worth keeping" isStreaming={false} messageTs="t2" onTogglePin={vi.fn()} {...props} />)

describe('the pinned mark on a message', () => {
  it.each([
    ['user', userRow],
    ['assistant', assistantRow],
  ] as const)('a pinned %s row shows it and opens the Pins list from it', (_role, renderRow) => {
    const onShowPins = vi.fn()
    renderRow({ pinned: true, onShowPins })
    const mark = screen.getByTestId('pinned-mark')
    expect(mark.tagName).toBe('BUTTON')
    expect(mark).toHaveTextContent(i18nT('pages.chat.pins.pinned_mark'))
    expect(mark).toHaveAttribute('title', i18nT('pages.chat.pins.show_all_pinned'))
    // Outside the action row, whose reveal-on-hover would hide it again.
    expect(mark.closest('[data-message-actions]')).toBeNull()
    fireEvent.click(mark)
    expect(onShowPins).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['user', userRow],
    ['assistant', assistantRow],
  ] as const)('an unpinned %s row has none', (_role, renderRow) => {
    renderRow({ pinned: false, onShowPins: vi.fn() })
    expect(screen.queryByTestId('pinned-mark')).toBeNull()
  })

  it('is a plain label when the host wires no Pins list', () => {
    userRow({ pinned: true })
    const mark = screen.getByTestId('pinned-mark')
    expect(mark.tagName).toBe('SPAN')
    expect(mark).toHaveTextContent(i18nT('pages.chat.pins.pinned_mark'))
  })
})
