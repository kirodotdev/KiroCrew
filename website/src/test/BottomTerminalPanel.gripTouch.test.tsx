/**
 * The grip's pressed style is what makes Chromium treat it as a touch target
 * (hover styles do not count on touch). jsdom cannot hit-test, so this pins it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import BottomTerminalPanel from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'

vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  getTerminalTitle: () => '',
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
vi.mock('../hooks/usePanelTabs', () => ({ usePanelTabs: () => ({}) }))

describe('BottomTerminalPanel resize grip on touch', () => {
  beforeEach(() => { __resetBottomTerminal() })

  it('carries a pressed style, so a touch on it stays on it', () => {
    renderWithProviders(<BottomTerminalPanel />)
    act(() => openBottomTerminal())
    const grip = screen.getByRole('separator', { name: 'Resize terminal panel' })
    expect(grip.className).toContain('group/drag')
    expect(grip.querySelector('[class*="group-active/drag:"]')).not.toBeNull()
  })
})
