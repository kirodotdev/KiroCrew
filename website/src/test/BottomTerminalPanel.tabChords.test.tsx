import { act, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, addTab } from '../hooks/useBottomTerminal'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import {
  PANEL_TOGGLE_SHORTCUTS_KEY,
  TERMINAL_TAB_COMMAND_EVENT,
  type TerminalTabCommand,
} from '../lib/panelToggleShortcuts'
import { renderHookWithProviders, renderWithProviders } from './helpers'

/**
 * The close / previous / next terminal chords (#13396). Each ships unbound, so a
 * user records one in Settings; the keydown hook hands it to the shell, which
 * asks the docked panel's tab strip through `TERMINAL_TAB_COMMAND_EVENT`.
 */

const deleteTerminal = vi.fn()
const disposeTerminal = vi.fn()

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: (id: string) => disposeTerminal(id),
  useDeleteTerminalSession: () => ({ mutate: deleteTerminal, mutateAsync: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: (id: string) => id,
  getTerminalTitle: (id: string) => id,
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => false),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))

const seedTabs = (count: number) => Array.from({ length: count }, () => addTab()!)

const send = (command: TerminalTabCommand) =>
  act(async () => { window.dispatchEvent(new CustomEvent(TERMINAL_TAB_COMMAND_EVENT, { detail: command })) })

beforeEach(() => {
  __resetBottomTerminal()
  localStorage.clear()
  deleteTerminal.mockReset()
  disposeTerminal.mockReset()
})

// Reset inside act: the strip is still mounted when this runs, and the reset
// re-renders every chip subscribed to the store.
afterEach(() => {
  act(() => { __resetBottomTerminal() })
})

describe('terminal tab chords', () => {
  it('selects the next and previous tab, wrapping at both ends', async () => {
    const [first, second, third] = seedTabs(3)
    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('tab', { name: third })).toHaveAttribute('aria-selected', 'true')

    await send('next')
    expect(screen.getByRole('tab', { name: first })).toHaveAttribute('aria-selected', 'true')
    await send('prev')
    await send('prev')
    expect(screen.getByRole('tab', { name: second })).toHaveAttribute('aria-selected', 'true')
  })

  it('closes the active tab the way its × does', async () => {
    const [first, second, third] = seedTabs(3)
    renderWithProviders(<TerminalTabsView variant="dock" />)

    await send('close')
    expect(deleteTerminal).toHaveBeenCalledWith(third)
    expect(disposeTerminal).toHaveBeenCalledWith(third)
    expect(screen.queryByRole('tab', { name: third })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: second })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: first })).toBeInTheDocument()
  })

  it('runs a user-bound chord from inside a shell, once per press for close', () => {
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({
      'terminal-next': { key: 'k', ctrl: true, shift: true },
      'terminal-close': { key: 'w', ctrl: true, shift: true },
    }))
    const onNextTerminalTab = vi.fn()
    const onCloseTerminalTab = vi.fn()
    renderHookWithProviders(() => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onNextTerminalTab, onCloseTerminalTab }))
    const term = document.createElement('div')
    term.className = 'xterm'
    const inner = document.createElement('textarea')
    term.appendChild(inner)
    document.body.appendChild(term)
    onTestFinished(() => term.remove())

    const press = (code: string, key: string, repeat = false) => {
      const event = new KeyboardEvent('keydown', { code, key, repeat, ctrlKey: true, shiftKey: true, cancelable: true, bubbles: true })
      inner.dispatchEvent(event)
      return event
    }
    press('KeyK', 'K')
    expect(onNextTerminalTab).toHaveBeenCalledTimes(1)
    expect(onCloseTerminalTab).not.toHaveBeenCalled()

    // Holding the close chord must not stop one shell per auto-repeat tick; the
    // repeat is still kept from the PTY.
    press('KeyW', 'W')
    expect(press('KeyW', 'W', true).defaultPrevented).toBe(true)
    expect(onCloseTerminalTab).toHaveBeenCalledTimes(1)
  })
})
