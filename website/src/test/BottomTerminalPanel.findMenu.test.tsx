/**
 * The dock terminal's overflow menu (Find, Hide) and its header Fullscreen
 * toggle (#10176, #9906, #41974).
 *
 * On Linux/Windows the shell keeps bare Ctrl+F (readline forward-char), so the
 * menu's Find item is a discoverable way into scrollback find beside
 * Ctrl+Shift+F, and fullscreen has no chord there at all: the header toggle is
 * the way in. The keycap each control shows must be the chord that platform
 * actually answers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal, addTab, setTerminalFullscreen, isBottomTerminalOpen } from '../hooks/useBottomTerminal'
import { openTerminalSearch } from '../components/terminalSessions'

const platformMock = vi.hoisted(() => ({ isMac: false }))
vi.mock('../utils/platform', async (orig) => ({
  ...(await orig<typeof import('../utils/platform')>()),
  get isMac() { return platformMock.isMac },
}))
const mobileMock = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => mobileMock.mobile,
}))
vi.mock('../components/LazyCliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
}))
vi.mock('../components/terminalSessions', () => ({
  preloadTerminal: vi.fn(),
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
  mobileMock.mobile = false
})

async function openMenu() {
  await userEvent.click(screen.getByRole('button', { name: 'More actions' }))
  await act(async () => {})
}

async function closeMenu() {
  await userEvent.keyboard('{Escape}')
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

  it('Linux/Windows: Find shows Ctrl+Shift+F', async () => {
    platformMock.isMac = false
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    expect(screen.getByTestId('terminal-find-shortcut').textContent).toBe('Ctrl+Shift+F')
  })

  it('macOS: Find shows ⌘F', async () => {
    platformMock.isMac = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    expect(screen.getByTestId('terminal-find-shortcut').textContent).toBe('⌘F')
  })
})

describe('TerminalTabsView header: Fullscreen toggle', () => {
  it('docked: the header holds More actions and Fullscreen, and Hide is a menu row', async () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('button', { name: 'Fullscreen' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Hide terminal panel' })).toBeNull()
    await openMenu()
    expect(screen.getByRole('menuitem', { name: 'Hide terminal panel' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /fullscreen/i })).toBeNull()
    await closeMenu()
  })

  it('one button enters and exits fullscreen and keeps focus across the flip', async () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    const toggle = screen.getByTestId('terminal-fullscreen-toggle')
    await userEvent.click(toggle)
    expect(screen.getByTestId('terminal-fullscreen-toggle')).toBe(toggle)
    expect(toggle).toHaveAccessibleName('Exit fullscreen')
    expect(toggle).toHaveFocus()
    await userEvent.click(toggle)
    expect(toggle).toHaveAccessibleName('Fullscreen')
    expect(toggle).toHaveFocus()
  })

  it('fullscreen: the menu keeps Hide and has no Fullscreen row, and Hide closes the panel', async () => {
    openBottomTerminal()
    act(() => { setTerminalFullscreen(true) })
    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument()
    await openMenu()
    expect(screen.queryByRole('menuitem', { name: /fullscreen/i })).toBeNull()
    await userEvent.click(screen.getByRole('menuitem', { name: 'Hide terminal panel' }))
    await act(async () => {})
    expect(isBottomTerminalOpen()).toBe(false)
  })

  it('Linux/Windows: the toggle tooltip is its label, with no aria-keyshortcuts', () => {
    platformMock.isMac = false
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    const toggle = screen.getByTestId('terminal-fullscreen-toggle')
    expect(toggle).toHaveAttribute('title', 'Fullscreen')
    expect(toggle).not.toHaveAttribute('aria-keyshortcuts')
  })

  it('macOS: the toggle tooltip names ⇧⌘F in both states and sets aria-keyshortcuts', async () => {
    platformMock.isMac = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    const toggle = screen.getByTestId('terminal-fullscreen-toggle')
    expect(toggle).toHaveAttribute('title', 'Fullscreen (⇧⌘F)')
    expect(toggle).toHaveAttribute('aria-keyshortcuts', 'Meta+Shift+F')
    expect(toggle).toHaveAccessibleName('Fullscreen')
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('title', 'Exit fullscreen (⇧⌘F)')
  })

  it('macOS: ⇧⌘F on the focused toggle toggles fullscreen and is claimed from the global shortcut', async () => {
    platformMock.isMac = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    const toggle = screen.getByTestId('terminal-fullscreen-toggle')
    const seen: boolean[] = []
    const onDocKey = (e: KeyboardEvent) => { if (e.code === 'KeyF') seen.push(e.defaultPrevented) }
    document.addEventListener('keydown', onDocKey)
    try {
      await userEvent.click(toggle)
      expect(toggle).toHaveAccessibleName('Exit fullscreen')
      await userEvent.keyboard('{Meta>}{Shift>}f{/Shift}{/Meta}')
      expect(toggle).toHaveAccessibleName('Fullscreen')
      expect(seen).toEqual([true])
    } finally {
      document.removeEventListener('keydown', onDocKey)
    }
  })

  it('mobile: no toggle, and Hide stays in the header and closes the panel', async () => {
    mobileMock.mobile = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.queryByTestId('terminal-fullscreen-toggle')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Hide terminal panel' }))
    expect(isBottomTerminalOpen()).toBe(false)
  })

  it('mobile: the menu has no Hide row', async () => {
    mobileMock.mobile = true
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await openMenu()
    expect(screen.queryByTestId('terminal-menu-hide')).toBeNull()
    await closeMenu()
  })

  it('narrowing into mobile while fullscreen drops fullscreen and brings Hide back', async () => {
    openBottomTerminal()
    act(() => { setTerminalFullscreen(true) })
    const { rerender } = renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument()
    mobileMock.mobile = true
    rerender(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    expect(screen.queryByTestId('terminal-fullscreen-toggle')).toBeNull()
    expect(screen.getByRole('button', { name: 'Hide terminal panel' })).toBeInTheDocument()
    mobileMock.mobile = false
    rerender(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    expect(screen.getByTestId('terminal-fullscreen-toggle')).toHaveAccessibleName('Fullscreen')
  })

  it('popout: no Fullscreen toggle', () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="popout" />)
    expect(screen.queryByTestId('terminal-fullscreen-toggle')).toBeNull()
  })
})
