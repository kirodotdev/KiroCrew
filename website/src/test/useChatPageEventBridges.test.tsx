/**
 * The chat page's event bridges (pages/chat/page/eventBridges.ts), driven as a
 * hook with the page's collaborators replaced by spies.
 *
 * ChatPage-level suites already cover the Run-in-terminal happy path and its
 * rollback; this file pins the bridges that had no page-level witness:
 *
 *  - the Web Preview feed: a marker opens the Browser tab ONCE per URL (the
 *    persisted applied key dedupes), a refused (non-loopback) marker opens
 *    nothing, a heuristic hit only offers the card;
 *  - Electron reachability: every open slot is tracked, a closed one untracked;
 *    the agent-opened signal surfaces the panel for the ACTIVE slot only;
 *  - `mc:prefill-terminal`: types (never submits) a single-line command and
 *    answers with the request's `reqId`, `ok: false` for every refusal;
 *  - `mc:prefill-composer` appends to the unsent draft; `mc:redaction-hosts-changed`
 *    reloads the named slot;
 *  - the Run-in-terminal liveness probe treats a malformed sessions answer as a
 *    probe failure (tab kept, the failure narrated);
 *  - cold file tabs: a settled read patches the tab, a 404 keeps a placeholder,
 *    and a failure is reported once, not once per re-render.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const preview = vi.hoisted(() => ({
  hit: null as null | { url: string; open: boolean },
  accept: true,
  pending: vi.fn(),
}))
vi.mock('../utils/detectPreviewUrl', () => ({
  detectPreviewUrl: () => preview.hit,
  previewFeedDecision: (hit: unknown) => hit,
}))
vi.mock('../components/WebPreviewPanel', () => ({
  normalizeUrl: (raw: string) => (raw.startsWith('http') ? raw : null),
  setSessionPreviewPending: (slot: string, url: string) => {
    preview.pending(slot, url)
    return preview.accept ? url : null
  },
}))
const term = vi.hoisted(() => ({
  ready: new Map<string, () => void>(),
  unsub: vi.fn(),
  sendRaw: vi.fn(() => true),
}))
vi.mock('../utils/terminalRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/terminalRegistry')>()),
  onTerminalReady: (id: string, cb: () => void) => { term.ready.set(id, cb); return term.unsub },
  sendRawToTerminalSession: term.sendRaw,
}))
const fileRead = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('../utils/fileReadQuery', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/fileReadQuery')>()),
  fetchFileRead: fileRead.fetch,
}))

import { useChatEventBridges, useColdFileTabHydration } from '../pages/chat/page/eventBridges'
import { __resetBottomTerminal, useBottomTerminal } from '../hooks/useBottomTerminal'
import { RUN_IN_TERMINAL_READY_DEADLINE_MS } from '../utils/fenceShell'
import { openActivityPanel, setPendingInput } from '../store/chatSlice'
import type { ChatMessage, ChatSlot } from '../types'

type BridgeOpts = Parameters<typeof useChatEventBridges>[0]

function harness(overrides: Partial<BridgeOpts> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const openView = vi.fn()
  const opts: BridgeOpts = {
    activeSlot: 'slot-a',
    activeSlotRef: { current: 'slot-a' },
    messages: [{ role: 'assistant', content: 'hi' } as ChatMessage],
    slots: [{ key: 'slot-a' }, { key: 'slot-b' }] as ChatSlot[],
    dispatch: vi.fn() as never,
    queryClient,
    showActionError: vi.fn(),
    tabsCtlRef: { current: { openView } as never },
    currentProjectRef: { current: '/proj' },
    deleteTerminalSessionRef: { current: { mutate: vi.fn() } as never },
    terminalReuseRef: { current: false },
    terminalCfgUnsettledRef: { current: false },
    terminalCfgReadFailedRef: { current: false },
    inputRef: { current: '' },
    ...overrides,
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  const hook = renderHook((p: BridgeOpts) => useChatEventBridges(p), { initialProps: opts, wrapper })
  return { opts, openView, hook }
}

function collect(name: string) {
  const results: unknown[] = []
  const on = (e: Event) => { results.push((e as CustomEvent).detail) }
  window.addEventListener(name, on)
  return { results, stop: () => window.removeEventListener(name, on) }
}

beforeEach(() => {
  localStorage.clear()
  preview.hit = null
  preview.accept = true
  preview.pending.mockClear()
  term.ready.clear()
  term.unsub.mockClear()
  term.sendRaw.mockClear()
  term.sendRaw.mockReturnValue(true)
  fileRead.fetch.mockReset()
  __resetBottomTerminal()
  delete (window as { browserAPI?: unknown }).browserAPI
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('Web Preview feed', () => {
  it('opens the Browser tab for a marker once per URL, persisting the applied key', () => {
    preview.hit = { url: 'http://localhost:5173/', open: true }
    const { opts, openView, hook } = harness()
    expect(preview.pending).toHaveBeenCalledWith('slot-a', 'http://localhost:5173/')
    expect(opts.dispatch).toHaveBeenCalledWith(openActivityPanel())
    expect(openView).toHaveBeenCalledWith('browser')
    expect(localStorage.getItem('mc-webpreview-applied:slot-a')).toBe('http://localhost:5173/')
    // A new transcript with the same marker must not reopen what the user closed.
    hook.rerender({ ...opts, messages: [...opts.messages, { role: 'assistant', content: 'again' } as ChatMessage] })
    expect(openView).toHaveBeenCalledTimes(1)
    expect(preview.pending).toHaveBeenCalledTimes(1)
  })

  it('dedupes on the in-memory key when the persisted write is lost', () => {
    preview.hit = { url: 'http://localhost:1/', open: true }
    const { opts, openView, hook } = harness()
    localStorage.removeItem('mc-webpreview-applied:slot-a')
    hook.rerender({ ...opts, messages: [...opts.messages] })
    expect(openView).toHaveBeenCalledTimes(1)
  })

  it('opens nothing for a marker the pending setter refuses (non-loopback)', () => {
    preview.hit = { url: 'http://example.com/', open: true }
    preview.accept = false
    const { opts, openView } = harness()
    expect(preview.pending).toHaveBeenCalledTimes(1)
    expect(opts.dispatch).not.toHaveBeenCalled()
    expect(openView).not.toHaveBeenCalled()
  })

  it('offers the card for a heuristic hit without opening the tab', () => {
    preview.hit = { url: 'http://localhost:3000/', open: false }
    const { opts, openView } = harness()
    expect(preview.pending).toHaveBeenCalledWith('slot-a', 'http://localhost:3000/')
    expect(opts.dispatch).not.toHaveBeenCalled()
    expect(openView).not.toHaveBeenCalled()
  })

  it('feeds nothing for an unparseable URL or with no active slot', () => {
    preview.hit = { url: 'not a url', open: true }
    harness()
    harness({ activeSlot: null })
    expect(preview.pending).not.toHaveBeenCalled()
  })
})

describe('Electron built-in browser bridges', () => {
  it('tracks every open slot and untracks one that closed, without re-registering the rest', () => {
    const trackSession = vi.fn().mockResolvedValue(undefined)
    ;(window as { browserAPI?: unknown }).browserAPI = { trackSession }
    const { opts, hook } = harness()
    expect(trackSession.mock.calls).toEqual([['slot-a', true], ['slot-b', true]])
    hook.rerender({ ...opts, slots: [{ key: 'slot-b' }] as ChatSlot[] })
    expect(trackSession.mock.calls.slice(2)).toEqual([['slot-a', false]])
  })

  it('surfaces the panel on agent-opened for the active slot only', () => {
    let fire: ((e: { panelId?: string }) => void) | undefined
    const off = vi.fn()
    ;(window as { browserAPI?: unknown }).browserAPI = {
      onAgentOpened: (cb: typeof fire) => { fire = cb; return off },
    }
    const { opts, openView, hook } = harness()
    act(() => { fire!({ panelId: 'slot-b' }) })
    act(() => { fire!({}) })
    expect(openView).not.toHaveBeenCalled()
    act(() => { fire!({ panelId: 'slot-a' }) })
    expect(opts.dispatch).toHaveBeenCalledWith(openActivityPanel())
    expect(openView).toHaveBeenCalledWith('browser')
    hook.unmount()
    expect(off).toHaveBeenCalled()
  })
})

describe('redaction-card bridges', () => {
  it('types a single-line command into a new dock terminal and answers with the reqId', () => {
    const dock = renderHook(() => useBottomTerminal())
    harness()
    const { results, stop } = collect('mc:prefill-terminal-result')
    try {
      act(() => { window.dispatchEvent(new CustomEvent('mc:prefill-terminal', { detail: { command: 'curl https://h', reqId: 'p1' } })) })
      expect(dock.result.current.tabs).toHaveLength(1)
      const id = dock.result.current.tabs[0].id
      expect(results).toEqual([])
      act(() => { term.ready.get(id)!() })
      expect(term.sendRaw).toHaveBeenCalledWith(id, 'curl https://h')
      expect(results).toEqual([{ reqId: 'p1', ok: true }])
    } finally { stop() }
  })

  it('refuses a missing, empty or multi-line command without opening a terminal', () => {
    const dock = renderHook(() => useBottomTerminal())
    harness()
    const { results, stop } = collect('mc:prefill-terminal-result')
    try {
      for (const [command, reqId] of [[undefined, 'a'], ['', 'b'], ['ls\nrm -rf ~', 'c'], ['ls\r', 'd']] as const) {
        act(() => { window.dispatchEvent(new CustomEvent('mc:prefill-terminal', { detail: { command, reqId } })) })
      }
      expect(results).toEqual([{ reqId: 'a', ok: false }, { reqId: 'b', ok: false }, { reqId: 'c', ok: false }, { reqId: 'd', ok: false }])
      expect(dock.result.current.tabs).toHaveLength(0)
      expect(term.sendRaw).not.toHaveBeenCalled()
    } finally { stop() }
  })

  it('answers ok:false at the ready deadline and drops its ready listener', () => {
    vi.useFakeTimers()
    renderHook(() => useBottomTerminal())
    harness()
    const { results, stop } = collect('mc:prefill-terminal-result')
    try {
      act(() => { window.dispatchEvent(new CustomEvent('mc:prefill-terminal', { detail: { command: 'ls', reqId: 'p2' } })) })
      act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_READY_DEADLINE_MS) })
      expect(term.unsub).toHaveBeenCalledTimes(1)
      expect(results).toEqual([{ reqId: 'p2', ok: false }])
    } finally { stop() }
  })

  it('appends a pre-filled request to the unsent draft', () => {
    const { opts } = harness({ inputRef: { current: 'draft so far' } })
    act(() => { window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text: 'please fetch it' } })) })
    act(() => { window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text: '' } })) })
    expect(opts.dispatch).toHaveBeenCalledTimes(1)
    expect(opts.dispatch).toHaveBeenCalledWith(setPendingInput('draft so far\n\nplease fetch it'))
  })

  it('reloads the named slot when a host is allowed, and ignores a malformed event', () => {
    const { opts } = harness()
    act(() => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: {} })) })
    expect(opts.dispatch).not.toHaveBeenCalled()
    act(() => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 'slot-b' } })) })
    expect(opts.dispatch).toHaveBeenCalledTimes(1)
    expect(opts.dispatch).toHaveBeenCalledWith(expect.any(Function))
  })

  it('asks the reload to re-serve the card\'s own reply when the event names it (#17023)', () => {
    const { opts } = harness()
    act(() => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 'slot-b', messageTs: '2026-01-01T00:00:00Z' } })) })
    act(() => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 'slot-b', messageTs: 7 } })) })
    // The thunk's own pending action carries the argument it was created with.
    const argOf = (i: number): unknown => {
      const thunk = (opts.dispatch as unknown as { mock: { calls: unknown[][] } }).mock.calls[i][0] as
        (d: (a: unknown) => unknown, g: () => unknown, x: unknown) => unknown
      const seen: { meta?: { arg?: unknown } }[] = []
      void thunk(a => { seen.push(a as never); return a }, () => ({ chat: { activeSlot: null } }), undefined)
      return seen[0]?.meta?.arg
    }
    expect(argOf(0)).toEqual({ key: 'slot-b', reachTs: '2026-01-01T00:00:00Z' })
    expect(argOf(1)).toBe('slot-b')
  })

  it('removes every listener on unmount', () => {
    const { opts, hook } = harness()
    hook.unmount()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text: 'x' } }))
      window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 's' } }))
    })
    expect(opts.dispatch).not.toHaveBeenCalled()
  })
})

describe('Run-in-terminal liveness probe', () => {
  async function probeWith(body: unknown) {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(body) }))
    const dock = renderHook(() => useBottomTerminal())
    const { opts } = harness()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: 'npm test', reqId: 'r' } })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 10) })
    await waitFor(() => expect(opts.showActionError).toHaveBeenCalled())
    warn.mockRestore()
    return { opts, dock }
  }

  it('keeps the tab and narrates a probe failure when the sessions list is malformed', async () => {
    const { opts, dock } = await probeWith({ sessions: 'nope' })
    expect(opts.showActionError).toHaveBeenCalledWith(expect.stringMatching(/./))
    expect(dock.result.current.tabs).toHaveLength(1)
    expect(opts.deleteTerminalSessionRef.current.mutate).not.toHaveBeenCalled()
  })

  it('treats a listed session without a boolean liveness as a probe failure', async () => {
    const dock0 = renderHook(() => useBottomTerminal())
    const sid = () => dock0.result.current.tabs[0]?.id
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchSpy = vi.fn(async () => ({ ok: true, json: () => Promise.resolve({ sessions: [{ session_id: sid(), alive: 'yes' }] }) }))
    vi.stubGlobal('fetch', fetchSpy)
    const { opts } = harness()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: 'npm test', reqId: 'r' } })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 10) })
    await waitFor(() => expect(opts.showActionError).toHaveBeenCalledTimes(1))
    expect(warn).toHaveBeenCalledWith('run-in-terminal: liveness probe failed:', 'Invalid terminal session liveness response')
    warn.mockRestore()
    expect(dock0.result.current.tabs).toHaveLength(1)
  })
})

describe('cold file tab hydration', () => {
  type Tab = { id: string; kind: string; path?: string; content?: string }
  function hydrate(tabs: Tab[]) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const patchTab = vi.fn()
    const showActionError = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )
    const hook = renderHook(
      (p: { tabs: Tab[] }) => useColdFileTabHydration({ tabsCtl: { tabs: p.tabs, patchTab } as never, showActionError }),
      { initialProps: { tabs }, wrapper },
    )
    return { hook, patchTab, showActionError }
  }

  it('patches a settled read into the tab with its binary and partial verdicts', async () => {
    fileRead.fetch.mockResolvedValue({ ok: true, text: 'body', binary: false, status: 200 })
    const { patchTab } = hydrate([{ id: 't1', kind: 'file', path: '/a.ts' }, { id: 't2', kind: 'file', path: '/b.ts', content: 'warm' }, { id: 'd', kind: 'diff', path: '/c' }])
    await waitFor(() => expect(patchTab).toHaveBeenCalledTimes(1))
    expect(fileRead.fetch).toHaveBeenCalledTimes(1)
    expect(patchTab).toHaveBeenCalledWith('t1', expect.objectContaining({ content: 'body', savedContent: 'body', binary: false }))
  })

  it('keeps a placeholder for a 404', async () => {
    fileRead.fetch.mockResolvedValue({ ok: false, status: 404 })
    const { patchTab, showActionError } = hydrate([{ id: 't1', kind: 'file', path: '/gone.ts' }])
    await waitFor(() => expect(patchTab).toHaveBeenCalledTimes(1))
    expect(patchTab.mock.calls[0][1]).toMatchObject({ binary: false, partial: false })
    expect(patchTab.mock.calls[0][1].content).toBe(patchTab.mock.calls[0][1].savedContent)
    expect(patchTab.mock.calls[0][1].content).toMatch(/./)
    expect(showActionError).not.toHaveBeenCalled()
  })

  it('reports a failed status or a thrown read once, leaving the tab cold', async () => {
    fileRead.fetch.mockImplementation(async (path: string) => {
      if (path === '/boom.ts') throw new Error('disk on fire')
      return { ok: false, status: 500 }
    })
    const tabs = [{ id: 'x', kind: 'file', path: '/err.ts' }, { id: 'y', kind: 'file', path: '/boom.ts' }]
    const { hook, patchTab, showActionError } = hydrate(tabs)
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(2))
    const texts = showActionError.mock.calls.map(c => c[0] as string)
    expect(texts.some(t => t.includes('/err.ts') && t.includes('500'))).toBe(true)
    expect(texts.some(t => t.includes('/boom.ts') && t.includes('disk on fire'))).toBe(true)
    hook.rerender({ tabs: [...tabs] })
    await act(async () => {})
    expect(showActionError).toHaveBeenCalledTimes(2)
    expect(patchTab).not.toHaveBeenCalled()
  })
})
