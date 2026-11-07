/**
 * A refused WebSocket upgrade reaches the browser as a bodyless close, the same
 * shape as a network drop. The registry follows a close that never opened with
 * ONE look at the list route before it redials: an explicit `enabled: false`
 * parks the connection as turned off by configuration, writes the answer into
 * the `terminal-enabled` query cache and stops the redial chain; anything else
 * (a 5xx, a non-owner's 403, a deadline) cannot rule and keeps today's backoff.
 * The registry never writes its own enabled flag: the shell's mirror does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'
import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import {
  disposeTerminalConnection,
  ensureTerminalConnection,
  isTerminalEnabled,
  setTerminalEnabledFlag,
  useAnyTerminalDisabledByConfig,
  useTerminalConnStatus,
  useTerminalDisabledByConfig,
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
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
  constructor(public url: string) { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateClose() { this.readyState = MockWebSocket.CLOSED; this.onclose?.(new CloseEvent('close')) }
  /** The refused-upgrade shape: an error, then the close the error handler asks for. */
  simulateRefused() { this.onerror?.(new Event('error')); this.simulateClose() }
}

const fakeTerm = () => ({ reset: vi.fn(), write: vi.fn(), cols: 80, rows: 24, onData: vi.fn(), onResize: vi.fn() }) as unknown as Terminal
const fakeFit = () => ({ fit: vi.fn() }) as unknown as FitAddon

const answer = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status })

const OWNED: string[] = []
function session(name: string) { const id = `off-${name}`; OWNED.push(id); return id }

/** Let the look's promise chain settle under fake timers. */
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })

describe('terminalRegistry: a refused dial looks before it redials', () => {
  let list: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    WS_INSTANCES.length = 0
    OWNED.length = 0
    queryClient.clear()
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(Math, 'random').mockReturnValue(0)
    list = vi.spyOn(api, 'terminalSessions')
  })

  afterEach(() => {
    // Hooks are still mounted here (RTL unmounts after this hook); both notify subscribers.
    act(() => {
      for (const id of OWNED) disposeTerminalConnection(id)
      setTerminalEnabledFlag(false)
    })
    queryClient.clear()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('T1: an explicit enabled:false parks the tab, writes the cache and stops the chain', async () => {
    list.mockResolvedValue(answer(200, { enabled: false, sessions: [] }))
    const id = session('explicit-false')
    const { result } = renderHook(() => ({
      off: useTerminalDisabledByConfig(id),
      status: useTerminalConnStatus(id),
      any: useAnyTerminalDisabledByConfig(),
    }))
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    const flagBefore = isTerminalEnabled()

    act(() => { WS_INSTANCES[0].simulateRefused() })
    await settle()

    expect(result.current).toEqual({ off: true, status: 'disconnected', any: true })
    expect(queryClient.getQueryData(['terminal-enabled'])).toEqual({ enabled: false, sessions: [] })
    expect(isTerminalEnabled()).toBe(flagBefore)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(WS_INSTANCES).toHaveLength(1)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('T2: a 500 from the list cannot rule, so the usual backoff redials', async () => {
    list.mockResolvedValue(answer(500))
    const id = session('list-500')
    ensureTerminalConnection(id, fakeTerm(), fakeFit())

    act(() => { WS_INSTANCES[0].simulateRefused() })
    await settle()
    expect(WS_INSTANCES).toHaveLength(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(WS_INSTANCES).toHaveLength(2)
    expect(queryClient.getQueryData(['terminal-enabled'])).toBeUndefined()
  })

  it('T3: a 403 (not the owner) redials like a drop; a rejected look releases the pending mark', async () => {
    list.mockResolvedValue(answer(403))
    const id = session('list-403')
    const { result } = renderHook(() => useTerminalDisabledByConfig(id))
    ensureTerminalConnection(id, fakeTerm(), fakeFit())

    act(() => { WS_INSTANCES[0].simulateRefused() })
    await settle()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(WS_INSTANCES).toHaveLength(2)
    expect(result.current).toBe(false)

    list.mockRejectedValue(new Error('deadline'))
    act(() => { WS_INSTANCES[1].simulateRefused() })
    await settle()
    // The second refused dial looked again (the pending mark was released) and redialled.
    expect(list).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(WS_INSTANCES).toHaveLength(3)
    expect(result.current).toBe(false)
  })

  it('T4: a socket that opened and then dropped never looks', async () => {
    list.mockResolvedValue(answer(200, { enabled: false, sessions: [] }))
    const id = session('drop')
    ensureTerminalConnection(id, fakeTerm(), fakeFit())

    act(() => { WS_INSTANCES[0].simulateOpen(); WS_INSTANCES[0].simulateClose() })
    await settle()
    expect(list).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(WS_INSTANCES).toHaveLength(2)
  })

  it('T4b: the open mark is per dial, so a later refused redial still looks once', async () => {
    list.mockResolvedValue(answer(200, { enabled: false, sessions: [] }))
    const id = session('drop-then-refused')
    const { result } = renderHook(() => useTerminalDisabledByConfig(id))
    ensureTerminalConnection(id, fakeTerm(), fakeFit())

    act(() => { WS_INSTANCES[0].simulateOpen(); WS_INSTANCES[0].simulateClose() })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(WS_INSTANCES).toHaveLength(2)

    act(() => { WS_INSTANCES[1].simulateRefused() })
    await settle()
    expect(list).toHaveBeenCalledTimes(1)
    expect(result.current).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(WS_INSTANCES).toHaveLength(2)
  })

  it('T4c: the flag returning to true redials every parked tab by itself', async () => {
    list.mockResolvedValue(answer(200, { enabled: false, sessions: [] }))
    const id = session('re-enabled')
    const { result } = renderHook(() => ({
      off: useTerminalDisabledByConfig(id),
      status: useTerminalConnStatus(id),
      any: useAnyTerminalDisabledByConfig(),
    }))
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    act(() => { WS_INSTANCES[0].simulateRefused() })
    await settle()
    expect(result.current.off).toBe(true)

    act(() => { setTerminalEnabledFlag(true) })
    expect(result.current).toEqual({ off: false, status: 'reconnecting', any: false })
    expect(WS_INSTANCES).toHaveLength(2)
  })
})
