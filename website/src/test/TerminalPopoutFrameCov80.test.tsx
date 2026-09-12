// TerminalPopoutFrame is the window shell for the popped-out terminal panel. Its
// only real logic is the tab lifecycle: a deep-linked popout with no tabs mints
// one, but once tabs have existed, losing the last one returns the panel to the
// main window instead of leaving an empty shell behind.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const { registerPopout, unregister, closeSelfToMain, revealReturn, openBottomTerminal, useBottomTerminal, disposeTerminalConnection, tabsBox } = vi.hoisted(() => {
  const unregister = vi.fn()
  const tabsBox = { tabs: [] as { id: string }[], epoch: 0, retired: false, preparing: false }
  return {
    registerPopout: vi.fn(() => unregister),
    unregister,
    closeSelfToMain: vi.fn(),
    revealReturn: vi.fn(),
    openBottomTerminal: vi.fn(),
    tabsBox,
    useBottomTerminal: vi.fn(() => ({ ...tabsBox })),
    disposeTerminalConnection: vi.fn(),
  }
})

vi.mock('../utils/terminalPopout', () => ({ registerPopout, closeSelfToMain, returnSelfToMain: revealReturn }))
vi.mock('../utils/terminalRegistry', () => ({ disposeTerminalConnection }))
vi.mock('../hooks/useBottomTerminal', () => ({
  useBottomTerminal,
  openBottomTerminal,
}))
vi.mock('../components/BottomTerminalPanel', () => ({
  TerminalTabsView: (p: Record<string, unknown>) => (
    <div data-testid="terminal-tabs" data-variant={String(p.variant)} data-scope={String(p.sessionScope)} />
  ),
}))

import TerminalPopoutFrame from '../pages/TerminalPopoutFrame'

beforeEach(() => {
  registerPopout.mockClear()
  unregister.mockClear()
  closeSelfToMain.mockClear()
  revealReturn.mockClear()
  openBottomTerminal.mockClear()
  useBottomTerminal.mockClear()
  disposeTerminalConnection.mockClear()
  tabsBox.tabs = []; tabsBox.epoch = 0; tabsBox.retired = false; tabsBox.preparing = false
  document.title = ''
  window.history.replaceState({}, '', '/popout/terminal')
})

afterEach(() => { window.history.replaceState({}, '', '/') })

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
    expect(closeSelfToMain).not.toHaveBeenCalled()
    expect(openBottomTerminal).toHaveBeenCalledWith(undefined, null)
  })

  it('captures origin scope once for registration, membership and tab actions', () => {
    window.history.replaceState({}, '', '/popout/terminal?sid=chat-A')
    tabsBox.tabs = [{ id: 'a1' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    expect(registerPopout).toHaveBeenCalledWith('chat-A', expect.any(Function))
    expect(useBottomTerminal).toHaveBeenLastCalledWith('chat-A')
    expect(screen.getByTestId('terminal-tabs')).toHaveAttribute('data-scope', 'chat-A')
    window.history.replaceState({}, '', '/popout/terminal?sid=chat-B')
    rerender(<TerminalPopoutFrame />)
    expect(useBottomTerminal).toHaveBeenLastCalledWith('chat-A')
    expect(registerPopout).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('terminal-tabs')).toHaveAttribute('data-scope', 'chat-A')
  })

  it('creates a missing terminal only in the URL session', () => {
    window.history.replaceState({}, '', '/popout/terminal?sid=chat-A')
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).toHaveBeenCalledWith(undefined, 'chat-A')
  })

  it('releases only its current tabs on native close and before unregistering', () => {
    tabsBox.tabs = [{ id: 'a1' }]
    const { rerender, unmount } = render(<TerminalPopoutFrame />)
    tabsBox.tabs = [{ id: 'a1' }, { id: 'a2' }]
    rerender(<TerminalPopoutFrame />)
    window.dispatchEvent(new Event('pagehide'))
    expect(disposeTerminalConnection.mock.calls).toEqual([['a1'], ['a2']])
    expect(closeSelfToMain).not.toHaveBeenCalled()
    unmount()
    expect(disposeTerminalConnection.mock.invocationCallOrder.at(-1)).toBeLessThan(unregister.mock.invocationCallOrder[0])
  })

  it('does not mint a second tab while one exists', () => {
    tabsBox.tabs = [{ id: 't1' }]
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })

  it('closes without revealing or minting a terminal after the last tab closes', () => {
    window.history.replaceState({}, '', '/popout/terminal?sid=chat-A')
    tabsBox.tabs = [{ id: 't1' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    tabsBox.tabs = []
    rerender(<TerminalPopoutFrame />)
    expect(closeSelfToMain).toHaveBeenCalledTimes(1)
    expect(revealReturn).not.toHaveBeenCalled()
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })
  it('does not mint in a retired scope even on a cold popup load', () => {
    tabsBox.retired = true; tabsBox.epoch = 1
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).not.toHaveBeenCalled()
    expect(closeSelfToMain).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('terminal-tabs')).not.toBeInTheDocument()
  })
  it('closes an old popup without rendering or disposing the revived generation', () => {
    tabsBox.tabs = [{ id: 'old-A' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    tabsBox.tabs = [{ id: 'new-A' }]; tabsBox.epoch = 2
    rerender(<TerminalPopoutFrame />)
    expect(screen.queryByTestId('terminal-tabs')).not.toBeInTheDocument()
    expect(closeSelfToMain).toHaveBeenCalledTimes(1)
    const release = registerPopout.mock.calls[0][1] as () => void
    release()
    expect(disposeTerminalConnection).toHaveBeenCalledWith('old-A')
    expect(disposeTerminalConnection).not.toHaveBeenCalledWith('new-A')
  })

  it('keeps the popup visible throughout unconfirmed preparation and cancellation', () => {
    tabsBox.tabs = [{ id: 'running-A' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    tabsBox.preparing = true; rerender(<TerminalPopoutFrame />)
    expect(screen.getByTestId('terminal-tabs')).toBeInTheDocument()
    expect(closeSelfToMain).not.toHaveBeenCalled()
    tabsBox.preparing = false; rerender(<TerminalPopoutFrame />)
    expect(screen.getByTestId('terminal-tabs')).toBeInTheDocument()
    expect(closeSelfToMain).not.toHaveBeenCalled()
    expect(disposeTerminalConnection).not.toHaveBeenCalled()
  })

})
