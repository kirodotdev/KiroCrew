/**
 * On a phone, a closed dock with a restored terminal tab builds that tab's
 * xterm off screen at an idle moment, so the tap that opens the dock does not
 * have to. Nothing is built on a wide screen, without a tab, or while open.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import BottomTerminalPanel from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal, closeBottomTerminal } from '../hooks/useBottomTerminal'

const h = vi.hoisted(() => ({ prewarm: vi.fn(), mobile: { value: true } }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
  prewarmTerminal: h.prewarm,
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => h.mobile.value }))
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

/** Leave the dock closed with one tab in it, as a reload restores it. */
function closedWithTab() {
  act(() => openBottomTerminal())
  act(() => closeBottomTerminal())
}

describe('BottomTerminalPanel prewarm', () => {
  beforeEach(() => { __resetBottomTerminal(); h.prewarm.mockClear(); h.mobile.value = true; vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); __resetBottomTerminal() })

  it('builds the restored tab off screen once the phone is idle', () => {
    closedWithTab()
    renderWithProviders(<BottomTerminalPanel />)
    expect(h.prewarm).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(5000) })
    expect(h.prewarm).toHaveBeenCalledTimes(1)
    expect(h.prewarm.mock.calls[0][0]).toMatch(/.+/)
  })

  it('builds nothing on a wide screen', () => {
    h.mobile.value = false
    closedWithTab()
    renderWithProviders(<BottomTerminalPanel />)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(h.prewarm).not.toHaveBeenCalled()
  })

  it('builds nothing without a terminal tab', () => {
    renderWithProviders(<BottomTerminalPanel />)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(h.prewarm).not.toHaveBeenCalled()
  })
})
