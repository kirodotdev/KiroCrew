/**
 * `artifact_update` WebSocket frame -> react-query cache, through the real
 * dispatch adapter (`hooks/websocket/serverState.ts`, routed by `useWebSocket.ts`).
 *
 * This is the live-refresh half of the artifact companion chat: the backend
 * broadcasts from its artifact mutation funnel, and the client must turn that
 * into per-slug query invalidation so every open surface (detail page, popout,
 * the companion panel's left pane) re-renders the new version without a manual
 * refresh. The delete variant is different in kind — it must DROP the cache and
 * emit a window event so a detail page can navigate away rather than serve
 * content that no longer exists.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { store } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import { setArtifactEditing, __resetArtifactEditing } from '../utils/artifactEditGuard'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
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

describe('useWebSocket artifact_update frame', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals(); __resetArtifactEditing() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function send(data: object) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'artifact_update', data }) })
  }

  it('invalidates the per-slug queries on a content update', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slug: 'cr-queue', version: 7, deleted: false })
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifact', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifact-versions', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifact-events', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifact-comments', 'cr-queue']))
    // The library list ordering is driven by updated_at, so it refreshes too.
    expect(keys).toContain(JSON.stringify(['artifacts']))
  })

  it('refreshes the task-dashboard inventory the idle dock does not poll', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slug: 'release-map', version: 1, deleted: false })
    expect(spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))).toContain(JSON.stringify(['command-center', 'artifacts']))
  })

  it('re-reads exactly the grown slot\'s board and its ancestors\' boards, without cancelling a read in flight', async () => {
    const team = [
      { key: 'root', messages: 1, running: false }, { key: 'worker', messages: 1, running: false, created_by: 'root' },
      { key: 'other', messages: 1, running: false },
    ]
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    // Let the open's own slot refresh settle before seeding the team over it.
    await act(async () => { ws.simulateOpen(); await new Promise(resolve => setTimeout(resolve, 0)) })
    // The hook reads the app's module store, as its other handlers do.
    act(() => { store.dispatch(sseSlots(team)) })
    spy.mockClear()
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: { slot: 'dashboard:worker' } }) })
    const work = spy.mock.calls.filter(c => (c[0]?.queryKey as unknown[] | undefined)?.[2] === 'work')
    expect(work.map(c => c[0]?.queryKey)).toEqual([['command-center', 'worker', 'work'], ['command-center', 'root', 'work']])
    expect(work.every(c => c[0]?.exact === true && c[1]?.cancelRefetch === false)).toBe(true)
    spy.mockClear()
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: {} }) })
    expect(spy).not.toHaveBeenCalled()
  })

  it('hands the connect-time workflow read to the command center snapshot', async () => {
    const { api } = await import('../api/client')
    const out = { runs: [{ run_id: 'r9', status: 'finished', session_key: 'root' }] }
    vi.mocked(api.workflowRuns).mockResolvedValueOnce(out)
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen(); await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(qc.getQueryData(['command-center', 'workflows'])).toEqual(out)
  })

  it.each(['run_finished', 'run_failed', 'run_cancelled'])('re-reads the workflow snapshot on %s, not on progress', type => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()
    const workflows = () => spy.mock.calls.filter(c => JSON.stringify(c[0]?.queryKey) === JSON.stringify(['command-center', 'workflows'])).length
    act(() => { ws.simulateMessage({ type: 'workflow_run_event', data: { run_id: 'r1', type: 'step_started', data: {} } }) })
    expect(workflows()).toBe(0)
    act(() => { ws.simulateMessage({ type: 'workflow_run_event', data: { run_id: 'r1', type, data: {} } }) })
    expect(workflows()).toBe(1)
  })

  it('emits a window event on delete WITHOUT evicting the artifact query', () => {
    // Eviction would drop the detail page's data, re-render it into a loading/404
    // state, and unmount the editor — destroying an unsaved edit buffer and
    // defeating the deletion listener's dirty-page guard. The listener owns the
    // decision (navigate when clean, retain when dirty); the transport only
    // notifies.
    const removeSpy = vi.spyOn(qc, 'removeQueries')
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const onDeleted = vi.fn()
    window.addEventListener('kirocrew:artifact-deleted', onDeleted)
    try {
      send({ slug: 'cr-queue', version: 7, deleted: true })
    } finally {
      window.removeEventListener('kirocrew:artifact-deleted', onDeleted)
    }
    expect(removeSpy).not.toHaveBeenCalled()
    expect(onDeleted).toHaveBeenCalledTimes(1)
    expect((onDeleted.mock.calls[0][0] as CustomEvent).detail).toEqual({ slug: 'cr-queue' })
    // A deleted artifact must NOT be re-fetched — only the library list is.
    const keys = invalidateSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['artifact', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifacts']))
  })

  it('withholds the content refresh while the artifact has an unsaved buffer', () => {
    // Refetching would move the editor's baseline while the buffer keeps the older
    // text, so the next Save would overwrite the update that just arrived.
    setArtifactEditing('cr-queue', true)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slug: 'cr-queue', version: 8, deleted: false })
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['artifact', 'cr-queue']))
    expect(keys).not.toContain(JSON.stringify(['artifact-versions', 'cr-queue']))
    // Comments and events carry no edit buffer, so they still refresh.
    expect(keys).toContain(JSON.stringify(['artifact-comments', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifact-events', 'cr-queue']))
    expect(keys).toContain(JSON.stringify(['artifacts']))
  })

  it('only withholds for the edited slug, not others', () => {
    setArtifactEditing('cr-queue', true)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slug: 'other-doc', version: 2, deleted: false })
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifact', 'other-doc']))
  })

  it('ignores a frame with no slug', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const onDeleted = vi.fn()
    window.addEventListener('kirocrew:artifact-deleted', onDeleted)
    try {
      send({ version: 7 })
    } finally {
      window.removeEventListener('kirocrew:artifact-deleted', onDeleted)
    }
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['artifacts']))
    expect(onDeleted).not.toHaveBeenCalled()
  })
})
