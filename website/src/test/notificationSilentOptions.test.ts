/**
 * Native page-context toasts must be SILENT: WebAudio (useNotificationSound) is
 * the single source of notification sound. If a `new Notification(...)` omits
 * `silent: true`, the OS plays its own system chime on top of the WebAudio
 * tone — a double sound. These tests pin `silent: true` on both call sites:
 *
 *  - useNativeNotification (the feed toast), and
 *  - the approval toast in useWebSocket (hidden tab + permission granted).
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
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

/** Records the options every `new Notification()` was constructed with. */
const NOTIF_OPTIONS: (NotificationOptions | undefined)[] = []
class RecordingNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  constructor(_title: string, options?: NotificationOptions) { NOTIF_OPTIONS.push(options) }
}

describe('native notification toasts are silent (WebAudio is the only sound)', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    NOTIF_OPTIONS.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Notification', RecordingNotification)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  it('useNativeNotification constructs the feed toast with silent: true', () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    act(() => {
      store.dispatch(addNotification({
        kind: 'cron', title: 'Job done', body: 'Nightly sync', ts: '1.0', job_id: 'job-1',
      } as AppNotification))
    })

    expect(NOTIF_OPTIONS).toHaveLength(1)
    expect(NOTIF_OPTIONS[0]?.silent).toBe(true)
  })

  it('the approval toast in useWebSocket sets silent: true', () => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-silent-1', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 1.0 },
      })
    })

    expect(NOTIF_OPTIONS).toHaveLength(1)
    expect(NOTIF_OPTIONS[0]?.silent).toBe(true)
  })
})
