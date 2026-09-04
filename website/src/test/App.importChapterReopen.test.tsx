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
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

/** Mutable so a test can flip a flag and re-render, the way boot resolving does. */
const themeState = {
  colorTheme: 'kiro',
  theme: 'dark' as const,
  mode: 'dark' as const,
  onboarded: true,
  importOnboarded: true,
  privacyAcked: true,
  themeBootReady: false,
  themes: [],
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
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'

/** The Import chapter renders nothing at all while closed. */
function importChapterOpen(): boolean {
  return screen.queryByText(/bring your crew with you/i) !== null
}

beforeEach(() => {
  themeState.themeBootReady = false
  themeState.importOnboarded = true
  themeState.privacyAcked = true
  themeState.onboarded = true
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

  it('still opens the chapter on its own for a user who has not finished import', async () => {
    themeState.importOnboarded = false
    themeState.themeBootReady = true
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(importChapterOpen()).toBe(true))
  })
})
