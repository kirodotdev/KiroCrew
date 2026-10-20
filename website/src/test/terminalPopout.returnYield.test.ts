import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PopoutMsg as Msg } from '../utils/popoutController'

/**
 * The dock `open` flag is per window. When ANOTHER main window clicks Return
 * on the detached bar, this window must yield its own flag: with it still set
 * from before the pop-out, both windows would re-dock and attach to one PTY,
 * the takeover #7638 describes. The popout window itself is not a yielder (it
 * handles bring-back by returning), and the window that sent the message never
 * receives it (BroadcastChannel does not self-deliver), so it keeps its flag.
 */
class StubChannel {
  static instances: StubChannel[] = []
  onmessage: ((e: { data: Msg }) => void) | null = null
  posted: Msg[] = []
  constructor(public name: string) { StubChannel.instances.push(this) }
  postMessage(msg: Msg) { this.posted.push(msg) }
  close() { /* noop */ }
}
function deliver(msg: Msg): void {
  const ch = StubChannel.instances[0]
  if (!ch) throw new Error('controller never opened a channel')
  ch.onmessage?.({ data: msg })
}

type Popout = typeof import('../utils/terminalPopout')
type Store = typeof import('../hooks/useBottomTerminal')
let popout: Popout
let store: Store

beforeEach(async () => {
  StubChannel.instances = []
  vi.stubGlobal('BroadcastChannel', StubChannel as unknown as typeof BroadcastChannel)
  vi.resetModules()
  store = await import('../hooks/useBottomTerminal')
  popout = await import('../utils/terminalPopout')
  store.__resetBottomTerminal()
})
afterEach(() => {
  popout.__resetForTests()
  store.__resetBottomTerminal()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('main window yields its dock on a foreign Return', () => {
  it('closes the panel here when another main window brings the terminal back', () => {
    store.openBottomTerminal()
    popout.subscribe(() => {}) // a main window: opens the channel, claims no popout id
    expect(store.isBottomTerminalOpen()).toBe(true)
    deliver({ t: 'bring-back', id: popout.TERMINAL_POPOUT_ID })
    expect(store.isBottomTerminalOpen()).toBe(false)
  })

  it('ignores a bring-back for another entity and other message kinds', () => {
    store.openBottomTerminal()
    popout.subscribe(() => {})
    deliver({ t: 'bring-back', id: 'chat-1' })
    deliver({ t: 'focus', id: popout.TERMINAL_POPOUT_ID })
    deliver({ t: 'ping' })
    expect(store.isBottomTerminalOpen()).toBe(true)
  })

  it('the popout window returns itself instead of touching the flag', () => {
    store.openBottomTerminal()
    popout.registerPopout()
    const close = vi.spyOn(window, 'close').mockImplementation(() => {})
    const navigated: string[] = []
    popout.__setNavigateForTests(url => navigated.push(url))
    deliver({ t: 'bring-back', id: popout.TERMINAL_POPOUT_ID })
    expect(close).toHaveBeenCalledTimes(1)
    expect(store.isBottomTerminalOpen()).toBe(true)
  })
})
