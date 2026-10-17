/**
 * The look-preview frame (utils/lookPreview.ts) is the real dashboard, scaled
 * down inside the first-run "Pick your look" step. Nothing may open OVER it:
 * a launch dialog that mounted there would render inside the picture, and no
 * test of that dialog would notice.
 *
 * App.tsx therefore mounts every self-opening launch surface from ONE block,
 * left out when `isLookPreviewFrame()` is true. This test is the pin for that
 * block: it boots App in frame mode with every surface's trigger "due" -- a
 * user who has not finished first run -- and asserts that no dialog is on
 * screen; then the same boot outside the frame, where the first-run chapter
 * DOES open, so the assertion is not vacuous.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

const themeState = {
  colorTheme: 'kiro',
  theme: 'light' as const,
  mode: 'light' as const,
  // First run NOT finished: outside the frame this opens the Customize tour.
  onboarded: false,
  importOnboarded: true,
  privacyAcked: true,
  themeBootReady: true,
  themes: [],
  // What the Pick your look step itself reads once it opens (the control case).
  allThemes: [{ value: 'kiro', label: 'Kiro' }],
  preference: 'light' as const,
  setTheme: vi.fn(),
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

// The frame flag, switched per test. Mocked at the module rather than through
// `window.location` because happy-dom's location getters do not survive a spread.
const frame = { on: false }
vi.mock('../utils/lookPreview', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/lookPreview')>()
  return { ...mod, isLookPreviewFrame: () => frame.on }
})

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

afterEach(() => { frame.on = false })

describe('App inside the look-preview frame', () => {
  it('mounts no launch dialog, even with first run still owed', async () => {
    frame.on = true
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(screen.queryAllByRole('dialog')).toHaveLength(0)
    expect(screen.queryByText('Pick your look')).toBeNull()
  })

  it('outside the frame the same boot opens the first-run chapter (the control)', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    await waitFor(() => expect(screen.getByText('Pick your look')).toBeInTheDocument())
    expect(screen.getAllByRole('dialog').length).toBeGreaterThan(0)
  })
})
