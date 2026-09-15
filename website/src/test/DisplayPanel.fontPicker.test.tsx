import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { DisplayPanel } from '../pages/settings/DisplayPanel'
import { renderWithProviders } from './helpers'

/**
 * The terminal font picker's honesty contract.
 *
 * The row exists because typing a family name blind renders the WRONG font
 * silently — the resolved stack falls back to generic monospace, so a typo looks
 * plausible. That makes the free-text escape hatch the one place the same defect
 * can come back: styling its preview with a family the machine does not have
 * renders the sample in that same fallback, which is indistinguishable from a
 * confirmed font and quietly confirms the typo.
 *
 * Detection is stubbed at the module boundary because the test environment's
 * canvas measures every font stack identically (its `measureText` never reads
 * `ctx.font`), so a real probe cannot distinguish installed from missing here.
 */
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => ({
    zoom: 100, zoomSupported: true, zoomIn: vi.fn(), zoomOut: vi.fn(),
    reset: vi.fn(), family: 'sans', setFontFamily: vi.fn(), cycleFamily: vi.fn(),
  }),
}))

// Mock useTheme — provides color theme state. ThemeProvider is a passthrough
// so renderWithProviders (in helpers.tsx) can still wrap children without
// pulling in the real provider's state machine. `mockUseTheme` is mutable so a
// test can flip `themeSwitching` on; a top-level beforeEach restores the default.
const { mockUseTheme, DEFAULT_THEME } = vi.hoisted(() => {
  const DEFAULT_THEME = {
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
  }
  return { mockUseTheme: vi.fn(() => DEFAULT_THEME), DEFAULT_THEME }
})
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => mockUseTheme(),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))

// Reset to the default theme shape before every test in this file (runs before
// the describe-scoped beforeEach hooks). clearAllMocks keeps implementations.
beforeEach(() => {
  mockUseTheme.mockReset()
  mockUseTheme.mockImplementation(() => DEFAULT_THEME)
})

// Mock useUIMode — provides chat/cli interface paradigm. UIModeProvider is a
// passthrough so the test doesn't need real provider wiring.
vi.mock('../hooks/useUIMode', () => ({
  useUIMode: () => ({
    uiMode: 'chat',
    setUIMode: vi.fn(),
    toggleUIMode: vi.fn(),
  }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

// Mock useSessionPalette — provides sidebar color palette data
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

const INSTALLED = 'Hack'

vi.mock('../utils/fontDetect', async () => {
  const actual = await vi.importActual<typeof import('../utils/fontDetect')>('../utils/fontDetect')
  return {
    ...actual,
    detectInstalledFonts: vi.fn(() => [INSTALLED]),
    isFontInstalled: vi.fn((family: string) => family === INSTALLED),
    isLocalFontAccessSupported: vi.fn(() => false),
    queryLocalMonospaceFonts: vi.fn(() => Promise.resolve({ ok: true, families: [] })),
  }
})

const detect = vi.mocked(await import('../utils/fontDetect'))

beforeEach(() => {
  detect.detectInstalledFonts.mockReturnValue([INSTALLED])
  detect.isFontInstalled.mockImplementation((family: string) => family === INSTALLED)
  detect.isLocalFontAccessSupported.mockReturnValue(false)
  detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: true, families: [] })
})

async function openFontPicker() {
  renderWithProviders(<DisplayPanel />)
  const trigger = screen.getByRole('button', { name: 'Font' })
  fireEvent.click(trigger)
  await screen.findByRole('listbox', { name: 'Font' })
  return screen.getByRole('textbox', { name: 'Search fonts…' })
}

describe('terminal font picker — free-text row never fakes a preview', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('marks a typed family the probe could not confirm', async () => {
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: 'Berkeley Mono' } })
    const row = screen.getByRole('option', { name: /Berkeley Mono/ })
    expect(row).toHaveTextContent('Not detected on this machine')
  })

  it('gives an unconfirmed name NO font styling, so it cannot pass as installed', async () => {
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: 'Berkeley Mono' } })
    // A row's own text rendered in its own family IS the preview here, so an
    // unconfirmed name must not carry it: styled with a family the machine lacks,
    // the text renders in the fallback and reads as a confirmed font.
    expect(screen.getByText(/Use “Berkeley Mono”/))
      .not.toHaveStyle({ fontFamily: "'Berkeley Mono', monospace" })
  })

  it('previews a family the probe DID confirm, in that family', async () => {
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: INSTALLED } })
    // Matches the detected option outright, so no free-text row is offered.
    expect(screen.getByText(INSTALLED)).toHaveStyle({ fontFamily: `'${INSTALLED}', monospace` })
  })

  it('commits the unconfirmed name anyway — it may be installed later', async () => {
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: 'Berkeley Mono' } })
    fireEvent.click(screen.getByRole('option', { name: /Berkeley Mono/ }))
    const persisted = JSON.parse(localStorage.getItem('mc-terminal-font') || '{}')
    expect(persisted.fontFamily).toBe('Berkeley Mono')
  })

  it('never folds a specimen into the trigger once a font is picked', async () => {
    // The trigger renders `<label> (<sublabel>)` for a selected option, so a
    // specimen string attached to a font row would leak into the closed control
    // as "JetBrains Mono (0O1lI)". Font rows carry no sublabel; the note on an
    // unconfirmed name is the one exception, and that name is not a detected row.
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: INSTALLED } })
    fireEvent.click(screen.getByRole('option', { name: new RegExp(INSTALLED) }))
    expect(screen.getByRole('button', { name: 'Font' }))
      .toHaveTextContent(new RegExp(`^${INSTALLED}$`))
  })
})

describe('terminal font picker — enumeration action', () => {
  it('is not offered where the Local Font Access API is absent', async () => {
    await openFontPicker()
    expect(screen.queryByRole('button', { name: /List all installed fonts/ })).not.toBeInTheDocument()
  })

  it('reports a successful grant even when the filter hides every font it added', async () => {
    // The user most likely to run this action is the one whose filter matched
    // nothing. Fonts get added but the same filter hides them, so without a
    // status the popup is pixel-identical after the permission dialog and the
    // grant reads as a failure.
    detect.isLocalFontAccessSupported.mockReturnValue(true)
    detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: true, families: ['Zed Mono'] })
    const box = await openFontPicker()
    fireEvent.change(box, { target: { value: 'zzz-no-such-font' } })
    fireEvent.click(screen.getByRole('button', { name: /List all installed fonts/ }))
    expect(await screen.findByText('Installed fonts added to the list.')).toBeInTheDocument()
  })
})

describe('terminal font picker — bundled fonts are always selectable', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('lists OpenDyslexicMono as a selectable option regardless of OS-detected list', async () => {
    // The OS detection mock returns only INSTALLED ('Hack'), NOT OpenDyslexicMono
    // — this test proves the bundled row is prepended independently of detection.
    // Users who pick Font Family = OpenDyslexic for the dashboard can then
    // apply OpenDyslexicMono to the terminal without knowing to type the exact
    // family name.
    await openFontPicker()
    expect(screen.getByRole('option', { name: /OpenDyslexicMono/ })).toBeInTheDocument()
  })
})
