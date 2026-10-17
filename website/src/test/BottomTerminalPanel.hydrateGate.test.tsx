/**
 * The terminal tab view mounts nothing for restored tabs until the backend has
 * ruled on them (#10977).
 *
 * The gate exists because of effect order: a CliPanel mounted for a restored
 * tab opens its WebSocket before the App-level probe fires, and the WS route
 * mints a fresh PTY for an id it does not know — so the probe would then find
 * the very shell it was asked about and keep a tab nobody wanted back. Boots
 * the store from seeded storage so the restored set is real, not simulated.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => '',
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => false),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))
vi.mock('../hooks/usePanelTabs', () => ({
  usePanelTabs: () => ({}),
}))

const STORAGE_KEY = 'mc-bottom-terminal'

let store: typeof import('../hooks/useBottomTerminal') | null = null

afterEach(() => {
  store?.__resetBottomTerminal()
  store = null
  localStorage.clear()
})

describe('TerminalTabsView — hydrate gate', () => {
  it('draws no tab or terminal until the restored set is reconciled, then only the kept ones', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      open: true, tabs: [{ id: 'live' }, { id: 'gone' }], activeId: 'gone',
    }))
    vi.resetModules()
    store = await import('../hooks/useBottomTerminal')
    const { TerminalTabsView } = await import('../components/BottomTerminalPanel')
    expect(store.isTerminalHydratePending()).toBe(true)

    renderWithProviders(<TerminalTabsView variant="dock" />)

    // Pending: no CliPanel has connected, and no chip offers to close a tab
    // whose shell may already be gone.
    expect(screen.queryByTestId('cli-live')).toBeNull()
    expect(screen.queryByTestId('cli-gone')).toBeNull()
    expect(screen.queryByRole('tablist')).toBeNull()

    const first = { enabled: true, sessions: [{ session_id: 'live', alive: true }] }
    act(() => { store!.reconcileRestoredTabs(first) })

    // A suspect is not yet gone: still gated through the confirm look, so no
    // view connects to either tab while the second answer is in flight.
    expect(screen.queryByTestId('cli-live')).toBeNull()
    expect(screen.queryByRole('tablist')).toBeNull()

    act(() => { store!.confirmRestoredTabs(first) })

    expect(screen.getByTestId('cli-live')).toBeInTheDocument()
    expect(screen.queryByTestId('cli-gone')).toBeNull()
    expect(screen.getByRole('tablist')).toBeInTheDocument()
  })

  it('mounts every restored tab when the first look could not rule', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      open: true, tabs: [{ id: 'a' }, { id: 'b' }], activeId: 'a',
    }))
    vi.resetModules()
    store = await import('../hooks/useBottomTerminal')
    const { TerminalTabsView } = await import('../components/BottomTerminalPanel')

    renderWithProviders(<TerminalTabsView variant="dock" />)
    expect(screen.queryByTestId('cli-a')).toBeNull()

    act(() => { store!.reconcileRestoredTabs(null) })

    expect(screen.getByTestId('cli-a')).toBeInTheDocument()
    expect(screen.getByTestId('cli-b')).toBeInTheDocument()
  })
})
