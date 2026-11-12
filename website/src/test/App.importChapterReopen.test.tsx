/**
 * Regression: the Import chapter closed itself right after a manual reopen.
 *
 * Import is the only first-run chapter with a manual entry point — the
 * `mc-start-import` event, fired by "import from another agent". The chapter
 * state was also DERIVED from the completion flags:
 *
 *   setShowAgentImport(!importOnboarded)
 *
 * For anyone who already finished import, `importOnboarded` is true, so that
 * line evaluates to `setShowAgentImport(false)`. `AgentImportFlow` syncs on the
 * `initialOpen` EDGE (true→false closes it), so any later run of the derive
 * effect — theme boot resolving, or any flag write — closed the page the user
 * had just opened. The reported symptom is a page that flashes open and
 * disappears.
 *
 * The sequence below is the realistic one: the user clicks while theme boot is
 * still in flight, and boot resolving is what re-runs the derive.
 *
 * The fix makes the derive OPEN-ONLY for this chapter. Nothing is lost: the real
 * completion paths (`onComplete`, `onSkipAll`) already close it themselves, so
 * the `false` branch was redundant for every case except the one it broke.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'

/** Mutable so a test can flip a flag and re-render, the way boot resolving does. */
const themeState = {
  colorTheme: 'kiro',
  theme: 'dark' as const,
  mode: 'dark' as const,
  onboarded: true,
  importOnboarded: true,
  privacyAcked: true,
  privacyAckVersion: null as number | null,
  themeBootReady: false,
  themes: [],
  // Read by the Customize chapter `/onboarding` opens once the import step closes.
  allThemes: [],
  markOnboarded: vi.fn(),
  markImportOnboarded: vi.fn(),
  markPrivacyAcked: vi.fn(),
  setColorTheme: vi.fn(),
  setMode: vi.fn(),
}

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => themeState,
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'
import { api } from '../api/client'

/** The Import chapter renders nothing at all while closed. */
function importChapterOpen(): boolean {
  return screen.queryByText(/bring your crew with you/i) !== null
}

beforeEach(() => {
  themeState.themeBootReady = false
  themeState.importOnboarded = true
  themeState.privacyAcked = true
  themeState.onboarded = true
  themeState.privacyAckVersion = null
})

describe('Import chapter, reopened by hand', () => {
  it('survives the derive effect re-running after theme boot resolves', async () => {
    const { rerender } = renderWithProviders(<App />, { route: '/chat' })

    // Precondition: an onboarded user sees no first-run chapter.
    expect(importChapterOpen()).toBe(false)

    // The manual entry point.
    act(() => { window.dispatchEvent(new CustomEvent('mc-start-import', { detail: {} })) })
    await waitFor(() => expect(importChapterOpen()).toBe(true))

    // Theme boot resolves, which re-runs the chapter-deriving effect. Before the
    // fix this drove initialOpen true→false and closed the page.
    act(() => { themeState.themeBootReady = true })
    rerender(<App />)

    await waitFor(() => expect(importChapterOpen()).toBe(true))
  })

  // #34449: the flags still say the tour is due, so a derive that ran while the
  // replayed importer was open put the Customize tour on top of it.
  it('does not open the tour over the replayed importer when theme boot resolves', async () => {
    themeState.onboarded = false
    const { rerender } = renderWithProviders(<App />, { route: '/chat' })

    act(() => { window.dispatchEvent(new CustomEvent('mc-start-import', { detail: {} })) })
    await waitFor(() => expect(importChapterOpen()).toBe(true))

    act(() => { themeState.themeBootReady = true })
    rerender(<App />)

    await waitFor(() => expect(importChapterOpen()).toBe(true))
    expect(screen.queryByText(/choose a mode, a color theme/i)).toBeNull()
  })

  it('opens a changed Privacy notice that came due under the importer once the importer exits', async () => {
    // An older acknowledged notice and an unreadable status: the notice is due.
    themeState.privacyAckVersion = 0
    const beacon = vi.spyOn(api, 'beaconStatus').mockRejectedValue(new Error('502'))
    const collection = vi.spyOn(api, 'collectionStatus').mockRejectedValue(new Error('502'))
    // "Skip all" records the import chapter as done before it closes.
    const importState = vi.spyOn(api, 'onboardingImportState').mockResolvedValue({} as never)
    onTestFinished(() => { beacon.mockRestore(); collection.mockRestore(); importState.mockRestore() })
    const { rerender } = renderWithProviders(<App />, { route: '/chat' })

    act(() => { window.dispatchEvent(new CustomEvent('mc-start-import', { detail: {} })) })
    await waitFor(() => expect(importChapterOpen()).toBe(true))
    act(() => { themeState.themeBootReady = true })
    rerender(<App />)
    await waitFor(() => expect(api.collectionStatus).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByRole('dialog', { name: 'Privacy' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Skip all setup and onboarding' }))
    // Only the right column changes: the shared shell keeps the import art (#32063).
    expect(await screen.findByRole('dialog', { name: 'Privacy' })).toBeInTheDocument()
  })

  it('keeps the tour /onboarding asked for after passing a notice that came due under the importer', async () => {
    themeState.privacyAckVersion = 0
    const beacon = vi.spyOn(api, 'beaconStatus').mockRejectedValue(new Error('502'))
    const collection = vi.spyOn(api, 'collectionStatus').mockRejectedValue(new Error('502'))
    const importState = vi.spyOn(api, 'onboardingImportState').mockResolvedValue({} as never)
    // Held until the notice is due, so the import step ends under it.
    let finishScan: (value: unknown) => void = () => {}
    const scan = vi.spyOn(api, 'onboardingImportScan').mockReturnValue(
      new Promise(resolve => { finishScan = resolve }) as never,
    )
    onTestFinished(() => {
      beacon.mockRestore(); collection.mockRestore(); importState.mockRestore(); scan.mockRestore()
    })
    const { rerender } = renderWithProviders(<App />, { route: '/chat' })

    act(() => {
      window.dispatchEvent(new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }))
    })
    await waitFor(() => expect(importChapterOpen()).toBe(true))
    act(() => { themeState.themeBootReady = true })
    rerender(<App />)
    await waitFor(() => expect(api.collectionStatus).toHaveBeenCalled())
    await act(async () => {})

    // An empty scan ends the `/onboarding` import step by itself.
    await act(async () => { finishScan({ sources: [], skipped: [], merge_only: true }) })
    const dialog = await screen.findByRole('dialog', { name: 'Privacy' })
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue without changes' }))

    expect(await screen.findByText('Pick your look')).toBeInTheDocument()
    await act(async () => {})
    expect(screen.getByText('Pick your look')).toBeInTheDocument()
  })

  it('still opens the chapter on its own for a user who has not finished import', async () => {
    themeState.importOnboarded = false
    themeState.themeBootReady = true
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(importChapterOpen()).toBe(true))
  })
})
