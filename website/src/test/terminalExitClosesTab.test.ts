/**
 * A shell that exits on its own closes its terminal tab, in the docked panel or
 * a chat's side panel. End-to-end across the real registry, `terminalExitClose`
 * and both tab stores, since the wiring between them is the feature. Only xterm
 * is substituted: it cannot boot under the test DOM.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

const xt = vi.hoisted(() => {
  class FakeTerminal {
    options: Record<string, unknown> = {}
    element: HTMLElement | undefined = undefined
    disposed = false
    cols = 80
    rows = 24
    loadAddon() { /* no addons under test */ }
    open() { /* never attached in this file */ }
    written: string[] = []
    write(data: string | Uint8Array) { if (typeof data === 'string') this.written.push(data) }
    reset() { /* not under test */ }
    focus() { /* not under test */ }
    dispose() { this.disposed = true }
    onData() { return { dispose() { /* noop */ } } }
    onResize() { return { dispose() { /* noop */ } } }
    onSelectionChange() { return { dispose() { /* noop */ } } }
    onScroll() { return { dispose() { /* noop */ } } }
  }
  class FakeFitAddon { fit() { /* not under test */ } }
  return { FakeTerminal, FakeFitAddon }
})

vi.mock('@xterm/xterm', () => ({ Terminal: xt.FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: xt.FakeFitAddon }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class {} }))
vi.mock('@xterm/xterm/css/xterm.css', () => ({}))

// Importing the wiring module for its MODULE SIDE EFFECT: it subscribes the tab
// store to shell exits. Nothing is rendered in this file.
import '../utils/terminalExitClose'
import {
  ensureTerminalConnection,
  disposeTerminalConnection,
  onTerminalExit,
  retryTerminalConnection,
  shellExitLine,
  unregisterTerminalWs,
} from '../utils/terminalRegistry'
import {
  adoptTab,
  removeTab,
  closeBottomTerminal,
  hasTab,
  isBottomTerminalOpen,
} from '../hooks/useBottomTerminal'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

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
  /** A real socket transitions before firing onclose; mirror that order. */
  simulateClose(code = 1000) {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close', { code }))
  }
  simulateJson(payload: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }))
  }
}

const fakeTerm = () => new xt.FakeTerminal() as unknown as Parameters<typeof ensureTerminalConnection>[1]
const fakeFit = () => new xt.FakeFitAddon() as unknown as Parameters<typeof ensureTerminalConnection>[2]

/** Adopt a tab under a known id and dial it, handing back the registry's socket.
 *  `adoptTab` is the store entry point that takes an id; `addTab` mints its own. */
function openTab(id: string): MockWebSocket {
  adoptTab(id)
  ensureTerminalConnection(id, fakeTerm(), fakeFit())
  const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
  ws.simulateOpen()
  return ws
}

const OWNED: string[] = []

describe('shell exit closes its terminal tab', () => {
  beforeEach(() => {
    WS_INSTANCES.length = 0
    OWNED.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(Math, 'random').mockReturnValue(0)
  })

  afterEach(() => {
    for (const id of OWNED) {
      disposeTerminalConnection(id)
      unregisterTerminalWs(id)
      removeTab(id)
    }
    closeBottomTerminal()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('drops the tab when the server reports the shell exited', () => {
    const id = 'exit-one'
    OWNED.push(id, 'exit-keep')
    openTab('exit-keep')
    const ws = openTab(id)

    ws.simulateJson({ type: 'exit', status: 0, close: true })

    expect(hasTab(id)).toBe(false)
    expect(hasTab('exit-keep')).toBe(true)
    // Another tab remains, so the panel stays open.
    expect(isBottomTerminalOpen()).toBe(true)
  })

  it('hides the whole panel when the exiting shell was the last tab', () => {
    const id = 'exit-last'
    OWNED.push(id)
    const ws = openTab(id)
    expect(isBottomTerminalOpen()).toBe(true)

    ws.simulateJson({ type: 'exit', status: 0, close: true })

    expect(hasTab(id)).toBe(false)
    expect(isBottomTerminalOpen()).toBe(false)
  })

  it('does not redial the dead session when its socket then closes', () => {
    vi.useFakeTimers()
    const id = 'exit-noredial'
    OWNED.push(id)
    const ws = openTab(id)
    const dialledBefore = WS_INSTANCES.length

    ws.simulateJson({ type: 'exit', status: 0, close: true })
    ws.simulateClose()
    vi.advanceTimersByTime(60_000)

    expect(WS_INSTANCES).toHaveLength(dialledBefore)
  })

  it('keeps the tab on a coded close when the frame is lost, and never redials', () => {
    // Without the frame neither the status nor the server's close decision
    // survived, so the tab is kept: the safe side of an unknown. The dead
    // socket is still released and nothing dials a fresh shell over it.
    vi.useFakeTimers()
    const id = 'exit-coded-close'
    OWNED.push(id)
    const seen = vi.fn()
    const off = onTerminalExit(seen)
    const ws = openTab(id)
    const dialledBefore = WS_INSTANCES.length

    ws.simulateClose(4001)
    vi.advanceTimersByTime(60_000)

    expect(seen).toHaveBeenCalledOnce()
    expect(seen).toHaveBeenCalledWith(id, { status: null, close: false })
    expect(hasTab(id)).toBe(true)
    expect(WS_INSTANCES).toHaveLength(dialledBefore)
    off()
  })

  it('keeps the tab and writes the exit line when the server says not to close', () => {
    // dashboard.terminal.close_on_exit is off by default: the pane keeps its
    // output, states the exit, and the socket is released with no redial.
    vi.useFakeTimers()
    const id = 'exit-kept'
    OWNED.push(id)
    adoptTab(id)
    const term = fakeTerm() as unknown as { written: string[] }
    ensureTerminalConnection(id, term as unknown as Parameters<typeof ensureTerminalConnection>[1], fakeFit())
    const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
    ws.simulateOpen()
    const dialledBefore = WS_INSTANCES.length

    ws.simulateJson({ type: 'exit', status: 2, close: false })
    ws.simulateClose(4001)
    vi.advanceTimersByTime(60_000)

    expect(hasTab(id)).toBe(true)
    expect(term.written).toEqual([shellExitLine(2)])
    expect(shellExitLine(2)).toContain('2')
    // The setting, this line and the bell note describe one event, so they
    // share a noun: "shell", never "process".
    expect(shellExitLine(2)).toMatch(/shell/i)
    expect(shellExitLine(2)).not.toMatch(/process/i)
    expect(shellExitLine(null)).toMatch(/shell/i)
    expect(shellExitLine(null)).not.toMatch(/process/i)
    expect(WS_INSTANCES).toHaveLength(dialledBefore)
    expect(ws.close).toHaveBeenCalledOnce()
  })

  it('a kept tab survives a remount without redialing or resetting its output', () => {
    // Hiding and reopening the panel mounts TerminalView again, which calls
    // ensureTerminalConnection. For a kept exit that must be a no-op: a fresh
    // dial would reset the retained output and be answered with the same exit.
    vi.useFakeTimers()
    const id = 'exit-kept-remount'
    OWNED.push(id)
    adoptTab(id)
    const term = fakeTerm() as unknown as { written: string[]; reset: () => void }
    const reset = vi.spyOn(term, 'reset')
    const t = term as unknown as Parameters<typeof ensureTerminalConnection>[1]
    ensureTerminalConnection(id, t, fakeFit())
    const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
    ws.simulateOpen()
    reset.mockClear()
    ws.simulateJson({ type: 'exit', status: 1, close: false })
    ws.simulateClose(4001)
    const dialledBefore = WS_INSTANCES.length

    ensureTerminalConnection(id, t, fakeFit())
    retryTerminalConnection(id)
    document.dispatchEvent(new Event('visibilitychange'))
    window.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(60_000)

    expect(WS_INSTANCES).toHaveLength(dialledBefore)
    expect(reset).not.toHaveBeenCalled()
    expect(term.written).toEqual([shellExitLine(1)])

    // The tombstone leaves with the tab: an explicit close, then a new tab
    // under the same id, dials again.
    disposeTerminalConnection(id)
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    expect(WS_INSTANCES).toHaveLength(dialledBefore + 1)
  })

  it('notifies once when the exit frame precedes the coded close', () => {
    const id = 'exit-frame-and-close'
    OWNED.push(id)
    const seen = vi.fn()
    const off = onTerminalExit(seen)
    const ws = openTab(id)

    ws.simulateJson({ type: 'exit', status: 23, close: true })
    ws.simulateClose(4001)

    expect(seen).toHaveBeenCalledOnce()
    expect(seen).toHaveBeenCalledWith(id, { status: 23, close: true })
    off()
  })

  it('still closes the tab when the exit status is unknown', () => {
    const id = 'exit-null'
    OWNED.push(id)
    const ws = openTab(id)

    // The status could not be resolved within the reap bound, on either
    // backend; the frame carries null.
    ws.simulateJson({ type: 'exit', status: null, close: true })

    expect(hasTab(id)).toBe(false)
  })

  it('reports the status to subscribers and stops after unsubscribe', () => {
    const id = 'exit-subscriber'
    OWNED.push(id)
    const seen: Array<[string, number | null]> = []
    const off = onTerminalExit((sessionId, exit) => { seen.push([sessionId, exit.status]) })

    openTab(id).simulateJson({ type: 'exit', status: 137, close: true })
    expect(seen).toEqual([[id, 137]])

    off()
    const second = 'exit-subscriber-2'
    OWNED.push(second)
    openTab(second).simulateJson({ type: 'exit', status: 0, close: true })
    expect(seen).toEqual([[id, 137]])
  })

  it('a throwing subscriber does not strand the others', () => {
    const id = 'exit-throwing'
    OWNED.push(id)
    const after = vi.fn()
    const offBad = onTerminalExit(() => { throw new Error('subscriber blew up') })
    const offGood = onTerminalExit(after)

    openTab(id).simulateJson({ type: 'exit', status: 0, close: true })

    expect(after).toHaveBeenCalledOnce()
    // The tab still closed: CliPanel's own subscriber ran too.
    expect(hasTab(id)).toBe(false)
    offBad()
    offGood()
  })

  it('releases a connection no host has a tab for', () => {
    // No tab in either host, yet the registry must still release the socket.
    const id = 'exit-unowned'
    OWNED.push(id)
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
    ws.simulateOpen()
    const dialledBefore = WS_INSTANCES.length

    expect(() => ws.simulateJson({ type: 'exit', status: 0, close: true })).not.toThrow()
    expect(hasTab(id)).toBe(false)
    expect(ws.close).toHaveBeenCalledOnce()
    expect(ws.onclose).toBeNull()

    // A fresh ensure for the same id must dial: the exited Conn no longer
    // occupies `conns` and cannot retain its Terminal through that map.
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    expect(WS_INSTANCES).toHaveLength(dialledBefore + 1)
  })

  it('keeps a displaced session parked and manually reconnectable', () => {
    vi.useFakeTimers()
    const id = 'exit-displaced'
    OWNED.push(id)
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
    ws.simulateOpen()

    ws.simulateJson({ type: 'error', code: 'displaced' })
    ws.simulateClose()
    vi.advanceTimersByTime(60_000)

    // The normal close path still retains the parked Conn and does not redial.
    expect(WS_INSTANCES).toHaveLength(1)
    ensureTerminalConnection(id, fakeTerm(), fakeFit())
    expect(WS_INSTANCES).toHaveLength(1)

    // Its existing manual takeover path remains live.
    retryTerminalConnection(id)
    expect(WS_INSTANCES).toHaveLength(2)
  })
})

/** The same exit in a chat's side panel, whose strip survives the lost tab. */
describe('shell exit closes a side-panel terminal tab', () => {
  beforeEach(() => {
    WS_INSTANCES.length = 0
    __resetPanelTabs()
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(Math, 'random').mockReturnValue(0)
  })

  afterEach(() => {
    __resetPanelTabs()
    closeBottomTerminal()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  /** Dial the session a panel tab minted, handing back the registry's socket. */
  function dial(sessionId: string): MockWebSocket {
    ensureTerminalConnection(sessionId, fakeTerm(), fakeFit())
    const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
    ws.simulateOpen()
    return ws
  }

  it('drops the tab and leaves the rest of the strip standing', () => {
    const { result } = renderHook(() => usePanelTabs('chat-a', []))
    act(() => { result.current.openView('files') })
    let sid = ''
    act(() => { sid = result.current.openTerminal({ cwd: '/srv/app' }) })
    expect(result.current.tabs.map(t => t.id)).toEqual(['files', `terminal:${sid}`])
    expect(result.current.activeId).toBe(`terminal:${sid}`)
    const ws = dial(sid)

    act(() => { ws.simulateJson({ type: 'exit', status: 0, close: true }) })

    // The terminal tab is gone; the panel itself is not: its other tab survives
    // and takes the focus the dead tab held.
    expect(result.current.tabs.map(t => t.id)).toEqual(['files'])
    expect(result.current.activeId).toBe('files')
    // The dock is a different host and was never involved.
    expect(hasTab(sid)).toBe(false)
    expect(isBottomTerminalOpen()).toBe(false)
  })

  it('stores no focus when the terminal was the strip\u2019s only tab', () => {
    // An emptied strip must not keep pointing at the tab it just dropped: the
    // host resolves a null focus to its own leading tab, a dangling id to nothing.
    const { result } = renderHook(() => usePanelTabs('chat-solo', []))
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    const ws = dial(sid)

    act(() => { ws.simulateJson({ type: 'exit', status: 0, close: true }) })

    expect(result.current.tabs).toEqual([])
    expect(result.current.activeId).toBeNull()
  })

  it('keeps focus on a host-owned leading tab when a background shell exits', () => {
    // A leading tab (Crewmates Notes) is a valid focus that is never in the
    // bucket, so a shell exiting elsewhere in the strip must not steal it.
    const { result } = renderHook(() => usePanelTabs('chat-lead', [], { leadingIds: ['notes'] }))
    act(() => { result.current.openView('files') })
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    act(() => { result.current.setActive('notes') })
    expect(result.current.activeId).toBe('notes')
    const ws = dial(sid)

    act(() => { ws.simulateJson({ type: 'exit', status: 0, close: true }) })

    expect(result.current.tabs.map(t => t.id)).toEqual(['files'])
    expect(result.current.activeId).toBe('notes')
  })

  it('closes the tab of a shell that exits in a chat the user is not looking at', () => {
    // The exit carries a session id and no slot, so a background chat's terminal
    // must close in its own strip while another chat is the active one.
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => usePanelTabs(slot, []),
      { initialProps: { slot: 'chat-bg' } },
    )
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    const ws = dial(sid)
    rerender({ slot: 'chat-front' })
    expect(result.current.tabs).toEqual([])

    act(() => { ws.simulateJson({ type: 'exit', status: 1, close: true }) })

    rerender({ slot: 'chat-bg' })
    expect(result.current.tabs).toEqual([])
  })

  it('keeps the tab on a coded close when the exit frame is lost', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => usePanelTabs('chat-coded', []))
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    const ws = dial(sid)
    const dialledBefore = WS_INSTANCES.length

    act(() => { ws.simulateClose(4001) })
    vi.advanceTimersByTime(60_000)

    // The decision did not survive, so the tab stays; no redial either.
    expect(result.current.tabs.map(t => t.id)).toEqual([`terminal:${sid}`])
    expect(WS_INSTANCES).toHaveLength(dialledBefore)
  })

  it('keeps the side-panel tab when the server says not to close', () => {
    const { result } = renderHook(() => usePanelTabs('chat-kept', []))
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    const ws = dial(sid)

    act(() => { ws.simulateJson({ type: 'exit', status: 1, close: false }) })

    expect(result.current.tabs.map(t => t.id)).toEqual([`terminal:${sid}`])
    expect(result.current.activeId).toBe(`terminal:${sid}`)
  })
})
