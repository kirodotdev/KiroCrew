/** `chat.side_closed`: a side conversation closed in another tab (or by an app) is
 *  cleared in this one too. Without it this tab keeps rows the model has dropped,
 *  and its next question opens a fresh conversation under them.
 *
 *  The singleton store is the Provider store here, as in production: the handler
 *  reads the queue it is about to drop from the singleton. */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { TAB_ID } from '../api/tabId'
import { store } from '../store'
import { sideClose, sideOptimisticAppend, sseSideQueue, sseSideResult } from '../store/chatSlice'
import { clearSideChatDrafts, readSideChatDraft, writeSideChatDraft } from '../chat-core/composer/sideChatDrafts'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    sideState: vi.fn().mockResolvedValue({ open: false }),
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

const SLOT = 'side-closed-slot'

describe('useWebSocket chat.side_closed', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    clearSideChatDrafts()
    vi.stubGlobal('WebSocket', MockWebSocket)
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'remember pineapple', message_id: 'u1' }))
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'noted', final: true, message_id: 'a1' }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    store.dispatch(sideClose(SLOT))
    clearSideChatDrafts()
  })

  function connect() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }: { children: React.ReactNode }) =>
        createElement(Provider, { store }, createElement(QueryClientProvider, { client: qc }, children)),
    })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('clears a conversation another tab closed', () => {
    const ws = connect()
    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } }) })

    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
    expect(store.getState().chat.slotSideClosed[SLOT]).toBe(true)
  })

  it('clears it when no tab is named, as for an app that closed it', () => {
    const ws = connect()
    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT } }) })

    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('ignores its own close echo when nothing reappeared after the optimistic clear', () => {
    const ws = connect()
    act(() => { store.dispatch(sideClose(SLOT)) })
    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: TAB_ID } }) })

    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
    expect(store.getState().chat.slotSideClosed[SLOT]).toBe(true)
    expect(readSideChatDraft(SLOT)).toBe('')
  })

  it('applies its own close echo when a gateway user frame reappeared after the optimistic clear', () => {
    const ws = connect()
    act(() => {
      store.dispatch(sideClose(SLOT))
      ws.simulateMessage({
        type: 'chat.side_result',
        data: { slot: SLOT, run_id: 'r2', role: 'user', content: 'question in the closing conversation', message_id: 'u2' },
      })
    })
    expect(store.getState().chat.slotSide[SLOT]?.pending).toBe(true)

    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: TAB_ID } }) })

    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
    expect(store.getState().chat.slotSideClosed[SLOT]).toBe(true)
  })

  it('keeps an optimistic question when its own close echo arrives before the gateway processes the send', () => {
    const ws = connect()
    act(() => {
      store.dispatch(sideClose(SLOT))
      store.dispatch(sideOptimisticAppend({
        slot: SLOT,
        message: { role: 'user', content: 'question for the replacement conversation', ts: '2026-10-24T00:00:00.000Z' },
      }))
    })

    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: TAB_ID } }) })

    expect(store.getState().chat.slotSide[SLOT]?.messages).toMatchObject([
      { role: 'user', content: 'question for the replacement conversation', optimistic: true },
    ])
    expect(readSideChatDraft(SLOT)).toBe('')
  })

  it('hands back a raw queued question when its own close echo clears gateway state', () => {
    const ws = connect()
    act(() => {
      store.dispatch(sideClose(SLOT))
      ws.simulateMessage({
        type: 'chat.side_result',
        data: { slot: SLOT, run_id: 'r2', role: 'user', content: 'active question', message_id: 'u2' },
      })
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'queued question', raw: true }))
    })

    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: TAB_ID } }) })

    expect(readSideChatDraft(SLOT)).toBe('queued question')
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
    expect(store.getState().chat.slotSideClosed[SLOT]).toBe(true)
  })

  it('hands a question this tab queued back to its composer, and never another tab\'s', () => {
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'my own question', raw: true }))
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'a question someone else queued' }))
    writeSideChatDraft(SLOT, 'half typed')
    const ws = connect()
    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } }) })

    const draft = readSideChatDraft(SLOT)
    expect(draft).toBe('half typed\n\nmy own question')
    expect(draft).not.toContain('someone else queued')
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('hands back a cancelled question still waiting for its panel, before the queued ones', () => {
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'cancelled question', raw: true }))
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'still queued', raw: true }))
    // Another tab cancels a card this tab typed: the text waits in the store for this
    // slot's panel, which is not on screen.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1', content: 'cancelled question', suppressRelease: true }))
    expect(store.getState().chat.slotSide[SLOT]?.releasedText).toBe('cancelled question')
    const ws = connect()
    act(() => { ws.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } }) })

    expect(readSideChatDraft(SLOT)).toBe('cancelled question\n\nstill queued')
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })
})
