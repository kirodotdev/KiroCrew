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
    render(<PanelToggles workspaceOpen={false} exitFullscreen={exit} />)
    const name = poppedOut ? 'pages.chatPage.focus_popped_out_window' : 'components.panelToggles.show_terminal'
    fireEvent.click(screen.getByRole('button', { name }))
    const action = poppedOut ? terminal.focus : terminal.toggle
    expect(exit).toHaveBeenCalledOnce()
    expect(action).toHaveBeenCalledOnce()
    expect(exit.mock.invocationCallOrder[0]).toBeLessThan(action.mock.invocationCallOrder[0])
  })

  it('reads an open terminal covered by fullscreen as hidden, and reveals it without toggling', () => {
    terminal.open = true
    const exit = vi.fn()
    render(<PanelToggles workspaceOpen exitFullscreen={exit} />)
    const button = screen.getByRole('button', { name: 'components.panelToggles.show_terminal' })
    expect(button.className).not.toContain('bg-accent/10')
    expect(button.querySelector('rect.pi-pane')).not.toHaveAttribute('fill-opacity', '0.45')
    fireEvent.click(button)
    expect(exit).toHaveBeenCalledOnce()
    expect(terminal.toggle).not.toHaveBeenCalled()
  })

  it('names the terminal control by what the click does while the terminal is popped out', () => {
    terminal.poppedOut = true
    render(<PanelToggles workspaceOpen={false} />)
    const button = screen.getByRole('button', { name: 'pages.chatPage.focus_popped_out_window' })
    expect(button).toHaveAttribute('title', 'pages.chatPage.focus_popped_out_window')
    expect(button).not.toHaveAttribute('aria-pressed')
    expect(screen.queryByRole('button', { name: 'components.panelToggles.show_terminal' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'components.panelToggles.hide_terminal' })).toBeNull()
  })

  it('renders bottom panel before side panel in one two-button group', () => {
    const { container } = render(<PanelToggles workspaceOpen={false} />)
    const controls = container.querySelector('[data-panel-toggles]') as HTMLElement
    const buttons = Array.from(controls.querySelectorAll('button'))
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toHaveAccessibleName('components.panelToggles.show_terminal')
    expect(buttons[1]).toHaveAccessibleName('components.panelToggles.show_side_panel')
    for (const button of buttons) {
      expect(button.className).toContain('w-7')
      expect(button.className).toContain('h-7')
      expect(button.querySelector('svg')).toHaveAttribute('width', '14')
      expect(button.querySelector('svg')).toHaveAttribute('height', '14')
    }
    expect(controls.className).toContain('gap-1.5')
  })

  it('names each closed panel by the Show action, with no pressed state', () => {
    render(<PanelToggles workspaceOpen={false} />)
    const terminalButton = screen.getByRole('button', { name: 'components.panelToggles.show_terminal' })
    const workspaceButton = screen.getByRole('button', { name: 'components.panelToggles.show_side_panel' })
    for (const button of [terminalButton, workspaceButton]) {
      expect(button).toHaveAttribute('title', button.getAttribute('aria-label'))
      expect(button).not.toHaveAttribute('aria-pressed')
    }
  })

  it('names each open panel by the Hide action and dims its glyph', () => {
    terminal.open = true
    render(<PanelToggles workspaceOpen />)
    const terminalButton = screen.getByRole('button', { name: 'components.panelToggles.hide_terminal' })
    const workspaceButton = screen.getByRole('button', { name: 'components.panelToggles.hide_side_panel' })
    for (const button of [terminalButton, workspaceButton]) {
      expect(button).toHaveAttribute('title', button.getAttribute('aria-label'))
      expect(button).not.toHaveAttribute('aria-pressed')
      expect(button.className).toContain('bg-accent/10')
    }
    expect(terminalButton.querySelector('rect.pi-pane')).toHaveAttribute('fill-opacity', '0.45')
    expect(workspaceButton.querySelector('rect.pi-pane')).toHaveAttribute('fill-opacity', '0.45')
  })

  it('keeps the side-panel toggle when terminals are disabled', () => {
    terminal.enabled = false
    render(<PanelToggles workspaceOpen={false} />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'components.panelToggles.show_side_panel' })).toBeInTheDocument()
  })
})
