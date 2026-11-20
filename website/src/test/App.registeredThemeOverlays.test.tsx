/**
 * A registered theme's `branding.overlays` mount in the shell until the user
 * turns that theme's Overlays part off (themeConsent.ts REGISTERED_OVERLAYS_PACK).
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

const themeState = {
  colorTheme: 'registered-overlay-app',
  theme: 'light' as const,
  mode: 'light' as const,
  onboarded: true,
  importOnboarded: true,
  themeBootReady: true,
  themeBootRead: true,
  themes: [],
  allThemes: [{ value: 'registered-overlay-app', label: 'Registered' }],
  preference: 'light' as const,
  setTheme: vi.fn(),
  markOnboarded: vi.fn(),
  markImportOnboarded: vi.fn(),

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
import { setActiveId } from '../store/instancesSlice'

registerThemeBranding({ 'registered-overlay-app': { overlays: [() => <div data-testid="registered-overlay" />] } })

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-import-onboarded', '1')
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

  it("paint in the shell's decor slot, under the top bar", async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const overlay = await screen.findByTestId('registered-overlay')
    await waitFor(() => expect(overlay.parentElement).toBe(screen.getByTestId('theme-decor-slot')))
  })

  it('clip the decor slot around a box marked keep-clear', async () => {
    const composer = document.createElement('div')
    composer.setAttribute('data-guide-keep-clear', '')
    composer.getBoundingClientRect = () => ({
      top: 400, left: 50, bottom: 590, right: 1000, width: 950, height: 190, x: 50, y: 400, toJSON: () => ({}),
    }) as DOMRect
    document.body.appendChild(composer)
    onTestFinished(() => { composer.remove() })
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('registered-overlay')
    const slot = screen.getByTestId('theme-decor-slot')
    await waitFor(() => expect(slot.style.clipPath).toContain('50px 400px, 1000px 400px, 1000px 590px'))
  })

  it("unmount while a remote Crew is shown, since they follow this dashboard's theme", async () => {
    const { store } = renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTestId('registered-overlay')).toBeInTheDocument()
    act(() => { store.dispatch(setActiveId('cd-1')) })
    expect(screen.queryByTestId('registered-overlay')).toBeNull()
    act(() => { store.dispatch(setActiveId(null)) })
    expect(await screen.findByTestId('registered-overlay')).toBeInTheDocument()
  })
})
