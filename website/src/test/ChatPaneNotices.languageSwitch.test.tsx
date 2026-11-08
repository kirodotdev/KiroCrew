/**
 * The chat pane's notices follow a language switch while the React Compiler
 * compiles the component (it is on the opt-in list in react-compiler.config.mjs,
 * and vitest runs listed files compiled).
 *
 * `i18nT()` and `fmtDate()` read the active catalog without being reactive
 * inputs, so a string the compiler memoized would survive a switch. The test
 * holds every prop referentially identical across the switch and re-renders the
 * same element, which is what `LanguageProvider`'s repaint does to an unchanged
 * subtree: only the language generation can make the strings change.
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

const PROPS: Props = {
  uploadHint: 'Too many files',
  setUploadHint: vi.fn(),
  uploadError: 'Upload failed',
  setUploadError: vi.fn(),
  sidError: '',
  setSidError: vi.fn(),
  sidMatches: null,
  setSidMatches: vi.fn(),
  openShortNameMatch: vi.fn(),
  activeSlot: 'slot-a',
  provider: { capabilities: { reasoningEffort: false } } as never,
  selectionCapabilitiesQ: { isError: false },
  chipDefault: { failed: true },
  actionError: null,
  setActionError: vi.fn(),
  switchSlotGone: { name: 'Old chat', kind: 'gone' },
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

describe('ChatPaneNotices under a language switch', () => {
  it('rebuilds its strings in the new language with unchanged props', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore()
    const tree = (
      <QueryClientProvider client={client}>
        <Provider store={store}>
          <MemoryRouter>
            <ChatPaneNotices {...PROPS} />
          </MemoryRouter>
        </Provider>
      </QueryClientProvider>
    )
    const { rerender } = render(tree)
    const englishConfig = i18nT('pages.settings.chatPanel.failed_to_load_config')
    const englishNoticeDismiss = i18nT('components.errorNotice.dismiss')
    expect(screen.getByText(englishConfig)).toBeInTheDocument()
    // The upload-error notice's props hold no translated string, so only the
    // child's own subscription can move its dismiss label to the new language.
    const uploadNotice = screen.getByTestId('upload-error')
    expect(within(uploadNotice).getByRole('button', { name: englishNoticeDismiss })).toBeInTheDocument()

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(tree)

    const chineseConfig = i18nT('pages.settings.chatPanel.failed_to_load_config')
    expect(chineseConfig).not.toBe(englishConfig)
    expect(screen.getByText(chineseConfig)).toBeInTheDocument()
    expect(screen.queryByText(englishConfig)).toBeNull()
    expect(screen.getAllByRole('button', { name: i18nT('app.dismiss') }).length).toBeGreaterThan(0)
    const chineseNoticeDismiss = i18nT('components.errorNotice.dismiss')
    expect(chineseNoticeDismiss).not.toBe(englishNoticeDismiss)
    expect(within(screen.getByTestId('upload-error')).getByRole('button', { name: chineseNoticeDismiss })).toBeInTheDocument()
  })
})
