/**
 * The dock terminal's overflow menu: Find and Fullscreen (#10176, #9906).
 *
 * On Linux/Windows the shell keeps bare Ctrl+F (readline forward-char), so the
 * menu's Find item is a discoverable way into scrollback find beside
 * Ctrl+Shift+F, and fullscreen has no chord there at all: its menu item is the
 * way in. The keycap each item shows must be the chord that platform actually
 * answers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal, addTab } from '../hooks/useBottomTerminal'
import { openTerminalSearch } from '../components/CliPanel'

const platformMock = vi.hoisted(() => ({ isMac: false }))
vi.mock('../utils/platform', async (orig) => ({
  ...(await orig<typeof import('../utils/platform')>()),
  get isMac() { return platformMock.isMac },
}))
vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
  openTerminalSearch: vi.fn(() => true),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => '',
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../hooks/usePanelTabs', () => ({
  usePanelTabs: () => ({}),
}))

beforeEach(() => {
  __resetBottomTerminal()
  vi.clearAllMocks()
})
afterEach(() => {
  __resetBottomTerminal()
  platformMock.isMac = false
})

async function openMenu() {
  await userEvent.click(screen.getByRole('button', { name: 'More actions' }))
  await act(async () => {})
}

describe('TerminalTabsView overflow menu — Find', () => {
  it('opens the find bar on the ACTIVE terminal', async () => {
    openBottomTerminal()
    // A newly added tab becomes the active one.
    const activeId = addTab()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    await userEvent.click(screen.getByRole('menuitem', { name: /Find/ }))
    expect(openTerminalSearch).toHaveBeenCalledTimes(1)
    expect(openTerminalSearch).toHaveBeenCalledWith(activeId)
    await act(async () => {})
  })

  it('Linux/Windows: Find shows Ctrl+Shift+F and Fullscreen shows no keycap', async () => {
    platformMock.isMac = false
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    expect(screen.getByTestId('terminal-find-shortcut').textContent).toBe('Ctrl+Shift+F')
    expect(screen.getByTestId('terminal-menu-fullscreen')).toBeInTheDocument()
    expect(screen.queryByTestId('terminal-fullscreen-shortcut')).toBeNull()
  })

  it('macOS: Find shows ⌘F and Fullscreen shows ⇧⌘F', async () => {
    platformMock.isMac = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    expect(screen.getByTestId('terminal-find-shortcut').textContent).toBe('⌘F')
    expect(screen.getByTestId('terminal-fullscreen-shortcut').textContent).toBe('⇧⌘F')
  })
})
