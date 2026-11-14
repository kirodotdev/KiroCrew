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
import { _resetTurnClockForTest } from '../hooks/turnClock'

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

    it('defaults to every turn, so a zero-second turn still toasts', () => {
      expect(loadChatCompleteMinSeconds()).toBe(0)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 0 })).toBe(true)
    })

    it('skips a turn shorter than the chosen length and keeps one that reaches it', () => {
      saveChatCompleteMinSeconds(30)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 29 })).toBe(false)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 30 })).toBe(true)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 400 })).toBe(true)
    })

    it('toasts when the length is unknown, whatever the chosen minimum', () => {
      saveChatCompleteMinSeconds(30)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false })).toBe(true)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: null })).toBe(true)
    })

    it.each(['abc', '-5', '1.5', '', '99999', '3601', ' 30'])('reads the stored value %j as every turn', raw => {
      localStorage.setItem(CHAT_COMPLETE_MIN_SECONDS_KEY, raw)
      expect(loadChatCompleteMinSeconds()).toBe(0)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 1 })).toBe(true)
    })

    it('stores only whole numbers in range', () => {
      saveChatCompleteMinSeconds(60)
      expect(loadChatCompleteMinSeconds()).toBe(60)
      saveChatCompleteMinSeconds(3600)
      expect(loadChatCompleteMinSeconds()).toBe(3600)
      saveChatCompleteMinSeconds(2.5)
      expect(loadChatCompleteMinSeconds()).toBe(0)
      saveChatCompleteMinSeconds(-1)
      expect(loadChatCompleteMinSeconds()).toBe(0)
      saveChatCompleteMinSeconds(3601)
      expect(loadChatCompleteMinSeconds()).toBe(0)
    })

    it('leaves every other gate in force for a long turn', () => {
      saveChatCompleteMinSeconds(30)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: true, turnSeconds: 90 })).toBe(false)
      expect(shouldNotifyOnChatComplete({ slot: undefined, reconnecting: false, turnSeconds: 90 })).toBe(false)
      saveChatCompleteNotify(false)
      expect(shouldNotifyOnChatComplete({ slot: 'slot-a', reconnecting: false, turnSeconds: 90 })).toBe(false)
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
    const t0 = Date.UTC(2026, 10, 12, 12, 0, 0)

    beforeEach(() => {
      localStorage.removeItem('mc-notify-min-turn-seconds')
      _resetTurnClockForTest()
      // Both clocks are faked: the turn clock reads the wall clock and the monotonic one.
      vi.useFakeTimers({ toFake: ['Date', 'performance'] })
      vi.setSystemTime(t0)
    })
    afterEach(() => {
      vi.useRealTimers()
      localStorage.removeItem('mc-notify-min-turn-seconds')
      _resetTurnClockForTest()
    })

    // Pins the persisted key itself: a rename would strand users' saved choice.
    const pinMinSeconds = (n: number) => localStorage.setItem('mc-notify-min-turn-seconds', String(n))
    const row = (slot: string, role: string) => ({ type: 'chat_message', data: { slot, role, content: 'x' } })
    // Real time passing moves both clocks; `vi.setSystemTime` alone moves only the wall clock.
    const advance = (ms: number) => { vi.advanceTimersByTime(ms) }

    it('skips a finish that came sooner than the chosen length, and toasts the next long one', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(10_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toEqual([])

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(45_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].options?.body).toBe('Response ready')
    })

    it('starts the clock at the first row only, not at later ones', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(25_000)
      act(() => { ws.simulateMessage(row('slot-a', 'assistant')) })
      advance(10_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('keeps each session on its own clock', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'First')
      seedSlot('slot-b', 'Second')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(40_000)
      act(() => { ws.simulateMessage(row('slot-b', 'user')) })
      advance(5_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-b' } }) })
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED.map(c => c.options?.tag)).toEqual(['kirocrew-chat-done:slot-a'])
    })

    it('still toasts a quick turn that stopped to ask a question', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Choose the approach')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a', needs_input: true } }) })

      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].options?.body).toBe('Waiting for your input')
    })

    it('still toasts a quick turn that failed', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      act(() => { ws.simulateMessage(row('slot-a', 'error')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].options?.body).toBe('This turn failed. Open the session to see why.')
    })

    it('toasts a turn whose start this window never saw', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('toasts a long turn the window joined late, from its reply or tool rows alone', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      // The window opened 20 minutes into the turn: it sees only rows that continue it.
      act(() => { ws.simulateMessage(row('slot-a', 'tool_result')) })
      act(() => { ws.simulateMessage(row('slot-a', 'assistant')) })
      advance(10_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('does not start the clock on a passive note, so idle time is not counted', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => {
        ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-a', role: 'inject', cls: 'reconcile-note', content: 'note' } })
      })
      advance(120_000)
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('does not start the clock on a notice appended into a turn the window joined late', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      // A policy-block notice and a stop-hook halt card are `inject` rows with no
      // turn-opening kind, and a steer is a `user` row sent into the running turn.
      act(() => { ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-a', role: 'inject', cls: 'msg msg-inject', content: 'blocked' } }) })
      act(() => { ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-a', role: 'user', content: 'also this', meta: { steer: true } } }) })
      advance(5_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('starts the clock on an inject row that opens a turn of its own', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Nightly sweep')

      act(() => { ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-a', role: 'inject', content: 'sweep', meta: { injectKind: 'cron' } } }) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('measures a long turn as long when the wall clock steps back during it', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(90_000)
      vi.setSystemTime(Date.now() - 120_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('measures a run whose sub-agents finish after a continuing completion from its first prompt', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      seedSlot('slot-a', 'Survey the repos')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(1_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a', continuing: true } }) })
      advance(20 * 60_000)
      act(() => { ws.simulateMessage(row('slot-a', 'subagent')) })
      advance(10_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].options?.body).toBe('Response ready')
    })

    it('keeps the run clock when the session list alone says sub-agents are still running', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      const slotRow = (running: boolean) => ({ key: 'slot-a', title: 'Survey the repos', messages: 1, running: false, subagents_running: running })
      act(() => { globalStore.dispatch(sseSlots([slotRow(true)])) })

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(1_000)
      // No `continuing` hint on the frame: the slot's running children make it one.
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toEqual([])
      act(() => { globalStore.dispatch(sseSlots([slotRow(false)])) })
      advance(20 * 60_000)
      act(() => { ws.simulateMessage(row('slot-a', 'subagent')) })
      advance(10_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('ends the run clock on a continuing completion that waits for input', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(60)
      const ws = mountOpened()
      seedSlot('slot-a', 'Choose the approach')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(90_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a', continuing: true, needs_input: true } }) })
      expect(CONSTRUCTED).toHaveLength(1)
      expect(CONSTRUCTED[0].options?.body).toBe('Waiting for your input')

      // The answer starts a new turn, measured from its own row.
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toHaveLength(1)
    })

    const pop = (extra: Record<string, unknown>) => ({ type: 'queue_pop', data: { slot: 'slot-a', queue_id: 'q-1', ...extra } })

    it('starts the clock on a drained send that becomes the person\'s row', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Crewmate thread')

      // An idle crewmate send: the drain's pop is the only frame that opens the turn.
      act(() => { ws.simulateMessage(pop({ content: 'quick one', drain_writes_row: false })) })
      advance(5_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toEqual([])

      act(() => { ws.simulateMessage(pop({ queue_id: 'q-2', content: 'long one', drain_writes_row: false })) })
      advance(45_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      expect(CONSTRUCTED).toHaveLength(1)
    })

    it('does not start the clock on the pop of a cancelled or swept entry', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(pop({ content: '' })) })
      act(() => { ws.simulateMessage(pop({ queue_id: 'q-2' })) })
      advance(120_000)
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('leaves the stamp to the row a drain writes for itself', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Survey the repos')

      act(() => { ws.simulateMessage(pop({ content: 'result', drain_writes_row: true })) })
      advance(120_000)
      act(() => { ws.simulateMessage(row('slot-a', 'subagent')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('starts no clock from an opener row or a drain pop replayed on reconnect', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      // A second open on the same socket is a reconnect: its frames are catch-up
      // until the connect snapshot lands.
      act(() => { ws.simulateOpen() })
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      act(() => { ws.simulateMessage(pop({ content: 'earlier', drain_writes_row: false })) })
      act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'slot-a', title: 'Refactor the planner', messages: 1, running: false }] }) })
      advance(120_000)
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('ends a turn this window stamped when its completion is replayed on reconnect', () => {
      saveChatCompleteNotify(true)
      pinMinSeconds(30)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(120_000)
      // The socket dropped and the turn finished meanwhile: its `chat_done` is replayed.
      act(() => { ws.simulateOpen() })
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })
      act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'slot-a', title: 'Refactor the planner', messages: 1, running: false }] }) })
      expect(CONSTRUCTED).toEqual([])
      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      advance(2_000)
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toEqual([])
    })

    it('toasts every finish while no minimum is chosen', () => {
      saveChatCompleteNotify(true)
      const ws = mountOpened()
      seedSlot('slot-a', 'Refactor the planner')

      act(() => { ws.simulateMessage(row('slot-a', 'user')) })
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-a' } }) })

      expect(CONSTRUCTED).toHaveLength(1)
    })
  })
})
