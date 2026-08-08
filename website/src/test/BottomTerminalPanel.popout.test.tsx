/**
 * Terminal panel pop-out behavior (issue #2004).
 *
 * Pins the three things that make the pop-out handoff correct:
 *
 * 1. The dock variant's "Pop out to window" control must release THIS window's
 *    WebSocket for EVERY tab (the PTYs stay alive server-side) before opening
 *    the popout window — otherwise the two windows fight over the sockets
 *    (backend replaces the socket on reconnect).
 * 2. The popout variant swaps the dock-only chrome: no move-to-chat on chips
 *    and no hide button (there is no chat/dock in that window), a "Return"
 *    control instead.
 * 3. The bottom-terminal store adopts tab-list changes made by ANOTHER window
 *    via localStorage `storage` events, so the two windows always agree on the
 *    tab list.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { TerminalTabsView, TerminalDetachedBar } from '../components/BottomTerminalPanel'
import {
  __resetBottomTerminal, openBottomTerminal, addTab, useBottomTerminal,
} from '../hooks/useBottomTerminal'
import { disposeTerminalConnection } from '../utils/terminalRegistry'
import { openPopout, isPopoutOpen, focusPopout, bringBack, returnSelfToMain } from '../utils/terminalPopout'
import { renderHook } from '@testing-library/react'

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => '',
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => true),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))
vi.mock('../hooks/usePanelTabs', () => ({
  usePanelTabs: () => ({ adoptTerminal: vi.fn() }),
}))

beforeEach(() => {
  __resetBottomTerminal()
  vi.clearAllMocks()
})
afterEach(() => {
  __resetBottomTerminal()
})

describe('TerminalTabsView dock variant — pop out', () => {
  it('opens the popout window, then releases every tab WebSocket', async () => {
    openBottomTerminal()
    const second = addTab()
    renderWithProviders(<TerminalTabsView variant="dock" />)

    await userEvent.click(screen.getByRole('button', { name: 'Pop out to window' }))

    // The popout window opened…
    expect(openPopout).toHaveBeenCalledTimes(1)
    // …and both tabs' local connections were released (PTYs stay alive
    // server-side; the popout reconnects with scrollback replay).
    const disposed = vi.mocked(disposeTerminalConnection).mock.calls.map(c => c[0])
    expect(disposed).toHaveLength(2)
    expect(disposed).toContain(second)
  })

  it('keeps every socket when window.open is vetoed (popup blocker)', async () => {
    // Regression: sockets used to be disposed BEFORE window.open — a vetoed
    // popup left the dock rendered but permanently disconnected.
    vi.mocked(isPopoutOpen).mockReturnValueOnce(false)
    openBottomTerminal()
    addTab()
    renderWithProviders(<TerminalTabsView variant="dock" />)

    await userEvent.click(screen.getByRole('button', { name: 'Pop out to window' }))

    expect(openPopout).toHaveBeenCalledTimes(1)
    expect(disposeTerminalConnection).not.toHaveBeenCalled()
  })

  it('shows dock chrome: hide button present, no Return control', () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('button', { name: 'Hide terminal panel' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Return to main window/ })).not.toBeInTheDocument()
  })
})

describe('TerminalTabsView popout variant', () => {
  it('shows a Return control and hides the dock-only chrome', async () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="popout" />)

    // No pop-out / hide / move-to-chat controls in the popout window.
    expect(screen.queryByRole('button', { name: 'Pop out to window' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Hide terminal panel' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Move to side panel' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Return to main window/ }))
    expect(returnSelfToMain).toHaveBeenCalledTimes(1)
  })

  it('still offers the + new-terminal button', () => {
    openBottomTerminal()
    renderWithProviders(<TerminalTabsView variant="popout" />)
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeInTheDocument()
  })
})

describe('TerminalDetachedBar (main window while popped out)', () => {
  it('releases the tabs\' local sockets and offers explicit Focus / Return actions', async () => {
    openBottomTerminal()
    addTab()
    renderWithProviders(<TerminalDetachedBar />)

    // Popout owns the sockets -- this window releases its copies.
    expect(vi.mocked(disposeTerminalConnection).mock.calls).toHaveLength(2)

    // Explicit controls, no timing heuristics: focus is focus...
    await userEvent.click(screen.getByRole('button', { name: 'Focus popout' }))
    expect(focusPopout).toHaveBeenCalledTimes(1)
    expect(bringBack).not.toHaveBeenCalled()
    // ...and re-dock only on the explicit Return action.
    await userEvent.click(screen.getByRole('button', { name: 'Return to dock' }))
    expect(bringBack).toHaveBeenCalledTimes(1)
  })
})

describe('useBottomTerminal cross-window storage sync', () => {
  it('adopts tab-list changes written by another window', () => {
    openBottomTerminal()
    const { result } = renderHook(() => useBottomTerminal())
    expect(result.current.tabs).toHaveLength(1)

    // Another window (the popout) added a tab and persisted the store.
    const foreign = {
      open: true,
      height: 300,
      tabs: [...result.current.tabs, { id: 'from-popout' }],
      activeId: 'from-popout',
    }
    act(() => {
      localStorage.setItem('mc-bottom-terminal', JSON.stringify(foreign))
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'mc-bottom-terminal',
        newValue: JSON.stringify(foreign),
      }))
    })

    expect(result.current.tabs.map(t => t.id)).toContain('from-popout')
    expect(result.current.activeId).toBe('from-popout')
  })

  it('ignores storage events for other keys', () => {
    openBottomTerminal()
    const { result } = renderHook(() => useBottomTerminal())
    const before = result.current
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated', newValue: '{}' }))
    })
    expect(result.current).toBe(before)
  })
})
