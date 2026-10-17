/**
 * `credential_redaction_changed` (owner sockets only): the owner flipped the
 * credential-redaction switch, possibly in ANOTHER browser tab. This document
 * must re-read the switch and drop every file body it holds -- react-query
 * `['file-read', path]` / `['file-diff', path]` and open side-panel tab bodies --
 * so no dashboard document keeps showing raw credentials after redaction is
 * back on.
 */
import { renderHook, act, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { useWebSocket, __resetRedactionHealForTests } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

vi.mock('../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../api/apiError')>('../api/apiError')).ApiError,
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    credentialRedaction: vi.fn(),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []
class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor() { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('useWebSocket credential_redaction_changed frame', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    __resetPanelTabs()
    __resetRedactionHealForTests()
  })
  afterEach(() => { vi.unstubAllGlobals() })
  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  async function reconnectWith(opts: { held?: { enabled: boolean }; server: { enabled: boolean; changed_at?: string } }) {
    qc.setQueryData(['file-read', '/tmp/raw.txt'], { content: 'AKIA-raw-while-off' })
    if (opts.held) qc.setQueryData(['credential-redaction'], { ...opts.held, changed_at: '' })
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue({ changed_at: '', ...opts.server })
    const tabs = renderHook(() => usePanelTabs('slot-a', []))
    act(() => tabs.result.current.openFile('/tmp/raw.txt', 'AKIA-raw-while-off', 'slot-a'))
    act(() => tabs.result.current.openDiff('/tmp/raw.txt', 'a', 'b'))
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    act(() => { first.onclose?.(new CloseEvent('close')) })
    const second = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { second.simulateOpen() })
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    return tabs
  }

  it('a RECONNECT purges when the switch moved while the socket was down (the push has no replay)', async () => {
    const tabs = await reconnectWith({ held: { enabled: false }, server: { enabled: true } })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBeUndefined()
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(false)
  })

  it('a RECONNECT with the switch unmoved leaves diff tabs and file bodies alone', async () => {
    const tabs = await reconnectWith({ held: { enabled: true }, server: { enabled: true } })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toEqual({ content: 'AKIA-raw-while-off' })
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBe('AKIA-raw-while-off')
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(true)
  })

  it('a document that never read the switch purges on reconnect when the switch is now ON and has been flipped before', async () => {
    // A file opened raw while OFF may be on screen without Settings ever having
    // been visited; only this re-read can catch the flip made elsewhere.
    const tabs = await reconnectWith({ server: { enabled: true, changed_at: '2026-01-01T00:00:00Z' } })
    expect(api.credentialRedaction).toHaveBeenCalledTimes(1)
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(false)
  })

  it('the shipped default (ON, never flipped, Settings never opened) purges nothing on a transient drop', async () => {
    const tabs = await reconnectWith({ server: { enabled: true, changed_at: '' } })
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(true)
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toEqual({ content: 'AKIA-raw-while-off' })
  })

  it('a refused read (non-owner) is asked once, then the reconnect heal stops asking', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(403, 'owner required', JSON.stringify({ code: 'dashboard_owner_required' })))
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    for (let i = 0; i < 3; i++) {
      const cur = WS_INSTANCES[WS_INSTANCES.length - 1]
      act(() => { cur.onclose?.(new CloseEvent('close')) })
      const next = WS_INSTANCES[WS_INSTANCES.length - 1]
      act(() => { next.simulateOpen() })
      await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    }
    expect(api.credentialRedaction).toHaveBeenCalledTimes(1)
  })

  it('a document that never read the switch is left alone when the switch is still OFF', async () => {
    const tabs = await reconnectWith({ server: { enabled: false } })
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(true)
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toEqual({ content: 'AKIA-raw-while-off' })
  })

  it('re-reads the switch and drops every cached file body plus the open tab bodies', () => {
    qc.setQueryData(['file-read', '/tmp/raw.txt'], { content: 'AKIA-raw-while-off' })
    qc.setQueryData(['file-diff', '/tmp/raw.txt'], { diff: '', original: 'AKIA-raw-while-off', status: 'clean' })
    qc.setQueryData(['credential-redaction'], { enabled: false, changed_at: '2026-01-01T00:00:00Z' })
    const tabs = renderHook(() => usePanelTabs('slot-a', []))
    act(() => tabs.result.current.openFile('/tmp/raw.txt', 'AKIA-raw-while-off', 'slot-a'))
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'credential_redaction_changed', data: { enabled: true, changed_at: '2026-01-01T00:01:00Z' } }) })
    const keys = invalidateSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['credential-redaction']))
    // The frame's payload seeds the switch entry, so the reconnect heal can compare later.
    expect(qc.getQueryData(['credential-redaction'])).toEqual({ enabled: true, changed_at: '2026-01-01T00:01:00Z' })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(qc.getQueryData(['file-diff', '/tmp/raw.txt'])).toBeUndefined()
    // The open tab body is gone too: it rehydrates through /api/file-read.
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBeUndefined()
  })

  // A MOUNTED observer -- the Library's SessionDocPreview is a `useQuery` on the
  // same `['file-read', path]` key -- must not keep rendering the raw body it
  // last received: removing the cache entry alone leaves the observer's result
  // in place with nothing to re-render it. The purge RESETS the query, so the
  // observer sees `data: undefined` at once and refetches under the pass now in
  // force.
  it('a MOUNTED file-read observer drops its raw body and refetches when the switch flips', async () => {
    // First read answers RAW at once; the second (the refetch the purge
    // triggers) is HELD, so the window between the flip and the re-read is
    // observable: the raw body must already be gone from the rendered result.
    let release: () => void = () => {}
    const queryFn = vi.fn()
      .mockResolvedValueOnce({ text: 'AKIA-raw-while-off', ok: true, status: 200 })
      .mockImplementationOnce(() => new Promise(r => { release = () => r({ text: '[REDACTED: credential]', ok: true, status: 200 }) }))
    const preview = renderHook(
      () => useQuery({ queryKey: ['file-read', '/tmp/raw.txt'], queryFn, staleTime: 10_000 }),
      { wrapper },
    )
    await waitFor(() => expect(preview.result.current.data?.text).toBe('AKIA-raw-while-off'))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'credential_redaction_changed', data: { enabled: true, changed_at: '2026-01-01T00:01:00Z' } }) })
    // The raw body leaves the rendered result BEFORE the refetch answers.
    await waitFor(() => expect(preview.result.current.data).toBeUndefined())
    expect(queryFn).toHaveBeenCalledTimes(2)
    act(() => { release() })
    await waitFor(() => expect(preview.result.current.data?.text).toBe('[REDACTED: credential]'))
  })
})
