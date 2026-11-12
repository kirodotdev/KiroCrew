/**
 * First run's chapters write the host's flags, so they wait for the host's own
 * answer: a browser whose first-run state read failed and that holds no flags
 * of its own must not replay first run from an empty localStorage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

const themeState = {
  colorTheme: 'kiro',
  theme: 'dark' as const,
  mode: 'dark' as const,
  // A new browser: no flag of its own says first run is done.
  onboarded: false,
  importOnboarded: false,
  privacyAcked: false,
  privacyAckVersion: null as number | null,
  themeBootReady: true,
  themeBootRead: false,
  themes: [],
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

const importChapterOpen = () => screen.queryByText(/bring your crew with you/i) !== null

beforeEach(() => {
  localStorage.clear()
  themeState.themeBootRead = false
  themeState.importOnboarded = false
  themeState.privacyAcked = false
  themeState.markOnboarded.mockClear()
})

describe('first run and the first-run state read', () => {
  it('stays closed when the read failed and this browser has no flags', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(importChapterOpen()).toBe(false)
  })

  it('closes the tour seeded from this browser\'s flags when the read failed', async () => {
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    themeState.importOnboarded = true
    themeState.privacyAcked = true
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    await waitFor(() => expect(screen.queryByText(/choose a mode, a color theme/i)).toBeNull())
    expect(themeState.markOnboarded).not.toHaveBeenCalled()
  })

  it('opens once the host answers that first run is due', async () => {
    themeState.themeBootRead = true
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByText(/bring your crew with you/i)
  })
})
