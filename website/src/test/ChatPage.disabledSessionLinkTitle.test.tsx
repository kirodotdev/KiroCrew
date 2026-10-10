/**
 * A `?sid=` link naming a session that is NOT open renders muted and its click
 * is declined (#9914: navigating a dead `?sid=` view is worse than not moving).
 * The affordance is honest, but the reason was not stated — hover explained
 * nothing. This asserts the disabled branch now carries an explanatory `title`.
 *
 * The resolvable case keeps its switch tooltip (covered by
 * `ChatPage.noteSessionLink.test.tsx`); here the roster holds a DIFFERENT
 * session, so the link names a session by shape (`namesASession`) yet does not
 * resolve — exactly the `text-muted` branch of `MdAnchor`.
 *
 * It must NOT fire on a session that is open. A short name (`chat-2`) resolves
 * through `sessionKeyFromShort`, which returns null in two cases that are not
 * "closed": the message carries no `writtenAtEpoch`, or two open slots share the
 * number (ambiguous). In both the named session may well be open — including the
 * one the reader is in — so "Session not open" would be a false statement. Those
 * cases are the review finding on #18036; they stay silent.
 *
 * The same explanation is also carried to touch / keyboard / AT users, gated by
 * the SAME not-open classification: a native `title` renders only on mouse hover,
 * so those users never saw it (the UX Watch finding). It is a VISUALLY-HIDDEN
 * span inside the anchor, so the link's own text stays the accessible NAME (an
 * `aria-label` would have REPLACED it, making several such links sound identical
 * — the review finding on #18036). The reason reaches AT as a description
 * appended to the name, without claiming a state we cannot verify — the silent
 * cases above stay silent for the hidden text too.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { i18nT } from '../i18n/t'

const NOT_OPEN_TITLE = i18nT('components.markdownRenderer.session_closed_link_disabled')

/** Real slot-key shape (`chat-<n>-<unix-ts>`); `sessionKeyFrom` refuses anything else. */
const OPEN = 'chat-2-1788000001'
/** A full-keyed session the roster does NOT hold: names a session by shape, does not resolve. */
const CLOSED = 'chat-9999-1788000099'

/** A roster that is present (so the renderer IS wired to route sessions) but does
 *  not contain the linked session, so the link falls to the disabled branch. */
const sessions = new Map<string, string>([[OPEN, 'some other session']])

describe('a disabled session link explains itself on hover', () => {
  it('carries the explanatory title on the muted, unresolved branch (full key not open)', () => {
    render(
      <MarkdownRenderer
        content={`see [the old chat](/chat?sid=${CLOSED})`}
        onSessionOpen={vi.fn()}
        sessions={sessions}
        activeSession={OPEN}
      />,
    )

    const anchor = screen.getByText('the old chat').closest('a')!
    // The disabled branch: muted, no live-link affordance.
    expect(anchor).toHaveClass('text-muted')
    // The fix: hover now says why nothing happens.
    const title = anchor.getAttribute('title')
    expect(title).toBeTruthy()
    expect(title).toBe(NOT_OPEN_TITLE)
    // And the same reason reaches touch / keyboard / AT users WITHOUT stealing the
    // link's accessible name: no aria-label (that would replace the text), the
    // name still contains the link text, and a visually-hidden span carries the
    // reason as a trailing description.
    expect(anchor.getAttribute('aria-label')).toBeNull()
    expect(anchor).toHaveAccessibleName(expect.stringContaining('the old chat'))
    expect(anchor).toHaveAccessibleName(expect.stringContaining(NOT_OPEN_TITLE))
    const hidden = anchor.querySelector('.sr-only')
    expect(hidden?.textContent).toContain(NOT_OPEN_TITLE)
  })

  it('carries the title for a SHORT name no open slot answers to', () => {
    // The roster holds `chat-2` but the link names `chat-5`; no open slot has
    // that number, so it genuinely is not open and the tooltip is true. The
    // timestamp reaches the resolver via `messageTs` (an epoch the renderer
    // reads as the message write time).
    render(
      <MarkdownRenderer
        content={'see [a gone chat](/chat?sid=chat-5)'}
        onSessionOpen={vi.fn()}
        sessions={sessions}
        activeSession={OPEN}
        messageTs={1788000050}
      />,
    )
    const anchor = screen.getByText('a gone chat').closest('a')!
    expect(anchor).toHaveClass('text-muted')
    expect(anchor.getAttribute('title')).toBe(NOT_OPEN_TITLE)
    expect(anchor.getAttribute('aria-label')).toBeNull()
    expect(anchor).toHaveAccessibleName(expect.stringContaining('a gone chat'))
    expect(anchor).toHaveAccessibleName(expect.stringContaining(NOT_OPEN_TITLE))
    expect(anchor.querySelector('.sr-only')?.textContent).toContain(NOT_OPEN_TITLE)
  })

  it('stays SILENT for a short name unresolved only because the message has no timestamp', () => {
    // `chat-2` IS in the roster and open, but with no `messageTs` the short
    // lookup fails closed. The session is open, so "not open" must not show.
    render(
      <MarkdownRenderer
        content={'see [that chat](/chat?sid=chat-2)'}
        onSessionOpen={vi.fn()}
        sessions={sessions}
        activeSession={OPEN}
        // messageTs deliberately omitted — no write time
      />,
    )
    const anchor = screen.getByText('that chat').closest('a')!
    expect(anchor.getAttribute('title')).not.toBe(NOT_OPEN_TITLE)
    expect(anchor.getAttribute('aria-label')).toBeNull()
    // No hidden reason, so the accessible name is just the link text.
    expect(anchor.querySelector('.sr-only')).toBeNull()
    expect(anchor).toHaveAccessibleName('that chat')
  })

  it('stays SILENT for an ambiguous short name (two open slots share the number)', () => {
    const ambiguous = new Map<string, string>([
      ['chat-7-1788000010', 'older seven'],
      ['chat-7-1788000020', 'newer seven'],
    ])
    render(
      <MarkdownRenderer
        content={'see [a seven](/chat?sid=chat-7)'}
        onSessionOpen={vi.fn()}
        sessions={ambiguous}
        activeSession={OPEN}
        messageTs={1788000030}
      />,
    )
    const anchor = screen.getByText('a seven').closest('a')!
    expect(anchor.getAttribute('title')).not.toBe(NOT_OPEN_TITLE)
    expect(anchor.getAttribute('aria-label')).toBeNull()
    expect(anchor.querySelector('.sr-only')).toBeNull()
    expect(anchor).toHaveAccessibleName('a seven')
  })

  it('stays SILENT on the active session (open, so "not open" would be false)', () => {
    render(
      <MarkdownRenderer
        content={`see [this chat](/chat?sid=${OPEN})`}
        onSessionOpen={vi.fn()}
        sessions={sessions}
        activeSession={OPEN}
      />,
    )
    const anchor = screen.getByText('this chat').closest('a')!
    // The active key is muted (click is a no-op) but is open, so no "not open" reason.
    expect(anchor.getAttribute('title')).not.toBe(NOT_OPEN_TITLE)
    expect(anchor.getAttribute('aria-label')).toBeNull()
    expect(anchor.querySelector('.sr-only')).toBeNull()
    expect(anchor).toHaveAccessibleName('this chat')
  })
})
