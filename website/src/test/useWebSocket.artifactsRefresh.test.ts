/**
 * The artifact library's freshness contract in `useWebSocket.ts` (#10867).
 *
 * The Artifacts page does not poll. Live `artifact_update` frames keep its
 * queries fresh while the socket is up — but a frame pushed while the socket
 * was DOWN was never delivered, and a list query that errored during the gap
 * (gateway restart 403s / connection refused) holds no data at all. Nothing
 * else would ever refetch it, so the tab renders an empty library until a
 * manual hard refresh. Two heal paths close that:
 *
 * 1. The server's generic `refresh` broadcast must invalidate the
 *    ['artifacts'] prefix (filtered list + all-tags) like the other
 *    refresh-driven caches.
 * 2. A reconnect must invalidate it too — same catch-up reasoning as the
 *    session-summary invalidation beside it.
 */
import { renderHook, render, screen, waitFor, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import SessionStatusFrame from '../pages/chat/command-center/SessionStatusFrame'
import type { DynamicDashboardCard } from '../types/dynamicDashboard'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }) }))
// Sanitizer safety has its own tests; exercise query/iframe lifetime here.
vi.mock('../pages/chat/command-center/dashboardDocument', () => ({ dashboardDocument: (html: string) => html }))
vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (html: string | null) => ({ url: html ? '/sandbox/card' : null, pending: false, failed: false, retry: vi.fn() }),
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    dashboardCard: vi.fn(),
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

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket artifact library freshness', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.dashboardCard).mockReset()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { qc.clear(); vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  it("invalidates ['artifacts'] on the server's refresh broadcast", () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()  // ignore the on-open burst; this asserts the frame's effect
    // The handler reads `data.kinds` — the envelope nests payload under `data`.
    act(() => { ws.simulateMessage({ type: 'refresh', data: { kinds: [] } }) })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifacts']))
    // The folder list fails under the same trigger and renders `?? []` the
    // same way — the broadcast heals both or the library recovers half-empty.
    expect(keys).toContain(JSON.stringify(['artifact-folders']))
  })

  it.each([
    ['dashboard_card', { slot: 'worker' }, ['dashboard-card', 'worker']],
    ['question_card', { slot: 'worker', card_id: 'q', questions: [] }, ['command-center', 'questions']],
    ['question_card_resolved', { slot: 'worker', card_id: 'q' }, ['command-center', 'questions']],
    ['approval', { slot: 'worker', id: 'a' }, ['command-center', 'approvals']],
    ['approval_resolved', { id: 'a' }, ['command-center', 'approvals']],
  ])('reconciles %s without waiting for the inventory timer', (type, data, key) => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()
    act(() => { ws.simulateMessage({ type, data }) })
    expect(spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))).toContain(JSON.stringify(key))
  })

  it("invalidates ['artifacts'] on a reconnect, not just on a live frame", () => {
    // Catch-up path: `artifact_update` frames pushed while the socket was down
    // were never delivered, and the page does not poll. A prefix invalidation
    // also flips an ERRORED list query back to a refetch, which is the #10867
    // recovery: without it a window that 403'd across a gateway restart shows
    // an empty library until a manual hard refresh.
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    const spy = vi.spyOn(qc, 'invalidateQueries')
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    const reconnected = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { reconnected.simulateOpen() })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifacts']))
    expect(keys).toContain(JSON.stringify(['artifact-folders']))
  })

  const cardKey = ['dashboard-card', 'worker', '', 'persistent']
  const oldCard: DynamicDashboardCard = {
    card: { html: '<p>Previous occupant</p>', data: {} }, status: 'published',
    published_at: 1, content_event_at: 1, stale: false,
  }
  const emptyCard: DynamicDashboardCard = {
    card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false,
  }

  it.each([
    ['dashboard_card', true], ['dashboard_card', false],
    ['slot_patch', true], ['slot_patch', false],
    ['reconnect', true], ['reconnect', false],
  ] as const)('clears %s card content before a same-key remount (active=%s)', async (event, active) => {
    let finishRead!: (value: DynamicDashboardCard) => void
    vi.mocked(api.dashboardCard).mockReturnValue(new Promise(resolve => { finishRead = resolve }))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen() })
    qc.setQueryData(cardKey, oldCard)
    qc.setQueryData(['dashboard-card', 'other'], oldCard)
    qc.setQueryData(['session-summary', 'worker'], { summary: 'Unrelated cache' })
    const view = render(createElement(SessionStatusFrame, { slot: 'worker', title: 'Worker', active }), { wrapper })
    if (active) expect(screen.getByTitle('Worker')).toBeInTheDocument()
    act(() => {
      if (event === 'reconnect') ws.simulateOpen()
      else ws.simulateMessage({ type: event, data: event === 'dashboard_card'
        ? { slot: 'worker', removed: true } : { removed: ['worker'] } })
    })
    expect(qc.getQueryData(cardKey)).toBeUndefined()
    if (event !== 'reconnect') expect(qc.getQueryData(['dashboard-card', 'other'])).toEqual(oldCard)
    else expect(qc.getQueryData(['dashboard-card', 'other'])).toBeUndefined()
    expect(qc.getQueryData(['session-summary', 'worker'])).toEqual({ summary: 'Unrelated cache' })
    if (!active) expect(api.dashboardCard).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTitle('Worker')).not.toBeInTheDocument())
    view.unmount()
    render(createElement(SessionStatusFrame, { slot: 'worker', title: 'Worker', active: true }), { wrapper })
    expect(screen.queryByTitle('Worker')).not.toBeInTheDocument()
    await act(async () => { finishRead(emptyCard) })
    await waitFor(() => expect(qc.getQueryData(cardKey)).toEqual(emptyCard))
  })

  it.each(['dashboard_card', 'slot_patch', 'reconnect'])('cancels an old in-flight read on %s without resurrecting its response', async (event) => {
    let finishOld!: (value: DynamicDashboardCard) => void
    let finishNew!: (value: DynamicDashboardCard) => void
    vi.mocked(api.dashboardCard)
      .mockReturnValueOnce(new Promise(resolve => { finishOld = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { finishNew = resolve }))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen() })
    render(createElement(SessionStatusFrame, { slot: 'worker', title: 'Worker', active: true }), { wrapper })
    await waitFor(() => expect(api.dashboardCard).toHaveBeenCalledTimes(1))
    act(() => {
      if (event === 'reconnect') ws.simulateOpen()
      else ws.simulateMessage({ type: event, data: event === 'dashboard_card'
        ? { slot: 'worker', removed: true } : { removed: ['worker'] } })
    })
    await waitFor(() => expect(api.dashboardCard).toHaveBeenCalledTimes(2))
    await act(async () => { finishOld(oldCard) })
    expect(qc.getQueryData(cardKey)).toBeUndefined()
    expect(screen.queryByTitle('Worker')).not.toBeInTheDocument()
    await act(async () => { finishNew(emptyCard) })
    await waitFor(() => expect(qc.getQueryData(cardKey)).toEqual(emptyCard))
  })

  it('keeps valid last-good content while an ordinary update is refetched', async () => {
    let finishRead!: (value: DynamicDashboardCard) => void
    vi.mocked(api.dashboardCard).mockReturnValue(new Promise(resolve => { finishRead = resolve }))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen() })
    qc.setQueryData(cardKey, oldCard)
    render(createElement(SessionStatusFrame, { slot: 'worker', title: 'Worker', active: true }), { wrapper })
    act(() => { ws.simulateMessage({ type: 'dashboard_card', data: { slot: 'worker' } }) })
    expect(qc.getQueryData(cardKey)).toEqual(oldCard)
    expect(screen.getByTitle('Worker')).toBeInTheDocument()
    await act(async () => { finishRead({ ...oldCard, status: 'failed', stale: true }) })
    await waitFor(() => expect(qc.getQueryData(cardKey)).toMatchObject({ status: 'failed' }))
    expect(screen.getByTitle('Worker')).toBeInTheDocument()
  })

  it('does not invalidate the library on first connect', () => {
    // First connect has no missed-frame gap: the page's own mount fetch is the
    // authoritative read, and invalidating here would double-fetch every load.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['artifacts']))
    expect(keys).not.toContain(JSON.stringify(['artifact-folders']))
  })
})
