/**
 * UserMessage — the paste-chip paths the main UserMessage suite leaves cold.
 *
 * A sent bubble renders a large paste as a chip ("[ Paste #1 · 4 lines ]"), so
 * every route OUT of the bubble has to re-expand it: the copy button, the edit
 * draft, and a native select+copy (the onCopy interceptor, which clones the
 * selected DOM and swaps each `[data-paste-seq]` chip for its original text).
 * The interceptor also has four bail-outs that must leave the browser's own
 * copy alone. Plus the pin affordance, which is pure prop plumbing.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import type { PasteBlock } from '../utils/pasteTokens'
import { i18nT } from '../i18n/t'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

import { copyToClipboard } from '../utils/clipboard'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers(); vi.clearAllMocks() })

const BLOCK: PasteBlock = { id: 'p1', seq: 1, lines: 4, content: 'zzq-line-1\nzzq-line-2\nzzq-line-3\nzzq-line-4' }
const TOKEN = '[ Paste #1 · 4 lines ]'

/** Stand-in for the real renderer: emits the chip as the markdown renderer
 *  does, i.e. a span carrying data-paste-seq. */
const renderWithChip = (content: string) => (
  <span data-testid="content">
    {content.split(TOKEN).flatMap((part, i) => (
      i === 0
        ? [<span key={`t${i}`}>{part}</span>]
        : [<span key={`c${i}`} data-paste-seq="1">{TOKEN}</span>, <span key={`t${i}`}>{part}</span>]
    ))}
  </span>
)

/** Select the whole bubble and fire a copy event whose clipboardData is
 *  observable (happy-dom's ClipboardEvent init does not accept one). */
function copyFrom(bubble: HTMLElement, opts: { collapse?: boolean; outside?: HTMLElement } = {}) {
  const setData = vi.fn()
  const range = document.createRange()
  range.selectNodeContents(opts.outside ?? bubble)
  if (opts.collapse) range.collapse(true)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)

  const ev = new Event('copy', { bubbles: true, cancelable: true })
  Object.defineProperty(ev, 'clipboardData', { value: { setData } })
  bubble.dispatchEvent(ev)
  return { setData, defaultPrevented: ev.defaultPrevented }
}

const bubbleOf = (container: HTMLElement) => container.querySelector('.msg-content') as HTMLElement

describe('UserMessage paste chips', () => {
  it('copies the EXPANDED text, not the chip label', () => {
    render(<UserMessage content={`before ${TOKEN} after`} meta={{ pastes: [BLOCK] }} renderContent={renderWithChip} />)
    fireEvent.click(screen.getByTitle(i18nT('pages.chat.userMessage.copy')))
    expect(copyToClipboard).toHaveBeenCalledWith(`before ${BLOCK.content} after`)
  })

  it('opens the editor on the EXPANDED text so the pasted body is editable', () => {
    render(
      <UserMessage
        content={`head ${TOKEN}`}
        meta={{ pastes: [BLOCK] }}
        renderContent={renderWithChip}
        canEdit
        onEditResend={() => {}}
      />,
    )
    fireEvent.click(screen.getByTitle(i18nT('pages.chat.userMessage.edit_resend')))
    expect(screen.getByRole('textbox')).toHaveValue(`head ${BLOCK.content}`)
  })

  it('rewrites a selection copy so the chip becomes its original content', () => {
    const { container } = render(
      <UserMessage content={`head ${TOKEN} tail`} meta={{ pastes: [BLOCK] }} renderContent={renderWithChip} />,
    )
    const { setData, defaultPrevented } = copyFrom(bubbleOf(container))

    expect(setData).toHaveBeenCalledTimes(1)
    const written = setData.mock.calls[0][1] as string
    expect(written).toContain(BLOCK.content)
    expect(written).not.toContain('Paste #1')
    expect(defaultPrevented).toBe(true)
  })

  it('leaves a chip with no matching paste block untouched', () => {
    const { container } = render(
      <UserMessage
        content={`head ${TOKEN} tail`}
        meta={{ pastes: [{ ...BLOCK, seq: 9 }] }}
        renderContent={renderWithChip}
      />,
    )
    const { setData } = copyFrom(bubbleOf(container))
    expect(setData.mock.calls[0][1]).toContain('Paste #1')
  })

  it('does not intercept the copy when the message has no pastes', () => {
    const { container } = render(<UserMessage content="plain zzq" renderContent={renderWithChip} />)
    const { setData, defaultPrevented } = copyFrom(bubbleOf(container))
    expect(setData).not.toHaveBeenCalled()
    expect(defaultPrevented).toBe(false)
  })

  it('does not intercept a collapsed selection', () => {
    const { container } = render(
      <UserMessage content={`head ${TOKEN}`} meta={{ pastes: [BLOCK] }} renderContent={renderWithChip} />,
    )
    const { setData } = copyFrom(bubbleOf(container), { collapse: true })
    expect(setData).not.toHaveBeenCalled()
  })

  it('does not intercept a selection that lies outside the bubble', () => {
    const outside = document.createElement('div')
    outside.textContent = 'zzq-outside'
    document.body.appendChild(outside)
    const { container } = render(
      <UserMessage content={`head ${TOKEN}`} meta={{ pastes: [BLOCK] }} renderContent={renderWithChip} />,
    )
    const { setData } = copyFrom(bubbleOf(container), { outside })
    expect(setData).not.toHaveBeenCalled()
    outside.remove()
  })

  it('does not intercept when the selection carries no chip', () => {
    const { container } = render(
      <UserMessage content="no chip here" meta={{ pastes: [BLOCK] }} renderContent={renderWithChip} />,
    )
    const { setData } = copyFrom(bubbleOf(container))
    expect(setData).not.toHaveBeenCalled()
  })
})

describe('UserMessage pin affordance', () => {
  it('offers Pin for an unpinned message and calls back on click', () => {
    const onTogglePin = vi.fn()
    render(
      <UserMessage
        content="zzq"
        messageTs="zzq-ts"
        renderContent={(c: string) => <span>{c}</span>}
        onTogglePin={onTogglePin}
      />,
    )
    fireEvent.click(screen.getByTitle(i18nT('pages.chat.userMessage.pin_message')))
    expect(onTogglePin).toHaveBeenCalledTimes(1)
  })

  it('offers Unpin for a pinned message', () => {
    render(
      <UserMessage
        content="zzq"
        messageTs="zzq-ts"
        pinned
        renderContent={(c: string) => <span>{c}</span>}
        onTogglePin={() => {}}
      />,
    )
    expect(screen.getByTitle(i18nT('pages.chat.userMessage.unpin_message'))).toBeInTheDocument()
  })

  it('hides the pin affordance without a message timestamp', () => {
    render(<UserMessage content="zzq" renderContent={(c: string) => <span>{c}</span>} onTogglePin={() => {}} />)
    expect(screen.queryByTitle(i18nT('pages.chat.userMessage.pin_message'))).not.toBeInTheDocument()
  })
})
