/**
 * `followup_card` WebSocket frame -> store, through the real dispatch adapter.
 *
 * The reducers and the card component have their own suites, but the adapter in
 * `useWebSocket.ts` — the code that decides which frames are well-formed enough
 * to become a card — was only ever exercised indirectly (GPT review, PR #461
 * round 10). Everything the server sends is already sanitized and redacted; this
 * pins the client's own shape filtering so a malformed or partial frame cannot
 * put junk in the store.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import chatReducer from '../store/chatSlice'

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

const ITEM = {
  title: 'Add rate limits',
  description: 'The upload endpoint is unbounded.',
  prompt: 'Add a token-bucket limiter to POST /api/upload.',
}

describe('useWebSocket followup_card frame', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-1' } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function send(data: object) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'followup_card', data }) })
  }

  it('stores a well-formed card against its own slot', () => {
    send({ slot: 'chat-1', items: [ITEM], ts: 42 })
    const card = testStore.getState().chat.followups['chat-1']
    expect(card.ts).toBe(42)
    expect(card.items).toEqual([{ ...ITEM, description: ITEM.description }])
  })

  it('keeps an optional branch and defaults a missing description', () => {
    send({ slot: 'chat-1', items: [{ title: 'T', prompt: 'P', branch: 'feat/x' }] })
    const item = testStore.getState().chat.followups['chat-1'].items[0]
    expect(item.branch).toBe('feat/x')
    expect(item.description).toBe('')
  })

  it('drops items missing the fields the card renders', () => {
    send({ slot: 'chat-1', items: [{ description: 'no title or prompt' }, ITEM] })
    expect(testStore.getState().chat.followups['chat-1'].items).toHaveLength(1)
  })

  it('ignores a frame with no slot, no items, or a non-array items field', () => {
    for (const data of [
      { items: [ITEM] },
      { slot: 'chat-1', items: [] },
      { slot: 'chat-1', items: 'nope' },
      { slot: 'chat-1' },
    ]) {
      WS_INSTANCES.length = 0
      testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-1' } })
      send(data)
      expect(testStore.getState().chat.followups['chat-1']).toBeUndefined()
    }
  })

  it('routes a card to a background slot without touching the active one', () => {
    send({ slot: 'chat-other', items: [ITEM] })
    expect(testStore.getState().chat.followups['chat-other']).toBeDefined()
    expect(testStore.getState().chat.followups['chat-1']).toBeUndefined()
  })
})
