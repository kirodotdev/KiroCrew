/**
 * #37165: an embed document (`/embed/chat`, `/embed/sessions`) draws no nav
 * rail, so the shell publishes a 0px rail track there. The chat page's activity
 * panel subtracts that track when it decides beside-or-fill and when it caps
 * its own width, and a phantom 74 or 236px rail made a frame wide enough to
 * seat the panel beside the chat fill it instead, with no resize edge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { useRailWidth, __resetRailWidth } from '../hooks/useRailWidth'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    colorTheme: 'kiro', theme: 'dark', mode: 'dark',
    onboarded: true, importOnboarded: true, privacyAcked: true, privacyAckVersion: null,
    themeBootReady: true, themeBootRead: true, themes: [], allThemes: [],
    markOnboarded: vi.fn(), markImportOnboarded: vi.fn(), markPrivacyAcked: vi.fn(),
    setColorTheme: vi.fn(), setMode: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}))

// The chat page reports the rail track it would size its panel against.
function RailProbe() {
  return <div data-testid="rail-probe" data-rail={useRailWidth()} />
}
vi.mock('../pages/ChatPage', () => ({ default: () => <RailProbe /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'

beforeEach(() => {
  localStorage.clear()
  __resetRailWidth()
})

describe('the rail track the shell publishes', () => {
  it('is 0 in an embedded chat frame, which renders no rail', async () => {
    renderWithProviders(<App />, { route: '/embed/chat/chat-7?sid=chat-7' })
    expect(await screen.findByTestId('rail-probe')).toHaveAttribute('data-rail', '0')
  })

  it('is 0 in an embedded sessions frame, which renders no rail either', async () => {
    renderWithProviders(<App />, { route: '/embed/sessions' })
    expect(await screen.findByTestId('rail-probe')).toHaveAttribute('data-rail', '0')
  })

  it('stays the drawn rail width in the main dashboard', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const probe = await screen.findByTestId('rail-probe')
    expect(Number(probe.getAttribute('data-rail'))).toBeGreaterThan(0)
  })
})
