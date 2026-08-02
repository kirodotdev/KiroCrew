/**
 * `artifact_update` WebSocket frame -> react-query cache, through the real
 * dispatch adapter in `useWebSocket.ts`.
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
import { useWebSocket } from '../hooks/useWebSocket'
import { setArtifactEditing, __resetArtifactEditing } from '../utils/artifactEditGuard'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
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

  it('invalidates session-scoped artifact queries so the Artifacts tab refreshes', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slug: 'widget-abc', version: 1, deleted: false })
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['session-artifact-records']))
    expect(keys).toContain(JSON.stringify(['session-artifacts']))
  })
})
