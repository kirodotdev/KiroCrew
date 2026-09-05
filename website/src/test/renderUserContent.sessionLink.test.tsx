// @vitest-environment happy-dom
/**
 * Regression test for #8253: a `/chat?sid=…` link in a USER message switches
 * session in place, exactly like the assistant / note rows.
 *
 * User-message rows render through renderUserContent → renderFileSegment,
 * which historically passed only presentation props to MarkdownRenderer.
 * Without the session triple (`onSessionOpen` / `sessions` / `activeSession`)
 * `resolveSessionChip` refuses at its first guard, the root-relative href
 * falls into the external-link branch (`ALLOWED_PROTOCOLS` holds only the
 * vscode schemes), and the anchor gains `target="_blank"` — a new tab where
 * every other row kind switches in place.
 *
 * These tests exercise the REAL MarkdownRenderer through renderUserContent, so
 * they pin the whole thread: helper options → renderer props → anchor.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'

const noop = () => {}

/** Real slot-key shape (`chat-<n>-<unix-ts>`); `sessionKeyFrom` refuses anything else. */
const HERE = 'chat-1-1788000000'
const THERE = 'chat-2-1788000001'
const UNKNOWN = 'chat-9-1788000009'

const SESSIONS: ReadonlyMap<string, string> = new Map([
  [HERE, 'this one'],
  [THERE, 'the other one'],
])

const triple = (onSessionOpen: (key: string) => void) => ({
  onFileOpen: noop,
  onSessionOpen,
  sessions: SESSIONS,
  activeSession: HERE,
})

describe('a /chat?sid= link in a user message (#8253)', () => {
  it('switches session in place: no target="_blank", click invokes onSessionOpen with the key', () => {
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [next](/chat?sid=${THERE})`, meta: undefined, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toBeInTheDocument()
    expect(anchor).not.toHaveAttribute('target')
    // The href stays real so a modified click (Cmd/Ctrl) still opens a tab.
    expect(anchor.getAttribute('href')).toContain(`sid=${THERE}`)
    // The switch tooltip is the chip's visible contract, same as note rows.
    expect(anchor.getAttribute('title')).toContain('the other one')
    fireEvent.click(anchor)
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it('leaves a link to the ACTIVE session inert with its existing behaviour', () => {
    // Negative control: resolveSessionChip refuses the active key (a click
    // would be a visible no-op), so the anchor keeps the pre-existing
    // external-branch shape — identical to what assistant rows render today.
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [here](/chat?sid=${HERE})`, meta: undefined, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    expect(anchor.getAttribute('title')).toBeNull()
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('leaves a link whose key names no open session as an ordinary link', () => {
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [gone](/chat?sid=${UNKNOWN})`, meta: undefined, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    expect(anchor.getAttribute('title')).toBeNull()
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('threads the triple through the attachment-caption path too', () => {
    // A standalone upload routes the caption through the second
    // MarkdownRenderer call in renderFileSegment; the triple must reach it as
    // well, or a link in an attachment caption keeps opening a new tab.
    const onSessionOpen = vi.fn()
    const content = `[attached_file 1] /home/user/report.docx\nsee [next](/chat?sid=${THERE})`
    const meta = { files: ['/home/user/report.docx'] }
    const { container } = render(
      <>{renderUserContent({ content, meta, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toBeInTheDocument()
    expect(anchor).not.toHaveAttribute('target')
    fireEvent.click(anchor)
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it('offers no chip when the triple is absent (most call sites)', () => {
    // `sessions` ABSENT is deliberately not the same as an empty map — a
    // caller that never wired the roster gets the pre-#8253 behaviour.
    const { container } = render(
      <>{renderUserContent({ content: `see [next](/chat?sid=${THERE})`, meta: undefined, onFileOpen: noop })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
  })
})
