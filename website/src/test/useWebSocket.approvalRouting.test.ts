/**
 * An approval frame with no `slot` has no owning conversation.
 *
 * Unowned approvals must reach the global surface (the notification feed) ONLY.
 * Routing one to `chat.activeSlot` would plant a permission card in whatever
 * chat the user happens to be viewing, where the card's slot-scoped Trust
 * control resolves against that innocent slot and 404s as soon as the short
 * background window elapses ("That approval expired").
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import type { RootState } from '../store'

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

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

/** A store whose viewed slot already holds a turn — the slot a `chat.activeSlot`
 *  fallback would hijack. */
function storeViewingSlot1() {
  return createTestStore({
    chat: {
      activeSlot: 'slot-1',
      messages: [{ role: 'user', content: 'unrelated work' }],
      toolLog: [],
      subagents: {},
      slotActivity: {},
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
  })
}

describe('useWebSocket unowned approval routing', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // useWebSocket reads `chat.activeSlot` off the SINGLETON store (not the
    // Provider store), so routing to the viewed slot is only reachable when the
    // singleton has a slot selected. Priming it is what lets these tests catch
    // a regression rather than passing either way.
    globalStore.dispatch(setActiveSlot('slot-1'))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount(store: ReturnType<typeof storeViewingSlot1>) {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  const cronApproval = (extra: Record<string, unknown> = {}) => ({
    type: 'approval',
    data: {
      id: 'ap-cron-1',
      source: 'cron',
      tool: 'Running: cd /work/repo && git status',
      tool_input: '{"command":"git status"}',
      ts: 1.0,
      ...extra,
    },
  })

  it('does not plant an unowned approval in the viewed conversation', () => {
    const store = storeViewingSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(cronApproval()) })

    const msgs = store.getState().chat.messages
    expect(msgs.some(m => m.role === 'permission')).toBe(false)
    // ...and nothing was written to the slot's activity log either.
    expect(store.getState().chat.toolLog.some(e => e.approval_id === 'ap-cron-1')).toBe(false)
  })

  it('still delivers the unowned approval to the global notification feed', () => {
    const store = storeViewingSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(cronApproval()) })

    const notifs = store.getState().notifications.items
    const match = notifs.find(n => n.approval_id === 'ap-cron-1')
    expect(match).toBeDefined()
    expect(match!.kind).toBe('approval')
    // Provenance survives: the feed says which job asked.
    expect(match!.body).toContain('cron')
  })

  it('still renders inline when the frame names its owning slot', () => {
    const store = storeViewingSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(cronApproval({ slot: 'slot-1', id: 'ap-owned-1' })) })

    const msgs = store.getState().chat.messages
    const card = msgs.find(m => m.role === 'permission')
    expect(card).toBeDefined()
    expect(card!.meta?.approval_id).toBe('ap-owned-1')
    expect(card!.meta?.source).toBe('cron')
  })
})
