// TerminalPopoutFrame is the window shell for the popped-out terminal panel. Its
// only real logic is the tab lifecycle: a deep-linked popout with no tabs mints
// one, but once tabs have existed, losing the last one returns the panel to the
// main window instead of leaving an empty shell behind.
// The off state is read from the enabled query and the registry's explicit-false
// mark, never from the registry flag: a refused dial in this window and a probe
// that says off both swap the tabs for a notice naming the switch.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'

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
  confirmRestoredTabs: vi.fn(),
  reconcileRestoredTabs: vi.fn(() => []),
}))
vi.mock('../hooks/usePanelTabs', () => ({ confirmRestoredPanelTerminals: vi.fn(), reconcileRestoredPanelTerminals: vi.fn(() => []) }))
vi.mock('../components/BottomTerminalPanel', () => ({
  TerminalTabsView: (p: Record<string, unknown>) => (
    <div data-testid="terminal-tabs" data-variant={String(p.variant)} />
  ),
}))

import TerminalPopoutFrame from '../pages/TerminalPopoutFrame'

const probe = (status: number, body?: unknown) =>
  server.use(http.get('/api/terminal/sessions', () => body === undefined ? new HttpResponse(null, { status }) : HttpResponse.json(body, { status })))

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrap = (el: React.ReactElement) => <QueryClientProvider client={qc}>{el}</QueryClientProvider>
  const utils = rtlRender(wrap(ui))
  return { ...utils, rerender: (next: React.ReactElement) => utils.rerender(wrap(next)) }
}

/** The probe has answered: the mint effect waits for that. */
const settled = () => waitFor(() => expect(screen.queryByTestId('terminal-tabs') ?? screen.queryByTestId('terminal-popout-off')).not.toBeNull())

beforeEach(() => {
  probe(200, { enabled: true, sessions: [] })
  setTerminalEnabledFlag(false)
  registerPopout.mockClear()
  unregister.mockClear()
  returnSelfToMain.mockClear()
  openBottomTerminal.mockClear()
  tabsBox.tabs = []
  document.title = ''
})

afterEach(() => { setTerminalEnabledFlag(false) })

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

  it('mints a tab when deep-linked with none, once the probe has answered enabled', async () => {
    render(<TerminalPopoutFrame />)
    expect(openBottomTerminal).not.toHaveBeenCalled()
    await waitFor(() => expect(openBottomTerminal).toHaveBeenCalledTimes(1))
    expect(returnSelfToMain).not.toHaveBeenCalled()
    expect(screen.queryByTestId('terminal-popout-off')).toBeNull()
  })

  it('T6: shows the off notice instead of tabs and never mints while the probe says off', async () => {
    probe(200, { enabled: false, sessions: [] })
    render(<TerminalPopoutFrame />)
    const notice = await screen.findByTestId('terminal-popout-off')
    expect(notice).toHaveTextContent('dashboard.terminal.enabled')
    expect(screen.queryByTestId('terminal-tabs')).toBeNull()
    expect(openBottomTerminal).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Back to the dashboard' }))
    expect(returnSelfToMain).toHaveBeenCalledTimes(1)
  })

  it('T6: an off popout keeps its notice when the last tab disappears, instead of leaving', async () => {
    // The tabs existed before the probe answered off; losing the last one must
    // not call returnSelfToMain while the terminal is off, nor mint a new tab.
    probe(200, { enabled: false, sessions: [] })
    tabsBox.tabs = [{ id: 't1' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    await screen.findByTestId('terminal-popout-off')
    tabsBox.tabs = []
    rerender(<TerminalPopoutFrame />)
    expect(screen.getByTestId('terminal-popout-off')).toBeInTheDocument()
    expect(returnSelfToMain).not.toHaveBeenCalled()
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })

  it('T6: a failed probe reads as enabled, so the tabs render and the mint happens', async () => {
    probe(500)
    render(<TerminalPopoutFrame />)
    await waitFor(() => expect(openBottomTerminal).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('terminal-tabs')).toBeInTheDocument()
  })

  it('does not mint a second tab while one exists', async () => {
    tabsBox.tabs = [{ id: 't1' }]
    render(<TerminalPopoutFrame />)
    await settled()
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })

  it('returns to the main window once the last tab is closed', async () => {
    tabsBox.tabs = [{ id: 't1' }]
    const { rerender } = render(<TerminalPopoutFrame />)
    await settled()
    tabsBox.tabs = []
    rerender(<TerminalPopoutFrame />)
    expect(returnSelfToMain).toHaveBeenCalledTimes(1)
    expect(openBottomTerminal).not.toHaveBeenCalled()
  })
})
