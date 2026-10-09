/**
 * A fresh install's first run ends on Captain's chat. This mounts App so the
 * callback App hands to the first-run chapters is the one under test: the
 * landing rule itself is covered beside `shell/boot/captainLanding.ts`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

const themeState = {
  colorTheme: 'kiro',
  theme: 'dark' as const,
  mode: 'dark' as const,
  onboarded: false,
  importOnboarded: true,
  privacyAcked: true,
  themeBootReady: true,
  themes: [],
  allThemes: [] as Array<{ value: string; label: string }>,
  preference: 'dark' as const,
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
vi.mock('../pages/members/MembersPage', () => ({ default: () => <div data-testid="members-page">MembersPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { renderWithProviders } from './helpers'
import App from '../App'
import { api } from '../api/client'

const CAPTAIN = { name: 'kirocrew-captain', kiro_agent: 'kirocrew-captain', display_name: 'Captain' }

async function skipFirstRun() {
  const skip = await screen.findByRole('button', { name: /skip all setup and onboarding/i })
  fireEvent.click(skip)
}

beforeEach(() => {
  themeState.onboarded = false
  themeState.markOnboarded.mockReset()
  vi.spyOn(api, 'kirocrewAgents').mockResolvedValue({ agents: [CAPTAIN] } as never)
})

describe('first run on a fresh install', () => {
  it('ends on Captain\'s chat when opened on the default landing', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    await skipFirstRun()
    expect(themeState.markOnboarded).toHaveBeenCalled()
    expect(await screen.findByTestId('members-page')).toBeInTheDocument()
  })

  it('stays on a session link the user opened', async () => {
    renderWithProviders(<App />, { route: '/chat/new-session?sid=chat-9-9' })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    await skipFirstRun()
    expect(themeState.markOnboarded).toHaveBeenCalled()
    expect(screen.queryByTestId('members-page')).not.toBeInTheDocument()
  })
})
