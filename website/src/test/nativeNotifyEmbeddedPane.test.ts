/**
 * In an embedded instance pane (the dashboard inside InstancesViewport's
 * iframe) `Notification.permission` is 'denied' -- the desktop grants
 * `notifications` to the main frame only and a browser denies it to a
 * cross-origin iframe. Every page-context toast site must therefore relay the
 * note to the parent frame (`mc-native-notify`) instead of constructing a
 * Notification that can never show. These tests drive the two real sites
 * through their hooks with the pane flag on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { store as globalStore } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { CHAT_COMPLETE_NOTIFY_KEY } from '../hooks/chatCompleteNotify'
import { readNotificationPermission } from '../hooks/useNotificationPermission'
import { addNotification } from '../store/notificationsSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { Notification as AppNotification } from '../types'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
import { isEmbeddedPane } from '../lib/embedded'

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

const CONSTRUCTED: string[] = []

/** What a subframe sees: permission denied; constructing would be inert. */
class DeniedNotification {
  static permission = 'denied'
  static requestPermission = vi.fn()
  constructor(title: string) { CONSTRUCTED.push(title) }
}

describe('embedded instance pane relays native notifications to the parent', () => {
  let queryClient: QueryClient
  let postMessage: ReturnType<typeof vi.fn>
  let originalParent: Window

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    WS_INSTANCES.length = 0
    CONSTRUCTED.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Notification', DeniedNotification)
    postMessage = vi.fn()
    originalParent = window.parent
    Object.defineProperty(window, 'parent', { configurable: true, value: { postMessage } })
    // The Instances hub that embedded this pane: a loopback http origin.
    Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://127.0.0.1:8787/' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Object.defineProperty(window, 'parent', { configurable: true, value: originalParent })
    delete (document as { hidden?: boolean }).hidden
    localStorage.removeItem(CHAT_COMPLETE_NOTIFY_KEY)
  })

  function mountWs(store: ReturnType<typeof createTestStore> | typeof globalStore) {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  function relayed() {
    return postMessage.mock.calls
      .map(c => c[0] as { type?: string })
      .filter(m => m && m.type === 'mc-native-notify')
  }

  it('approval frame: no direct toast site remains; the feed still records the approval', () => {
    // Approvals reach the OS through the bell note path (useNativeNotification)
    // -- one toast per event -- so the socket handler itself relays nothing.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    const store = createTestStore()
    const { ws } = mountWs(store)

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-pane-1', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 1.0 },
      })
    })

    expect(CONSTRUCTED).toHaveLength(0)
    expect(relayed()).toHaveLength(0)
    expect(store.getState().notifications.items.find(n => n.approval_id === 'ap-pane-1')).toBeDefined()
  })

  it('chat finished while away (opt-in on): relays with the slot title and a per-slot tag', () => {
    // The real gate: opted in, away, and -- in a pane -- permitted by the parent
    // rather than by the pane's own (denied) verdict. useWebSocket reads slot
    // titles off the imported store, so seed that one.
    localStorage.setItem(CHAT_COMPLETE_NOTIFY_KEY, '1')
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    act(() => {
      globalStore.dispatch(sseSlots([{ key: 'slot-bg', title: 'Deploy notes', messages: 1, running: false }]))
    })
    const { ws } = mountWs(globalStore)

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-bg' } })
    })

    expect(CONSTRUCTED).toHaveLength(0)
    const notes = relayed()
    expect(notes).toHaveLength(1)
    expect(notes[0]).toEqual({
      type: 'mc-native-notify', v: 1, title: 'Deploy notes', body: 'Response ready', tag: 'kirocrew-chat-done:slot-bg', silent: false,
    })
  })

  it('chat finished with the opt-in off: keeps the site guard -- nothing relayed', () => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    act(() => {
      globalStore.dispatch(sseSlots([{ key: 'slot-bg2', title: 'Quiet', messages: 1, running: false }]))
    })
    const { ws } = mountWs(globalStore)

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-bg2' } })
    })

    expect(relayed()).toHaveLength(0)
  })

  it('bell note (window focused): relays nothing -- the away gate is the pane\'s own', () => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    const hadFocus = document.hasFocus
    document.hasFocus = () => true
    try {
      const store = createTestStore()
      function wrapper({ children }: { children: React.ReactNode }) {
        return createElement(Provider, { store }, children)
      }
      renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })
      act(() => {
        store.dispatch(addNotification({ kind: 'approval', title: 'T', body: 'B', ts: '1.0', approval_id: 'ap-pane-4' } as AppNotification))
      })
      expect(relayed()).toHaveLength(0)
      expect(CONSTRUCTED).toHaveLength(0)
    } finally {
      document.hasFocus = hadFocus
    }
  })

  it('permission-facing UI defers to the hub: the pane reads unsupported, not its own denied', () => {
    expect(readNotificationPermission()).toBe('unsupported')
    // An embedded frame with no relay target keeps reporting its real verdict.
    Object.defineProperty(document, 'referrer', { configurable: true, value: 'https://host.example/' })
    expect(readNotificationPermission()).toBe('denied')
  })

  it('bell note (window away): relays the note, constructs nothing, never prompts', () => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    act(() => {
      store.dispatch(addNotification({
        kind: 'approval',
        title: 'Tool approval',
        body: 'Bash',
        ts: '1.0',
        approval_id: 'ap-pane-3',
      } as AppNotification))
    })

    expect(CONSTRUCTED).toHaveLength(0)
    expect(DeniedNotification.requestPermission).not.toHaveBeenCalled()
    expect(relayed()).toEqual([
      { type: 'mc-native-notify', v: 1, title: 'Tool approval', body: 'Bash', tag: 'ap-pane-3', silent: true },
    ])
  })
})
