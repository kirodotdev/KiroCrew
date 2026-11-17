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
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { useWebSocket } from '../hooks/useWebSocket'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from '../hooks/notificationEvent'
import {
  CHAT_COMPLETE_MIN_SECONDS_KEY,
  CHAT_COMPLETE_NOTIFY_KEY,
  loadChatCompleteMinSeconds,
  loadChatCompleteNotify,
  saveChatCompleteMinSeconds,
  saveChatCompleteNotify,
  setChatCompleteHostReader,
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

  it.each([
    // [this origin's storage, hub's relayed opt-in, opted in]
    [null, true, true],
    [null, false, false],
    ['0', true, false],
    ['1', false, true],
  ] as const)('reads own storage %s with the hub opt-in %s as %s (#20191)', (own, hostOptIn, expected) => {
    setChatCompleteHostReader(() => hostOptIn)
    onTestFinished(() => setChatCompleteHostReader(null))
    if (own !== null) localStorage.setItem(CHAT_COMPLETE_NOTIFY_KEY, own)
    expect(loadChatCompleteNotify()).toBe(expected)
  })

  it('stays silent while the OS permission is undecided or refused', () => {
    saveChatCompleteNotify(true)
    setHidden(true)
    MockNotification.permission = 'default'
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
    MockNotification.permission = 'denied'
    expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(false)
  })

  describe('minimum turn length', () => {
    beforeEach(() => {
      localStorage.removeItem(CHAT_COMPLETE_MIN_SECONDS_KEY)
      saveChatCompleteNotify(true)
      setHidden(true)
    })
    afterEach(() => { localStorage.removeItem(CHAT_COMPLETE_MIN_SECONDS_KEY) })
    const gate = (turnSeconds?: number | null, reconnecting = false) =>
      shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting, turnSeconds })

    it('skips only a turn known to be shorter than the chosen length', () => {
      expect(loadChatCompleteMinSeconds()).toBe(0)
      expect(gate(0)).toBe(true)
      saveChatCompleteMinSeconds(30)
      expect([gate(29), gate(30), gate(400), gate(), gate(null)]).toEqual([false, true, true, true, true])
      expect(gate(90, true)).toBe(false)
    })

    it.each(['abc', '-5', '1.5', '', '99999', '3601', ' 30'])('reads the stored value %j as every turn', raw => {
      localStorage.setItem(CHAT_COMPLETE_MIN_SECONDS_KEY, raw)
      expect(loadChatCompleteMinSeconds()).toBe(0)
    })

    it('stores only whole numbers in range', () => {
      const stored = [60, 3600, 2.5, -1, 3601].map(n => { saveChatCompleteMinSeconds(n); return loadChatCompleteMinSeconds() })
      expect(stored).toEqual([60, 3600, 0, 0, 0])
    })
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

  it('does not toast for an intermediate turn even when desktop alerts are enabled', () => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-a', 'Refactor the planner')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a', continuing: true } }) })
    expect(CONSTRUCTED).toEqual([])

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a', continuing: false } }) })
    expect(CONSTRUCTED).toHaveLength(1)
  })

  it.each([
    { continuing: false, needs_input: true },
    { continuing: true, needs_input: true },
    {},
  ])('keeps the question handoff toast without a second sound for %j', frame => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-question', 'Choose the approach')
    const kinds: (string | undefined)[] = []
    const onSound = (event: Event) => kinds.push((event as CustomEvent<McNotificationDetail>).detail.kind)
    window.addEventListener(MC_NOTIFICATION_EVENT, onSound)
    try {
      act(() => { ws.simulateMessage({ type: 'question_card', data: {
        slot: 'slot-question', card_id: 'question-toast',
        questions: [{ question: 'Which approach?', options: [{ label: 'Use A' }] }],
      } }) })
      expect(kinds).toEqual(['approval'])
      expect(CONSTRUCTED).toEqual([])

      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-question', ...frame } }) })
      expect(kinds).toEqual(['approval'])
      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].title).toBe('Choose the approach')
      // The body must say the agent is WAITING, not that a response is ready:
      // the user is being asked to answer, and "Response ready" over a pending
      // question card sends them looking for a finished reply.
      expect(CONSTRUCTED[0].options?.body).toBe('Waiting for your input')
      expect(CONSTRUCTED[0].options?.tag).toBe('kirocrew-chat-done:slot-question')
      expect(CONSTRUCTED[0].options?.silent).toBe(true)
    } finally {
      window.removeEventListener(MC_NOTIFICATION_EVENT, onSound)
      act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: {
        slot: 'slot-question', card_id: 'question-toast',
      } }) })
    }
  })

  it('names an untitled session "Untitled session", never its slot key (#23084)', () => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-b')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-b' } }) })

    expect(CONSTRUCTED.map(c => c.title)).toEqual(['Untitled session'])
  })

  it.each([
    // An explicit question the server certified without a card frame.
    { continuing: false, needs_input: true },
    // A manual step-through pause: work remains, but the next stage needs Go.
    { continuing: true, needs_input: true },
  ])('reads the frame hint as waiting for input, audible once, for %j', frame => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-input', 'Roll out the migration')
    const kinds: (string | undefined)[] = []
    const onSound = (event: Event) => kinds.push((event as CustomEvent<McNotificationDetail>).detail.kind)
    window.addEventListener(MC_NOTIFICATION_EVENT, onSound)
    try {
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-input', ...frame } }) })
      // No question card preceded this frame, so the completion chime is the
      // one sound — and the toast may carry the OS sound too (not silent).
      expect(kinds).toEqual(['turn'])
      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].title).toBe('Roll out the migration')
      expect(CONSTRUCTED[0].options?.body).toBe('Waiting for your input')
      expect(CONSTRUCTED[0].options?.silent).toBe(false)
    } finally {
      window.removeEventListener(MC_NOTIFICATION_EVENT, onSound)
    }
  })

  it.each([
    // A current frame that certifies the conversation simply finished.
    { continuing: false, needs_input: false },
    // An older relay frame with no activity hint at all.
    {},
  ])('keeps "Response ready" for an ordinary completion %j', frame => {
    saveChatCompleteNotify(true)
    const ws = mountOpened()
    seedSlot('slot-done', 'Refactor the planner')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-done', ...frame } }) })

    expect(CONSTRUCTED).toHaveLength(1)
    expect(CONSTRUCTED[0].options?.body).toBe('Response ready')
    expect(CONSTRUCTED[0].options?.silent).toBe(false)
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

  describe('minimum turn length', () => {
    afterEach(() => { localStorage.removeItem('mc-notify-min-turn-seconds') })
    // Pins the persisted key itself: a rename would strand users' saved choice.
    const openSlot = (minSeconds: number) => {
      saveChatCompleteNotify(true)
      localStorage.setItem('mc-notify-min-turn-seconds', String(minSeconds))
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')
      return (type: string, data: Record<string, unknown>) => act(() => { ws.simulateMessage({ type, data: { slot: 'slot-a', ...data } }) })
    }

    it.each([
      { frame: { run_seconds: 59 }, toasts: 0 },
      { frame: { run_seconds: 60 }, toasts: 1 },
      // An older gateway, or a run whose start it did not record: unknown is announced.
      { frame: {}, toasts: 1 },
      { frame: { run_seconds: 'long' }, toasts: 1 },
      { frame: { run_seconds: -1 }, toasts: 1 },
      { frame: { run_seconds: 2, needs_input: true }, toasts: 1 },
    ])('reads the run length the gateway reports: %j', ({ frame, toasts }) => {
      openSlot(60)('chat_done', frame)
      expect(CONSTRUCTED).toHaveLength(toasts)
    })

    it('toasts a short run that ended in an error', () => {
      const send = openSlot(60)
      send('chat_message', { role: 'error', content: 'x' })
      send('chat_done', { run_seconds: 2 })
      expect(CONSTRUCTED.map(c => c.options?.body)).toEqual(['This turn failed. Open the session to see why.'])
    })

    it('measures a run from the gateway in a window that reloaded mid-run', () => {
      const send = openSlot(60)
      // This fresh window's first row is a follow-up turn's opener (a sub-agent
      // result), seconds before the 20-minute run ends.
      send('chat_message', { role: 'subagent', content: 'x' })
      send('chat_done', { run_seconds: 1200 })
      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('toasts every finish while no minimum is chosen', () => {
      openSlot(0)('chat_done', { run_seconds: 0 })
      expect(CONSTRUCTED).toHaveLength(1)
    })
  })
})
