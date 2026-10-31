/**
 * The gone-session pane notice (#14762): a session reached by a nav arrow /
 * deep link that was CLOSED since it was visited is not destroyed — closing is
 * the routine end of a session and its transcript survives under Older Sessions
 * ("Already finished — transcript under Older Sessions"). So the notice must not
 * claim the session was "deleted", and it must offer a route to the surviving
 * transcript.
 *
 * Renders the real ChatPaneNotices (same harness as the language-switch suite)
 * and asserts on the live i18n catalog, so a regression in the copy or a
 * dropped footer action fails here.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPaneNotices from '../pages/chat/page/ChatPaneNotices'
import { i18next } from '../i18n'
import { i18nT } from '../i18n/t'
import { createTestStore } from './helpers'

type Props = ComponentProps<typeof ChatPaneNotices>

const BASE: Props = {
  uploadHint: '',
  setUploadHint: vi.fn(),
  uploadError: '',
  setUploadError: vi.fn(),
  sidError: '',
  setSidError: vi.fn(),
  sidMatches: null,
  setSidMatches: vi.fn(),
  openShortNameMatch: vi.fn(),
  activeSlot: 'slot-a',
  provider: { capabilities: { reasoningEffort: false } } as never,
  selectionCapabilitiesQ: { isError: false },
  chipDefault: { failed: false },
  actionError: null,
  setActionError: vi.fn(),
  switchSlotGone: null,
  setVoiceRecoverySlot: vi.fn(),
  pinError: null,
  pinStatus: null,
  dismissPinStatus: vi.fn(),
  unresumableResume: null,
  undeletableHistory: null,
  dispatch: vi.fn() as never,
}

function renderNotices(props: Props) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore()
  return render(
    <QueryClientProvider client={client}>
      <Provider store={store}>
        <MemoryRouter>
          <ChatPaneNotices {...props} />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

afterEach(async () => {
  await act(async () => { await i18next.changeLanguage('en') })
  vi.clearAllMocks()
})

describe('ChatPaneNotices gone-session notice → Older Sessions', () => {
  it('does not claim a closed session was deleted', () => {
    renderNotices({ ...BASE, switchSlotGone: { name: '', kind: 'gone', gestureNeutral: true } })
    const notice = screen.getByTestId('switch-slot-gone')
    expect(within(notice).queryByText(/deleted/i)).toBeNull()
    // Both the body copy and the footer link name Older Sessions.
    expect(within(notice).getAllByText(/Older Sessions/i).length).toBeGreaterThan(0)
  })

  it('offers an Open Older Sessions action pointing at the history pane', () => {
    renderNotices({ ...BASE, switchSlotGone: { name: '', kind: 'gone', gestureNeutral: true } })
    const link = screen.getByTestId('switch-slot-gone-older-sessions')
    expect(link).toHaveTextContent(i18nT('pages.chatPage.open_older_sessions'))
    // The history pane answers `?history=1` on arrival; an <a> keeps the
    // browser's own navigation affordances (middle-click, copy-link).
    expect(link).toHaveAttribute('href', '/chat?history=1')
    expect(link.tagName).toBe('A')
  })

  it('does NOT offer Older Sessions for a row-click / named gone notice', () => {
    // A gone notice WITHOUT `gestureNeutral` is the session-row / named
    // (`announceOnMissing`) path, whose pre-existing copy reads "… was deleted
    // and cannot be opened". Offering "Open Older Sessions" under that sentence
    // would contradict it, so the footer is gated OFF here (UX Review blocker).
    renderNotices({ ...BASE, switchSlotGone: { name: 'Beta', kind: 'gone' } })
    const notice = screen.getByTestId('switch-slot-gone')
    expect(within(notice).getByText(/deleted and cannot be opened/i)).toBeInTheDocument()
    expect(screen.queryByTestId('switch-slot-gone-older-sessions')).toBeNull()
  })

  it('does NOT offer Older Sessions for the unnamed row-click gone notice', () => {
    renderNotices({ ...BASE, switchSlotGone: { name: '', kind: 'gone' } })
    expect(screen.getByTestId('switch-slot-gone')).toBeInTheDocument()
    expect(screen.queryByTestId('switch-slot-gone-older-sessions')).toBeNull()
  })

  it('does NOT offer Older Sessions for a genuine open failure (kind: failed)', () => {
    renderNotices({ ...BASE, switchSlotGone: { name: '', kind: 'failed' } })
    expect(screen.getByTestId('switch-slot-gone')).toBeInTheDocument()
    expect(screen.queryByTestId('switch-slot-gone-older-sessions')).toBeNull()
  })
})
