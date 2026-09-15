import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Terminal: the Default shell field.
 *
 * Its own file because it needs the api client mocked (the shared
 * DisplayPanel.test.tsx deliberately runs against the real client): the field
 * is server-persisted (dashboard.terminal.shell — the gateway host spawns the
 * shell, unlike the client-local terminal font beside it), drafted locally,
 * and committed on blur, with the backend's executable-check rejection
 * surfaced inline and the draft kept so the user can fix the typo.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => {
  /** Minimal stand-in with the same shape the panel reads (status + body). */
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    api: {
      kirocrewConfig: kirocrewConfigMock,
      patchConfig: patchConfigMock,
      installTheme: vi.fn(() => Promise.resolve({ ok: true })),
    },
    ApiError,
  }
})

// Same provider doubles as DisplayPanel.test.tsx — the panel reads all of
// these on render and none is under test here.
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
  useUIMode: () => ({
    uiMode: 'chat',
    setUIMode: vi.fn(),
    toggleUIMode: vi.fn(),
  }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: {
      activePct: [60, 60, 60],
      idlePct: [30, 30, 30],
    },
  }),
}))

import { DisplayPanel } from '../pages/settings/DisplayPanel'

function seed(shell: string) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: { terminal: { shell } } }),
  )
}

describe('DisplayPanel → Terminal default shell', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed('')
  })

  it('renders the field seeded from the server config', async () => {
    seed('/usr/bin/fish')
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('/usr/bin/fish'))
  })

  it('PATCHes the trimmed value on blur, and not per keystroke', async () => {
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '  /usr/bin/fish ' } })
    expect(patchConfigMock).not.toHaveBeenCalled()
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.terminal.shell', '/usr/bin/fish'),
    )
  })

  it('does not PATCH on blur when the value is unchanged', async () => {
    seed('/usr/bin/fish')
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('/usr/bin/fish'))
    fireEvent.blur(input)
    // Give a microtask turn for any (wrong) mutation to fire.
    await waitFor(() => expect(patchConfigMock).not.toHaveBeenCalled())
  })

  it('surfaces a code-mapped catalog message inline and keeps the draft', async () => {
    const { ApiError } = await import('../api/client')
    patchConfigMock.mockImplementation(() =>
      Promise.reject(
        new ApiError(
          400,
          'must be an executable shell',
          JSON.stringify({ error: 'must be an executable shell', code: 'shell_not_executable' }),
        ),
      ),
    )
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '/opt/typo' } })
    fireEvent.blur(input)
    // The CATALOG copy renders — never the backend's raw English sentence,
    // which would ship untranslated into every non-English locale.
    await waitFor(() =>
      expect(screen.getByText(/Not an executable shell/)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/must be an executable shell/)).not.toBeInTheDocument()
    // The rejected draft stays editable — the user fixes the typo, not retypes.
    expect(input.value).toBe('/opt/typo')
  })

  it('falls back to the generic catalog message when no code is present', async () => {
    patchConfigMock.mockImplementation(() => Promise.reject(new Error('boom')))
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '/opt/typo' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(screen.getByText(/Could not save the shell setting/)).toBeInTheDocument(),
    )
  })

  it('keeps showing the saved value after a successful commit (no blink-back)', async () => {
    // Stateful mocks: the refetch onSettled triggers must return what was
    // saved, as the real backend does — otherwise the optimistic write and
    // the refetch cannot be told apart.
    let stored = ''
    patchConfigMock.mockImplementation(((_path: string, value: string) => {
      stored = value
      return Promise.resolve({})
    }) as never)
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ dashboard: { terminal: { shell: stored } } }),
    )
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '/usr/bin/fish' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.terminal.shell', '/usr/bin/fish'),
    )
    // Draft cleared, optimistic cache + refetch both carry the new value.
    await waitFor(() => expect(input.value).toBe('/usr/bin/fish'))
  })
})
