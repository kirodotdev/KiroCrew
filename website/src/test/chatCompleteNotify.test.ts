/**
 * Opt-in native toast when a background chat finishes.
 *
 * Two halves, because the failure modes are unrelated:
 *
 *  - the PREFERENCE + predicate (`chatCompleteNotify.ts`). Default-OFF and the
 *    away gate are the whole product decision here, and both fail silently in
 *    the wrong direction: a broken default spams a toast per concurrent
 *    completion, a broken away gate toasts over the window the user is looking
 *    at. The permission request on enable is pinned too — without it the
 *    feature is a no-op for any user who has never received a feed
 *    notification, which is the only other place that asks.
 *  - the WIRING in the `chat_done` branch of `useWebSocket`. The toast has to
 *    name WHICH session finished; a title read from the wrong place, or a `tag`
 *    that is not per-slot, degrades to exactly the unhelpful "something
 *    finished" toast the feature exists to replace.
 *
 * Deliberately the SINGLETON store, like `useWebSocket.slotsFrameDedupe`:
 * useWebSocket dispatches via useAppDispatch() but reads slots off the imported
 * singleton, so a separate Provider store would leave the title lookup reading
 * an empty list and the assertions would pass vacuously.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { useWebSocket } from '../hooks/useWebSocket'
import {
  CHAT_COMPLETE_NOTIFY_KEY,
  loadChatCompleteNotify,
  saveChatCompleteNotify,
  shouldNotifyOnChatComplete,
} from '../hooks/chatCompleteNotify'

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

interface Constructed { title: string; options?: NotificationOptions }

const CONSTRUCTED: Constructed[] = []

/** Records every toast instead of showing one, and lets a test choose the
 *  permission state the browser reports. */
class MockNotification {
  static permission: NotificationPermission = 'granted'
  static requestPermission = vi.fn(() => Promise.resolve('granted' as NotificationPermission))
  constructor(title: string, options?: NotificationOptions) {
    CONSTRUCTED.push({ title, options })
  }
}

/** Pretend the window is minimized (or on another virtual desktop). */
function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

describe('chatCompleteNotify preference and predicate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem(CHAT_COMPLETE_NOTIFY_KEY)
    MockNotification.permission = 'granted'
    vi.stubGlobal('Notification', MockNotification)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    // Drop the per-test own property so the prototype getter is visible again.
    delete (document as { hidden?: boolean }).hidden
    localStorage.removeItem(CHAT_COMPLETE_NOTIFY_KEY)
  })

  it('is off until the user opts in', () => {
    expect(loadChatCompleteNotify()).toBe(false)
    saveChatCompleteNotify(true)
    expect(loadChatCompleteNotify()).toBe(true)
    saveChatCompleteNotify(false)
    expect(loadChatCompleteNotify()).toBe(false)
  })

  it('asks for the OS permission when enabled from the un-asked state', () => {
    MockNotification.permission = 'default'
    saveChatCompleteNotify(true)
    expect(MockNotification.requestPermission).toHaveBeenCalledTimes(1)
  })

  it('does not re-ask when the permission was already decided, or when disabling', () => {
    MockNotification.permission = 'granted'
    saveChatCompleteNotify(true)
    MockNotification.permission = 'denied'
    saveChatCompleteNotify(true)
    MockNotification.permission = 'default'
    saveChatCompleteNotify(false)
    expect(MockNotification.requestPermission).not.toHaveBeenCalled()
  })

  it('stays silent while the user has not opted in, however away they are', () => {
    setHidden(true)
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
  })

  it('fires for a hidden window and for a visible but unfocused one', () => {
    saveChatCompleteNotify(true)
    setHidden(true)
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(true)

    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(true)
  })

  it('stays silent while the user is watching the window', () => {
    saveChatCompleteNotify(true)
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
  })

  it('suppresses slot-less events and reconnect catch-up replay', () => {
    saveChatCompleteNotify(true)
    setHidden(true)
    expect(shouldNotifyOnChatComplete({ slot: undefined, reconnecting: false })).toBe(false)
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: true })).toBe(false)
  })

  it('stays silent while the OS permission is undecided or refused', () => {
    saveChatCompleteNotify(true)
    setHidden(true)
    MockNotification.permission = 'default'
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
    MockNotification.permission = 'denied'
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
  })
})

describe('useWebSocket chat_done native toast', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    CONSTRUCTED.length = 0
    WS_INSTANCES.length = 0
    localStorage.removeItem(CHAT_COMPLETE_NOTIFY_KEY)
    globalStore.dispatch(sseSlots([]))
    MockNotification.permission = 'granted'
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Notification', MockNotification)
    // Away: the only state in which the toast is allowed to fire.
    setHidden(true)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
    localStorage.removeItem(CHAT_COMPLETE_NOTIFY_KEY)
  })

  function mountOpened() {
    function wrapper({ children }: { children: React.ReactNode }) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: qc }, children),
      )
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  function seedSlot(key: string, title?: string) {
    act(() => {
      globalStore.dispatch(sseSlots([
        { key, ...(title === undefined ? {} : { title }), messages: 1, running: false },
      ]))
    })
  }

  it('names the finished session and tags the toast per slot', () => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-a', 'Refactor the planner')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

    expect(CONSTRUCTED).toHaveLength(1)
    expect(CONSTRUCTED[0].title).toBe('Refactor the planner')
    expect(CONSTRUCTED[0].options?.body).toBe('Response ready')
    // Per-slot, so two sessions finishing while away produce two toasts rather
    // than one overwriting the other.
    expect(CONSTRUCTED[0].options?.tag).toBe('kirocrew-chat-done:slot-a')
  })

  it('falls back to the slot key when the session has no title', () => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-b')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-b' } }) })

    expect(CONSTRUCTED.map(c => c.title)).toEqual(['slot-b'])
  })

  it('does not toast while the user has not opted in', () => {
    const ws = mountOpened()
    seedSlot('slot-a', 'Refactor the planner')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

    expect(CONSTRUCTED).toEqual([])
  })

  it('does not toast while the window is on screen and focused', () => {
    saveChatCompleteNotify(true)
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const ws = mountOpened()
    seedSlot('slot-a', 'Refactor the planner')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

    expect(CONSTRUCTED).toEqual([])
    vi.restoreAllMocks()
  })
})
