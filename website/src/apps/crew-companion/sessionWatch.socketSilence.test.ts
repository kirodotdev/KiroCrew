/**
 * Silence watchdog on the crew-companion `/api/ws` socket.
 *
 * The companion watches the gateway for turn completion over the same dashboard
 * socket Mochi uses. A socket can stay OPEN while its transport is gone (a phone
 * off-network, an OS-frozen tab) without `onclose` ever firing, so the companion
 * drives the shared silence watchdog (`utils/socketSilenceWatchdog`): a visible
 * page whose socket is silent for WS_SILENCE_MS is replaced, and a socket still
 * receiving the gateway's 5s `dashboard` status frame is not.
 *
 * The clock is the watcher's injected `now`, so the window is driven by `setNow`;
 * the check interval is a real `setInterval` driven by fake timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { watchSessions, type SessionDone } from './sessionWatch'
import {
  WS_SILENCE_MS,
  WS_SILENCE_CHECK_MS,
} from '../../utils/socketSilenceWatchdog'

/** A socket the test opens, feeds frames, and asks whether it was closed. */
function fakeSocket() {
  const ws = {
    readyState: 0, // CONNECTING
    onopen: null as null | (() => void),
    onmessage: null as null | ((ev: { data: string }) => void),
    onclose: null as null | (() => void),
    onerror: null as null | (() => void),
    closed: false,
    send() {},
    close() { this.closed = true; this.readyState = 3; this.onclose?.() },
    open() { this.readyState = 1; this.onopen?.() },
    status() { this.onmessage?.({ data: JSON.stringify({ type: 'dashboard', data: {} }) }) },
    frame(type: string, data: unknown) { this.onmessage?.({ data: JSON.stringify({ type, data }) }) },
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
  function harness(onDone: (done: SessionDone) => void = () => {}) {
    const sockets: ReturnType<typeof fakeSocket>[] = []
    let t = 1_000_000
    const connect = () => {
      const ws = fakeSocket()
      sockets.push(ws)
      return ws as unknown as WebSocket
    }
    const stop = watchSessions({
      onDone,
      isSilent: () => false,
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

  it('a replacement after a healthy socket starts the backoff over', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const h = harness()
    // A first socket that never opens grows the next delay to 2000.
    h.sockets[0].close()
    vi.advanceTimersByTime(1000)
    h.sockets[1].open()

    // Healthy for a whole silence window, then silent: the watchdog replaces it.
    h.tickChecks(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(h.sockets).toHaveLength(3)

    // The replacement fails at once; the retry comes after 1000, not 2000.
    h.sockets[2].close()
    vi.advanceTimersByTime(1000)
    expect(h.sockets).toHaveLength(4)
    h.stop()
    vi.restoreAllMocks()
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

  /**
   * Start a turn on `slot`, then let the socket go silent until the watchdog
   * replaces it and the fresh socket opens. The turn has run longer than the
   * notify threshold by then, so any `finish` would reach `onDone`.
   */
  function replaceDuringTurn(slot: string, beforeSilence?: (ws: ReturnType<typeof fakeSocket>) => void) {
    const done: SessionDone[] = []
    const h = harness((d) => done.push(d))
    h.sockets[0].open()
    h.sockets[0].frame('chat_status', { slot })
    beforeSilence?.(h.sockets[0])
    h.tickChecks(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(h.sockets).toHaveLength(2)
    h.sockets[1].open()
    return { h, done }
  }

  it('does not report a turn still running as failed on a watchdog replacement', () => {
    const { h, done } = replaceDuringTurn('chat-a')

    // The replacement itself raises nothing: the gateway may still be running it.
    expect(done).toEqual([])

    // The fresh socket's slots frame lists it running, so it stays open...
    h.sockets[1].frame('slots', [{ key: 'chat-a', running: true, title: 'A' }])
    expect(done).toEqual([])

    // ...and its own chat_done closes it once, as a success, timed from the
    // start the silent socket saw.
    h.tickChecks(WS_SILENCE_CHECK_MS)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toHaveLength(1)
    expect(done[0]).toMatchObject({ slot: 'chat-a', title: 'A', failed: false })
    expect(done[0].elapsedMs).toBe(WS_SILENCE_MS + WS_SILENCE_CHECK_MS * 2)
    h.stop()
  })

  it.each([
    { turn: 2, turn_gen: 'gateway-a' },
    { turn: 1, turn_gen: 'gateway-b' },
  ])('does not carry an error into a successor with identity $turn_gen:$turn', (successor) => {
    const { h, done } = replaceDuringTurn('chat-a', (ws) => {
      ws.frame('slots', [{ key: 'chat-a', running: true, turn: 1, turn_gen: 'gateway-a' }])
      ws.frame('chat_message', { slot: 'chat-a', role: 'error' })
    })

    h.sockets[1].frame('slots', [{ key: 'chat-a', running: true, ...successor }])
    expect(done).toEqual([{
      slot: 'chat-a', title: '', failed: true,
      elapsedMs: WS_SILENCE_MS + WS_SILENCE_CHECK_MS,
    }])

    // Repeated snapshots cannot settle the predecessor twice or reset the successor.
    h.tickChecks(WS_SILENCE_CHECK_MS)
    h.sockets[1].frame('slots', [{ key: 'chat-a', running: true, ...successor }])
    h.tickChecks(WS_SILENCE_CHECK_MS)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toHaveLength(2)
    expect(done[1]).toEqual({
      slot: 'chat-a', title: '', failed: false, elapsedMs: WS_SILENCE_CHECK_MS * 2,
    })
    h.stop()
  })

  it('does not carry a deliberate stop into a successor with a different identity', () => {
    const { h, done } = replaceDuringTurn('chat-a', (ws) => {
      ws.frame('slots', [{ key: 'chat-a', running: true, stopping: true, turn: 1, turn_gen: 'gateway-a' }])
    })

    h.sockets[1].frame('slots', [{ key: 'chat-a', running: true, turn: 2, turn_gen: 'gateway-a' }])
    expect(done).toEqual([])
    h.tickChecks(WS_SILENCE_CHECK_MS * 2)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toEqual([{
      slot: 'chat-a', title: '', failed: false, elapsedMs: WS_SILENCE_CHECK_MS * 2,
    }])
    h.stop()
  })

  it.each(['running', 'failed', 'stopped'])('keeps timing and flags for the same identity when %s', (state) => {
    const row = { key: 'chat-a', running: true, turn: 1, turn_gen: 'gateway-a' }
    const { h, done } = replaceDuringTurn('chat-a', (ws) => {
      ws.frame('slots', [{ ...row, stopping: state === 'stopped' }])
      if (state === 'failed') ws.frame('chat_message', { slot: 'chat-a', role: 'error' })
    })

    h.sockets[1].frame('slots', [row])
    expect(done).toEqual([])
    h.tickChecks(WS_SILENCE_CHECK_MS)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toEqual(state === 'stopped' ? [] : [{
      slot: 'chat-a', title: '', failed: state === 'failed',
      elapsedMs: WS_SILENCE_MS + WS_SILENCE_CHECK_MS * 2,
    }])
    h.stop()
  })

  it('keeps the successor stop from the settling frame without suppressing its predecessor', () => {
    const { h, done } = replaceDuringTurn('chat-a', (ws) => {
      ws.frame('slots', [{ key: 'chat-a', running: true, turn: 1, turn_gen: 'gateway-a' }])
    })

    h.sockets[1].frame('slots', [{ key: 'chat-a', running: true, stopping: true, turn: 2, turn_gen: 'gateway-a' }])
    expect(done).toHaveLength(1)
    expect(done[0].failed).toBe(true)
    h.tickChecks(WS_SILENCE_CHECK_MS * 2)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toHaveLength(1)
    h.stop()
  })

  it.each(['predecessor', 'successor'])('keeps the turn when the %s identity is unknown', (unknown) => {
    const { h, done } = replaceDuringTurn('chat-a', (ws) => {
      ws.frame('slots', [{
        key: 'chat-a', running: true,
        ...(unknown === 'predecessor' ? {} : { turn: 1, turn_gen: 'gateway-a' }),
      }])
    })

    h.sockets[1].frame('slots', [{
      key: 'chat-a', running: true,
      ...(unknown === 'successor' ? {} : { turn: 2, turn_gen: 'gateway-a' }),
    }])
    expect(done).toEqual([])
    h.tickChecks(WS_SILENCE_CHECK_MS)
    h.sockets[1].frame('chat_done', { slot: 'chat-a' })
    expect(done).toEqual([{
      slot: 'chat-a', title: '', failed: false,
      elapsedMs: WS_SILENCE_MS + WS_SILENCE_CHECK_MS * 2,
    }])
    h.stop()
  })

  it('reports a turn the first slots frame no longer lists running as failed, once', () => {
    const { h, done } = replaceDuringTurn('chat-b')
    expect(done).toEqual([])

    h.sockets[1].frame('slots', [{ key: 'chat-b', running: false, title: 'B' }])
    expect(done).toHaveLength(1)
    expect(done[0]).toMatchObject({ slot: 'chat-b', title: 'B', failed: true })

    // A later slots frame or a stray chat_done does not report it a second time.
    h.sockets[1].frame('slots', [{ key: 'chat-b', running: false }])
    h.sockets[1].frame('chat_done', { slot: 'chat-b' })
    expect(done).toHaveLength(1)
    h.stop()
  })

  it('settles only on the first slots frame after the replacement', () => {
    const { h, done } = replaceDuringTurn('chat-c')
    h.sockets[1].frame('slots', [{ key: 'chat-c', running: true }])

    // A turn that starts afterwards and is absent from a later slots frame is
    // left to its chat_done; the settle ran once.
    h.sockets[1].frame('chat_status', { slot: 'chat-d' })
    h.sockets[1].frame('slots', [{ key: 'chat-c', running: true }])
    expect(done).toEqual([])
    h.stop()
  })

  it('fails the in-flight turn when the fresh socket closes before its slots frame', () => {
    const { h, done } = replaceDuringTurn('chat-e')
    h.sockets[1].close()
    expect(done).toHaveLength(1)
    expect(done[0]).toMatchObject({ slot: 'chat-e', failed: true })
    h.stop()
  })
})
