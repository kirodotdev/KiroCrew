import { act, screen } from '@testing-library/react'
import { beforeEach, describe, it, vi } from 'vitest'
import { expectWheelToScrollRow, renderWithProviders } from './helpers'
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

describe('terminal tab strip wheel', () => {
  it('scrolls sideways on a vertical wheel only while the tabs are clipped', async () => {
    addTab('/project')
    addTab('/other')
    renderWithProviders(<TerminalTabsView variant="dock" />)
    await act(async () => {})
    expectWheelToScrollRow(screen.getByRole('tablist'))
  })
})
