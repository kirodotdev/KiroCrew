import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { expectWheelToScrollRow, openAllTabsMenu, renderWithProviders, stubTabRow } from './helpers'
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
  it('lists every terminal and switches to the one picked', async () => {
    addTab('/a')
    addTab('/b')
    addTab('/c')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    const rows = openAllTabsMenu()
    expect(rows).toHaveLength(3)
    act(() => { fireEvent.click(rows[0]) })
    expect(screen.getAllByRole('tab').map(t => t.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false'])
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
