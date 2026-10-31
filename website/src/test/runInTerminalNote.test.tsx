/**
 * What became of a Run in terminal click reaches the chat it came from
 * (hooks/useRunInTerminalBridge -> utils/terminalRunNote): ran, copied instead,
 * or failed with why, sent once per click for the host's own chat slot. The
 * gateway decides whether that slot is a crewmate's thread. A request outside
 * the host's scope names no chat, so nothing is sent, and nothing navigates.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const note = vi.hoisted(() => ({ calls: [] as Array<[string, Record<string, unknown>]> }))
const term = vi.hoisted(() => ({ sendOk: true, copyOk: true }))
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
  sendToTerminalSession: () => term.sendOk,
}))
vi.mock('../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyToClipboard: () => Promise.resolve(term.copyOk),
}))

import { useRunInTerminalBridge, useRunInTerminalRefs } from '../hooks/useRunInTerminalBridge'
import { __resetBottomTerminal } from '../hooks/useBottomTerminal'

let scope = ''

function Host({ ready = true, runsHere = true }: { ready?: boolean; runsHere?: boolean | undefined; reuse?: boolean }) {
  const cwdRef = useRef<string | undefined>('/proj')
  const readyRef = useRef(ready)
  const runsHereRef = useRef<boolean | undefined>(runsHere)
  const slotRef = useRef<string | null | undefined>('member-atlas')
  const refs = useRunInTerminalRefs()
  scope = useRunInTerminalBridge({
    queryClient: qcRef!, showActionError: () => {}, cwdRef, readyRef, runsHereRef, slotRef, ...refs,
  })
  return null
}

let qcRef: QueryClient | null = null
const mount = (props: Parameters<typeof Host>[0] = {}) => {
  qcRef = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qcRef.setQueryData(['kirocrewConfig'], props.reuse ? { dashboard: { terminal: { reuse_current: true } } } : {})
  return render(<QueryClientProvider client={qcRef}><Host {...props} /></QueryClientProvider>)
}
const run = (detail: Record<string, unknown>) => act(() => {
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail }))
})

describe('Run in terminal tells the chat it came from', () => {
  beforeEach(() => {
    __resetBottomTerminal()
    note.calls = []
    term.sendOk = true
    term.copyOk = true
  })
  afterEach(() => { act(() => { __resetBottomTerminal() }) })

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
})
