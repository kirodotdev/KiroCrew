/**
 * A failed chunk load of the lazy unrestored-tabs notice stays a notice.
 *
 * The notice is an optional part of the chat pane, loaded in its own chunk. When
 * that chunk fails to load (gateway unreachable, a stale chunk after a rebuild,
 * a browser that keeps the failed fetch), the failure must show as one more
 * block among the pane's notices, never as a card that takes the pane's full
 * height and pushes the session header and transcript out of view. Try Again
 * must run the import again: React.lazy caches a rejected import, so clearing a
 * boundary's error re-renders the same rejection without fetching anything.
 * Reload page is the way back for a browser that keeps failing the same chunk.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPaneNotices from '../pages/chat/page/ChatPaneNotices'
import { i18nT } from '../i18n/t'
import { createTestStore } from './helpers'

const chunk = vi.hoisted(() => ({ fail: true, imports: 0 }))

// The notice's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
vi.mock('../pages/chat/page/UnrestoredTabsNotice', () => {
  chunk.imports += 1
  if (chunk.fail) throw new TypeError('Importing a module script failed.')
  return { default: () => <div data-testid="unrestored-tabs-notice-loaded" /> }
})

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

const PROPS: ComponentProps<typeof ChatPaneNotices> = {
  uploadHint: '',
  setUploadHint: vi.fn(),
  uploadError: '',
  setUploadError: vi.fn(),
  sidError: '',
  setSidError: vi.fn(),
  sidMatches: null,
  setSidMatches: vi.fn(),
  openShortNameMatch: vi.fn(),
  activeSlot: null,
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

afterEach(() => { vi.restoreAllMocks() })

describe('ChatPaneNotices when the unrestored-tabs chunk fails to load', () => {
  it('shows a block notice, not a pane-height card, and Try Again imports the chunk again', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <Provider store={createTestStore()}>
          <MemoryRouter>
            <div data-testid="chat-pane" className="relative flex flex-col h-full overflow-hidden">
              <ChatPaneNotices {...PROPS} />
              <div data-testid="transcript" />
            </div>
          </MemoryRouter>
        </Provider>
      </QueryClientProvider>,
    )
    const tryAgain = await screen.findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(chunk.imports).toBe(1)

    const pane = screen.getByTestId('chat-pane')
    expect.soft(
      pane.querySelector(':scope > .h-full'),
      'the failure takes the chat pane\'s full height',
    ).toBeNull()
    expect.soft(
      screen.queryByText(i18nT('pages.chatPage.could_not_check_restored_tabs')),
      'the failure names what it could not do',
    ).toBeInTheDocument()
    const reload = screen.queryByRole('button', { name: i18nT('components.errorBoundary.reload_page') })
    expect.soft(reload, 'the failure offers Reload page').toBeInTheDocument()
    if (reload) {
      const original = window.location
      const reloadSpy = vi.fn()
      Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload: reloadSpy } })
      try {
        fireEvent.click(reload)
      } finally {
        Object.defineProperty(window, 'location', { configurable: true, value: original })
      }
      expect(reloadSpy).toHaveBeenCalledTimes(1)
    }

    chunk.fail = false
    fireEvent.click(tryAgain)
    expect(
      await screen.findByTestId('unrestored-tabs-notice-loaded', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
      'Try Again brings the notice back once its chunk loads',
    ).toBeInTheDocument()
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
  })
})
