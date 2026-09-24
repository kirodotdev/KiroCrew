// useRunInTerminalBridge — the app-wide `mc:run-in-terminal` listener.
//
// The listener used to live on ChatPage, so a request dispatched from any other
// route (an app panel at /apps/<id>, /projects, …) was silently dropped even
// though the dock panel it drives is shell-level. These tests exercise the hook
// in isolation (renderHook — no router, no page) because that is the point: the
// bridge must not depend on which page is mounted.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useRunInTerminalBridge, RUN_IN_TERMINAL_TIMEOUT_MS } from '../hooks/useRunInTerminalBridge'
import {
  __resetBottomTerminal, useBottomTerminal, MAX_TERMINALS, addTab, removeTab,
} from '../hooks/useBottomTerminal'
import { registerTerminalWs, unregisterTerminalWs } from '../utils/terminalRegistry'
import {
  RUN_IN_TERMINAL_OPENING_GRACE_MS,
  runInTerminalText,
} from '../utils/fenceShell'

const terminalPopoutOpen = vi.hoisted(() => ({ value: false }))
vi.mock('../utils/terminalPopout', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/terminalPopout')>()),
  isPopoutOpen: () => terminalPopoutOpen.value,
}))

const disposeTerminalSessionSpy = vi.hoisted(() => vi.fn())
const deleteTerminalSessionSpy = vi.hoisted(() => vi.fn())
vi.mock('../components/CliPanel', () => ({
  disposeTerminalSession: disposeTerminalSessionSpy,
  useDeleteTerminalSession: () => ({ mutate: deleteTerminalSessionSpy }),
}))

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

let queryClient: QueryClient
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderBridge(cwd?: string, hasDock = true, onError?: (message: string) => void) {
  return renderHook(() => useRunInTerminalBridge(cwd, hasDock, onError), { wrapper })
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), { status: 200 })
}

async function flushAsyncWork() {
  // React Query's fetchQuery and Response.json each add a microtask after the
  // deadline callback. Keep this explicit instead of using waitFor while fake
  // timers are active; waitFor's polling timer cannot advance itself here.
  for (let i = 0; i < 12; i++) await Promise.resolve()
}

describe('useRunInTerminalBridge', () => {
  const registered: string[] = []
  let capture: ReturnType<typeof captureResults>

  beforeEach(() => {
    vi.useFakeTimers()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.mocked(runInTerminalText).mockClear()
    disposeTerminalSessionSpy.mockClear()
    deleteTerminalSessionSpy.mockClear()
    terminalPopoutOpen.value = false
    __resetBottomTerminal()
    capture = captureResults()
  })
  afterEach(() => {
    capture.stop()
    for (const id of registered.splice(0)) unregisterTerminalWs(id)
    __resetBottomTerminal()
    queryClient.clear()
    vi.useRealTimers()
  })

  it('ignores a request that carries no command', () => {
    renderBridge()
    request({ reqId: 'r1' })
    expect(dockState().tabs).toHaveLength(0)
    expect(capture.results).toHaveLength(0)
  })

  it('ignores a request whose command is an empty string', () => {
    renderBridge()
    request({ code: '', reqId: 'r3' })
    expect(dockState().tabs).toHaveLength(0)
    expect(capture.results).toHaveLength(0)
  })

  it('opens a dock tab in the given cwd and answers exactly once with the reqId', () => {
    renderBridge('/work/project-a')
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
    renderBridge()
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
    renderBridge()
    for (let i = 0; i < MAX_TERMINALS; i++) act(() => { addTab() })
    request({ code: 'echo hi', reqId: 'r5' })
    expect(capture.results).toEqual([{ reqId: 'r5', ok: false }])
    expect(dockState().tabs).toHaveLength(MAX_TERMINALS)
  })

  it('reads the latest cwd without re-binding the listener', () => {
    const { rerender } = renderHook(({ cwd }: { cwd?: string }) => useRunInTerminalBridge(cwd), {
      initialProps: { cwd: '/a' },
      wrapper,
    })
    rerender({ cwd: '/b' })
    request({ code: 'pwd', reqId: 'r6' })
    const dock = dockState()
    expect(dock.tabs).toHaveLength(1)   // one listener → one tab, not two
    expect(dock.tabs[0].cwd).toBe('/b')
  })

  it('answers ok:false at once, minting no tab, in windows without a dock (popout / embed)', () => {
    renderBridge('/a', false)
    request({ code: 'echo hi', reqId: 'r7' })
    // Immediate — the requester must not sit on its own fallback timer.
    expect(capture.results).toEqual([{ reqId: 'r7', ok: false }])
    expect(dockState().tabs).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_TIMEOUT_MS + 500) })
    expect(capture.results).toHaveLength(1)
  })

  it('stops listening on unmount', () => {
    const { unmount } = renderBridge()
    unmount()
    request({ code: 'echo hi', reqId: 'r8' })
    expect(dockState().tabs).toHaveLength(0)
  })

  it('keeps a live shell and reports an error after the ready deadline', async () => {
    let sessionId = ''
    const fetchSpy = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      enabled: true,
      sessions: [{ session_id: sessionId, alive: true }],
    })))
    vi.stubGlobal('fetch', fetchSpy)
    const errors: string[] = []
    renderBridge(undefined, true, message => errors.push(message))
    request({ code: 'npm test', reqId: 'r9' })
    sessionId = dockState().tabs[0].id

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions')
    expect(dockState().tabs).toHaveLength(1)
    expect(deleteTerminalSessionSpy).not.toHaveBeenCalled()
    expect(disposeTerminalSessionSpy).not.toHaveBeenCalled()
    expect(errors).toHaveLength(1)
    expect(fetchSpy).not.toHaveBeenCalledWith(
      `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('rolls back a tab and PTY when the probe reports a dead shell', async () => {
    let sessionId = ''
    const fetchSpy = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      enabled: true,
      sessions: [{ session_id: sessionId, alive: false }],
    })))
    vi.stubGlobal('fetch', fetchSpy)
    const errors: string[] = []
    renderBridge(undefined, true, message => errors.push(message))
    request({ code: 'npm test', reqId: 'r10' })
    sessionId = dockState().tabs[0].id

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions')
    expect(deleteTerminalSessionSpy).toHaveBeenCalledWith(sessionId)
    expect(disposeTerminalSessionSpy).toHaveBeenCalledWith(sessionId)
    expect(errors).toHaveLength(1)
  })

  it('does not delete an absent session until the opening grace period expires', async () => {
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, sessions: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, sessions: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchSpy)
    const errors: string[] = []
    renderBridge(undefined, true, message => errors.push(message))
    request({ code: 'npm test', reqId: 'r11' })
    const sessionId = dockState().tabs[0].id

    await act(async () => {
      await vi.advanceTimersByTimeAsync(
        RUN_IN_TERMINAL_TIMEOUT_MS + RUN_IN_TERMINAL_OPENING_GRACE_MS + 1_000,
      )
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).toHaveBeenCalledTimes(2)
    expect(deleteTerminalSessionSpy).not.toHaveBeenCalled()
    expect(disposeTerminalSessionSpy).toHaveBeenCalledWith(sessionId)
    expect(errors).toHaveLength(1)
  })

  it('keeps the tab when the liveness probe fails', async () => {
    const fetchSpy = vi.fn().mockRejectedValue(new Error('network unavailable'))
    vi.stubGlobal('fetch', fetchSpy)
    const errors: string[] = []
    renderBridge(undefined, true, message => errors.push(message))
    request({ code: 'npm test', reqId: 'r12' })
    const sessionId = dockState().tabs[0].id

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions')
    expect(dockState().tabs).toHaveLength(1)
    expect(deleteTerminalSessionSpy).not.toHaveBeenCalled()
    expect(disposeTerminalSessionSpy).not.toHaveBeenCalled()
    expect(errors).toHaveLength(1)
    expect(fetchSpy).not.toHaveBeenCalledWith(
      `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('leaves a tab already closed by the user alone', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    renderBridge()
    request({ code: 'npm test', reqId: 'r13' })
    const sessionId = dockState().tabs[0].id
    act(() => { removeTab(sessionId) })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(deleteTerminalSessionSpy).not.toHaveBeenCalled()
  })

  it('leaves teardown to the terminal popout when it opens before the deadline', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    renderBridge()
    request({ code: 'npm test', reqId: 'r14' })
    terminalPopoutOpen.value = true

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(dockState().tabs).toHaveLength(1)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(deleteTerminalSessionSpy).not.toHaveBeenCalled()
  })

  it('shares one liveness probe between dispatches that reach their deadlines together', async () => {
    let sessionIds: string[] = []
    const fetchSpy = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      enabled: true,
      sessions: sessionIds.map(session_id => ({ session_id, alive: true })),
    })))
    vi.stubGlobal('fetch', fetchSpy)
    renderBridge()
    request({ code: 'npm test', reqId: 'r15' })
    request({ code: 'npm run lint', reqId: 'r16' })
    sessionIds = dockState().tabs.map(tab => tab.id)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_TIMEOUT_MS + 1_000)
      await vi.runAllTimersAsync()
      await flushAsyncWork()
    })

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(dockState().tabs).toHaveLength(2)
  })
})
