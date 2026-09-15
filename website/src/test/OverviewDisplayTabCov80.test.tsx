import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// The tab is a control surface over three hooks. Stubbing them is what makes each
// control's WIRING assertable — the hooks themselves have their own tests, and the
// real theme editor would drag a colour-picker tree into every case here.
const zoom = {
  zoom: 110, zoomSupported: true,
  zoomIn: vi.fn(), zoomOut: vi.fn(), reset: vi.fn(),
  family: 'sans' as string, setFontFamily: vi.fn(),
}
const theme = {
  preference: 'system' as string, setTheme: vi.fn(),
  colorTheme: 'zzq-base' as string, setColorTheme: vi.fn(),
  allThemes: [
    { value: 'zzq-base', label: 'zzq-base-label', custom: false },
    { value: 'custom-zzq-mine', label: 'zzq-mine-label', custom: true },
  ] as { value: string; label: string; custom?: boolean }[],
}
const editor = {
  editorOpen: false, isEditing: false,
  openNewTheme: vi.fn(), closeEditor: vi.fn(), openEditTheme: vi.fn(),
}
vi.mock('../hooks/ZoomProvider', () => ({ useZoomCtx: () => zoom }))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => theme }))
vi.mock('../components/themeEditor', () => ({
  useThemeEditor: () => editor,
  ThemeEditorPanel: () => <div data-testid="theme-editor-panel" />,
}))

const DisplayTab = (await import('../pages/overview/DisplayTab')).default

beforeEach(() => {
  vi.clearAllMocks()
  zoom.zoomSupported = true
  zoom.family = 'sans'
  theme.preference = 'system'
  theme.colorTheme = 'zzq-base'
  editor.editorOpen = false
  editor.isEditing = false
})

/** True when the control carries the selected-state classes. */
function isActive(el: HTMLElement): boolean {
  return el.className.includes('bg-accent-subtle')
}

describe('DisplayTab — zoom', () => {
  it('drives the three zoom controls and shows the current level', async () => {
    render(<DisplayTab />)
    await userEvent.click(screen.getByRole('button', { name: '−' }))
    expect(zoom.zoomOut).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: '+' }))
    expect(zoom.zoomIn).toHaveBeenCalledTimes(1)
    // The level itself is the reset affordance.
    await userEvent.click(screen.getByRole('button', { name: '110%' }))
    expect(zoom.reset).toHaveBeenCalledTimes(1)
  })

  it('falls back to a keyboard hint where native zoom is unavailable', () => {
    zoom.zoomSupported = false
    render(<DisplayTab />)
    expect(screen.queryByRole('button', { name: '+' })).toBeNull()
    expect(screen.getByText(/Zoom with/)).toBeInTheDocument()
  })
})

describe('DisplayTab — font and mode', () => {
  it('marks the active font family and switches on click', async () => {
    render(<DisplayTab />)
    expect(isActive(screen.getByRole('button', { name: 'Sans' }))).toBe(true)
    expect(isActive(screen.getByRole('button', { name: 'Mono' }))).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: 'Mono' }))
    expect(zoom.setFontFamily).toHaveBeenCalledWith('mono')
  })

  it('marks the active colour scheme and switches on click', async () => {
    theme.preference = 'dark'
    render(<DisplayTab />)
    expect(isActive(screen.getByRole('button', { name: /Dark/ }))).toBe(true)
    await userEvent.click(screen.getByRole('button', { name: /Light/ }))
    expect(theme.setTheme).toHaveBeenCalledWith('light')
    await userEvent.click(screen.getByRole('button', { name: /Auto/ }))
    expect(theme.setTheme).toHaveBeenCalledWith('system')
  })
})

describe('DisplayTab — colour themes', () => {
  it('lists every theme and applies the one clicked', async () => {
    render(<DisplayTab />)
    expect(isActive(screen.getByRole('button', { name: 'zzq-base-label' }))).toBe(true)
    await userEvent.click(screen.getByRole('button', { name: 'zzq-mine-label' }))
    expect(theme.setColorTheme).toHaveBeenCalledWith('custom-zzq-mine')
  })

  it('offers an edit affordance on custom themes only, addressed by slug', async () => {
    render(<DisplayTab />)
    const edits = screen.getAllByRole('button', { name: 'Edit theme' })
    expect(edits).toHaveLength(1)
    await userEvent.click(edits[0])
    // The `custom-` prefix is the STORED value; the editor keys on the bare slug.
    expect(editor.openEditTheme).toHaveBeenCalledWith('zzq-mine')
    // Editing must not also select the theme — the click is stopped.
    expect(theme.setColorTheme).not.toHaveBeenCalled()
  })

  it('opens the creator from the closed state', async () => {
    render(<DisplayTab />)
    await userEvent.click(screen.getByRole('button', { name: /New Theme/i }))
    expect(editor.openNewTheme).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('theme-editor-panel')).toBeNull()
  })

  it('shows the panel and a close action while creating', async () => {
    editor.editorOpen = true
    render(<DisplayTab />)
    expect(screen.getByTestId('theme-editor-panel')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Creating/ }))
    expect(editor.closeEditor).toHaveBeenCalledTimes(1)
    expect(editor.openNewTheme).not.toHaveBeenCalled()
  })

  it('distinguishes editing an existing theme from creating a new one', () => {
    editor.editorOpen = true
    editor.isEditing = true
    render(<DisplayTab />)
    expect(screen.getByRole('button', { name: /Editing/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Creating/ })).toBeNull()
  })
})
