// useRunInTerminalBridge — the app-wide `mc:run-in-terminal` listener.
//
// The listener used to live on ChatPage, so a request dispatched from any other
// route (an app panel at /apps/<id>, /projects, …) was silently dropped even
// though the dock panel it drives is shell-level. These tests exercise the hook
// in isolation (renderHook — no router, no page) because that is the point: the
// bridge must not depend on which page is mounted.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useRunInTerminalBridge, RUN_IN_TERMINAL_TIMEOUT_MS } from '../hooks/useRunInTerminalBridge'
import { __resetBottomTerminal, useBottomTerminal, MAX_TERMINALS, addTab } from '../hooks/useBottomTerminal'
import { registerTerminalWs, unregisterTerminalWs } from '../utils/terminalRegistry'
import { runInTerminalText } from '../utils/fenceShell'

// Pass-through spy: the shell-aware rewrite itself is covered by fenceShell's own
// tests; here we only pin that the bridge hands it the request's `lang` and the
// SESSION's shell (read at ready time), exactly as the ChatPage handler did.
vi.mock('../utils/fenceShell', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../utils/fenceShell')>()
  return { ...mod, runInTerminalText: vi.fn(mod.runInTerminalText) }
})

type Result = { reqId?: string; ok?: boolean }

/** An already-open socket wired straight into the registry (no PTY behind it). */
class OpenSocket {
  static OPEN = 1
  readyState = OpenSocket.OPEN
  send = vi.fn<(data: string | ArrayBufferLike | Uint8Array) => void>()
  close = vi.fn()
}

function captureResults(): { results: Result[]; stop: () => void } {
  const results: Result[] = []
  const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
  window.addEventListener('mc:run-in-terminal-result', onResult)
  return { results, stop: () => window.removeEventListener('mc:run-in-terminal-result', onResult) }
}

function request(detail: Record<string, unknown>) {
  act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail })) })
}

function dockState() {
  const { result } = renderHook(() => useBottomTerminal())
  return result.current
}

describe('useRunInTerminalBridge', () => {
  const registered: string[] = []
  let capture: ReturnType<typeof captureResults>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(runInTerminalText).mockClear()
    __resetBottomTerminal()
    capture = captureResults()
  })
  afterEach(() => {
    capture.stop()
    for (const id of registered.splice(0)) unregisterTerminalWs(id)
    __resetBottomTerminal()
    vi.useRealTimers()
  })

  it('ignores a request that carries no command', () => {
    renderHook(() => useRunInTerminalBridge(undefined))
    request({ reqId: 'r1' })
    expect(dockState().tabs).toHaveLength(0)
    expect(capture.results).toHaveLength(0)
  })

  it('ignores a request whose command is an empty string', () => {
    renderHook(() => useRunInTerminalBridge(undefined))
    request({ code: '', reqId: 'r3' })
    expect(dockState().tabs).toHaveLength(0)
    expect(capture.results).toHaveLength(0)
  })

  it('opens a dock tab in the given cwd and answers exactly once with the reqId', () => {
    renderHook(() => useRunInTerminalBridge('/work/project-a'))
    request({ code: 'npm test', reqId: 'r2' })
    const dock = dockState()
    expect(dock.open).toBe(true)
    expect(dock.tabs).toHaveLength(1)
    expect(dock.tabs[0].cwd).toBe('/work/project-a')
    // No PTY ever connects: the timeout leg answers, and the `settled` latch is
    // what guarantees the requester is told once and only once.
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_TIMEOUT_MS + 500) })
    expect(capture.results).toEqual([{ reqId: 'r2', ok: false }])
  })

  it('writes the command with a trailing newline once the tab\'s shell is ready', () => {
    renderHook(() => useRunInTerminalBridge(undefined))
    request({ code: 'ls -la  ', reqId: 'r4', lang: 'fish' })
    const id = dockState().tabs[0].id
    expect(runInTerminalText).not.toHaveBeenCalled()   // shell unknown until ready
    // The tab's socket comes up: the registry fires the ready listener.
    const ws = new OpenSocket()
    registered.push(id)
    act(() => { registerTerminalWs(id, ws as unknown as WebSocket) })
    // The request's fence lang reaches the shell-aware rewrite, with the
    // session's (here unreported) shell — so the line goes out unchanged.
    expect(runInTerminalText).toHaveBeenCalledWith('ls -la  ', 'fish', undefined, {})
    expect(ws.send).toHaveBeenCalledTimes(1)
    const sent = new TextDecoder().decode(ws.send.mock.calls[0][0] as Uint8Array)
    expect(sent).toBe('ls -la\n')
    expect(capture.results).toEqual([{ reqId: 'r4', ok: true }])
    // The timeout leg must not answer a second time.
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_TIMEOUT_MS + 500) })
    expect(capture.results).toHaveLength(1)
  })

  it('answers ok:false immediately when the dock is at its tab cap', () => {
    renderHook(() => useRunInTerminalBridge(undefined))
    for (let i = 0; i < MAX_TERMINALS; i++) act(() => { addTab() })
    request({ code: 'echo hi', reqId: 'r5' })
    expect(capture.results).toEqual([{ reqId: 'r5', ok: false }])
    expect(dockState().tabs).toHaveLength(MAX_TERMINALS)
  })

  it('reads the latest cwd without re-binding the listener', () => {
    const { rerender } = renderHook(({ cwd }: { cwd?: string }) => useRunInTerminalBridge(cwd), {
      initialProps: { cwd: '/a' },
    })
    rerender({ cwd: '/b' })
    request({ code: 'pwd', reqId: 'r6' })
    const dock = dockState()
    expect(dock.tabs).toHaveLength(1)   // one listener → one tab, not two
    expect(dock.tabs[0].cwd).toBe('/b')
  })

  it('answers ok:false at once, minting no tab, in windows without a dock (popout / embed)', () => {
    renderHook(() => useRunInTerminalBridge('/a', false))
    request({ code: 'echo hi', reqId: 'r7' })
    // Immediate — the requester must not sit on its own fallback timer.
    expect(capture.results).toEqual([{ reqId: 'r7', ok: false }])
    expect(dockState().tabs).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_TIMEOUT_MS + 500) })
    expect(capture.results).toHaveLength(1)
  })

  it('stops listening on unmount', () => {
    const { unmount } = renderHook(() => useRunInTerminalBridge(undefined))
    unmount()
    request({ code: 'echo hi', reqId: 'r8' })
    expect(dockState().tabs).toHaveLength(0)
  })
})
