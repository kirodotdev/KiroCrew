/**
 * Where the thread action lives on each message row, and what the row costs.
 *
 * The action row on both message kinds already carries more peer buttons than the
 * two-button rule allows. The rule lets a row in that position keep the count it
 * has but never grow, so a thread action cannot be one more button there. It is a
 * menu item on a finished row. The streaming reply is the exception and keeps a
 * visible control, because that row holds exactly one.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('../../test/__mocks__/@radix-ui/react-dropdown-menu'))

import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import UserMessage from './UserMessage'
import AssistantMessage from './AssistantMessage'

/** Peer buttons in the same horizontal group as the given control. */
function rowButtonCount(el: HTMLElement): number {
  const row = el.closest('[data-message-actions]') ?? el.parentElement!
  return row.querySelectorAll('button').length
}

describe('the thread action on a user message', () => {
  const props = {
    content: 'Nine issues came in overnight.',
    meta: { mid: 'm-1' },
    messageTs: '2026-09-29T07:02:00Z',
    slotKey: 'chat-41',
    renderContent: (t: string) => <span>{t}</span>,
  }

  it('is in the overflow menu, not a peer button in the row', () => {
    const onReplyInThread = vi.fn()
    renderWithProviders(<UserMessage {...props} onReplyInThread={onReplyInThread} onTogglePin={vi.fn()} />)
    const trigger = screen.getByTestId('user-message-more-actions')
    expect(trigger).toBeInTheDocument()
    fireEvent.click(trigger)
    const item = screen.getByTestId('reply-in-thread')
    expect(item.tagName).not.toBe('BUTTON')
    fireEvent.click(item)
    expect(onReplyInThread).toHaveBeenCalledTimes(1)
  })

  it('does not grow the row: the trigger stands where the copy-link button stood', () => {
    const withThread = renderWithProviders(
      <UserMessage {...props} onReplyInThread={vi.fn()} onTogglePin={vi.fn()} />,
    )
    const withCount = rowButtonCount(withThread.getByTestId('user-message-more-actions'))
    withThread.unmount()
    // The same row with no thread hooks at all: copy link is a row button and there
    // is no trigger. Equal counts is the claim -- the rule is about growth.
    const plain = renderWithProviders(<UserMessage {...props} onTogglePin={vi.fn()} />)
    const plainRow = plain.container.querySelector('[data-message-actions]')!
    expect(plainRow.querySelectorAll('button').length).toBe(withCount)
  })

  it('keeps Pin one click, and moves the permalink instead', () => {
    // Pin is a habitual one-click action and threads are offered on every chat
    // surface, so demoting Pin would cost a second click everywhere, for good. The
    // permalink is the narrowest of the four controls the row carries, so it is the
    // one that goes into the menu beside the thread action.
    const onTogglePin = vi.fn()
    renderWithProviders(
      <UserMessage {...props} onReplyInThread={vi.fn()} onTogglePin={onTogglePin} />,
    )
    const row = screen.getByTestId('user-message-more-actions').closest('[data-message-actions]')!
    const pin = row.querySelector('button[aria-pressed]')
    expect(pin).not.toBeNull()
    fireEvent.click(pin as HTMLElement)
    expect(onTogglePin).toHaveBeenCalledTimes(1)
    // And the permalink is reachable, one level in.
    fireEvent.click(screen.getByTestId('user-message-more-actions'))
    expect(screen.getByTestId('copy-link-to-message')).toBeInTheDocument()
  })
})

describe('the thread action on an assistant message', () => {
  const props = {
    content: 'Four are open.',
    text: 'Four are open.',
    meta: { mid: 'm-2' },
    messageTs: '2026-09-29T07:03:00Z',
    slotKey: 'chat-41',
    renderContent: (t: string) => <span>{t}</span>,
  }

  it('is in More on a finished reply', () => {
    const onReplyInThread = vi.fn()
    renderWithProviders(<AssistantMessage {...props} onReplyInThread={onReplyInThread} />)
    fireEvent.click(screen.getByTestId('assistant-more-actions'))
    const item = screen.getByTestId('reply-in-thread')
    expect(item.tagName).not.toBe('BUTTON')
    fireEvent.click(item)
    expect(onReplyInThread).toHaveBeenCalledTimes(1)
  })

  it('stays a visible control while the reply is still streaming', () => {
    // Waiting for the turn to end to offer a thread is the behaviour this epic
    // exists to remove: the moment a long answer goes the wrong way is the moment
    // to branch off it.
    renderWithProviders(<AssistantMessage {...props} isStreaming onReplyInThread={vi.fn()} />)
    const btn = screen.getByTestId('reply-in-thread-streaming')
    expect(btn.tagName).toBe('BUTTON')
    expect(rowButtonCount(btn)).toBe(1)
  })

  it('sits at the TOP of the reply, so its position does not depend on body height', () => {
    // The defect: a control in the body's action row rides the growing bubble, so
    // it walks away from the cursor between the decision to click and the click.
    // The header row is the reply's first child and the body is its sibling, so
    // appending text cannot move it -- asserted structurally, because jsdom lays
    // nothing out and a geometry assertion here would measure zero either way.
    const short = renderWithProviders(
      <AssistantMessage {...props} content="Four." text="Four." isStreaming onReplyInThread={vi.fn()} />,
    )
    const headerOf = (root: HTMLElement) => {
      const reply = root.querySelector('[data-role="assistant"]')!
      return { reply, header: reply.querySelector('[data-testid="thread-opener-header"]')! }
    }
    const a = headerOf(short.container as HTMLElement)
    expect(a.header).not.toBeNull()
    expect(a.reply.firstElementChild).toBe(a.header)
    expect(a.header.contains(short.getByTestId('message-bubble'))).toBe(false)
    short.unmount()

    // The same reply with a body two thousand lines long: same position.
    const long = renderWithProviders(
      <AssistantMessage
        {...props}
        content={'a line of the answer\n'.repeat(2000)}
        text={'a line of the answer\n'.repeat(2000)}
        isStreaming
        onReplyInThread={vi.fn()}
      />,
    )
    const b = headerOf(long.container as HTMLElement)
    expect(b.reply.firstElementChild).toBe(b.header)
    expect(b.header.compareDocumentPosition(long.getByTestId('message-bubble')))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('is sticky, and outside the clipping bubble so sticky can work', () => {
    // `.message-bubble` is overflow-hidden. A clipping ancestor between a sticky
    // element and the scroll container turns sticky back into static, so the header
    // has to be a sibling of the bubble rather than a child of it.
    renderWithProviders(<AssistantMessage {...props} isStreaming onReplyInThread={vi.fn()} />)
    const header = screen.getByTestId('thread-opener-header')
    expect(header.className).toContain('sticky')
    expect(header.className).toContain('top-0')
    expect(screen.getByTestId('message-bubble').contains(header)).toBe(false)
    // The strip spans the row, so it must not swallow clicks on the prose beneath.
    expect(header.className).toContain('pointer-events-none')
    expect(screen.getByTestId('reply-in-thread-streaming').className).toContain('pointer-events-auto')
  })

  it('offers the same button in the same place once the reply is persisted', () => {
    // Raymond's rule: one place, streaming or not. The More-menu item stays as the
    // secondary path rather than being the only one on a finished reply.
    renderWithProviders(<AssistantMessage {...props} onReplyInThread={vi.fn()} />)
    const reply = screen.getByTestId('message-bubble').closest('[data-role="assistant"]')!
    expect(reply.firstElementChild).toBe(screen.getByTestId('thread-opener-header'))
    fireEvent.click(screen.getByTestId('assistant-more-actions'))
    expect(screen.getByTestId('reply-in-thread')).toBeInTheDocument()
  })
})
