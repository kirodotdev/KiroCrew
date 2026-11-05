/**
 * A scripted send's `meta.banner` (POST /api/chat `banner`): the bubble shows
 * the short label, and the full message the agent received opens behind a
 * disclosure. A row without one renders its content as before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import React from 'react'
import UserMessage from '../pages/chat/UserMessage'
import SearchHighlightContext, { MessageSearchScope } from '../hooks/SearchHighlightContext'
import { getCurrentSearchRange } from '../utils/domHighlight'
import { prependQuote } from '../chat-core/composer/messageQuote'

const FULL = 'Work order: rebase CR-1 onto mainline, rerun the build, report the result.'
const LABEL = 'Rebase CR-1 and rebuild'

function renderRow(meta?: Record<string, unknown>) {
  return render(
    <UserMessage
      content={FULL}
      meta={meta}
      messageTs="2026-11-03T19:00:00Z"
      renderContent={c => <p data-testid="rendered-content">{c}</p>}
    />,
  )
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UserMessage banner', () => {
  it('shows the label and opens the full message on demand', () => {
    renderRow({ banner: LABEL })

    expect(screen.getByTestId('user-message-banner').textContent).toBe(LABEL)
    expect(screen.queryByText(FULL)).toBeNull()
    const toggle = screen.getByTestId('user-message-banner-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggle)

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('rendered-content').textContent).toBe(FULL)
  })

  it('marks a banner row as sent by a script, above the label', () => {
    renderRow({ banner: LABEL })

    const marker = screen.getByTestId('user-message-banner-from')
    expect(marker.textContent).toBe('Sent by a script')
    expect(marker.compareDocumentPosition(screen.getByTestId('user-message-banner')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it.each([undefined, {}, { banner: '   ' }, { banner: 42 }])(
    'renders the content as before without a usable label (%o)',
    meta => {
      renderRow(meta as Record<string, unknown> | undefined)

      expect(screen.getByTestId('rendered-content').textContent).toBe(FULL)
      expect(screen.queryByTestId('user-message-banner')).toBeNull()
      expect(screen.queryByTestId('user-message-banner-from')).toBeNull()
    },
  )
})

describe('UserMessage banner and transcript search', () => {
  function renderSearched(currentOccurrenceIdx: number) {
    return render(
      <SearchHighlightContext.Provider value={{ term: 'rebase', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx }}>
        <MessageSearchScope messageIdx={0}>
          <UserMessage
            content={FULL}
            meta={{ banner: LABEL }}
            messageTs="2026-11-03T19:00:00Z"
            renderContent={c => <p data-testid="rendered-content">{c}</p>}
          />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
  }

  it('opens the full message when it holds the active match and marks the match inside it', () => {
    renderSearched(0)

    expect(screen.getByTestId('user-message-banner-toggle').getAttribute('aria-expanded')).toBe('true')
    const range = getCurrentSearchRange()
    expect(range?.toString().toLowerCase()).toBe('rebase')
    expect(screen.getByTestId('user-message-banner-full').contains(range!.startContainer)).toBe(true)
  })

  it('counts a carried quote and the full message in order, never the label or toggle', () => {
    const quote = { role: 'assistant' as const, text: 'alpha from the reply' }
    const content = prependQuote('alpha in the work order', quote)
    const view = (occ: number) => (
      <SearchHighlightContext.Provider value={{ term: 'alpha', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: occ }}>
        <MessageSearchScope messageIdx={0}>
          <UserMessage
            content={content}
            meta={{ banner: 'alpha label', quote }}
            messageTs="2026-11-03T19:00:00Z"
            renderContent={c => <p data-testid="rendered-content">{c}</p>}
          />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>
    )
    const { rerender } = render(view(0))
    expect(screen.getByTestId('user-message-banner').contains(getCurrentSearchRange()!.startContainer)).toBe(false)
    expect(screen.getByTestId('user-message-banner-full').contains(getCurrentSearchRange()!.startContainer)).toBe(false)

    rerender(view(1))
    expect(screen.getByTestId('user-message-banner-full').contains(getCurrentSearchRange()!.startContainer)).toBe(true)

    rerender(view(2))
    expect(getCurrentSearchRange()).toBeNull()
  })

  it('leaves the row collapsed when the active match is elsewhere', () => {
    renderSearched(-1)

    expect(screen.getByTestId('user-message-banner-toggle').getAttribute('aria-expanded')).toBe('false')
  })
})
