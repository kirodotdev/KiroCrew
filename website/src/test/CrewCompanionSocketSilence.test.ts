/**
 * Silence watchdog on the crew-companion `/api/ws` socket.
 *
 * The companion watches the gateway for turn completion over the same dashboard
 * socket Mochi uses, and like it the socket reconnected only from `onclose` — so a
 * socket that stayed OPEN while its transport was gone (a phone off-network, an
 * OS-frozen tab) sat stale, and the "tell me when sessions are done" bubble simply
 * never fired again until a reload. It now shares the dashboard's silence watchdog
 * (`utils/socketSilenceWatchdog`): a visible page whose socket is silent for
 * WS_SILENCE_MS is replaced, and a socket still receiving the gateway's 5s
 * `dashboard` status frame is not.
 *
 * The clock is the watcher's injected `now`, so the window is driven by `setNow`;
 * the check interval is a real `setInterval` driven by fake timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { watchSessions } from '../apps/crew-companion/sessionWatch'
import {
  WS_SILENCE_MS,
  WS_SILENCE_CHECK_MS,
} from '../utils/socketSilenceWatchdog'

/** A socket the test opens, feeds frames, and asks whether it was closed. */
function fakeSocket() {
  const ws = {
    readyState: 0, // CONNECTING
    onopen: null as null | (() => void),
    onmessage: null as null | ((ev: { data: string }) => void),
    onclose: null as null | (() => void),
    onerror: null as null | (() => void),
    closed: false,
    close() { this.closed = true; this.readyState = 3; this.onclose?.() },
    open() { this.readyState = 1; this.onopen?.() },
    status() { this.onmessage?.({ data: JSON.stringify({ type: 'dashboard', data: {} }) }) },
  }
  return ws
}

const STATUS = 1 // WebSocket.OPEN

describe('crew-companion socket silence watchdog', () => {
  let hidden = false

  beforeEach(() => {
    vi.useFakeTimers()
    hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    // WebSocket.OPEN is read inside the watchdog's isOpen predicate.
    vi.stubGlobal('WebSocket', { OPEN: STATUS })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  /**
   * Drive the watcher with a chain of sockets: each `connect()` hands out the next
   * fake, so a replacement is observable as a new socket in the list. `t` is the
   * watchdog's clock; advancing it AND firing the real interval is how a silence
   * window elapses.
   */
  function harness() {
    const sockets: ReturnType<typeof fakeSocket>[] = []
    let t = 1_000_000
    const connect = () => {
      const ws = fakeSocket()
      sockets.push(ws)
      return ws as unknown as WebSocket
    }
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => true,
      now: () => t,
      connect,
    })
    return {
      sockets,
      stop,
      setNow: (v: number) => { t = v },
      /** Fire every silence check whose interval elapsed over `ms` of visible time. */
      tickChecks: (ms: number) => {
        for (let elapsed = WS_SILENCE_CHECK_MS; elapsed <= ms; elapsed += WS_SILENCE_CHECK_MS) {
          t += WS_SILENCE_CHECK_MS
          vi.advanceTimersByTime(WS_SILENCE_CHECK_MS)
        }
      },
      show: () => { hidden = false; document.dispatchEvent(new Event('visibilitychange')) },
      hide: () => { hidden = true; document.dispatchEvent(new Event('visibilitychange')) },
    }
  }

  it('replaces a silent OPEN socket after the window', () => {
    const h = harness()
    h.sockets[0].open()
    expect(h.sockets).toHaveLength(1)

    // Visible, silent, past the window: the first check after it replaces the socket.
    h.tickChecks(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)

    expect(h.sockets[0].closed).toBe(true)
    expect(h.sockets).toHaveLength(2)
    h.sockets[1].open()
    h.stop()
  })

  it('keeps a socket that receives the 5s status frame', () => {
    const h = harness()
    h.sockets[0].open()

    // A status frame every check interval, for four windows: never silent.
    for (let elapsed = 0; elapsed < WS_SILENCE_MS * 4; elapsed += WS_SILENCE_CHECK_MS) {
      h.sockets[0].status()
      h.tickChecks(WS_SILENCE_CHECK_MS)
    }

    expect(h.sockets).toHaveLength(1)
    expect(h.sockets[0].closed).toBe(false)
    h.stop()
  })

  it('leaves a silent socket alone while the page is hidden', () => {
    const h = harness()
    h.sockets[0].open()
    h.hide()

    // Hidden time does not count towards silence.
    h.tickChecks(WS_SILENCE_MS * 3)
    expect(h.sockets).toHaveLength(1)
    expect(h.sockets[0].closed).toBe(false)
    h.stop()
  })

  it('does not watch after the caller stops', () => {
    const h = harness()
    h.sockets[0].open()
    h.stop()

    // The interval is detached, so no later tick can replace the socket.
    h.tickChecks(WS_SILENCE_MS * 3)
    expect(h.sockets).toHaveLength(1)
  })
})
