/**
 * DisplayPanel — terminal Shell setting (issue #2831).
 *
 * Pins the settings-side contract of the configurable terminal shell:
 *
 * 1. The input hydrates once from the server config (dashboard.terminal.shell).
 * 2. The value commits on BLUR via PATCH (api.patchConfig), never per keystroke,
 *    so a half-typed path can never land in config.json.
 * 3. A blur with an unchanged value is a no-op (no PATCH fired).
 * 4. A rejected write surfaces the failure hint.
 *
 * The spawn side (backend reading dashboard.terminal.shell for new PTYs) is
 * pre-existing behavior owned by handlers/terminal.py and its own tests.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DisplayPanel } from '../pages/settings/DisplayPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

const zoomCtx = {
  zoom: 100,
  zoomSupported: true,
  zoomIn: vi.fn(),
  zoomOut: vi.fn(),
  reset: vi.fn(),
  family: 'sans',
  setFontFamily: vi.fn(),
  cycleFamily: vi.fn(),
}
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => zoomCtx,
}))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: 'default',
    setColorTheme: vi.fn(),
    allThemes: [{ value: 'default', label: 'Default', custom: false }],
    theme: 'dark',
    themeVersion: 0,
    themeSwitching: false,
    addCustomTheme: vi.fn(),
    deleteCustomTheme: vi.fn(),
    loadCustomThemes: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))

vi.mock('../hooks/useUIMode', () => ({
  useUIMode: () => ({ uiMode: 'chat', setUIMode: vi.fn(), toggleUIMode: vi.fn() }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: { activePct: [60, 60, 60], idlePct: [30, 30, 30] },
  }),
}))

const shellInput = () => screen.getByRole('textbox', { name: 'Shell' })

describe('DisplayPanel – terminal shell setting', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('hydrates the input from dashboard.terminal.shell', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({
      dashboard: { terminal: { shell: '/opt/homebrew/bin/fish' } },
    } as never)
    renderWithProviders(<DisplayPanel />)
    await waitFor(() => expect(shellInput()).toHaveValue('/opt/homebrew/bin/fish'))
  })

  it('commits the trimmed value on blur via PATCH', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: {} } as never)
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)
    await waitFor(() => expect(shellInput()).toHaveValue(''))

    await user.type(shellInput(), '/bin/zsh ')
    expect(patch).not.toHaveBeenCalled() // never per keystroke
    await user.tab()

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith('dashboard.terminal.shell', '/bin/zsh'))
  })

  it('does not PATCH when the value is unchanged on blur', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({
      dashboard: { terminal: { shell: '/bin/zsh' } },
    } as never)
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)
    await waitFor(() => expect(shellInput()).toHaveValue('/bin/zsh'))

    await user.click(shellInput())
    await user.tab()

    expect(patch).not.toHaveBeenCalled()
  })

  it('surfaces the failure hint when the write is rejected', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: {} } as never)
    vi.spyOn(api, 'patchConfig').mockRejectedValue(new Error('400'))
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)
    await waitFor(() => expect(shellInput()).toHaveValue(''))

    await user.type(shellInput(), 'fish')
    await user.tab()

    await waitFor(() =>
      expect(screen.getByText('Could not save the shell. Check the value and try again.')).toBeInTheDocument())
  })
})
