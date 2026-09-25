/**
 * Verifies the two member event-log frames feed the projection store:
 *   - `member_projection` applies one (slug, key, value, seq) via the store's
 *     higher-seq-wins rule, and a malformed frame (missing slug/key/seq) is a
 *     no-op.
 *   - `members_subscribed` truncates held rows whose seq ran ahead of the
 *     server's authoritative lastSeq (a torn tail after a restart).
 *
 * The fields ride at the TOP LEVEL of the frame (not under `data`), per
 * the two frame kinds below. The wire shape is `{ type, data }` and every field
 * rides under `data`, which is what this file asserts against.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { memberProjectionStore } from '../state/memberProjectionStore'

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

describe('useWebSocket member projection frames', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    memberProjectionStore.clear()
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function open() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('applies a member_projection frame to the store', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'x' }, seq: 3 } })
    })
    expect(memberProjectionStore.get('oncall', 'roster')).toEqual({ model: 'x' })
  })

  it('drops a member_projection frame missing slug/key/seq', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { key: 'roster', value: { model: 'x' }, seq: 3 } })
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', value: { model: 'x' }, seq: 3 } })
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'x' } } })
    })
    expect(memberProjectionStore.has('oncall')).toBe(false)
  })

  it('members_subscribed truncates a torn tail past the server lastSeq', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'ahead' }, seq: 9 } })
      ws.simulateMessage({ type: 'members_subscribed', data: { lastSeqs: { oncall: 5 } } })
    })
    expect(memberProjectionStore.get('oncall', 'roster')).toBeUndefined()
  })

  it('a truncation RESETS both the roster and the per-member projections', () => {
    // The truncation drops EVERY key a slug holds, while each read owns only
    // some of them: a roster row carries `roster`, and the open member's
    // activity, wake and driving views come from its own per-member read. The
    // two query keys are SIBLINGS under ['kirocrew-agents'], so repairing one
    // does not match the other, and the drawer would stay blank until something
    // else happened to refetch it.
    //
    // Both are RESET rather than invalidated, because invalidating a query with no
    // enabled observer only marks it stale, leaving its pre-rollback block in
    // cache; the next mount seeds that block at the rolled-back sequence, and
    // higher-seq-wins then rejects the authoritative lower-seq baseline, so the
    // stale values repaint with no way back. Neither read is exempt: the
    // per-member one is disabled while no member is open, and the roster's only
    // observer away from the members page is the crewmates gate, which holds it
    // `enabled: eligible`.
    const invalidated: unknown[] = []
    const reset: unknown[] = []
    const iSpy = vi.spyOn(qc, 'invalidateQueries').mockImplementation((arg) => {
      invalidated.push((arg as { queryKey?: unknown } | undefined)?.queryKey)
      return Promise.resolve()
    })
    const rSpy = vi.spyOn(qc, 'resetQueries').mockImplementation((arg) => {
      reset.push((arg as { queryKey?: unknown } | undefined)?.queryKey)
      return Promise.resolve()
    })
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'wake', value: { patrol: 'stopped' }, seq: 9 } })
      ws.simulateMessage({ type: 'members_subscribed', data: { lastSeqs: { oncall: 5 } } })
    })
    const projections = JSON.stringify(['kirocrew-agents', 'member-projections'])
    const roster = JSON.stringify(['kirocrew-agents', 'members-roster'])
    expect(reset.map((k) => JSON.stringify(k))).toContain(projections)
    expect(reset.map((k) => JSON.stringify(k))).toContain(roster)
    // Marking either query stale is exactly what leaves the rolled-back block
    // reachable, so it must not be the call used for either of them.
    expect(invalidated.map((k) => JSON.stringify(k))).not.toContain(projections)
    expect(invalidated.map((k) => JSON.stringify(k))).not.toContain(roster)
    iSpy.mockRestore()
    rSpy.mockRestore()
  })
})
