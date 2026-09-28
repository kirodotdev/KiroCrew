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
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'

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

  it('declines a plain click to the ACTIVE session, matching the chip (#9927)', () => {
    // resolveSessionChip refuses the active key, so `sessionLink` is null — but
    // the renderer CAN route sessions, so the plain click is declined
    // (preventDefault, no navigation, no switch): a no-op on the session you are
    // already in, exactly as the backtick chip renders the active key inert.
    // The href stays real (target=_blank) so a modified click still opens a
    // duplicate tab for anyone who wants one.
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [here](/chat?sid=${HERE})`, meta: undefined, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    expect(anchor.getAttribute('title')).toBeNull()
    const notCancelled = fireEvent.click(anchor)
    expect(notCancelled).toBe(false)
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('declines a plain click to a closed/unknown session instead of navigating (#9914)', () => {
    // A `?sid=` link whose key names no OPEN session used to fall through to
    // native navigation on a plain click, landing on a dead/blank `?sid=` view.
    // It must now be intercepted and declined (preventDefault, no navigation),
    // matching the backtick chip's existing behaviour for a closed key.
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [gone](/chat?sid=${UNKNOWN})`, meta: undefined, ...triple(onSessionOpen) })}</>,
    )
    const anchor = container.querySelector('a')!
    // Unresolvable: no switch tooltip, and it does not open in place.
    expect(anchor.getAttribute('title')).toBeNull()
    expect(onSessionOpen).not.toHaveBeenCalled()
    // The plain primary click is cancelled — the browser does not follow the
    // raw href to an unresolvable sid. fireEvent.click returns false when a
    // handler called preventDefault().
    const notCancelled = fireEvent.click(anchor)
    expect(notCancelled).toBe(false)
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

  it('offers no chip when the triple is absent, and still navigates (most call sites)', () => {
    // `sessions` ABSENT is deliberately not the same as an empty map — a
    // caller that never wired the roster gets the pre-#8253 behaviour.
    const { container } = render(
      <>{renderUserContent({ content: `see [next](/chat?sid=${THERE})`, meta: undefined, onFileOpen: noop })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    // No session controller is wired, so there is nothing that could switch
    // sessions: the link is an ordinary external link and a plain click must
    // still navigate (NOT be swallowed). Gating the #9914 decline on the
    // controller — not on the href shape alone — is what preserves this.
    const notCancelled = fireEvent.click(anchor)
    expect(notCancelled).toBe(true)
  })

  it('still navigates when offline: onSessionOpen wired but sessions withheld (#9927)', () => {
    // ChatPage keeps `onSessionOpen` wired while disconnected but withholds the
    // roster: `sessions={connected ? sessionTitles : undefined}`. The #9914
    // decline must match resolveSessionChip's full guard (onSessionOpen AND
    // sessions), or an offline `?sid=` link becomes a dead no-op instead of a
    // normal navigating link.
    const onSessionOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent({ content: `see [next](/chat?sid=${THERE})`, meta: undefined, onFileOpen: noop, onSessionOpen, activeSession: HERE })}</>,
    )
    const anchor = container.querySelector('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    const notCancelled = fireEvent.click(anchor)
    expect(notCancelled).toBe(true)
    expect(onSessionOpen).not.toHaveBeenCalled()
  })
})
