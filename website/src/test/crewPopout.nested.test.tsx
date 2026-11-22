/**
 * A crew's chat pop-out framed by a desktop hub's `/popout/crew` window learns
 * its host through the pop-out branch's EmbeddedHostBridge, so a pop-out opened
 * from inside it (a side-panel file) opens another hub-framed window instead of
 * going to the system browser without its token.
 */
import { describe, it, expect, vi, onTestFinished } from 'vitest'
import { act, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    colorTheme: 'kiro', theme: 'dark', mode: 'dark',
    onboarded: true, importOnboarded: true,
    themeBootReady: true, themeBootRead: true, themes: [], allThemes: [],
    markOnboarded: vi.fn(), markImportOnboarded: vi.fn(),
    setColorTheme: vi.fn(), setMode: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: () => true }))
vi.mock('../pages/PopoutFrame', async () => {
  const { openFilePopout } = await import('../utils/filePopout')
  return { default: () => <button type="button" onClick={() => openFilePopout('/w/notes.md', 's1')}>open file</button> }
})

import { renderWithProviders } from './helpers'
import App from '../App'

const HUB = 'http://127.0.0.1:7777'

describe('a nested pop-out inside a framed desktop pop-out', () => {
  it('opens through the hub framing page', async () => {
    const original = window.location.pathname + window.location.search
    window.history.replaceState(null, '', '/popout/chat/s1?sid=s1')
    onTestFinished(() => window.history.replaceState(null, '', original))
    vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    onTestFinished(() => vi.restoreAllMocks())

    renderWithProviders(<App />, { route: '/popout/chat/s1?sid=s1' })
    const button = await screen.findByRole('button', { name: 'open file' })
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        source: window.parent, origin: HUB, data: { type: 'mc-host-model', v: 1, tabs: [], electron: true, crewPopout: 1 },
      }))
    })
    await userEvent.click(button)

    expect(open).toHaveBeenCalledTimes(1)
    const url = new URL(String(open.mock.calls[0][0]))
    expect(url.origin + url.pathname).toBe(`${HUB}/popout/crew`)
    expect(url.searchParams.get('port')).toBe(window.location.port)
    expect(url.searchParams.get('path')).toBe('/popout/file?path=%2Fw%2Fnotes.md&sid=s1')
  })
})
