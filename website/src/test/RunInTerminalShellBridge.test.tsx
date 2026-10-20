/**
 * The shell's Run-in-terminal receiver (shell/runInTerminalShellBridge.tsx).
 *
 * Pins the behavior #10708 adds: a request with no scope (a code block outside
 * every chat, on any route) opens ONE dock terminal in the selected session's
 * project, the shell stands down while a host that answers unscoped requests
 * itself (the chat page) is mounted, it never answers another host's scope,
 * and a failed run shows a dismiss-only notice.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const term = vi.hoisted(() => ({ unsub: vi.fn() }))
const cfg = vi.hoisted(() => ({ fail: false }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: { ...actual.api, kirocrewConfig: () => (cfg.fail ? Promise.reject(new Error('down')) : Promise.resolve({})) },
  }
})
vi.mock('../utils/terminalRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/terminalRegistry')>()),
  onTerminalReady: () => term.unsub,
}))

import { RunInTerminalShellNotice, useRunInTerminalShellBridge } from '../shell/runInTerminalShellBridge'

function RunInTerminalShellBridge({ cwd, hasDock = true }: { cwd: string | undefined; hasDock?: boolean }) {
  const { error, dismiss } = useRunInTerminalShellBridge(cwd, hasDock)
  return <RunInTerminalShellNotice error={error} onDismiss={dismiss} />
}
import { useRunInTerminalBridge, useRunInTerminalRefs } from '../hooks/useRunInTerminalBridge'
import { __resetBottomTerminal, addTab, MAX_TERMINALS, useBottomTerminal } from '../hooks/useBottomTerminal'

function client() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A settled config read with reuse off: the run path, not the copy path.
  qc.setQueryData(['kirocrewConfig'], {})
  return qc
}

const run = (detail: Record<string, unknown>) => act(() => {
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail }))
})

/** A stand-in for the chat page: a host that answers unscoped requests. */
function UnscopedHost({ qc }: { qc: QueryClient }) {
  const cwdRef = useRef<string | undefined>('/chat-project')
  useRunInTerminalBridge({ queryClient: qc, showActionError: () => {}, cwdRef, acceptUnscoped: true, ...useRunInTerminalRefs() })
  return null
}

describe('RunInTerminalShellBridge', () => {
  let qc: QueryClient
  let results: Array<{ reqId: string; ok: boolean }>
  const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
  const wrap = (node: ReactNode) => <QueryClientProvider client={qc}>{node}</QueryClientProvider>

  beforeEach(() => {
    __resetBottomTerminal()
    qc = client()
    results = []
    cfg.fail = false
    window.addEventListener('mc:run-in-terminal-result', onResult)
  })
  afterEach(() => {
    window.removeEventListener('mc:run-in-terminal-result', onResult)
    act(() => { __resetBottomTerminal() })
  })

  it('opens one dock terminal in the selected project for an unscoped request', () => {
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    run({ code: 'npm test', reqId: 'r1' })
    expect(dock.result.current.tabs).toEqual([expect.objectContaining({ cwd: '/proj' })])
  })

  it('starts in the server default when no project is selected', () => {
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd={undefined} />))
    run({ code: 'ls', reqId: 'r1' })
    expect(dock.result.current.tabs).toHaveLength(1)
    expect(dock.result.current.tabs[0].cwd).toBeUndefined()
  })

  it('ignores a request scoped to another host and an empty command', () => {
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    run({ code: 'ls', reqId: 'r1', scope: 'some-chat' })
    run({ code: '', reqId: 'r2' })
    run({ reqId: 'r3' })
    expect(dock.result.current.tabs).toHaveLength(0)
    expect(results).toEqual([])
  })

  it('answers nothing in a window with no dock (popout, embed)', () => {
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd="/proj" hasDock={false} />))
    run({ code: 'ls', reqId: 'r1' })
    expect(dock.result.current.tabs).toHaveLength(0)
    expect(results).toEqual([])
  })

  it('stands down while the chat page answers unscoped requests, and resumes when it unmounts', () => {
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    const chat = render(wrap(<UnscopedHost qc={qc} />))
    run({ code: 'ls', reqId: 'r1' })
    // Exactly one terminal for one click, and it is the chat page's.
    expect(dock.result.current.tabs).toEqual([expect.objectContaining({ cwd: '/chat-project' })])
    chat.unmount()
    run({ code: 'ls', reqId: 'r2' })
    expect(dock.result.current.tabs).toHaveLength(2)
    expect(dock.result.current.tabs[1].cwd).toBe('/proj')
  })

  it('answers ok:false and shows a dismiss-only notice when the terminal cap is full', async () => {
    const user = userEvent.setup()
    act(() => { for (let i = 0; i < MAX_TERMINALS; i++) addTab() })
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    expect(screen.queryByTestId('run-in-terminal-error')).not.toBeInTheDocument()
    run({ code: 'ls', reqId: 'r1' })
    expect(results).toEqual([{ reqId: 'r1', ok: false }])
    const notice = screen.getByTestId('run-in-terminal-error')
    expect(notice).toHaveTextContent("Couldn't run in terminal")
    expect(within(notice).queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    await user.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('run-in-terminal-error')).not.toBeInTheDocument()
  })

  it('clears the failure notice once a later run succeeds, and keeps it on another failure', () => {
    act(() => { for (let i = 0; i < MAX_TERMINALS; i++) addTab() })
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    run({ code: 'ls', reqId: 'r1' })
    expect(screen.getByTestId('run-in-terminal-error')).toBeInTheDocument()
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId: 'x', ok: false } })) })
    expect(screen.getByTestId('run-in-terminal-error')).toBeInTheDocument()
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId: 'r2', ok: true } })) })
    expect(screen.queryByTestId('run-in-terminal-error')).not.toBeInTheDocument()
  })

  it('keeps the config-read failure notice when that same run succeeds', async () => {
    // A failed reuse-setting read: the run still goes to a fresh tab.
    cfg.fail = true
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const dock = renderHook(() => useBottomTerminal())
    render(wrap(<RunInTerminalShellBridge cwd="/proj" />))
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    run({ code: 'ls', reqId: 'r1' })
    expect(dock.result.current.tabs).toHaveLength(1)
    expect(screen.getByTestId('run-in-terminal-error')).toBeInTheDocument()
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId: 'r1', ok: true } })) })
    expect(screen.getByTestId('run-in-terminal-error')).toBeInTheDocument()
  })
})
