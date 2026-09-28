/**
 * The mobile bottom-float capsule (Settings' floating search) is `fixed` to
 * the viewport bottom — the same edge the bottom-docked terminal panel owns
 * while it is open. Issue #9251: the capsule rendered on top of the docked
 * terminal's lower rows. The fix suppresses the capsule while the terminal is
 * ENABLED and either popped out (TerminalDetachedBar is a full-width bottom
 * strip regardless of dock position) or open AND docked at the bottom. A
 * right-docked, closed, or feature-disabled terminal leaves the capsule alone
 * (the store's `open` flag is localStorage-persisted, so a disabled terminal
 * can still read open — App renders no panel then, and the capsule must not
 * yield to nothing).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'
import {
  openBottomTerminal,
  setTerminalPosition,
  __resetBottomTerminal,
} from '../hooks/useBottomTerminal'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'

// The capsule only exists on the mobile root list.
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true }))

// Controllable popped-out flag; the real hook needs cross-window beacons.
const { popoutState } = vi.hoisted(() => ({ popoutState: { out: false } }))
vi.mock('../utils/terminalPopout', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/terminalPopout')>()
  return { ...actual, useTerminalPoppedOut: () => popoutState.out }
})

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'about', label: 'About', icon: null },
]

function renderRootList() {
  return render(
    <MemoryRouter initialEntries={['/settings']}>
      <SidePanelLayout
        title="Settings"
        tabs={TABS}
        headerRight={<div data-testid="capsule-content">search</div>}
        headerRightDock="bottom-float"
      >
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('bottom-float capsule vs bottom-docked terminal (#9251)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetBottomTerminal()
    setTerminalEnabledFlag(true)
    popoutState.out = false
  })
  afterEach(() => {
    cleanup()
    __resetBottomTerminal()
    setTerminalEnabledFlag(false)
    vi.restoreAllMocks()
  })

  it('renders the capsule while the terminal is closed', () => {
    renderRootList()
    expect(screen.getByTestId('capsule-content')).toBeTruthy()
  })

  it('suppresses the capsule while the terminal is open and docked at the bottom', () => {
    renderRootList()
    act(() => {
      setTerminalPosition('bottom')
      openBottomTerminal()
    })
    expect(screen.queryByTestId('capsule-content')).toBeNull()
  })

  it('keeps the capsule while the terminal is open but docked on the right', () => {
    renderRootList()
    act(() => {
      setTerminalPosition('right')
      openBottomTerminal()
    })
    expect(screen.getByTestId('capsule-content')).toBeTruthy()
  })

  it('restores the capsule when the bottom-docked terminal moves to the right dock', () => {
    renderRootList()
    act(() => {
      setTerminalPosition('bottom')
      openBottomTerminal()
    })
    expect(screen.queryByTestId('capsule-content')).toBeNull()
    act(() => {
      setTerminalPosition('right')
    })
    expect(screen.getByTestId('capsule-content')).toBeTruthy()
  })

  it('keeps the capsule when the store reads open+bottom but the terminal feature is disabled', () => {
    renderRootList()
    act(() => {
      setTerminalPosition('bottom')
      openBottomTerminal()
      // Persisted-open trap: `open` survives in localStorage while
      // dashboard.terminal.enabled=false renders no panel at all.
      setTerminalEnabledFlag(false)
    })
    expect(screen.getByTestId('capsule-content')).toBeTruthy()
  })

  it('suppresses the capsule while the terminal is popped out, even right-docked', () => {
    // TerminalDetachedBar renders as a full-width bottom strip whenever the
    // terminal is enabled and popped out, regardless of the stored dock
    // position — so popout alone owns the bottom edge.
    popoutState.out = true
    setTerminalPosition('right')
    openBottomTerminal()
    renderRootList()
    expect(screen.queryByTestId('capsule-content')).toBeNull()
  })

  it('keeps the capsule when popped out but the terminal feature is disabled', () => {
    popoutState.out = true
    setTerminalEnabledFlag(false)
    renderRootList()
    expect(screen.getByTestId('capsule-content')).toBeTruthy()
  })
})
