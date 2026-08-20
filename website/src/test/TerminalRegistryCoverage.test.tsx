/**
 * Behavioural coverage for the terminal session registry.
 *
 * `terminalRegistry` is module-scope state (the sockets, titles, cwds and
 * persistent connection managers all live outside React so a terminal tab can
 * unmount without dropping its PTY). The existing terminal specs mock this
 * module wholesale, so almost nothing below the enabled-flag block was
 * exercised. This file drives the real module: the registry/ready-listener
 * handshake, the send helpers, the two `useSyncExternalStore` hooks, and the
 * whole `connect` lifecycle (open/message/close/error, exponential backoff and
 * the retry ceiling) through a WebSocket double.
 *
 * Harness note: because the state is module-scope and shared across tests,
 * every test uses its own session id and `afterEach` disposes the ids it
 * created — otherwise a leftover reconnect timer from one test would create a
 * socket during the next one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'
import {
  setTerminalEnabledFlag,
  isTerminalEnabled,
  useTerminalEnabled,
  useTerminalTitle,
  getTerminalCwd,
  registerTerminalWs,
  unregisterTerminalWs,
  getTerminalWs,
  onTerminalReady,
  sendToTerminalSession,
  sendRawToTerminalSession,
  ensureTerminalConnection,
  disposeTerminalConnection,
} from '../utils/terminalRegistry'

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState: number = MockWebSocket.CONNECTING
  binaryType = 'blob'
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn<(data: string | ArrayBufferLike | Uint8Array) => void>()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })

  constructor(public url: string) { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  /** A real socket fires onclose AFTER transitioning; mirror that order. */
  simulateClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }

  simulateError() { this.onerror?.(new Event('error')) }

  simulateBinary(bytes: Uint8Array) {
    const buf = new ArrayBuffer(bytes.length)
    new Uint8Array(buf).set(bytes)
    this.onmessage?.(new MessageEvent('message', { data: buf }))
  }

  simulateText(raw: string) {
    this.onmessage?.(new MessageEvent('message', { data: raw }))
  }

  simulateJson(payload: unknown) { this.simulateText(JSON.stringify(payload)) }
}

/** Minimal xterm double: the registry only reads reset/write/cols/rows/element
 *  and registers one onData + one onResize listener per connection. */
class FakeTerm {
  reset = vi.fn()
  write = vi.fn<(data: Uint8Array) => void>()
  cols = 80
  rows = 24
  element: { offsetParent: unknown } | undefined = undefined
  dataCb: ((data: string) => void) | undefined
  resizeCb: ((size: { cols: number; rows: number }) => void) | undefined
  onData = (cb: (data: string) => void) => { this.dataCb = cb }
  onResize = (cb: (size: { cols: number; rows: number }) => void) => { this.resizeCb = cb }

  /** Pretend the tab is laid out, so the open handler takes the fit branch. */
  layOut() { this.element = { offsetParent: {} } }
  asTerminal() { return this as unknown as Terminal }
}

class FakeFit {
  fit = vi.fn()
  asFitAddon() { return this as unknown as FitAddon }
}

const decode = (ws: MockWebSocket, call = 0) => {
  const arg = ws.send.mock.calls[call][0]
  return typeof arg === 'string' ? arg : new TextDecoder().decode(arg as Uint8Array)
}

/** Sockets registered by hand (no Conn behind them) still need clearing. */
const OWNED: string[] = []
function session(name: string): string {
  const id = `term-${name}`
  OWNED.push(id)
  return id
}

/** An open socket wired straight into the registry, no connection manager. */
function openSocket(sessionId: string): MockWebSocket {
  const ws = new MockWebSocket(`ws://localhost/api/ws/terminal/${sessionId}`)
  ws.readyState = MockWebSocket.OPEN
  registerTerminalWs(sessionId, ws as unknown as WebSocket)
  return ws
}

describe('terminalRegistry', () => {
  beforeEach(() => {
    WS_INSTANCES.length = 0
    OWNED.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Backoff adds up to 20% jitter; pin it so delays are exact.
    vi.spyOn(Math, 'random').mockReturnValue(0)
  })

  afterEach(() => {
    for (const id of OWNED) {
      disposeTerminalConnection(id)
      unregisterTerminalWs(id)
    }
    setTerminalEnabledFlag(false)
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('enabled flag', () => {
    it('publishes the flag through the hook and stops on unmount', () => {
      const { result, unmount, rerender } = renderHook(() => useTerminalEnabled())
      expect(result.current).toBe(false)

      act(() => { setTerminalEnabledFlag(true) })
      rerender()
      expect(result.current).toBe(true)
      expect(isTerminalEnabled()).toBe(true)

      unmount()
      // No listener left to notify — the imperative read still reflects it.
      act(() => { setTerminalEnabledFlag(false) })
      expect(isTerminalEnabled()).toBe(false)
    })
  })

  describe('socket registry', () => {
    it('hands back only a socket that is actually open', () => {
      const id = session('open-check')
      const ws = openSocket(id)
      expect(getTerminalWs(id)).toBe(ws as unknown as WebSocket)

      ws.readyState = MockWebSocket.CLOSING
      expect(getTerminalWs(id)).toBeNull()
    })

    it('returns null for a session that was never registered', () => {
      expect(getTerminalWs(session('never'))).toBeNull()
    })

    it('forgets a session on unregister', () => {
      const id = session('unregister')
      openSocket(id)
      unregisterTerminalWs(id)
      expect(getTerminalWs(id)).toBeNull()
    })
  })

  describe('onTerminalReady', () => {
    it('runs the callback immediately when the socket is already open', () => {
      const id = session('ready-now')
      openSocket(id)
      const cb = vi.fn()
      const off = onTerminalReady(id, cb)
      expect(cb).toHaveBeenCalledTimes(1)
      // The unsubscribe for the already-fired case is a no-op, not a throw.
      expect(() => off()).not.toThrow()
    })

    it('defers every waiter until the socket registers, then drains once', () => {
      const id = session('ready-later')
      const first = vi.fn()
      const second = vi.fn()
      onTerminalReady(id, first)
      onTerminalReady(id, second)
      expect(first).not.toHaveBeenCalled()

      openSocket(id)
      expect(first).toHaveBeenCalledTimes(1)
      expect(second).toHaveBeenCalledTimes(1)

      // The waiter set is dropped on drain, so a later socket does not re-fire.
      openSocket(id)
      expect(first).toHaveBeenCalledTimes(1)
    })

    it('does not run a waiter that unsubscribed before the socket opened', () => {
      const id = session('ready-cancel')
      const cb = vi.fn()
      onTerminalReady(id, cb)()
      openSocket(id)
      expect(cb).not.toHaveBeenCalled()
    })
  })

  describe('send helpers', () => {
    it('sends a trimmed line with one trailing newline', () => {
      const id = session('send-line')
      const ws = openSocket(id)
      expect(sendToTerminalSession(id, 'echo hi   \n\n')).toBe(true)
      expect(decode(ws)).toBe('echo hi\n')
    })

    it('sends raw data without appending a newline', () => {
      const id = session('send-raw')
      const ws = openSocket(id)
      expect(sendRawToTerminalSession(id, '/tmp/pa')).toBe(true)
      expect(decode(ws)).toBe('/tmp/pa')
    })

    it('reports failure when the session has no open socket', () => {
      const id = session('send-closed')
      expect(sendToTerminalSession(id, 'ls')).toBe(false)
      expect(sendRawToTerminalSession(id, 'ls')).toBe(false)
    })

    it('reports failure when the socket throws on send', () => {
      const id = session('send-throws')
      const ws = openSocket(id)
      ws.send.mockImplementation(() => { throw new Error('socket gone') })
      expect(sendToTerminalSession(id, 'ls')).toBe(false)
      expect(sendRawToTerminalSession(id, 'ls')).toBe(false)
    })
  })

  describe('useTerminalTitle', () => {
    it('starts undefined and follows the backend title frames', () => {
      const id = session('title')
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]

      let renders = 0
      const { result, rerender } = renderHook(() => {
        renders += 1
        return useTerminalTitle(id)
      })
      expect(result.current).toBeUndefined()

      act(() => { ws.simulateJson({ type: 'title', text: 'npm run dev' }) })
      rerender()
      expect(result.current).toBe('npm run dev')

      const afterFirst = renders
      // Same title again: setSessionTitle short-circuits, so no notification.
      act(() => { ws.simulateJson({ type: 'title', text: 'npm run dev' }) })
      expect(renders).toBe(afterFirst)
    })

    it('ignores title frames without string text', () => {
      const id = session('title-shape')
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]

      const { result, rerender } = renderHook(() => useTerminalTitle(id))
      act(() => { ws.simulateJson({ type: 'title', text: 42 }) })
      rerender()
      expect(result.current).toBeUndefined()
    })

    it('tracks separate sessions independently', () => {
      const a = session('title-a')
      const b = session('title-b')
      ensureTerminalConnection(a, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      ensureTerminalConnection(b, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      const [wsA, wsB] = WS_INSTANCES

      const hookA = renderHook(() => useTerminalTitle(a))
      const hookB = renderHook(() => useTerminalTitle(b))
      act(() => {
        wsA.simulateJson({ type: 'title', text: 'build' })
        wsB.simulateJson({ type: 'title', text: 'tests' })
      })
      hookA.rerender()
      hookB.rerender()
      expect(hookA.result.current).toBe('build')
      expect(hookB.result.current).toBe('tests')
    })
  })

  describe('cwd frames', () => {
    it('records the live cwd and ignores frames with a non-string path', () => {
      const id = session('cwd')
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]
      expect(getTerminalCwd(id)).toBeUndefined()

      ws.simulateJson({ type: 'cwd', path: '/home/builder/kiro-crew' })
      expect(getTerminalCwd(id)).toBe('/home/builder/kiro-crew')

      ws.simulateJson({ type: 'cwd', path: null })
      expect(getTerminalCwd(id)).toBe('/home/builder/kiro-crew')
    })
  })

  describe('connection lifecycle', () => {
    it('dials the session endpoint and requests binary frames', () => {
      const id = session('dial')
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      expect(WS_INSTANCES).toHaveLength(1)
      expect(WS_INSTANCES[0].url).toBe(`ws://localhost:6776/api/ws/terminal/${id}`)
      expect(WS_INSTANCES[0].binaryType).toBe('arraybuffer')
    })

    it('passes an encoded spawn cwd as a query parameter', () => {
      const id = session('dial-cwd')
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon(), '/home/me/my repo')
      expect(WS_INSTANCES[0].url).toContain('?cwd=%2Fhome%2Fme%2Fmy%20repo')
    })

    it('is idempotent — a second mount reuses the first socket', () => {
      const id = session('idempotent')
      const first = new FakeTerm()
      const second = new FakeTerm()
      ensureTerminalConnection(id, first.asTerminal(), new FakeFit().asFitAddon())
      ensureTerminalConnection(id, second.asTerminal(), new FakeFit().asFitAddon())
      expect(WS_INSTANCES).toHaveLength(1)
      expect(second.dataCb).toBeUndefined()
    })

    it('waits for the shell-ready frame before releasing queued commands', () => {
      const id = session('open')
      const term = new FakeTerm()
      const fit = new FakeFit()
      const waiter = vi.fn()
      onTerminalReady(id, waiter)
      ensureTerminalConnection(id, term.asTerminal(), fit.asFitAddon())
      const ws = WS_INSTANCES[0]

      ws.simulateOpen()
      expect(term.reset).toHaveBeenCalledTimes(1)
      expect(getTerminalWs(id)).toBeNull()
      expect(waiter).not.toHaveBeenCalled()

      ws.simulateJson({ type: 'ready' })
      expect(getTerminalWs(id)).toBe(ws as unknown as WebSocket)
      expect(waiter).toHaveBeenCalledTimes(1)
      // Hidden tab: fit() would measure 0x0, so no fit and no resize frame.
      expect(fit.fit).not.toHaveBeenCalled()
      expect(ws.send).not.toHaveBeenCalled()
    })

    it('fits and ships dimensions when the tab is laid out', () => {
      const id = session('open-laid-out')
      const term = new FakeTerm()
      term.layOut()
      term.cols = 120
      term.rows = 40
      const fit = new FakeFit()
      ensureTerminalConnection(id, term.asTerminal(), fit.asFitAddon())
      const ws = WS_INSTANCES[0]

      ws.simulateOpen()
      expect(fit.fit).toHaveBeenCalledTimes(1)
      expect(JSON.parse(decode(ws))).toEqual({ type: 'resize', cols: 120, rows: 40 })
    })

    it('writes binary frames straight to the terminal', () => {
      const id = session('binary')
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      const bytes = new TextEncoder().encode('hello from the PTY')

      WS_INSTANCES[0].simulateBinary(bytes)
      expect(term.write).toHaveBeenCalledTimes(1)
      expect(new TextDecoder().decode(term.write.mock.calls[0][0])).toBe('hello from the PTY')
    })

    it('swallows a control frame that is not JSON', () => {
      const id = session('bad-json')
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      expect(() => WS_INSTANCES[0].simulateText('not json at all')).not.toThrow()
      expect(term.write).not.toHaveBeenCalled()
    })

    it('forwards keystrokes and resizes only while the socket is open', () => {
      const id = session('io')
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]

      // Still CONNECTING: both listeners must drop the payload.
      term.dataCb?.('early')
      term.resizeCb?.({ cols: 10, rows: 5 })
      expect(ws.send).not.toHaveBeenCalled()

      ws.simulateOpen()
      term.dataCb?.('ls -al')
      term.resizeCb?.({ cols: 100, rows: 30 })
      expect(decode(ws, 0)).toBe('ls -al')
      expect(JSON.parse(decode(ws, 1))).toEqual({ type: 'resize', cols: 100, rows: 30 })
    })

    it('closes the socket from the error handler without reconnecting itself', () => {
      const id = session('error')
      vi.useFakeTimers()
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]

      ws.simulateError()
      expect(ws.close).toHaveBeenCalledTimes(1)
      // The error path only closes; the reconnect belongs to onclose.
      vi.advanceTimersByTime(60_000)
      expect(WS_INSTANCES).toHaveLength(1)
    })
  })

  describe('reconnect backoff', () => {
    it('unregisters and redials with a doubling delay', () => {
      const id = session('backoff')
      vi.useFakeTimers()
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())

      const ws1 = WS_INSTANCES[0]
      ws1.simulateOpen()
      ws1.simulateJson({ type: 'ready' })
      expect(getTerminalWs(id)).not.toBeNull()

      ws1.simulateClose()
      expect(getTerminalWs(id)).toBeNull()
      vi.advanceTimersByTime(999)
      expect(WS_INSTANCES).toHaveLength(1)
      vi.advanceTimersByTime(1)
      expect(WS_INSTANCES).toHaveLength(2)

      // Second drop without an intervening open: the delay doubles.
      WS_INSTANCES[1].simulateClose()
      vi.advanceTimersByTime(1999)
      expect(WS_INSTANCES).toHaveLength(2)
      vi.advanceTimersByTime(1)
      expect(WS_INSTANCES).toHaveLength(3)
    })

    it('resets the delay after a successful reconnect', () => {
      const id = session('backoff-reset')
      vi.useFakeTimers()
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())

      WS_INSTANCES[0].simulateClose()
      vi.advanceTimersByTime(1000)
      WS_INSTANCES[1].simulateOpen()   // retries back to 0
      WS_INSTANCES[1].simulateClose()
      vi.advanceTimersByTime(1000)
      expect(WS_INSTANCES).toHaveLength(3)
    })

    it('stops redialling at the retry ceiling', () => {
      const id = session('ceiling')
      vi.useFakeTimers()
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())

      // Ten drops with no successful open in between: the tenth schedules a
      // redial that then refuses to dial, so the socket count stops at ten.
      for (let attempt = 0; attempt < 11; attempt += 1) {
        const latest = WS_INSTANCES[WS_INSTANCES.length - 1]
        if (latest.readyState !== MockWebSocket.CLOSED) latest.simulateClose()
        vi.advanceTimersByTime(60_000)
      }
      expect(WS_INSTANCES).toHaveLength(10)
    })
  })

  describe('disposeTerminalConnection', () => {
    it('tears down the socket, the timer and every per-session record', () => {
      const id = session('dispose')
      vi.useFakeTimers()
      const term = new FakeTerm()
      ensureTerminalConnection(id, term.asTerminal(), new FakeFit().asFitAddon())
      const ws = WS_INSTANCES[0]
      ws.simulateOpen()
      ws.simulateJson({ type: 'title', text: 'vitest' })
      ws.simulateJson({ type: 'cwd', path: '/srv/app' })

      disposeTerminalConnection(id)
      expect(ws.close).toHaveBeenCalledTimes(1)
      expect(ws.onclose).toBeNull()
      expect(getTerminalWs(id)).toBeNull()
      expect(getTerminalCwd(id)).toBeUndefined()

      const { result } = renderHook(() => useTerminalTitle(id))
      expect(result.current).toBeUndefined()

      // Nothing reconnects afterwards, and a second dispose is a no-op.
      vi.advanceTimersByTime(60_000)
      expect(WS_INSTANCES).toHaveLength(1)
      expect(() => disposeTerminalConnection(id)).not.toThrow()
      expect(ws.close).toHaveBeenCalledTimes(1)
    })

    it('cancels a pending reconnect timer', () => {
      const id = session('dispose-pending')
      vi.useFakeTimers()
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      WS_INSTANCES[0].simulateClose()

      disposeTerminalConnection(id)
      vi.advanceTimersByTime(60_000)
      expect(WS_INSTANCES).toHaveLength(1)
    })

    it('drops waiters left behind by a tab closed before it ever connected', () => {
      const id = session('dispose-waiters')
      const waiter = vi.fn()
      onTerminalReady(id, waiter)
      ensureTerminalConnection(id, new FakeTerm().asTerminal(), new FakeFit().asFitAddon())
      disposeTerminalConnection(id)

      // A later session reusing the id must not inherit the stale waiter.
      openSocket(id)
      expect(waiter).not.toHaveBeenCalled()
    })

    it('is a no-op for a session that never had a connection', () => {
      expect(() => disposeTerminalConnection(session('dispose-unknown'))).not.toThrow()
    })
  })
})
