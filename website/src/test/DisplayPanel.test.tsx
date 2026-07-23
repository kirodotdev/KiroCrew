import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DisplayPanel } from '../pages/settings/DisplayPanel'
import { renderWithProviders } from './helpers'

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
// pulling in the real provider's state machine.
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: 'default',
    setColorTheme: vi.fn(),
    allThemes: [
      { value: 'default', label: 'Default', custom: false },
    ],
    theme: 'dark',
    themeVersion: 0,
    addCustomTheme: vi.fn(),
    deleteCustomTheme: vi.fn(),
    loadCustomThemes: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))

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
    const backdrop = screen.getByText('Create Theme').closest('[class*="fixed inset-0"]')
    expect(backdrop).toBeInTheDocument()
    expect(backdrop).toHaveClass('z-[49]')

    // The Sidebar Colors buttons should NOT be accessible to the user
    // because the modal overlay (z-[49]) sits above the content area.
    // The modal is rendered OUTSIDE the SettingsCard (not trapped in card-glow stacking context),
    // and its z-index ensures it overlays the Sidebar Colors section below.
    const modalOverlay = backdrop!

    // Verify DOM order: modal comes before Sidebar Colors in the tree,
    // meaning the fixed overlay covers the section below it
    const parent = modalOverlay.parentElement!
    const children = Array.from(parent.children)
    const modalIdx = children.indexOf(modalOverlay)
    const sidebarIdx = children.findIndex(el => el.textContent?.includes('Sidebar Colors'))
    expect(modalIdx).toBeLessThan(sidebarIdx)
  })

  it('renders ThemeEditorPanel modal outside of SettingsCard to avoid card-glow stacking context', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DisplayPanel />)

    await user.click(screen.getByText('+ New Theme'))

    await waitFor(() => {
      expect(screen.getByText('Create Theme')).toBeInTheDocument()
    })

    // The modal container (fixed inset-0) should NOT be inside any .card-glow element
    const modalContainer = screen.getByText('Create Theme').closest('[class*="fixed inset-0"]')
    expect(modalContainer).toBeInTheDocument()

    // Walk up the DOM tree — no ancestor should have card-glow class
    let el = modalContainer!.parentElement
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

    // Close via the X button
    const headerBtns = screen.getByText('Create Theme').closest('.flex')!.querySelectorAll('button')
    const xBtn = headerBtns[0] // The X button in the header
    await user.click(xBtn)

    // Modal should be gone
    await waitFor(() => {
      expect(screen.queryByText('Create Theme')).not.toBeInTheDocument()
    })

    // Sidebar Colors section should still be visible and interactive
    expect(screen.getByText('Sidebar Colors')).toBeInTheDocument()
    expect(screen.getByText('Palette')).toBeInTheDocument()
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
