/**
 * Page-context `new Notification()` is desktop-only: Android Chrome throws
 * "Illegal constructor" even with permission granted (the platform requires
 * ServiceWorkerRegistration.showNotification, and this app registers no
 * service worker). The native toast is best-effort — a throwing constructor
 * must never take down the code around it:
 *
 * - on the WebSocket approval path, an uncaught throw kills the rest of the
 *   message handler, so the approval never reaches the notification feed;
 * - in useNativeNotification, it kills the effect that watches the feed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { addNotification } from '../store/notificationsSlice'
import type { Notification as AppNotification } from '../types'

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

/** What Android Chrome does: permission is granted, construction throws. */
class ThrowingNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  constructor() {
    throw new TypeError('Illegal constructor')
  }
}

describe('page-context Notification construction is best-effort', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Notification', ThrowingNotification)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // Restore the prototype getter shadowed by the per-test own property.
    delete (document as { hidden?: boolean }).hidden
  })

  function mountWs(store: ReturnType<typeof createTestStore>) {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  it('an approval frame still reaches the notification feed when the toast constructor throws', () => {
    // The hidden-tab + permission-granted branch is the only one that
    // constructs a Notification on the approval path.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    const store = createTestStore()
    const { ws } = mountWs(store)

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-android-1', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 1.0 },
      })
    })

    const match = store.getState().notifications.items.find(n => n.approval_id === 'ap-android-1')
    expect(match).toBeDefined()
    expect(match!.kind).toBe('approval')
  })

  it('useNativeNotification survives a throwing toast constructor', () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    // A new unacked notification triggers the toast effect; the throw must
    // stay inside it.
    expect(() => {
      act(() => {
        store.dispatch(addNotification({
          kind: 'approval',
          title: 'Tool approval',
          body: 'Bash',
          ts: '1.0',
          approval_id: 'ap-android-2',
        } as AppNotification))
      })
    }).not.toThrow()
  })
})
