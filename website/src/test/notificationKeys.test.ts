/**
 * Keyed notes: a note carrying a `key` replaces the one its source already
 * published under that key, a quiet replacement reaches the bell without
 * announcing itself, and `notification_dismiss` withdraws a note from every view.
 * The backend half is pinned by `test/test_notification_keys.py`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { useNativeNotification } from '../hooks/useNativeNotification'
import reducer, { ackNotificationByTs, addNotification, dismissNotificationByTs, fetchNotifications } from '../store/notificationsSlice'
import { api } from '../api/client'
import type { Notification } from '../types'

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

class FakeNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  static titles: string[] = []
  constructor(title: string) {
    FakeNotification.titles.push(title)
  }
}

const agentNote = (over: Partial<Notification>): Notification => ({
  kind: 'agent', title: 't', body: '', ts: '1', source: 'system', channel: 'system.agent', ...over,
})

describe('addNotification with a key', () => {
  const empty = reducer(undefined, { type: '@@init' })

  it('replaces the note the same source published under that key', () => {
    let state = reducer(empty, addNotification(agentNote({ ts: '1', key: 'cr:1', title: 'Pushed' })))
    state = reducer(state, addNotification(agentNote({ ts: '2', key: 'cr:1', title: 'Merged' })))
    expect(state.items.map(n => n.title)).toEqual(['Merged'])
  })

  it('keeps notes with another key, another source or no key', () => {
    let state = reducer(empty, addNotification(agentNote({ ts: '1', key: 'cr:1' })))
    state = reducer(state, addNotification(agentNote({ ts: '2', key: 'cr:2' })))
    state = reducer(state, addNotification(agentNote({ ts: '3', key: 'cr:1', source: 'app:mochi' })))
    state = reducer(state, addNotification(agentNote({ ts: '4' })))
    state = reducer(state, addNotification(agentNote({ ts: '5' })))
    expect(state.items.map(n => n.ts)).toEqual(['1', '2', '3', '4', '5'])
  })
})

describe('useWebSocket keyed notes', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function mount() {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { store, ws }
  }

  it('plays no sound and raises no banner for a quiet replacement', () => {
    const { store, ws } = mount()
    const sounds: string[] = []
    const banners: string[] = []
    const onSound = (e: Event) => { sounds.push((e as CustomEvent).detail?.kind) }
    const onBanner = (e: Event) => { banners.push((e as CustomEvent).detail?.note?.title) }
    window.addEventListener('mc-notification', onSound)
    window.addEventListener('mc-live-notification', onBanner)
    try {
      act(() => {
        ws.simulateMessage({ type: 'notification', data: agentNote({ ts: '1', key: 'cr:1', title: 'Pushed' }) })
      })
      act(() => {
        ws.simulateMessage({
          type: 'notification',
          data: agentNote({ ts: '2', key: 'cr:1', title: 'CI green', quiet: true }),
        })
      })
      expect(sounds).toEqual(['agent'])
      expect(banners).toEqual(['Pushed'])
      expect(store.getState().notifications.items.map(n => n.title)).toEqual(['CI green'])
    } finally {
      window.removeEventListener('mc-notification', onSound)
      window.removeEventListener('mc-live-notification', onBanner)
    }
  })

  it('drops the note a notification_dismiss frame names', () => {
    const { store, ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'notification', data: agentNote({ ts: '1', key: 'poller:ci', title: 'Poller down' }) })
      ws.simulateMessage({ type: 'notification', data: agentNote({ ts: '2', title: 'Unrelated' }) })
    })
    act(() => { ws.simulateMessage({ type: 'notification_dismiss', data: { ts: '1' } }) })
    expect(store.getState().notifications.items.map(n => n.title)).toEqual(['Unrelated'])
  })
})

describe('useNativeNotification and quiet replacements', () => {
  beforeEach(() => {
    FakeNotification.titles = []
    vi.stubGlobal('Notification', FakeNotification)
    // The OS toast fires only while the user is away from the window.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  it('toasts the first note but not a quiet replacement of a note already read', () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    act(() => { store.dispatch(addNotification(agentNote({ ts: '1', key: 'cr:1', title: 'Pushed' }))) })
    act(() => { store.dispatch(ackNotificationByTs('1')) })
    act(() => {
      store.dispatch(addNotification(agentNote({ ts: '2', key: 'cr:1', title: 'CI green', quiet: true })))
    })
    act(() => { store.dispatch(addNotification(agentNote({ ts: '3', key: 'cr:1', title: 'Approved' }))) })

    expect(FakeNotification.titles).toEqual(['Pushed', 'Approved'])
  })

  it('toasts a louder replacement of a note still unread, though the unread count holds', () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    act(() => { store.dispatch(addNotification(agentNote({ ts: '1', key: 'poller:ci', title: 'Poller slow' }))) })
    act(() => {
      store.dispatch(addNotification(agentNote({
        ts: '2', key: 'poller:ci', title: 'Poller down', priority: 'critical',
      })))
    })

    expect(store.getState().notifications.items).toHaveLength(1)
    expect(FakeNotification.titles).toEqual(['Poller slow', 'Poller down'])
  })

  it('toasts the newest unseen note restored by a reconnect fetch', async () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    const unrelated = agentNote({ ts: '2', title: 'Unrelated' })
    act(() => { store.dispatch(addNotification(unrelated)) })

    const critical = agentNote({
      ts: '1', key: 'poller:ci', title: 'Poller down', priority: 'critical',
    })
    vi.mocked(api.notifications).mockResolvedValueOnce({
      notifications: [critical, unrelated], unread: 2,
    } as never)
    await act(async () => { await store.dispatch(fetchNotifications()) })

    expect(store.getState().notifications.items.map(n => n.title)).toEqual(['Poller down', 'Unrelated'])
    expect(FakeNotification.titles).toEqual(['Unrelated', 'Poller down'])
  })

  it('does not toast again when the newest note is read and an older one becomes the latest', () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })

    act(() => { store.dispatch(addNotification(agentNote({ ts: '1', title: 'First' }))) })
    act(() => { store.dispatch(addNotification(agentNote({ ts: '2', title: 'Second' }))) })
    act(() => { store.dispatch(ackNotificationByTs('2')) })

    expect(FakeNotification.titles).toEqual(['First', 'Second'])
  })
})

describe('fetchNotifications and keyed changes landing mid-flight', () => {
  beforeEach(() => { vi.mocked(api.notifications).mockReset() })

  const stale = agentNote({ ts: '1', key: 'poller:ci', title: 'Poller down' })

  /** Starts a fetch whose response is held until `release` is called. */
  function heldFetch(store: ReturnType<typeof createTestStore>) {
    let release!: (value: unknown) => void
    vi.mocked(api.notifications)
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve }) as never)
    const pending = store.dispatch(fetchNotifications())
    return { pending, release: (items: Notification[]) => release({ notifications: items, unread: items.length }) }
  }

  it('does not restore a note dismissed while the fetch was in flight', async () => {
    const store = createTestStore()
    store.dispatch(addNotification(stale))
    const fetch = heldFetch(store)
    store.dispatch(dismissNotificationByTs('1'))
    fetch.release([stale])
    await fetch.pending
    expect(store.getState().notifications.items).toEqual([])
  })

  it('keeps a replacement that arrived while the fetch was in flight', async () => {
    const store = createTestStore()
    store.dispatch(addNotification(stale))
    const fresh = agentNote({ ts: '2', key: 'poller:ci', title: 'Poller up' })
    const fetch = heldFetch(store)
    store.dispatch(addNotification(fresh))
    fetch.release([stale])
    await fetch.pending
    expect(store.getState().notifications.items.map(n => n.title)).toEqual(['Poller up'])
  })

  it('keeps a replacement that reached an empty view while its boot fetch was in flight', async () => {
    const store = createTestStore()
    const fresh = agentNote({ ts: '2', key: 'poller:ci', title: 'Poller up' })
    const older = agentNote({ ts: '0', title: 'Older history' })
    const fetch = heldFetch(store)
    store.dispatch(addNotification(fresh))
    fetch.release([older, stale])
    await fetch.pending
    expect(store.getState().notifications.items.map(n => n.title)).toEqual(['Older history', 'Poller up'])
  })

  it('applies the server membership when nothing keyed changed during the fetch', async () => {
    const store = createTestStore()
    store.dispatch(addNotification(stale))
    store.dispatch(dismissNotificationByTs('1'))
    const fetch = heldFetch(store)
    const later = agentNote({ ts: '3', key: 'poller:ci', title: 'Poller down again' })
    fetch.release([later])
    await fetch.pending
    expect(store.getState().notifications.items.map(n => n.title)).toEqual(['Poller down again'])
    expect(api.notifications).toHaveBeenCalledTimes(1)
  })
})
