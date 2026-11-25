import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { expectWheelToScrollRow, openAllTabsMenu, renderWithProviders, stubTabRow } from './helpers'
import { probeTargets } from '../guide/probeRegistry'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, addTab } from '../hooks/useBottomTerminal'

vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => 'workspace',
  getTerminalTitle: () => 'workspace',
  disposeTerminalConnection: vi.fn(),
}))

beforeEach(() => { __resetBottomTerminal() })
afterEach(() => { vi.restoreAllMocks() })

describe('terminal tab strip wheel', () => {
  it('scrolls sideways on a vertical wheel only while the tabs are clipped', async () => {
    addTab('/project')
    addTab('/other')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    expectWheelToScrollRow(screen.getByRole('tablist'))
  })
})

describe('terminal tab strip all-tabs menu', () => {
  it('lists every terminal in the dock\'s More actions menu and switches to the one picked', async () => {
    addTab('/a')
    addTab('/b')
    addTab('/c')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    // One menu on the dock strip: no separate tab-list chevron beside +.
    expect(screen.queryByTestId('all-tabs-trigger')).not.toBeInTheDocument()
    const rows = openAllTabsMenu('terminal-more-trigger')
    expect(rows).toHaveLength(3)
    // The panel's own actions share the menu.
    expect(screen.getByRole('menuitem', { name: 'Pop out to window' })).toBeInTheDocument()
    act(() => { fireEvent.click(rows[0]) })
    expect(screen.getAllByRole('tab').map(t => t.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false'])
  })

  it('keeps the dock\'s More actions menu a guide probe target, so a guide can open it to find an item', async () => {
    addTab('/a')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    const more = screen.getByTestId('terminal-more-trigger')
    expect(probeTargets().some(t => t.kind === 'popup' && t.trigger() === more)).toBe(true)
  })

  it('keeps the tab-list chevron in the popout, which has no More actions menu', async () => {
    addTab('/a')
    addTab('/b')
    renderWithProviders(<TerminalTabsView variant="popout" />)
    await act(async () => {})
    expect(screen.queryByTestId('terminal-more-trigger')).not.toBeInTheDocument()
    expect(openAllTabsMenu()).toHaveLength(2)
    expect(screen.queryByRole('menuitem', { name: 'Close all terminals' })).not.toBeInTheDocument()
  })

  it('scrolls a new terminal into view', async () => {
    addTab('/a')
    addTab('/b')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    const row = stubTabRow(screen.getByRole('tablist'))
    let id = ''
    act(() => { id = addTab('/c') ?? '' })
    expect(row.tabRect(id).right).toBeLessThanOrEqual(100)
    expect(row.left()).toBeGreaterThan(0)
  })
})
