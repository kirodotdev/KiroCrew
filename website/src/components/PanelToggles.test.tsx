import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import PanelToggles from './PanelToggles'

const terminal = vi.hoisted(() => ({ poppedOut: false, enabled: true, toggle: vi.fn(), focus: vi.fn() }))
// Minimal store: the active chat slot is the terminal session scope, and its
// dashboard slot carries the cwd the toggle opens a terminal in.
const state = vi.hoisted(() => ({
  chat: { activeSlot: 'chat-1', activityOpen: false },
  dashboard: { slots: [{ key: 'chat-1', project: '/repo' }] },
}))
vi.mock('../store', () => ({ useAppSelector: (select: (s: typeof state) => unknown) => select(state) }))
vi.mock('../hooks/useBottomTerminal', () => ({
  useBottomTerminalOpen: () => false,
  toggleBottomTerminal: terminal.toggle,
}))
vi.mock('../utils/terminalRegistry', () => ({ useTerminalEnabled: () => terminal.enabled }))
vi.mock('../utils/terminalPopout', () => ({
  useTerminalPoppedOut: () => terminal.poppedOut,
  focusPopout: terminal.focus,
}))
vi.mock('../i18n/t', () => ({ i18nT: (key: string) => key }))

describe('fixed panel toggles', () => {
  beforeEach(() => { vi.clearAllMocks(); terminal.poppedOut = false; terminal.enabled = true })
  const clickTerminalControl = () => {
    fireEvent.click(screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' }))
  }

  it.each([false, true])('exits fullscreen before the terminal action, popped out: %s', poppedOut => {
    terminal.poppedOut = poppedOut
    const exit = vi.fn()
    render(<PanelToggles showWorkspace exitFullscreen={exit} />)
    clickTerminalControl()
    const action = poppedOut ? terminal.focus : terminal.toggle
    expect(exit).toHaveBeenCalledOnce()
    expect(action).toHaveBeenCalledOnce()
    expect(exit.mock.invocationCallOrder[0]).toBeLessThan(action.mock.invocationCallOrder[0])
    expect(poppedOut ? terminal.toggle : terminal.focus).not.toHaveBeenCalled()
  })

  // Terminal state is per chat session: the toggle must read and drive the
  // active slot's scope, or it opens the global terminal while the bottom
  // panel shows the session's.
  it('toggles the terminal directly, in the active session scope, when the workspace is not fullscreen', () => {
    render(<PanelToggles showWorkspace />)
    clickTerminalControl()
    expect(terminal.toggle).toHaveBeenCalledExactlyOnceWith('/repo', 'chat-1')
  })

  it('focuses the popped-out terminal of the active session', () => {
    terminal.poppedOut = true
    render(<PanelToggles showWorkspace />)
    clickTerminalControl()
    expect(terminal.focus).toHaveBeenCalledExactlyOnceWith('chat-1')
    expect(terminal.toggle).not.toHaveBeenCalled()
  })

  // Fullscreen is the workspace panel's own action and renders in that panel's
  // group, so this row never grows past the two-button cap.
  it('renders the workspace and terminal toggles as one two-button group', () => {
    const { container } = render(<PanelToggles showWorkspace exitFullscreen={vi.fn()} />)
    const controls = container.querySelector('[data-panel-toggles]') as HTMLElement
    expect(controls.querySelectorAll('button')).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /full_screen/ })).not.toBeInTheDocument()
    expect(controls.className).not.toContain('border-l')
  })

  it('sizes each toggle from the toolbar button token', () => {
    render(<PanelToggles showWorkspace />)
    for (const button of screen.getAllByRole('button')) {
      expect(button.className).toContain('w-[var(--panel-toolbar-button-size)]')
      expect(button.className).toContain('h-[var(--panel-toolbar-button-size)]')
      expect(button.className).not.toMatch(/\bw-7\b/)
    }
  })

  it('renders only the workspace toggle when terminals are disabled', () => {
    terminal.enabled = false
    render(<PanelToggles showWorkspace />)
    expect(screen.queryByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  // The split-pane controls beside this row draw the house panel glyphs
  // (icons/panels via SplitGlyph); the toggles use the same family so a title
  // row never shows two renderings of "a panel". The pane rect carries state:
  // solid while closed, dimmed while open.
  it.each([false, true])('draws the house panel glyph with open following pressed state: %s', open => {
    render(<PanelToggles showWorkspace workspaceOpen={open} />)
    const workspace = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_side_panel' })
    const svg = workspace.querySelector('svg') as SVGElement
    expect(svg.getAttribute('class') ?? '').not.toContain('lucide')
    const pane = svg.querySelector('rect.pi-pane') as SVGRectElement
    expect(pane).not.toBeNull()
    expect(pane.getAttribute('fill-opacity')).toBe(open ? '0.45' : null)
    expect(workspace).toHaveAttribute('aria-pressed', String(open))
    expect(workspace.className).toContain('pi-morph')
    const terminalPane = screen.getByRole('button', { name: 'hooks.useKeyboardShortcuts.toggle_terminal' }).querySelector('rect.pi-pane')
    expect(terminalPane?.getAttribute('fill-opacity')).toBeNull()
  })
})
