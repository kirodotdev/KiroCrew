/**
 * The compiled chat pane's crash card follows a language switch.
 *
 * `ErrorBoundary` is a class component, so it cannot subscribe through
 * `useLanguageGeneration()`. `ChatPaneNotices` is on the React Compiler opt-in
 * list, and without a changing prop the compiled pane would hand back the same
 * cached boundary element after a switch, leaving a visible crash card in the
 * previous language. The pane passes `languageGeneration` to the boundary; this
 * test makes the lazy unrestored-tabs notice throw so the card shows, switches
 * language with every pane prop unchanged, and expects the card in Chinese.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPaneNotices from '../pages/chat/page/ChatPaneNotices'
import { i18next } from '../i18n'
import { i18nT } from '../i18n/t'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/page/UnrestoredTabsNotice', () => ({
  default: function ThrowingNotice(): never { throw new Error('chunk failed') },
}))

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

afterEach(async () => {
  await act(async () => { await i18next.changeLanguage('en') })
})

describe('ChatPaneNotices crash card under a language switch', () => {
  it('renders the boundary card in the new language with unchanged props', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (
      <QueryClientProvider client={client}>
        <Provider store={createTestStore()}>
          <MemoryRouter>
            <ChatPaneNotices {...PROPS} />
          </MemoryRouter>
        </Provider>
      </QueryClientProvider>
    )
    const { rerender } = render(tree)
    const englishHeading = i18nT('components.errorBoundary.something_went_wrong')
    // The card appears only after the lazy `import()` of the mocked notice
    // resolves, the notice throws and the boundary commits its fallback. A named
    // ceiling for that chain on a loaded worker, not the 1000 ms default.
    expect(await screen.findByText(englishHeading, {}, { timeout: 5000 })).toBeInTheDocument()

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(tree)

    const chineseHeading = i18nT('components.errorBoundary.something_went_wrong')
    expect(chineseHeading).not.toBe(englishHeading)
    expect(screen.getByText(chineseHeading)).toBeInTheDocument()
    expect(screen.queryByText(englishHeading)).toBeNull()
  })
})
