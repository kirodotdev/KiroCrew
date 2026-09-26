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
    render(<PanelToggles exitFullscreen={exit} />)
    const name = poppedOut ? 'pages.chatPage.focus_popped_out_window' : 'hooks.useKeyboardShortcuts.toggle_terminal'
    fireEvent.click(screen.getByRole('button', { name }))
    const action = poppedOut ? terminal.focus : terminal.toggle
    expect(exit).toHaveBeenCalledOnce()
    expect(action).toHaveBeenCalledOnce()
    expect(exit.mock.invocationCallOrder[0]).toBeLessThan(action.mock.invocationCallOrder[0])
  })

  it('names the terminal control by what the click does while the terminal is popped out', () => {
    terminal.poppedOut = true
    render(<PanelToggles />)
    const button = screen.getByRole('button', { name: 'pages.chatPage.focus_popped_out_window' })
    expect(button).toHaveAttribute('title', 'pages.chatPage.focus_popped_out_window')
    expect(button).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })).toBeNull()
  })

  it('renders Lucide bottom and side panels in one two-button group', () => {
    const { container } = render(<PanelToggles workspaceOpen={false} />)
    const controls = container.querySelector('[data-panel-toggles]') as HTMLElement
    const buttons = Array.from(controls.querySelectorAll('button'))
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toHaveAccessibleName('hooks.useKeyboardShortcuts.toggle_terminal')
    expect(buttons[1]).toHaveAccessibleName('hooks.useKeyboardShortcuts.toggle_side_panel')
    expect(buttons[0].querySelector('svg')).toHaveClass('lucide-panel-bottom')
    expect(buttons[1].querySelector('svg')).toHaveClass('lucide-panel-right')
    for (const button of buttons) {
      expect(button.className).toContain('w-7')
      expect(button.className).toContain('h-7')
      expect(button.querySelector('svg')).toHaveClass('lucide-inline', 'text-[14px]')
      expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    }
    expect(controls.className).toContain('gap-1.5')
  })

  it('reflects open state in the button styling and pressed state', () => {
    terminal.open = true
    render(<PanelToggles workspaceOpen />)
    const terminalButton = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })
    const workspaceButton = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })
    expect(terminalButton).toHaveAttribute('aria-pressed', 'true')
    expect(workspaceButton).toHaveAttribute('aria-pressed', 'true')
    expect(terminalButton.className).toContain('text-accent')
    expect(terminalButton.className).toContain('bg-accent/10')
    expect(workspaceButton.className).toContain('text-accent')
    expect(workspaceButton.className).toContain('bg-accent/10')
  })

  it('keeps the side-panel toggle when terminals are disabled', () => {
    terminal.enabled = false
    render(<PanelToggles />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })).toBeInTheDocument()
  })
})
