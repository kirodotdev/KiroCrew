/**
 * The docked panel holds a first-open terminal (CliPanel `deferOpen`) from
 * the render that opens the dock until the slide is over, and opens it even
 * if no animation-complete ever arrives. Every reopen holds again.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import BottomTerminalPanel from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal, closeBottomTerminal } from '../hooks/useBottomTerminal'

const seen = vi.hoisted(() => ({ deferOpen: [] as (boolean | undefined)[] }))
vi.mock('../components/CliPanel', () => ({
  default: ({ deferOpen }: { deferOpen?: boolean }) => { seen.deferOpen.push(deferOpen); return <div data-testid="cli" /> },
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

describe('BottomTerminalPanel deferOpen', () => {
  beforeEach(() => { __resetBottomTerminal(); seen.deferOpen.length = 0; vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); __resetBottomTerminal() })

  it('holds the terminal from the opening render until the slide settles, on every open', () => {
    renderWithProviders(<BottomTerminalPanel />)
    for (let round = 0; round < 2; round++) {
      seen.deferOpen.length = 0
      act(() => openBottomTerminal())
      // The very first render of the terminal already holds it.
      expect(seen.deferOpen[0]).toBe(true)
      act(() => { vi.advanceTimersByTime(1000) })
      expect(seen.deferOpen.at(-1)).toBe(false)
      act(() => closeBottomTerminal())
      act(() => { vi.advanceTimersByTime(1000) })
    }
  })
})
