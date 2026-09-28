import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  applyMessage,
  pruneStale,
  popoutWindowName,
  buildPopoutUrl,
  type PopoutMap,
} from '../utils/chatPopout'

/**
 * Pure-logic tests for the chat-popout coordination helpers. The
 * BroadcastChannel/heartbeat wiring is intentionally not exercised here — these
 * pin the state math the main window relies on to track live popouts.
 */
describe('chatPopout.applyMessage', () => {
  it('adds a slot on open with the current timestamp', () => {
    const next = applyMessage({}, { t: 'open', id: 'chat-1' }, 1000)
    expect(next).toEqual({ 'chat-1': 1000 })
  })

  it('refreshes lastSeen on pong', () => {
    const next = applyMessage({ 'chat-1': 1000 }, { t: 'pong', id: 'chat-1' }, 5000)
    expect(next['chat-1']).toBe(5000)
  })

  it('removes a slot on close', () => {
    const next = applyMessage({ 'chat-1': 1000, 'chat-2': 1000 }, { t: 'close', id: 'chat-1' }, 2000)
    expect(next).toEqual({ 'chat-2': 1000 })
  })

  it('returns the same reference for close of an unknown slot (no churn)', () => {
    const map: PopoutMap = { 'chat-1': 1000 }
    expect(applyMessage(map, { t: 'close', id: 'ghost' }, 2000)).toBe(map)
  })

  it('ignores control/heartbeat messages', () => {
    const map: PopoutMap = { 'chat-1': 1000 }
    expect(applyMessage(map, { t: 'ping' }, 2000)).toBe(map)
    expect(applyMessage(map, { t: 'focus', id: 'chat-1' }, 2000)).toBe(map)
    expect(applyMessage(map, { t: 'bring-back', id: 'chat-1' }, 2000)).toBe(map)
  })
})

describe('chatPopout.pruneStale', () => {
  it('drops entries older than the stale window', () => {
    const map: PopoutMap = { fresh: 10_000, stale: 1_000 }
    const next = pruneStale(map, 20_000, 12_000)
    expect(next).toEqual({ fresh: 10_000 })
  })

  it('keeps the same reference when nothing is stale (identity-stable)', () => {
    const map: PopoutMap = { a: 19_000, b: 20_000 }
    expect(pruneStale(map, 20_000, 12_000)).toBe(map)
  })

  it('treats an entry exactly at the boundary as still alive', () => {
    const map: PopoutMap = { edge: 8_000 }
    expect(pruneStale(map, 20_000, 12_000)).toBe(map)
  })
})

describe('chatPopout.popoutWindowName', () => {
  it('is stable and filesystem-safe for a slot key', () => {
    expect(popoutWindowName('chat-1-123')).toBe('mc-popout-chat-1-123')
  })

  it('sanitizes characters that are invalid in a window name', () => {
    expect(popoutWindowName('dashboard:chat/1 2')).toBe('mc-popout-dashboard_chat_1_2')
  })
})

describe('chatPopout.buildPopoutUrl', () => {
  const origin = window.location.origin

  it('carries the slot as ?sid and slugs the title into the path', () => {
    expect(buildPopoutUrl('chat-1', 'My Session')).toBe(`${origin}/popout/chat/my-session?sid=chat-1`)
  })

  it('omits the slug when the title equals the slot key', () => {
    expect(buildPopoutUrl('chat-1', 'chat-1')).toBe(`${origin}/popout/chat?sid=chat-1`)
  })

  it('omits the slug when there is no title', () => {
    expect(buildPopoutUrl('chat-1')).toBe(`${origin}/popout/chat?sid=chat-1`)
  })
})

// ── controller-level tests (stubbed BroadcastChannel — no live channel) ──────

import {
  registerPopout,
  openPopout,
  focusPopout,
  bringBack,
  isSelfPopout,
  returnSelfToMain,
  subscribe,
  getSnapshot,
  HEARTBEAT_MS,
  STALE_MS,
  type PopoutMsg as Msg,
  __resetForTests,
  __setNavigateForTests,
} from '../utils/chatPopout'

/**
 * Minimal BroadcastChannel stub: records posted messages and lets tests
 * deliver inbound ones via the controller's `onmessage` — exercising the real
 * `handleMessage` responder switch (ping→pong, focus, bring-back) and the
 * heartbeat without a live channel.
 */
class StubChannel {
  static instances: StubChannel[] = []
  onmessage: ((e: { data: Msg }) => void) | null = null
  posted: Msg[] = []
  constructor(public name: string) { StubChannel.instances.push(this) }
  postMessage(msg: Msg) { this.posted.push(msg) }
  close() { /* noop */ }
}

function channelOf(): StubChannel {
  const ch = StubChannel.instances[0]
  if (!ch) throw new Error('controller never opened a channel')
  return ch
}

/** Deliver an inbound message as if another window posted it. */
function deliver(msg: Msg): void {
  channelOf().onmessage?.({ data: msg })
}

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: hidden ? 'hidden' : 'visible' })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  StubChannel.instances = []
  vi.stubGlobal('BroadcastChannel', StubChannel as unknown as typeof BroadcastChannel)
})

afterEach(() => {
  __resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
  setHidden(false)
})

describe('chatPopout responder role (popout window side)', () => {
  it('announces open on register and answers pings with a pong for its own slot', () => {
    registerPopout('chat-1')
    expect(channelOf().posted).toContainEqual({ t: 'open', id: 'chat-1' })
    deliver({ t: 'ping' })
    expect(channelOf().posted).toContainEqual({ t: 'pong', id: 'chat-1' })
  })

  it('focuses itself on a focus message addressed to its slot — and ignores other slots', () => {
    registerPopout('chat-1')
    const focus = vi.spyOn(window, 'focus').mockImplementation(() => {})
    deliver({ t: 'focus', id: 'other' })
    expect(focus).not.toHaveBeenCalled()
    deliver({ t: 'focus', id: 'chat-1' })
    expect(focus).toHaveBeenCalledTimes(1)
  })

  it('bring-back closes the window; when close is refused (deep-linked, no opener) it announces close and navigates to the main chat view', () => {
    registerPopout('chat-1')
    const close = vi.spyOn(window, 'close').mockImplementation(() => {}) // jsdom window stays !closed → fallback runs
    const navigated: string[] = []
    __setNavigateForTests(url => navigated.push(url))
    deliver({ t: 'bring-back', id: 'chat-1' })
    expect(close).toHaveBeenCalledTimes(1)
    expect(channelOf().posted).toContainEqual({ t: 'close', id: 'chat-1' })
    expect(navigated).toEqual(['/chat?sid=chat-1'])
  })

  it('returnSelfToMain from the Return button takes the same close-then-navigate fallback', () => {
    registerPopout('chat-1')
    vi.spyOn(window, 'close').mockImplementation(() => {})
    const navigated: string[] = []
    __setNavigateForTests(url => navigated.push(url))
    returnSelfToMain()
    expect(navigated).toEqual(['/chat?sid=chat-1'])
  })

  it('isSelfPopout is true only for the registered slot and clears on cleanup', () => {
    expect(isSelfPopout('chat-1')).toBe(false)
    const cleanup = registerPopout('chat-1')
    expect(isSelfPopout('chat-1')).toBe(true)
    expect(isSelfPopout('chat-2')).toBe(false)
    cleanup()
    expect(isSelfPopout('chat-1')).toBe(false)
  })
})

describe('chatPopout.openPopout (main window side)', () => {
  it('opens a named window, marks the slot open, and reuses (focuses) the live handle on re-invoke', () => {
    const fakeWin = { closed: false, focus: vi.fn() } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(fakeWin)
    openPopout('chat-1', 'My Session')
    expect(open).toHaveBeenCalledTimes(1)
    expect(open.mock.calls[0][1]).toBe('mc-popout-chat-1')
    expect(getSnapshot().has('chat-1')).toBe(true)
    openPopout('chat-1', 'My Session') // dedupe: focus existing, no second open
    expect(open).toHaveBeenCalledTimes(1)
    expect((fakeWin as unknown as { focus: ReturnType<typeof vi.fn> }).focus.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('surfaces a blocked popup instead of failing silently, and does not mark the slot open', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    openPopout('chat-1')
    expect(alert).toHaveBeenCalledTimes(1)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(getSnapshot().has('chat-1')).toBe(false)
  })

  it('focusPopout / bringBack without a live handle route through the channel', () => {
    focusPopout('chat-1')
    bringBack('chat-1')
    expect(channelOf().posted).toContainEqual({ t: 'focus', id: 'chat-1' })
    expect(channelOf().posted).toContainEqual({ t: 'bring-back', id: 'chat-1' })
  })
})

describe('chatPopout heartbeat (visibility-aware)', () => {
  it('pings on subscribe and every interval while visible; stops when the last subscriber leaves', () => {
    vi.useFakeTimers()
    const unsub = subscribe(() => {})
    const pings = () => channelOf().posted.filter(m => m.t === 'ping').length
    expect(pings()).toBe(1)
    vi.advanceTimersByTime(HEARTBEAT_MS * 2)
    expect(pings()).toBe(3)
    unsub()
    vi.advanceTimersByTime(HEARTBEAT_MS * 3)
    expect(pings()).toBe(3)
  })

  it('goes quiescent while the tab is hidden', () => {
    vi.useFakeTimers()
    const unsub = subscribe(() => {})
    const pings = () => channelOf().posted.filter(m => m.t === 'ping').length
    setHidden(true)
    vi.advanceTimersByTime(HEARTBEAT_MS * 10)
    expect(pings()).toBe(1) // only the initial subscribe ping
    unsub()
  })

  it('on return to visible, re-pings immediately and does NOT prune a live popout that missed pings while hidden', () => {
    vi.useFakeTimers()
    const unsub = subscribe(() => {})
    deliver({ t: 'pong', id: 'chat-1' }) // a live popout known to this window
    expect(getSnapshot().has('chat-1')).toBe(true)
    setHidden(true)
    vi.advanceTimersByTime(STALE_MS * 5) // hidden long past the stale window (throttled-timer scenario)
    setHidden(false) // grace-refresh + immediate ping must run before any prune
    expect(getSnapshot().has('chat-1')).toBe(true)
    expect(channelOf().posted.filter(m => m.t === 'ping').length).toBe(2)
    deliver({ t: 'pong', id: 'chat-1' }) // popout answers the wake ping — stays alive
    vi.advanceTimersByTime(HEARTBEAT_MS)
    expect(getSnapshot().has('chat-1')).toBe(true)
    unsub()
  })
})
