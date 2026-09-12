import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
vi.mock('../hooks/useBottomTerminal', () => ({ refreshTerminalState: async () => true }))

import {
  openPopout,
  focusPopout,
  bringBack,
  isPopoutOpen,
  isSelfPopout,
  registerPopout,
  returnSelfToMain,
  useTerminalPoppedOut,
  hasFreshBeacon,
  subscribe,
  getSnapshot,
  __setNavigateForTests,
  __resetForTests,
  TERMINAL_POPOUT_ID,
  type PopoutMsg,
} from '../utils/terminalPopout'

/**
 * The window-control + liveness half of the terminal popout (the identity and
 * beacon-math half is covered by terminalPopout.test.ts). PTY ownership hangs
 * off these: a main window that wrongly reads "no popout" mounts the docked
 * panel and steals the popout's sockets, so open/focus/bring-back and the
 * hook's union of both liveness sources are pinned here.
 */
const BEACON_KEY = 'mc-terminal-popout-alive'

class Channel {
  static current: Channel
  onmessage: ((event: { data: PopoutMsg }) => void) | null = null
  constructor() { Channel.current = this }
  postMessage() {}
  close() {}
}
function announceClosed() {
  Channel.current.onmessage?.({ data: { t: 'close', id: TERMINAL_POPOUT_ID } })
}

function fakeWindow(): { focus: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn>; closed: boolean } {
  return { focus: vi.fn(), close: vi.fn(), closed: false }
}

function Probe() {
  const poppedOut = useTerminalPoppedOut(null)
  return <span data-testid="probe">{poppedOut ? 'zzz-live' : 'zzz-docked'}</span>
}

beforeEach(() => {
  vi.stubGlobal('BroadcastChannel', Channel)
  localStorage.removeItem(BEACON_KEY)
  __resetForTests()
})

afterEach(() => {
  __resetForTests()
  localStorage.removeItem(BEACON_KEY)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('terminalPopout window control', () => {
  it('opens a window and optimistically marks the panel popped out', () => {
    const win = fakeWindow()
    const open = vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)

    expect(isPopoutOpen(null)).toBe(false)
    openPopout(null)

    expect(open).toHaveBeenCalledTimes(1)
    const [url, name] = open.mock.calls[0]
    expect(String(url)).toBe(`${window.location.origin}/popout/terminal`)
    expect(name).toBe('mc-popout-terminal')
    // Synchronously true — callers distinguish a real open from a vetoed one
    // without waiting for a heartbeat round-trip.
    expect(isPopoutOpen(null)).toBe(true)
    expect(getSnapshot().has(TERMINAL_POPOUT_ID)).toBe(true)
    expect(win.focus).toHaveBeenCalled()
  })

  it('focuses the existing window instead of opening a second one', () => {
    const win = fakeWindow()
    const open = vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)
    openPopout(null)
    win.focus.mockClear()

    openPopout(null)
    expect(open).toHaveBeenCalledTimes(1)
    expect(win.focus).toHaveBeenCalledTimes(1)
  })

  it('does NOT mark the panel popped out when the popup is blocked', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    openPopout(null)

    expect(isPopoutOpen(null)).toBe(false)
    expect(alert).toHaveBeenCalled()
    expect(warn).toHaveBeenCalled()
  })

  it('focuses through the held handle', () => {
    const win = fakeWindow()
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)
    openPopout(null)
    win.focus.mockClear()

    focusPopout(null)
    expect(win.focus).toHaveBeenCalledTimes(1)
  })

  it('bringBack retains ownership until the popup announces close', () => {
    const win = fakeWindow()
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)
    openPopout(null)

    bringBack(null)
    expect(win.close).not.toHaveBeenCalled()
    expect(isPopoutOpen(null)).toBe(true)
    announceClosed()
    expect(isPopoutOpen(null)).toBe(false)
  })
})

describe('terminalPopout responder role', () => {
  it('registerPopout claims the singleton id and writes the liveness beacon', () => {
    expect(isSelfPopout(null)).toBe(false)
    const cleanup = registerPopout(null)

    expect(isSelfPopout(null)).toBe(true)
    expect(hasFreshBeacon(null)).toBe(true)

    cleanup()
    expect(isSelfPopout(null)).toBe(false)
    // Beacon cleared on teardown, or a reloaded main window would keep the
    // panel undocked with no popout alive.
    expect(localStorage.getItem(BEACON_KEY)).toBeNull()
  })

  it('returnSelfToMain navigates to the dashboard when close is refused', () => {
    const navigate = vi.fn()
    __setNavigateForTests(navigate)
    const cleanup = registerPopout(null)

    returnSelfToMain(null)
    // happy-dom keeps the window alive, which is exactly the deep-linked
    // (no script opener) case: the control must still do something visible.
    expect(navigate).toHaveBeenCalledWith('/')
    cleanup()
  })
})

describe('useTerminalPoppedOut', () => {
  it('is false with no popout and no beacon', () => {
    render(<Probe />)
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-docked')
  })

  it('reads a fresh beacon synchronously on first render (main-window reload)', () => {
    localStorage.setItem(BEACON_KEY, String(Date.now()))
    render(<Probe />)
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-live')
  })

  it('re-evaluates on a cross-window storage event for the beacon key', () => {
    render(<Probe />)
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-docked')

    localStorage.setItem(BEACON_KEY, String(Date.now()))
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: BEACON_KEY }))
    })
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-live')
  })

  it('ignores storage events for unrelated keys', () => {
    render(<Probe />)
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'zzz-unrelated' }))
    })
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-docked')
  })

  it('tracks the BroadcastChannel map too (open → live, bring back → docked)', async () => {
    const win = fakeWindow()
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)
    render(<Probe />)

    act(() => { openPopout(null) })
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-live')

    await act(async () => { bringBack(null); announceClosed() })
    expect(screen.getByTestId('probe')).toHaveTextContent('zzz-docked')
  })
})

describe('terminalPopout subscribe', () => {
  it('notifies a listener when the map changes and stops after unsubscribe', () => {
    const win = fakeWindow()
    vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window)
    const seen = vi.fn()
    const unsub = subscribe(seen)

    openPopout(null)
    expect(seen).toHaveBeenCalled()

    unsub()
    seen.mockClear()
    bringBack(null)
    expect(seen).not.toHaveBeenCalled()
  })
})
