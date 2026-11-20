/**
 * Run in terminal reports only commands from the host's own main transcript.
 * Execution scope also covers other chats, which still run without reporting.
 * The gateway decides whether the reported slot is a Bot's thread.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor, fireEvent, screen, within } from '@testing-library/react'
import { useRef, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const note = vi.hoisted(() => ({ calls: [] as Array<[string, Record<string, unknown>]> }))
const term = vi.hoisted(() => ({ sendOk: true, copyOk: true, sent: [] as string[] }))
vi.mock('../utils/terminalRunNote', () => ({
  noteTerminalRun: (slot: string, n: Record<string, unknown>) => { note.calls.push([slot, n]) },
}))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, kirocrewConfig: () => Promise.resolve({}) } }
})
vi.mock('../utils/terminalRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/terminalRegistry')>()),
  onTerminalReady: (_id: string, ready: () => void) => { ready(); return () => {} },
  sendToTerminalSession: (_id: string, text: string) => { term.sent.push(text); return term.sendOk },
}))
vi.mock('../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyToClipboard: () => Promise.resolve(term.copyOk),
}))

import { useRunInTerminalBridge, useRunInTerminalRefs } from '../hooks/useRunInTerminalBridge'
import { __resetBottomTerminal } from '../hooks/useBottomTerminal'
import { RunInTerminalScope, RunInTerminalOriginSlot } from '../components/runInTerminalScope'
import RunInTerminalBtn from '../components/RunInTerminalBtn'
import RunYourselfCard from '../components/RunYourselfCard'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'

let scope = ''

function Host({ ready = true, runsHere = true, children }: { ready?: boolean; runsHere?: boolean | undefined; reuse?: boolean; children?: ReactNode }) {
  const cwdRef = useRef<string | undefined>('/proj')
  const readyRef = useRef(ready)
  const runsHereRef = useRef<boolean | undefined>(runsHere)
  const slotRef = useRef<string | null | undefined>('member-atlas')
  const refs = useRunInTerminalRefs()
  scope = useRunInTerminalBridge({
    queryClient: qcRef!, showActionError: () => {}, cwdRef, readyRef, runsHereRef, slotRef, ...refs,
  })
  return <RunInTerminalScope.Provider value={scope}>{children}</RunInTerminalScope.Provider>
}

let qcRef: QueryClient | null = null
const mount = (props: Parameters<typeof Host>[0] = {}) => {
  qcRef = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qcRef.setQueryData(['kirocrewConfig'], props.reuse ? { dashboard: { terminal: { reuse_current: true } } } : {})
  return render(<QueryClientProvider client={qcRef}><Host {...props} /></QueryClientProvider>)
}
const run = (detail: Record<string, unknown>) => act(() => {
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { originSlot: 'member-atlas', ...detail } }))
})

async function confirmIn(surface: HTMLElement) {
  fireEvent.click(within(surface).getByRole('button', { name: 'Run in terminal' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^Run( anyway)?$/ }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
}

describe('Run in terminal tells the chat it came from', () => {
  beforeEach(() => {
    __resetBottomTerminal()
    note.calls = []
    term.sendOk = true
    term.copyOk = true
    term.sent = []
    setTerminalEnabledFlag(true)
  })
  afterEach(() => {
    act(() => {
      __resetBottomTerminal()
      setTerminalEnabledFlag(false)
    })
  })

  it('reports a run sent to a new tab once, with where the command sat', () => {
    const before = window.location.href
    mount()
    run({ code: 'npm test', reqId: 'r1', scope, source: 'run_yourself' })
    expect(note.calls).toEqual([
      ['member-atlas', { command: 'npm test', outcome: 'ran', reason: undefined, source: 'run_yourself' }],
    ])
    expect(window.location.href).toBe(before)
  })

  it('reports a command the terminal did not take', () => {
    term.sendOk = false
    mount()
    run({ code: 'ls', reqId: 'r1', scope })
    expect(note.calls).toEqual([
      ['member-atlas', { command: 'ls', outcome: 'failed', reason: 'not_sent', source: 'code_block' }],
    ])
  })

  it('reports a refusal and why', () => {
    mount({ runsHere: false })
    run({ code: 'ls', reqId: 'r1', scope })
    mount({ ready: false })
    run({ code: 'ls', reqId: 'r2', scope })
    expect(note.calls.map(([, n]) => n.reason)).toEqual(['remote_chat', 'not_ready'])
  })

  it('reports a copy instead of a run, and a refused copy', async () => {
    mount({ reuse: true })
    run({ code: 'ls', reqId: 'r1', scope })
    await waitFor(() => expect(note.calls).toHaveLength(1))
    expect(note.calls[0][1]).toMatchObject({ outcome: 'copied' })
    term.copyOk = false
    run({ code: 'ls', reqId: 'r2', scope })
    await waitFor(() => expect(note.calls).toHaveLength(2))
    expect(note.calls[1][1]).toMatchObject({ outcome: 'failed', reason: 'copy_failed' })
  })

  it('sends nothing for a request outside this host', () => {
    mount()
    run({ code: 'ls', reqId: 'r1', scope: 'another-host' })
    run({ code: 'ls', reqId: 'r2' })
    expect(note.calls).toEqual([])
  })

  it.each([undefined, 'side:member-atlas', 'worker-1', 'crew-1', 'app-1'])('runs but reports nothing for non-owner origin %s', (originSlot) => {
    mount()
    const results: unknown[] = []
    const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
    window.addEventListener('mc:run-in-terminal-result', onResult)
    try {
      run({ code: 'echo side', reqId: 'side-1', scope, originSlot })
      expect(term.sent).toEqual(['echo side'])
      expect(results).toEqual([{ reqId: 'side-1', ok: true }])
      expect(note.calls).toEqual([])
    } finally {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
    }
  })

  it.each(['code_block', 'run_yourself'] as const)('only the main transcript opts its %s into reporting under the same execution scope', async (source) => {
    const command = source === 'run_yourself'
      ? <RunYourselfCard code="echo hello" />
      : <RunInTerminalBtn code="echo hello" lang="bash" />
    mount({ children: <>
      <div data-testid="main-transcript">
        <RunInTerminalOriginSlot.Provider value="member-atlas">{command}</RunInTerminalOriginSlot.Provider>
      </div>
      <div data-testid="non-owner-transcript">{command}</div>
    </> })
    await confirmIn(screen.getByTestId('non-owner-transcript'))
    expect(term.sent).toEqual(['echo hello'])
    expect(note.calls).toEqual([])
    await confirmIn(screen.getByTestId('main-transcript'))
    expect(term.sent).toEqual(['echo hello', 'echo hello'])
    expect(note.calls).toEqual([
      ['member-atlas', { command: 'echo hello', outcome: 'ran', reason: undefined, source }],
    ])
  })
})
