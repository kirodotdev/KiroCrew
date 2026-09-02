// TerminalPopoutFrame is the window shell for the popped-out terminal panel. Its
// only real logic is the tab lifecycle: a deep-linked popout with no tabs mints
// one, but once tabs have existed, losing the last one returns the panel to the
// main window instead of leaving an empty shell behind.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const { registerPopout, unregister, returnSelfToMain, openBottomTerminal, tabsBox } = vi.hoisted(() => {
  const unregister = vi.fn()
  return {
    registerPopout: vi.fn(() => unregister),
    unregister,
    returnSelfToMain: vi.fn(),
    openBottomTerminal: vi.fn(),
    tabsBox: { tabs: [] as { id: string }[] },
  }
})

vi.mock('../utils/terminalPopout', () => ({ registerPopout, returnSelfToMain }))
vi.mock('../hooks/useBottomTerminal', () => ({
  useBottomTerminal: () => ({ tabs: tabsBox.tabs }),
  openBottomTerminal,
}))
vi.mock('../components/BottomTerminalPanel', () => ({
  TerminalTabsView: (p: Record<string, unknown>) => (
    <div data-testid="terminal-tabs" data-variant={String(p.variant)} />
  ),
}))

import TerminalPopoutFrame from '../pages/TerminalPopoutFrame'

beforeEach(() => {
  registerPopout.mockClear()
  unregister.mockClear()
  returnSelfToMain.mockClear()
  openBottomTerminal.mockClear()
  tabsBox.tabs = []
  document.title = ''
})

describe('TerminalPopoutFrame', () => {
  it('renders the shared tab strip in its popout variant', () => {
    tabsBox.tabs = [{ id: 't1' }]
    render(<TerminalPopoutFrame />)
    expect(screen.getByTestId('terminal-tabs')).toHaveAttribute('data-variant', 'popout')
  })

  it('registers as the live terminal popout and unregisters on unmount', () => {
    tabsBox.tabs = [{ id: 't1' }]
    const { unmount } = render(<TerminalPopoutFrame />)
    expect(registerPopout).toHaveBeenCalled()
    unmount()
    expect(unregister).toHaveBeenCalled()
  })

  it('sets the OS window title', () => {
    tabsBox.tabs = [{ id: 't1' }]
    render(<TerminalPopoutFrame />)
    expect(document.title).toBe('Terminal — Kiro Crew')
  })

  it('mints a tab when deep-linked with none', () => {
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).toHaveBeenCalledTimes(1)
    expect(returnSelfToMain).not.toHaveBeenCalled()
  })

  it('does not mint a second tab while one exists', () => {
    tabsBox.tabs = [{ id: 't1' }]
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })

  it('returns to the main window once the last tab is closed', () => {
    tabsBox.tabs = [{ id: 't1' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    tabsBox.tabs = []
    rerender(<TerminalPopoutFrame />)
    expect(returnSelfToMain).toHaveBeenCalledTimes(1)
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })
})
