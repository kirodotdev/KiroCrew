/**
 * Silence watchdog on the Mochi panel's `/api/ws` socket.
 *
 * The panel shares the dashboard socket for the live chat stream, and like the
 * dashboard's own hook it reconnected only from `onclose` — so a socket that
 * stayed OPEN while its transport was gone (a phone off-network, an OS-frozen tab)
 * left the pet's chat silent until a reload. It now shares the dashboard's silence
 * watchdog (`utils/socketSilenceWatchdog`): a visible socket silent for
 * WS_SILENCE_MS is replaced, and a socket still receiving the gateway's 5s
 * `dashboard` status frame is not.
 *
 * `vi.useFakeTimers()` mocks both `setInterval` (the check) AND the wall clock the
 * watchdog reads by default, so advancing timers advances the silence window too.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

import {
  WS_SILENCE_MS,
  WS_SILENCE_CHECK_MS,
} from '../../../utils/socketSilenceWatchdog'

/** Records EVERY socket the bridge opens, so a replacement is observable. */
class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSED = 3
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(readonly url: string) { FakeWebSocket.instances.push(this) }

  open(): void { this.readyState = FakeWebSocket.OPEN; this.onopen?.() }
  close(): void { this.closed = true; this.readyState = FakeWebSocket.CLOSED; this.onclose?.() }
  status(): void { this.onmessage?.({ data: JSON.stringify({ type: 'dashboard', data: {} }) }) }
}

describe('mochi panelBridge socket silence watchdog', () => {
  let bridge: typeof import('../panel/panelBridge')
  let hidden = false

  beforeEach(async () => {
    vi.resetModules()
    vi.useFakeTimers()
    hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    bridge = await import('../panel/panelBridge')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  /** Advance fake time, which fires the check interval AND moves the fake clock. */
  function advance(ms: number) { vi.advanceTimersByTime(ms) }

  function openFirst() {
    // Subscribing opens the socket (connect() runs on first subscribe).
    bridge.onChatDone(() => {})
    FakeWebSocket.instances[0].open()
  }

  it('replaces a silent OPEN socket after the window', () => {
    openFirst()
    expect(FakeWebSocket.instances).toHaveLength(1)

    advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)

    expect(FakeWebSocket.instances[0].closed).toBe(true)
    expect(FakeWebSocket.instances).toHaveLength(2)
    // The replacement opens and carries the watchdog forward.
    FakeWebSocket.instances[1].open()
    expect(FakeWebSocket.instances[1].url).toContain('/api/ws')
  })

  it('keeps a socket that receives the 5s status frame', () => {
    openFirst()

    for (let elapsed = 0; elapsed < WS_SILENCE_MS * 4; elapsed += WS_SILENCE_CHECK_MS) {
      FakeWebSocket.instances[0].status()
      advance(WS_SILENCE_CHECK_MS)
    }

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].closed).toBe(false)
  })

  it('leaves a silent socket alone while the page is hidden', () => {
    openFirst()
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))

    advance(WS_SILENCE_MS * 3)

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].closed).toBe(false)
  })
})
