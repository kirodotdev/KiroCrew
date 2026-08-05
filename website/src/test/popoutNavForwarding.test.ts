import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  createPopoutController,
  NAV_CLAIM_MS,
  type PopoutController,
  type PopoutMsg,
  type NavIntent,
} from '../utils/popoutController'
import { applyNavIntentInMain, writePrefill, PREFILL_STORAGE_KEY } from '../utils/navIntent'

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
      },
    )
    expect(order).toEqual(['prefill', 'switchSlot', 'navigate'])
    expect(setItem).toHaveBeenCalledWith(PREFILL_STORAGE_KEY, expect.stringContaining('"prompt":"do it"'))
  })

  it('skips slot switching for intents without a slotKey', () => {
    const switchSlot = vi.fn()
    const navigate = vi.fn()
    applyNavIntentInMain({ path: '/artifacts' }, { navigate, switchSlot })
    expect(switchSlot).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith('/artifacts')
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
