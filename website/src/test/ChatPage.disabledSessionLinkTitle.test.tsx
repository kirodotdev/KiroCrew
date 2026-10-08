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
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { i18nT } from '../i18n/t'

/** Real slot-key shape (`chat-<n>-<unix-ts>`); `sessionKeyFrom` refuses anything else. */
const OPEN = 'chat-2-1788000001'
/** A full-keyed session the roster does NOT hold: names a session by shape, does not resolve. */
const CLOSED = 'chat-9999-1788000099'

/** A roster that is present (so the renderer IS wired to route sessions) but does
 *  not contain the linked session, so the link falls to the disabled branch. */
const sessions = new Map<string, string>([[OPEN, 'some other session']])

describe('a disabled session link explains itself on hover', () => {
  it('carries a non-empty title on the muted, unresolved branch', () => {
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
    expect(title).toBe(i18nT('components.markdownRenderer.session_closed_link_disabled'))
  })
})
