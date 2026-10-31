import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  createPopoutController,
  NAV_CLAIM_MS,
  type PopoutController,
  type PopoutMsg,
  type NavIntent,
} from '../utils/popoutController'
import {
  applyNavIntentInMain,
  chatDeepLinkSlot,
  claimChatLaunch,
  takeDeferredPendingInput,
  writePrefill,
  DEFERRED_PENDING_INPUT_TTL_MS,
  PREFILL_STORAGE_KEY,
} from '../utils/navIntent'
import { ERROR_HANDOFF_KEY, subscribeChatHandoff } from '../utils/errorReport'

/** The App SDK's launch slot on the window, as `openChat` writes it. */
function launchWindow() {
  return window as Window & { __mc_chat_launch?: { message?: string; agent?: string; slotKey?: string; autoSend?: boolean; ts?: number } }
}

/**
 * Navigation-intent forwarding tests (popout navigation containment).
 *
 * A popout window is pinned to its entity: affordances that would leave it
 * forward a `NavIntent` to a main dashboard window over the popout
 * BroadcastChannel via a two-phase claim handshake (nav-request → nav-offer →
 * nav-go), so with several main tabs open exactly ONE navigates. When no main
 * claims within NAV_CLAIM_MS the destination opens in a new tab instead.
 *
 * These tests run multiple REAL controller instances over a cross-delivering
 * BroadcastChannel stub — one instance per simulated window — exercising the
 * actual handleMessage dispatch on both sides of the handshake.
 */

/**
 * Cross-delivering BroadcastChannel stub: posting on one instance delivers to
 * every OTHER live instance with the same channel name (mirroring the spec:
 * BroadcastChannel never self-delivers). Delivery is synchronous, which the
 * handshake tolerates — only the no-offer case is time-driven.
 */
class BusChannel {
  static byName = new Map<string, Set<BusChannel>>()
  static reset(): void { BusChannel.byName.clear() }
  onmessage: ((e: { data: PopoutMsg }) => void) | null = null
  posted: PopoutMsg[] = []
  private closed = false
  constructor(public name: string) {
    const set = BusChannel.byName.get(name) ?? new Set<BusChannel>()
    set.add(this)
    BusChannel.byName.set(name, set)
  }
  postMessage(msg: PopoutMsg): void {
    this.posted.push(msg)
    for (const ch of BusChannel.byName.get(this.name) ?? []) {
      if (ch !== this && !ch.closed) ch.onmessage?.({ data: msg })
    }
  }
  close(): void {
    this.closed = true
    BusChannel.byName.get(this.name)?.delete(this)
  }
}

function mkController(): PopoutController {
  return createPopoutController({
    channelName: 'test-nav',
    logLabel: 'testNav',
    buildUrl: id => `/popout/thing/${id}`,
    windowName: id => `mc-test-${id}`,
    mainViewUrl: id => (id ? `/things/${id}` : '/things'),
  })
}

const controllers: PopoutController[] = []
function controller(): PopoutController {
  const c = mkController()
  controllers.push(c)
  return c
}

beforeEach(() => {
  BusChannel.reset()
  vi.stubGlobal('BroadcastChannel', BusChannel as unknown as typeof BroadcastChannel)
})

afterEach(() => {
  controllers.splice(0).forEach(c => c.__resetForTests())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
  sessionStorage.clear()
})

describe('forwardToMain claim handshake', () => {
  it('delivers the intent to a registered main window and never opens a fallback tab', () => {
    vi.useFakeTimers()
    const main = controller()
    const popout = controller()
    const handled: NavIntent[] = []
    main.setNavIntentHandler(intent => handled.push(intent))
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', slotKey: 'chat-42' })

    expect(handled).toEqual([{ path: '/chat', slotKey: 'chat-42' }])
    vi.advanceTimersByTime(NAV_CLAIM_MS * 2)
    expect(opened).toEqual([]) // claim landed — no new-tab fallback
  })

  it('with two main windows, exactly one performs the navigation', () => {
    vi.useFakeTimers()
    const main1 = controller()
    const main2 = controller()
    const popout = controller()
    const handled1: NavIntent[] = []
    const handled2: NavIntent[] = []
    main1.setNavIntentHandler(i => handled1.push(i))
    main2.setNavIntentHandler(i => handled2.push(i))

    popout.forwardToMain({ path: '/artifacts' })

    expect(handled1.length + handled2.length).toBe(1)
  })

  it('carries the prefill payload through the handshake (sessionStorage is per-window)', () => {
    const main = controller()
    const popout = controller()
    const handled: NavIntent[] = []
    main.setNavIntentHandler(i => handled.push(i))

    const intent: NavIntent = {
      path: '/chat',
      slotKey: 'new-1',
      prefill: { slotKey: 'new-1', prompt: 'Iterate on artifact x: fix the header' },
    }
    popout.forwardToMain(intent)
    expect(handled).toEqual([intent])
  })

  it('a popout window (no handler registered) never claims another popout’s intent', () => {
    vi.useFakeTimers()
    const otherPopout = controller()
    otherPopout.registerPopout('bystander')
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/artifacts' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/artifacts`]) // fell back — nobody offered
  })

  it('after the handler is unregistered, the main stops offering and the fallback fires', () => {
    vi.useFakeTimers()
    const main = controller()
    const popout = controller()
    const handled: NavIntent[] = []
    const cleanup = main.setNavIntentHandler(i => handled.push(i))
    cleanup()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/artifacts' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(handled).toEqual([])
    expect(opened).toHaveLength(1)
  })
})

describe('forwardToMain no-main fallback (new tab)', () => {
  it('opens the destination in a new tab after NAV_CLAIM_MS with sid + prefill as query params', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: Array<[string, string]> = []
    popout.__setWindowOpenForTests((url, target) => opened.push([url, target]))

    popout.forwardToMain({
      path: '/chat',
      slotKey: 'chat-42',
      prefill: { slotKey: 'chat-42', prompt: 'Fix the thing' },
    })
    expect(opened).toEqual([]) // still inside the claim window
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([
      [`${window.location.origin}/chat?sid=chat-42&prefill=Fix+the+thing`, '_blank'],
    ])
  })

  it('omits the query string entirely for a bare path intent', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/artifacts' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/artifacts`])
  })

  it('replays a forwarded query string and turns a staged pendingInput into ?new=1&prefill= (an app’s chat exit)', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    // What an app pop-out forwards for `navigate('/chat?autoSend=1&newSession=1')`
    // after `dispatch(setPendingInput(...))`: the flags ChatPage reads on arrival
    // survive, and the prompt — which lived only in the pop-out's store — rides
    // as the `?new=1&prefill=` the chat page creates a seeded session from. A
    // bare `?prefill=` would be dropped there: without a session it is an in-app
    // sentinel, not a prompt.
    popout.forwardToMain({ path: '/chat', search: '?autoSend=1&newSession=1', pendingInput: 'Open a session here' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([
      `${window.location.origin}/chat?autoSend=1&newSession=1&prefill=Open+a+session+here&new=1`,
    ])
  })

  it('a forwarded error hand-off seeds one fresh chat in the fallback tab', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', errorHandoffs: ['First diagnostic', 'Second diagnostic'] })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?prefill=First+diagnostic&new=1`])
  })

  it('an error hand-off drops the named slot and opens a fresh chat', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', slotKey: 'chat-42', errorHandoffs: ['Diag'] })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?prefill=Diag&new=1`])
  })

  it('an error hand-off drops a replayed sid but keeps the other query parameters', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', search: '?sid=chat-9&autoSend=1', errorHandoffs: ['Diag'] })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?autoSend=1&prefill=Diag&new=1`])
  })

  it('the in-app ?prefill=1 sentinel is replaced by the staged text, with ?new=1 added', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    // File Explorer's "Chat about this file": the real text rides Redux, the URL
    // only carries the sentinel. The fallback must not ship the sentinel.
    popout.forwardToMain({ path: '/chat', search: '?prefill=1', pendingInput: 'Explain src/main.ts' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?prefill=Explain+src%2Fmain.ts&new=1`])
  })

  it('an app launch’s message falls back to ?new=1&prefill=; its agent and auto-send do not cross a cold URL', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', chatLaunch: { message: 'Review this', agent: 'reviewer', autoSend: true } })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?prefill=Review+this&new=1`])
  })

  it('an app launch into an existing session seeds that session (?sid=&prefill=), never a new one', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    // The SDK spells the target as `/chat?sid=…` and names it on the launch too.
    popout.forwardToMain({ path: '/chat', search: '?sid=chat-3', slotKey: 'chat-3', chatLaunch: { message: 'Continue', slotKey: 'chat-3' } })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?sid=chat-3&prefill=Continue`])
  })

  it('a session deep link keeps its ?sid= once through the replayed query', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', search: '?sid=chat-7', slotKey: 'chat-7' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([`${window.location.origin}/chat?sid=chat-7`])
  })

  it('a second forward supersedes a pending one — only the newest intent falls back', () => {
    vi.useFakeTimers()
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/chat', slotKey: 'first' })
    popout.forwardToMain({ path: '/chat', slotKey: 'second' })
    vi.advanceTimersByTime(NAV_CLAIM_MS * 2)
    expect(opened).toEqual([`${window.location.origin}/chat?sid=second`])
  })
})

describe('applyNavIntentInMain', () => {
  it('writes the prefill BEFORE switching slot and navigating (ChatPage reads it on slot activation)', () => {
    const order: string[] = []
    const setItem = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => order.push('prefill'))
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'new-1', prefill: { slotKey: 'new-1', prompt: 'do it' } },
      {
        navigate: () => order.push('navigate'),
        switchSlot: () => order.push('switchSlot'),
        mayLeave: () => true,
      },
    )
    expect(order).toEqual(['prefill', 'switchSlot', 'navigate'])
    expect(setItem).toHaveBeenCalledWith(PREFILL_STORAGE_KEY, expect.stringContaining('"prompt":"do it"'))
  })

  it('hands a forwarded pendingInput to the staging dep before navigating with the replayed query', () => {
    const order: string[] = []
    applyNavIntentInMain(
      { path: '/chat', search: '?autoSend=1&newSession=1', pendingInput: 'Open a session here' },
      {
        navigate: p => order.push(`navigate:${p}`),
        switchSlot: k => order.push(`switch:${k}`),
        mayLeave: () => true,
        setPendingInput: (text, to, slotKey) => order.push(`pending:${text}:${to}:${slotKey}`),
      },
    )
    // The shell stages the input for the exact destination and session before
    // navigation, then publishes it only after that navigation commits.
    expect(order).toEqual([
      'pending:Open a session here:/chat?autoSend=1&newSession=1:null',
      'navigate:/chat?autoSend=1&newSession=1',
    ])
  })

  it('stages a session-bound pendingInput before switching that session and navigating', () => {
    const order: string[] = []
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'chat-1', pendingInput: 'Open this session' },
      {
        navigate: path => order.push(`navigate:${path}`),
        switchSlot: key => order.push(`switch:${key}`),
        mayLeave: () => true,
        setPendingInput: (text, to, slotKey) => order.push(`pending:${text}:${to}:${slotKey}`),
      },
    )
    expect(order).toEqual([
      'pending:Open this session:/chat:chat-1',
      'switch:chat-1',
      'navigate:/chat',
    ])
  })

  it('re-stages error hand-offs and notifies mounted chat before navigating', () => {
    const order: string[] = []
    const unsubscribe = subscribeChatHandoff(() => {
      const raw = sessionStorage.getItem(ERROR_HANDOFF_KEY)
      order.push(`handoff:${JSON.stringify((JSON.parse(raw ?? '[]') as Array<{ prompt: string }>).map(entry => entry.prompt))}`)
    })
    applyNavIntentInMain(
      { path: '/chat', errorHandoffs: ['diag one', 'diag two'] },
      {
        navigate: path => order.push(`navigate:${path}`),
        switchSlot: vi.fn(),
        mayLeave: () => true,
      },
    )
    unsubscribe()

    expect(order).toEqual([
      'handoff:["diag one","diag two"]',
      'navigate:/chat',
    ])
    const stored = JSON.parse(sessionStorage.getItem(ERROR_HANDOFF_KEY) ?? '[]') as Array<{ prompt: string }>
    expect(stored.map(entry => entry.prompt)).toEqual(['diag one', 'diag two'])
  })

  it('a refused re-stage launches a fresh seeded session instead of touching the open composer', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    const navigate = vi.fn()
    const switchSlot = vi.fn()
    const setPendingInput = vi.fn()
    const params = new URLSearchParams()
    params.set('new', '1')
    params.set('prefill', 'First diagnostic\n\nSecond diagnostic')

    applyNavIntentInMain(
      { path: '/chat', errorHandoffs: ['First diagnostic', 'Second diagnostic'] },
      { navigate, switchSlot, mayLeave: () => true, setPendingInput },
    )

    expect(navigate).toHaveBeenCalledOnce()
    expect(navigate).toHaveBeenCalledWith(`/chat?${params}`)
    expect(setPendingInput).not.toHaveBeenCalled()
    expect(switchSlot).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(ERROR_HANDOFF_KEY)).toBeNull()
  })

  it('a refused re-stage drops a named session but keeps other query parameters for the fresh launch', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    const navigate = vi.fn()
    const switchSlot = vi.fn()
    const setPendingInput = vi.fn()

    applyNavIntentInMain(
      {
        path: '/chat',
        search: '?sid=chat-7&autoSend=1',
        slotKey: 'chat-7',
        errorHandoffs: ['Diag'],
      },
      { navigate, switchSlot, mayLeave: () => true, setPendingInput },
    )

    expect(navigate).toHaveBeenCalledWith('/chat?autoSend=1&new=1&prefill=Diag')
    expect(switchSlot).toHaveBeenCalledWith('chat-7')
    expect(setPendingInput).not.toHaveBeenCalled()
  })

  it('a refused re-stage drops a stale hand-off paired with a non-chat destination', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    const navigate = vi.fn()
    const setPendingInput = vi.fn()
    const notified = vi.fn()
    const unsubscribe = subscribeChatHandoff(notified)

    applyNavIntentInMain(
      { path: '/settings', errorHandoffs: ['Diag'] },
      { navigate, switchSlot: vi.fn(), mayLeave: () => true, setPendingInput },
    )
    unsubscribe()

    expect(navigate).toHaveBeenCalledWith('/settings')
    expect(setPendingInput).not.toHaveBeenCalled()
    expect(notified).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(ERROR_HANDOFF_KEY)).toBeNull()
  })

  it('navigates to the bare path when the intent carries no query', () => {
    const navigate = vi.fn()
    applyNavIntentInMain({ path: '/settings' }, { navigate, switchSlot: vi.fn(), mayLeave: () => true })
    expect(navigate).toHaveBeenCalledWith('/settings')
  })

  it('re-stages a forwarded app launch on THIS window, with a fresh ts, before navigating', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)
    const order: string[] = []
    applyNavIntentInMain(
      { path: '/chat', chatLaunch: { message: 'Review this', agent: 'reviewer' } },
      {
        navigate: p => {
          order.push(`navigate:${p}`)
          // ChatPage's launch intake reads the window slot when the page mounts.
          order.push(`launch:${JSON.stringify(launchWindow().__mc_chat_launch)}`)
        },
        switchSlot: vi.fn(),
        mayLeave: () => true,
      },
    )
    expect(order).toEqual([
      'navigate:/chat',
      'launch:{"message":"Review this","agent":"reviewer","ts":1700000000000}',
    ])
    delete launchWindow().__mc_chat_launch
  })

  it('a refused leave stages no launch either', () => {
    applyNavIntentInMain(
      { path: '/chat', chatLaunch: { message: 'Review this' } },
      { navigate: vi.fn(), switchSlot: vi.fn(), mayLeave: () => false },
    )
    expect(launchWindow().__mc_chat_launch).toBeUndefined()
  })

  it('skips slot switching for intents without a slotKey', () => {
    const switchSlot = vi.fn()
    const navigate = vi.fn()
    applyNavIntentInMain({ path: '/artifacts' }, { navigate, switchSlot, mayLeave: () => true })
    expect(switchSlot).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith('/artifacts')
  })

  it('drops the whole intent when the page on screen refuses to be left', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const switchSlot = vi.fn()
    const navigate = vi.fn()
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'new-1', prefill: { slotKey: 'new-1', prompt: 'do it' } },
      { navigate, switchSlot, mayLeave: () => false },
    )
    expect(setItem).not.toHaveBeenCalledWith(PREFILL_STORAGE_KEY, expect.anything())
    expect(switchSlot).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('navigates once the page on screen agrees to be left', () => {
    const navigate = vi.fn()
    const mayLeave = vi.fn(() => true)
    applyNavIntentInMain({ path: '/artifacts' }, { navigate, switchSlot: vi.fn(), mayLeave })
    expect(mayLeave).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('/artifacts')
  })

  it('does not ask when the intent appends to the composer already on screen', () => {
    const appendToOpenComposer = vi.fn(() => true)
    const mayLeave = vi.fn(() => false)
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'chat-1', prefill: { slotKey: 'chat-1', prompt: 'ref', append: 'ref' } },
      { navigate: vi.fn(), switchSlot: vi.fn(), mayLeave, appendToOpenComposer },
    )
    expect(mayLeave).not.toHaveBeenCalled()
    expect(appendToOpenComposer).toHaveBeenCalledWith('chat-1', 'ref')
  })

  it('appends to the composer already open on the target slot instead of leaving a stale prefill', () => {
    const appendToOpenComposer = vi.fn(() => true)
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const switchSlot = vi.fn()
    const navigate = vi.fn()
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'chat-1', prefill: { slotKey: 'chat-1', prompt: 'old draft\n\nref', append: 'ref' } },
      { navigate, switchSlot, appendToOpenComposer, mayLeave: () => true },
    )
    expect(appendToOpenComposer).toHaveBeenCalledWith('chat-1', 'ref')
    expect(setItem).not.toHaveBeenCalledWith(PREFILL_STORAGE_KEY, expect.anything())
    expect(switchSlot).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('still seeds the prefill when no composer on screen shows the target slot', () => {
    const appendToOpenComposer = vi.fn(() => false)
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const switchSlot = vi.fn()
    applyNavIntentInMain(
      { path: '/chat', slotKey: 'chat-1', prefill: { slotKey: 'chat-1', prompt: 'ref', append: 'ref' } },
      { navigate: vi.fn(), switchSlot, appendToOpenComposer, mayLeave: () => true },
    )
    expect(appendToOpenComposer).toHaveBeenCalledWith('chat-1', 'ref')
    expect(setItem).toHaveBeenCalledWith(PREFILL_STORAGE_KEY, expect.stringContaining('"prompt":"ref"'))
    expect(switchSlot).toHaveBeenCalledWith('chat-1')
  })
})

describe('chatDeepLinkSlot', () => {
  it('reads the session out of the dashboard session deep link', () => {
    expect(chatDeepLinkSlot('/chat?sid=chat-69-1785905004')).toBe('chat-69-1785905004')
    // The slug is decorative — ChatPage writes it from the session title.
    expect(chatDeepLinkSlot('/chat/fix-the-parser?sid=chat-1')).toBe('chat-1')
    // `?slot=` is the legacy spelling ChatPage still accepts.
    expect(chatDeepLinkSlot('/chat?slot=chat-2')).toBe('chat-2')
    expect(chatDeepLinkSlot('/chat?sid=chat%20a%2Fb')).toBe('chat a/b')
  })

  it('names no session for a path that is not one', () => {
    // Each of these must fall through to a plain navigate: routing them as a
    // session link would dispatch a switchSlot for a session that was never named.
    expect(chatDeepLinkSlot('/chat')).toBe('')
    expect(chatDeepLinkSlot('/settings?tab=about')).toBe('')
    expect(chatDeepLinkSlot('/chats?sid=chat-1')).toBe('')
    expect(chatDeepLinkSlot('/logs?sid=chat-1')).toBe('')
    expect(chatDeepLinkSlot('')).toBe('')
  })
})

describe('writePrefill', () => {
  it('seeds the composer-prefill channel with slotKey, prompt, and a timestamp', () => {
    writePrefill('slot-9', 'hello')
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    expect(raw).not.toBeNull()
    const parsed = JSON.parse(raw as string)
    expect(parsed.slotKey).toBe('slot-9')
    expect(parsed.prompt).toBe('hello')
    expect(typeof parsed.ts).toBe('number')
  })
})

describe('takeDeferredPendingInput', () => {
  const NOW = 1_700_000_000_000
  const staged = {
    text: 'QA prompt',
    fromKey: 'before',
    to: '/chat?autoSend=1&newSession=1',
    slotKey: null,
    ts: NOW,
  }
  const committed = (href: string, activeSlot: string | null = null, key = 'after') => ({
    key,
    href,
    activeSlot,
  })

  it('returns null when nothing is staged', () => {
    expect(takeDeferredPendingInput(null, committed(staged.to), NOW)).toBeNull()
  })

  it('publishes at an equal location when no session is named', () => {
    expect(takeDeferredPendingInput(staged, committed(staged.to), NOW + 1)).toBe('QA prompt')
  })

  it('returns null while the router key is unchanged', () => {
    expect(takeDeferredPendingInput(staged, committed(staged.to, null, 'before'), NOW)).toBeNull()
  })

  it('keeps the inclusive TTL edge and returns null past it', () => {
    expect(takeDeferredPendingInput(staged, committed(staged.to), NOW + DEFERRED_PENDING_INPUT_TTL_MS)).toBe('QA prompt')
    expect(takeDeferredPendingInput(staged, committed(staged.to), NOW + DEFERRED_PENDING_INPUT_TTL_MS + 1)).toBeNull()
  })

  it('returns null when a user chat navigation commits before a session-less launch', () => {
    expect(takeDeferredPendingInput(staged, committed('/chat'), NOW)).toBeNull()
  })

  it('publishes on ChatPage session rewrites using sid or legacy slot', () => {
    const sessionStaged = { ...staged, to: '/chat', slotKey: 'chat-2' }
    expect(takeDeferredPendingInput(
      sessionStaged,
      committed('/chat/some-slug?sid=chat-2', 'chat-2'),
      NOW,
    )).toBe('QA prompt')
    expect(takeDeferredPendingInput(
      sessionStaged,
      committed('/chat/some-slug?slot=chat-2', 'chat-2'),
      NOW,
    )).toBe('QA prompt')
  })

  it('returns null when the committed rewrite names another session', () => {
    const sessionStaged = { ...staged, to: '/chat', slotKey: 'chat-2' }
    expect(takeDeferredPendingInput(
      sessionStaged,
      committed('/chat/some-slug?sid=chat-9', 'chat-2'),
      NOW,
    )).toBeNull()
  })

  it('returns null when another session is active at an equal location', () => {
    const sessionStaged = { ...staged, to: '/chat', slotKey: 'chat-2' }
    expect(takeDeferredPendingInput(sessionStaged, committed('/chat', 'chat-9'), NOW)).toBeNull()
  })

  it('does not allow a chat rewrite for a non-chat destination', () => {
    const sessionStaged = { ...staged, to: '/schedule', slotKey: 'chat-2' }
    expect(takeDeferredPendingInput(
      sessionStaged,
      committed('/chat?sid=chat-2', 'chat-2'),
      NOW,
    )).toBeNull()
  })

  it('normalizes a trailing question mark before comparing locations', () => {
    expect(takeDeferredPendingInput(
      { ...staged, to: '/chat?' },
      committed('/chat'),
      NOW,
    )).toBe('QA prompt')
  })

  it('returns null for a malformed committed href', () => {
    expect(takeDeferredPendingInput(staged, committed('http://['), NOW)).toBeNull()
  })
})

describe('claimChatLaunch', () => {
  // Freshness is a comparison of two clock reads (the launch's `ts`, then the
  // claim's `Date.now()`): a pinned clock makes the verdict a fact of the
  // fixture, not of the wall clock between the two reads. The file-level
  // afterEach restores real timers.
  const NOW = 1_700_000_000_000
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => { delete launchWindow().__mc_chat_launch })

  it('takes a fresh App SDK launch off this window, less its ts, and clears the slot', () => {
    launchWindow().__mc_chat_launch = { message: 'Review this', agent: 'reviewer', autoSend: false, ts: NOW }
    expect(claimChatLaunch()).toEqual({ message: 'Review this', agent: 'reviewer', autoSend: false })
    expect(launchWindow().__mc_chat_launch).toBeUndefined()
  })

  it('keeps a launch right at the ten-second edge, and drops one past it', () => {
    launchWindow().__mc_chat_launch = { message: 'edge', ts: NOW - 10_000 }
    expect(claimChatLaunch()).toEqual({ message: 'edge' })
    launchWindow().__mc_chat_launch = { message: 'old', ts: NOW - 10_001 }
    expect(claimChatLaunch()).toBeUndefined()
    expect(launchWindow().__mc_chat_launch).toBeUndefined()
  })

  it('is undefined when nothing is staged', () => {
    expect(claimChatLaunch()).toBeUndefined()
  })
})
