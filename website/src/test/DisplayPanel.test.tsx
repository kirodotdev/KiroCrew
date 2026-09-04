import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DisplayPanel } from '../pages/settings/DisplayPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// Mock useZoomCtx — DisplayPanel uses it for zoom/font controls. The object is
// module-scoped and mutable so individual tests can flip zoomSupported to
// cover both the desktop stepper and the plain-browser shortcut hint.
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

describe('DisplayPanel – ThemeEditorPanel overlay', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('hides Sidebar Colors buttons behind the modal backdrop when ThemeEditorPanel is open', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    // Verify Sidebar Colors section is visible initially
    expect(screen.getByText('Sidebar Colors')).toBeInTheDocument()
    expect(screen.getByText('Palette')).toBeInTheDocument()

    // Open the theme editor
    const newThemeBtn = screen.getByText('+ New Theme')
    await user.click(newThemeBtn)

    // ThemeEditorPanel modal should be open
    await waitFor(() => {
      expect(screen.getByText('Create Theme')).toBeInTheDocument()
    })

    // The modal backdrop should be present and cover the content
    const dialog = screen.getByRole('dialog', { name: 'Create Theme' })
    const backdrop = document.querySelector('.fixed.inset-0.bg-bg\\/60') as HTMLElement
    expect(backdrop).toBeInTheDocument()

    // The overlay used to be a hand-rolled `fixed inset-0 z-[49] bg-black/50`
    // div. The shared Modal owns the backdrop now and puts the dialog on its
    // own z-[100]/[101] layer, above the page rather than one step under the
    // floating theme-experience toggle.
    expect(document.querySelector('.bg-black\\/50')).toBeNull()
    expect(backdrop.className).toContain('z-[100]')
    expect((dialog.parentElement as HTMLElement).className).toContain('z-[101]')

    // Modal portals to document.body, so the dialog is no longer a sibling of
    // the Sidebar Colors section in the panel's own tree: it is a child of body,
    // which is what places it above every section regardless of DOM order.
    expect(dialog.closest('body')).toBe(document.body)
    expect(screen.getByText('Sidebar Colors').closest('[role="dialog"]')).toBeNull()
  })

  it('renders ThemeEditorPanel modal outside of SettingsCard to avoid card-glow stacking context', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    await user.click(screen.getByText('+ New Theme'))

    await waitFor(() => {
      expect(screen.getByText('Create Theme')).toBeInTheDocument()
    })

    // Walk up the DOM tree from the portalled dialog — no ancestor should have
    // card-glow class (a transform/filter ancestor would clip `fixed`).
    let el: HTMLElement | null = screen.getByRole('dialog', { name: 'Create Theme' })
    while (el) {
      expect(el.className).not.toContain('card-glow')
      el = el.parentElement
    }
  })

  it('closes ThemeEditorPanel and shows Sidebar Colors buttons again', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    // Open theme editor
    await user.click(screen.getByText('+ New Theme'))
    await waitFor(() => {
      expect(screen.getByText('Create Theme')).toBeInTheDocument()
    })

    // Close via Modal's own header close button
    await user.click(screen.getByRole('button', { name: 'Close' }))

    // Modal should be gone
    await waitFor(() => {
      expect(screen.queryByText('Create Theme')).not.toBeInTheDocument()
    })

    // Sidebar Colors section should still be visible and interactive
    expect(screen.getByText('Sidebar Colors')).toBeInTheDocument()
    expect(screen.getByText('Palette')).toBeInTheDocument()
  })

  it('dismisses the theme editor on Escape and on a backdrop click', async () => {
    // Escape is the capability the hand-rolled overlay lacked; the backdrop
    // click it already had must survive the conversion. Both are the ACCIDENTAL
    // exits, so both are only available while the form is untouched.
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    await user.click(screen.getByText('+ New Theme'))
    await screen.findByRole('dialog', { name: 'Create Theme' })
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('Create Theme')).not.toBeInTheDocument())

    await user.click(screen.getByText('+ New Theme'))
    await screen.findByRole('dialog', { name: 'Create Theme' })
    fireEvent.click(document.querySelector('.fixed.inset-0.bg-bg\\/60') as HTMLElement)
    await waitFor(() => expect(screen.queryByText('Create Theme')).not.toBeInTheDocument())
  })

  it('refuses Escape and backdrop dismissal once the theme draft has content', async () => {
    // Escape is a path this conversion ADDS, and closeEditor discards the draft
    // unconditionally — so on a part-filled form the accidental exits must not
    // fire. Only the explicit ones (header close, the panel's Cancel) close it.
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    await user.click(screen.getByText('+ New Theme'))
    await screen.findByRole('dialog', { name: 'Create Theme' })
    await user.type(screen.getByPlaceholderText('My Custom Theme'), 'Midnight')

    // Settle past Modal's exit animation before asserting PRESENCE: the panel
    // lingers in the DOM while AnimatePresence plays the exit, so a short wait
    // would pass whether or not the dismissal was refused.
    fireEvent.keyDown(window, { key: 'Escape' })
    await new Promise(r => setTimeout(r, 600))
    expect(screen.getByRole('dialog', { name: 'Create Theme' })).toBeInTheDocument()

    fireEvent.click(document.querySelector('.fixed.inset-0.bg-bg\\/60') as HTMLElement)
    await new Promise(r => setTimeout(r, 600))
    expect(screen.getByRole('dialog', { name: 'Create Theme' })).toBeInTheDocument()
    // The draft survived both, name included.
    expect(screen.getByPlaceholderText('My Custom Theme')).toHaveValue('Midnight')

    // The explicit exit still works on the same dirty form.
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByText('Create Theme')).not.toBeInTheDocument())
  })

  it('locks page scroll and puts initial focus inside the dialog while the editor is open', async () => {
    // Both come from the shared Modal (scroll lock + focus trap) and neither
    // existed on the hand-rolled overlay.
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    await user.click(screen.getByText('+ New Theme'))
    const dialog = await screen.findByRole('dialog', { name: 'Create Theme' })
    expect(document.body.style.overflow).toBe('hidden')
    expect(dialog.contains(document.activeElement)).toBe(true)
  })
})


describe('DisplayPanel – theme install', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the renamed "Theme" section with an Install control', () => {
    renderWithProviders(<DisplayPanel />)
    expect(screen.getByText('Install Theme')).toBeInTheDocument()
    expect(screen.getByLabelText('Theme source')).toBeInTheDocument()
    expect(screen.getByLabelText('Theme source location')).toBeInTheDocument()
  })

  it('installs a theme from a GitHub URL via api.installTheme', async () => {
    const user = userEvent.setup()
    const spy = vi
      .spyOn(api, 'installTheme')
      .mockResolvedValue({ ok: true, slug: 'lcars' })
    renderWithProviders(<DisplayPanel />)

    await user.type(
      screen.getByLabelText('Theme source location'),
      'https://github.com/u/lcars'
    )
    await user.click(screen.getByText('Install'))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith({
        type: 'github',
        url: 'https://github.com/u/lcars',
      })
    })
    spy.mockRestore()
  })

  it('picking "Local folder" retargets the install at a filesystem path', async () => {
    // Regression guard for the native-<select> → SimpleSelect migration: the
    // source picker is a Radix Select, so a `change` event on the trigger does
    // nothing — open it, then click the option. The placeholder and the
    // installTheme payload are the two observable consequences of the state move.
    const spy = vi
      .spyOn(api, 'installTheme')
      .mockResolvedValue({ ok: true, slug: 'lcars' })
    renderWithProviders(<DisplayPanel />)

    const trigger = screen.getByRole('combobox', { name: 'Theme source' })
    expect(trigger).toHaveTextContent('GitHub')
    expect(screen.getByLabelText('Theme source location')).toHaveAttribute(
      'placeholder',
      'https://github.com/user/theme'
    )

    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Local folder' }))

    expect(trigger).toHaveTextContent('Local folder')
    const location = screen.getByLabelText('Theme source location')
    expect(location).toHaveAttribute('placeholder', '/path/to/theme')

    fireEvent.change(location, { target: { value: '/srv/themes/lcars' } })
    fireEvent.click(screen.getByText('Install'))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith({ type: 'local', path: '/srv/themes/lcars' })
    })
    spy.mockRestore()
  })

  it('shows the "Applying…" status indicator while a theme switch is in flight', () => {
    mockUseTheme.mockImplementation(() => ({ ...DEFAULT_THEME, themeSwitching: true }))
    renderWithProviders(<DisplayPanel />)
    expect(screen.getByText(/Applying/)).toBeInTheDocument()
  })

  it('does not show the "Applying…" indicator when no switch is in flight', () => {
    renderWithProviders(<DisplayPanel />)
    expect(screen.queryByText(/Applying/)).not.toBeInTheDocument()
  })

  it('shows "Fetching…" on the install button while installTheme is pending', async () => {
    const user = userEvent.setup()
    const spy = vi
      .spyOn(api, 'installTheme')
      .mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.installTheme>)
    renderWithProviders(<DisplayPanel />)

    await user.type(screen.getByLabelText('Theme source location'), 'https://github.com/u/x')
    await user.click(screen.getByText('Install'))

    // installTheme never resolves → the button stays in the 'fetching' phase.
    expect(await screen.findByRole('button', { name: /Fetching/ })).toBeInTheDocument()
    spy.mockRestore()
  })
})

describe('DisplayPanel – font family setting', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('tells the user the code font tracks the theme, not this preference', () => {
    // A theme pack's `mono` face reaches code blocks, inline code and diffs
    // under EVERY option here, System included (website/docs/theming-contract.md
    // § Fonts). Without this sentence a user who picks System and still sees the
    // code font change reads the option as broken.
    // Since OpenDyslexic shipped, the description also carves out that option:
    // it applies its own OpenDyslexicMono to code surfaces, so the "follows the
    // active theme" rule doesn't hold for it. The assertion pins both halves so
    // a future edit can't silently drop either.
    renderWithProviders(<DisplayPanel />)

    expect(screen.getByText('Font Family')).toBeInTheDocument()
    expect(
      screen.getByText('UI font family for the dashboard. Code font follows the active theme, except OpenDyslexic which supplies its own.'),
    ).toBeInTheDocument()
  })
})

describe('DisplayPanel – plain diffs setting', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('persists the choice to the key the diff surfaces read', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    const toggle = screen.getByRole('switch', { name: 'Plain diffs' })
    // Highlighted diffs are what a new install shows, so the switch starts off.
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    await user.click(toggle)
    // The literal key matters: PierrePatch and DiffBlock read `mc-diff-plain`
    // through usePlainDiff, and nothing on the server mediates between them.
    await waitFor(() => expect(localStorage.getItem('mc-diff-plain')).toBe('1'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('seeds from the stored preference', () => {
    localStorage.setItem('mc-diff-plain', '1')
    renderWithProviders(<DisplayPanel />)

    expect(screen.getByRole('switch', { name: 'Plain diffs' })).toHaveAttribute('aria-checked', 'true')
  })
})

describe('DisplayPanel – zoom setting', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    zoomCtx.zoomSupported = true
    zoomCtx.zoom = 100
  })

  /** Scope queries to the zoom stepper's button row — the panel has other
   *  steppers (e.g. "Highlight recent sessions") with identical
   *  Increase/Decrease labels. Only the zoom value renders with a % suffix,
   *  and that text sits on the reset button whose parent is the row. */
  const zoomRow = () => within(screen.getByText(/^\d+%$/).parentElement as HTMLElement)

  it('desktop: renders the native zoom stepper and drives the bridge callbacks', async () => {
    const user = userEvent.setup()
    zoomCtx.zoom = 125
    renderWithProviders(<DisplayPanel />)

    expect(screen.getByText('Zoom Level')).toBeInTheDocument()
    expect(screen.getByText('125%')).toBeInTheDocument()
    // Single zoom control only — the legacy Font Size stepper must be gone.
    expect(screen.queryByText('Font Size')).not.toBeInTheDocument()

    await user.click(zoomRow().getByLabelText('Increase'))
    expect(zoomCtx.zoomIn).toHaveBeenCalledTimes(1)
    await user.click(zoomRow().getByLabelText('Decrease'))
    expect(zoomCtx.zoomOut).toHaveBeenCalledTimes(1)
    await user.click(screen.getByText('125%'))
    expect(zoomCtx.reset).toHaveBeenCalledTimes(1)
  })

  it('browser: shows the shortcut hint instead of a stepper', () => {
    zoomCtx.zoomSupported = false
    renderWithProviders(<DisplayPanel />)

    expect(screen.getByText('Zoom Level')).toBeInTheDocument()
    expect(screen.getByText(/Use your browser's zoom/)).toBeInTheDocument()
    // No zoom % value button renders in browser mode (other steppers keep theirs).
    expect(screen.queryByText(/^\d+%$/)).not.toBeInTheDocument()
    expect(screen.queryByText('Font Size')).not.toBeInTheDocument()
  })
})

describe('DisplayPanel – dropped overrides notice', () => {
  // The runtime scoper silently removes overrides.css rules the theming
  // contract disallows; the ONLY other signal is a console warning no dashboard
  // user has open. These pin the Settings-side surface: shown for the active
  // pack with the rule names an author needs, absent otherwise.
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const REPORT = { slug: 'manrope', rules: ['body { --font-body }', '.session-card'] }

  it('names the dropped rules when the active theme had rules removed', () => {
    mockUseTheme.mockImplementation(() => ({
      ...DEFAULT_THEME,
      colorTheme: 'custom-manrope',
      overridesDropReport: REPORT,
    }))
    renderWithProviders(<DisplayPanel />)
    expect(screen.getByText("Some of this theme's styles were ignored")).toBeInTheDocument()
    // The rule names are the actionable part — a bare count tells an author
    // nothing to edit.
    expect(screen.getByText(/body \{ --font-body \}/)).toBeInTheDocument()
    expect(screen.getByText(/\.session-card/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Theming guide' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('renders nothing when no rules were dropped', () => {
    mockUseTheme.mockImplementation(() => ({
      ...DEFAULT_THEME,
      colorTheme: 'custom-manrope',
      overridesDropReport: null,
    }))
    renderWithProviders(<DisplayPanel />)
    expect(screen.queryByText("Some of this theme's styles were ignored")).not.toBeInTheDocument()
  })

  it('ignores a report that belongs to a theme other than the active one', () => {
    // Belt-and-braces for the switch race: the provider clears the report on
    // theme change, but a stale report must still never be attributed to the
    // wrong pack in the UI.
    mockUseTheme.mockImplementation(() => ({
      ...DEFAULT_THEME,
      colorTheme: 'custom-other',
      overridesDropReport: REPORT,
    }))
    renderWithProviders(<DisplayPanel />)
    expect(screen.queryByText("Some of this theme's styles were ignored")).not.toBeInTheDocument()
  })
})

describe('DisplayPanel – Font Family picker (OpenDyslexic option)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  // The Font Family row is a SettingsButtonGroup that lists Sans / Mono /
  // System, plus OpenDyslexic as a fourth built-in a11y option. The buttons
  // render their label as accessible text; asserting on the button role is
  // enough to prove the option is discoverable — actually clicking it would
  // just re-verify the shared SettingsButtonGroup wiring, which has its own
  // tests.
  it('lists OpenDyslexic as a fourth font family option alongside Sans/Mono/System', () => {
    renderWithProviders(<DisplayPanel />)
    expect(screen.getByRole('button', { name: 'Sans' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mono' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'System' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'OpenDyslexic' })).toBeInTheDocument()
  })
})
