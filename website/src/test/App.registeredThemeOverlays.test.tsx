/**
 * A registered theme's `branding.overlays` mount in the shell until the user
 * turns that theme's Overlays part off (themeConsent.ts REGISTERED_OVERLAYS_PACK).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

const themeState = {
  colorTheme: 'registered-overlay-app',
  theme: 'light' as const,
  mode: 'light' as const,
  onboarded: true,
  importOnboarded: true,
  privacyAcked: true,
  themeBootReady: true,
  themes: [],
  allThemes: [{ value: 'registered-overlay-app', label: 'Registered' }],
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

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'
import { registerThemeBranding } from '../themeBranding'
import { saveExperienceChoice } from '../utils/themeConsent'

registerThemeBranding({ 'registered-overlay-app': { overlays: [() => <div data-testid="registered-overlay" />] } })

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

describe("App – a registered theme's overlays", () => {
  it('mount by default and unmount once Overlays is turned off for that theme', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTestId('registered-overlay')).toBeInTheDocument()
    act(() => {
      saveExperienceChoice('registered-overlay-app', { overlays: false }, null)
    })
    expect(screen.queryByTestId('registered-overlay')).toBeNull()
  })
})
