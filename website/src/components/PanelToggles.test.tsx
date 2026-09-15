import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import PanelToggles from './PanelToggles'

const terminal = vi.hoisted(() => ({ poppedOut: false, enabled: true, open: false, toggle: vi.fn(), focus: vi.fn() }))
vi.mock('../store', () => ({ useAppSelector: () => undefined }))
vi.mock('../hooks/useBottomTerminal', () => ({
  useBottomTerminalOpen: () => terminal.open,
  toggleBottomTerminal: terminal.toggle,
}))
vi.mock('../utils/terminalRegistry', () => ({ useTerminalEnabled: () => terminal.enabled }))
vi.mock('../utils/terminalPopout', () => ({
  useTerminalPoppedOut: () => terminal.poppedOut,
  focusPopout: terminal.focus,
}))
vi.mock('../i18n/t', () => ({ i18nT: (key: string) => key }))

describe('workspace panel toggles', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    terminal.poppedOut = false
    terminal.enabled = true
    terminal.open = false
  })

  it.each([false, true])('exits fullscreen before the terminal action, popped out: %s', poppedOut => {
    terminal.poppedOut = poppedOut
    const exit = vi.fn()
    render(<PanelToggles showWorkspace exitFullscreen={exit} />)
    const name = poppedOut ? 'pages.chatPage.focus_popped_out_window' : 'hooks.useKeyboardShortcuts.toggle_terminal'
    fireEvent.click(screen.getByRole('button', { name }))
    const action = poppedOut ? terminal.focus : terminal.toggle
    expect(exit).toHaveBeenCalledOnce()
    expect(action).toHaveBeenCalledOnce()
    expect(exit.mock.invocationCallOrder[0]).toBeLessThan(action.mock.invocationCallOrder[0])
  })

  it('names the terminal control by what the click does while the terminal is popped out', () => {
    terminal.poppedOut = true
    render(<PanelToggles showWorkspace />)
    const button = screen.getByRole('button', { name: 'pages.chatPage.focus_popped_out_window' })
    expect(button).toHaveAttribute('title', 'pages.chatPage.focus_popped_out_window')
    expect(button).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })).toBeNull()
  })

  it('renders bottom panel before side panel in one two-button group', () => {
    const { container } = render(<PanelToggles showWorkspace workspaceOpen={false} />)
    const controls = container.querySelector('[data-panel-toggles]') as HTMLElement
    const buttons = Array.from(controls.querySelectorAll('button'))
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toHaveAccessibleName('hooks.useKeyboardShortcuts.toggle_terminal')
    expect(buttons[1]).toHaveAccessibleName('hooks.useKeyboardShortcuts.toggle_side_panel')
    for (const button of buttons) {
      expect(button.className).toContain('w-7')
      expect(button.className).toContain('h-7')
      expect(button.querySelector('svg')).toHaveAttribute('width', '14')
      expect(button.querySelector('svg')).toHaveAttribute('height', '14')
    }
    expect(controls.className).toContain('gap-1.5')
  })

  it('reflects open state in the panel glyphs and pressed state', () => {
    terminal.open = true
    render(<PanelToggles showWorkspace workspaceOpen />)
    const terminalButton = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })
    const workspaceButton = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })
    expect(terminalButton).toHaveAttribute('aria-pressed', 'true')
    expect(workspaceButton).toHaveAttribute('aria-pressed', 'true')
    expect(terminalButton.querySelector('rect.pi-pane')).toHaveAttribute('fill-opacity', '0.45')
    expect(workspaceButton.querySelector('rect.pi-pane')).toHaveAttribute('fill-opacity', '0.45')
  })

  it('keeps the side-panel toggle when terminals are disabled', () => {
    terminal.enabled = false
    render(<PanelToggles showWorkspace />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })).toBeInTheDocument()
  })
})
