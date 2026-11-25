/**
 * The two error notices in the composer dock sit on the dock's glass.
 *
 * The dock floats over the transcript. On its own the inline notice is bare
 * text and the block notice a translucent tint, so a transcript line scrolled
 * under the dock shows through the notice's words. Each one is pinned to the
 * `thick` glass pane the Read aloud bar wears; the block notice draws no
 * translucent box of its own on that pane.
 */
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, renderHook, screen } from '@testing-library/react'

vi.mock('../components/VoicePlaybackBar', () => ({ default: () => null }))

import { COMPOSER_RADIUS } from '../components/composerRadius'
import { useComposerAboveBand } from '../pages/chat/page/composerDock'

type BandOptions = Parameters<typeof useComposerAboveBand>[0]

function renderBand(controls: Partial<BandOptions['controls']>, rest: Partial<BandOptions> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  const { result } = renderHook(() => useComposerAboveBand({
    langGen: 0,
    controls: {
      sessionControls: [], sessionControlsError: null, sessionControlStatusError: null,
      chatFolders: [], chatFoldersError: null, folderSortMode: 'manual', folderSortError: null,
      ...controls,
    } as unknown as BandOptions['controls'],
    folderSuggestion: undefined,
    activeSlot: 'slot-1',
    sidebarOnScreen: true,
    folderSuggestionAccept: () => {},
    folderSuggestionUndo: () => {},
    folderSuggestionDecline: () => {},
    slotFolderId: undefined,
    slotProject: '',
    setProject: async () => {},
    projectLocked: false,
    activeTip: null,
    dismissTip: () => {},
    onSendNow: () => {},
    ...rest,
  }), { wrapper })
  render(result.current, { wrapper })
}

function expectOnDockGlass(notice: HTMLElement) {
  // The same material step as the Read aloud bar beside it.
  expect(notice.parentElement).toHaveClass('liquid-glass', 'glass-thick')
}

describe('composer dock error notices', () => {
  it('draws the session-controls failure on the dock glass', () => {
    renderBand({ sessionControlsError: new Error('apps route failed') })
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('Session controls unavailable')
    expect(notice).toHaveTextContent('apps route failed')
    expectOnDockGlass(notice)
    // Red text on a red-mixed pane falls below AA in some dark palettes.
    expect(notice.parentElement).not.toHaveClass('glass-danger')
  })

  it('draws the folder-order failure on the dock glass, with no box of its own', () => {
    renderBand({ folderSortError: 'gateway restarting' }, {
      sidebarOnScreen: false,
      folderSuggestion: { folderId: 'home', folderName: 'Home', breadcrumb: 'Home', ts: 100, turns: 0 } as BandOptions['folderSuggestion'],
    })
    const notice = screen.getByTestId('folder-suggestion-order-unavailable')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('gateway restarting')
    expectOnDockGlass(notice)
    expect(notice.parentElement).toHaveClass('glass-danger')
    // As round as the folder suggestion card under it.
    expect(notice.parentElement?.style.borderRadius).toBe(`${COMPOSER_RADIUS}px`)
    expect(notice.className).not.toMatch(/\bbg-danger-subtle\b/)
  })
})
