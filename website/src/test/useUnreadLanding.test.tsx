// Feature: re-entering a crewmate chat lands on the last message the reader saw.
//
// The hook owns three decisions, each pinned here against a scroller the test
// lays out by hand (happy-dom has no layout, so the rects are stubbed):
//   1. the landing: the divider's live rect is placed below the scroller's own
//      top padding, once per slot entry, through a scrollTop write; a start
//      row outside the mounted window is pointed at first and landed on the
//      frame it mounts; a reader with a saved position of their own, or a
//      caught-up entry, is never landed -- not even by a message arriving
//      later in the visit;
//   2. the marker: saved only while the reader is at the bottom, and NOT while
//      a landing is still owed nor on the stale at-bottom value a landing write
//      has not yet corrected; a write the browser clamped, or one that stayed
//      inside the bottom band, does not block the save, since the transcript
//      may never report the reader off a bottom they never left;
//   3. the visit: the run is resolved once against the first transcript on
//      hand and held, so the divider keeps marking where the visit started
//      after the marker in storage moves on, and the pill drops its count once
//      the reader has read through to the bottom.
// The frames are driven by a hand-rolled requestAnimationFrame, never by a
// sleep, so every assertion reads a state the test itself advanced to.
import { renderHook, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MARKER_WALK_MAX_PAGES, UNREAD_LANDING_PAD_PX, displayIndexOfMessage, useUnreadLanding } from '../components/chat/useUnreadLanding'
import { UNREAD_DIVIDER_ATTR } from '../app-sdk/NewMessagesDivider'
import { loadSeenTail, saveSeenTail } from '../chat-core/transcript/caughtUpMarker'
import { ANCHOR_KEY_PREFIX } from '../hooks/virtualizer/ScrollAnchorCache'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

const msg = (ts: string, role = 'assistant'): ChatMessage => ({ role, content: ts, cls: '', ts })
const T = ['t0', 't1', 't2', 't3', 't4']
const SEEN = [msg(T[0], 'user'), msg(T[1])]
const WITH_NEW = [...SEEN, msg(T[2]), msg(T[3])]
const SLOT = 'member-radar'
const ANCHOR_SESSION = `pane:${SLOT}`

let frames: Array<{ id: number; cb: FrameRequestCallback }> = []
let nextFrameId = 1
let originalRaf: typeof requestAnimationFrame
let originalCancelRaf: typeof cancelAnimationFrame
const runFrame = () => {
  const due = frames.splice(0)
  for (const f of due) f.cb(16)
}

/** A scroller `clientHeight` tall over `scrollHeight` of content, parked at
 *  the bottom, whose divider sits `dividerTop` px below its own top edge at
 *  that parking position -- negative for a divider above the viewport, which
 *  is where an unseen run's start sits when the transcript opens at the end.
 *  Rects follow scrollTop; writes clamp like a browser. */
function makeScroller(dividerTop: number | null, { paddingTop = 0, scrollHeight = 3000, clientHeight = 500 } = {}) {
  const sc = document.createElement('div')
  sc.style.paddingTop = `${paddingTop}px`
  document.body.appendChild(sc)
  const max = Math.max(0, scrollHeight - clientHeight)
  let scrollTop = max
  Object.defineProperty(sc, 'scrollTop', { get: () => scrollTop, set: (v: number) => { scrollTop = Math.min(max, Math.max(0, v)) }, configurable: true })
  Object.defineProperty(sc, 'scrollHeight', { get: () => scrollHeight, configurable: true })
  Object.defineProperty(sc, 'clientHeight', { get: () => clientHeight, configurable: true })
  sc.getBoundingClientRect = () => ({ top: 100, left: 0, width: 600, height: clientHeight, bottom: 100 + clientHeight, right: 600, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
  // Rows above the divider re-pricing after the landing move it by `drift`.
  let drift = 0
  const addDivider = (at: number) => {
    const d = document.createElement('div')
    d.setAttribute(UNREAD_DIVIDER_ATTR, '')
    d.getBoundingClientRect = () => ({ top: 100 + at + drift - (scrollTop - max), left: 0, width: 600, height: 20, bottom: 0, right: 600, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    sc.appendChild(d)
  }
  if (dividerTop !== null) addDivider(dividerTop)
  return { sc, ref: { current: sc }, max, scrollTopNow: () => scrollTop, addDivider, driftDivider: (px: number) => { drift += px } }
}

beforeEach(() => {
  localStorage.clear()
  frames = []
  nextFrameId = 1
  originalRaf = globalThis.requestAnimationFrame
  originalCancelRaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { const id = nextFrameId++; frames.push({ id, cb }); return id }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => { frames = frames.filter(f => f.id !== id) }) as typeof cancelAnimationFrame
})
afterEach(() => {
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancelRaf
  document.body.replaceChildren()
})

const noRows = (): readonly DisplayItem[] => []
const NO_OLDER = { hasMore: false, loading: false, load: null }

function renderLanding(scroller: ReturnType<typeof makeScroller>, init: {
  messages?: readonly ChatMessage[]
  isAtBottom?: boolean
  enabled?: boolean
  getDisplayItems?: () => readonly DisplayItem[]
  mountIndex?: (i: number) => void
  restoreOwned?: () => boolean
  hydrated?: boolean
  shown?: boolean
  older?: { hasMore: boolean; loading: boolean; load: (() => void) | null }
  slotKey?: string
} = {}) {
  const slot = init.slotKey ?? SLOT
  const props = {
    enabled: init.enabled ?? true,
    slotKey: slot,
    anchorSessionId: `pane:${slot}`,
    hydrated: init.hydrated ?? true,
    older: init.older ?? NO_OLDER,
    shown: init.shown ?? true,
    messages: init.messages ?? WITH_NEW,
    isAtBottom: init.isAtBottom ?? true,
    scrollerRef: scroller.ref,
    getDisplayItems: init.getDisplayItems ?? noRows,
    mountIndex: init.mountIndex ?? (() => {}),
    restoreOwned: init.restoreOwned ?? (() => false),
  }
  const hook = renderHook((p: typeof props) => useUnreadLanding(p), { initialProps: props })
  return { ...hook, props }
}

describe('useUnreadLanding', () => {
  it('resolves the run from the marker the previous visit left', () => {
    saveSeenTail(SLOT, T[1])
    const { result } = renderLanding(makeScroller(-900))
    expect(result.current.unreadRun).toEqual({ startId: T[2], count: 2 })
    expect(result.current.unreadCount).toBe(2)
  })

  it('lands the divider below the scroller padding, once, and settles on the next frame', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-900, { paddingTop: 52 })
    const { rerender, props } = renderLanding(scroller)
    const target = 52 + UNREAD_LANDING_PAD_PX
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-900 - target))
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-900 - target))
    // A later append (the crewmate keeps talking) does not re-land the reader.
    rerender({ ...props, messages: [...WITH_NEW, msg(T[4])], isAtBottom: false })
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-900 - target))
  })

  it('points the window at a start row that is not mounted, then lands when it is', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    const mountIndex = vi.fn()
    const rows: DisplayItem[] = [
      { kind: 'single', msg: SEEN[0], idx: 0 },
      { kind: 'turn', items: [{ kind: 'single', msg: SEEN[1], idx: 1 }, { kind: 'single', msg: WITH_NEW[2], idx: 2 }], complete: true },
      { kind: 'single', msg: WITH_NEW[3], idx: 3 },
    ]
    renderLanding(scroller, { mountIndex, getDisplayItems: () => rows })
    expect(mountIndex).toHaveBeenCalledWith(1)
    expect(scroller.scrollTopNow()).toBe(scroller.max)
    // Owed, not landed: the entry's at-bottom value must not become the marker.
    expect(loadSeenTail(SLOT)).toBe(T[1])
    scroller.addDivider(-700)
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-700 - UNREAD_LANDING_PAD_PX))
  })

  it('gives up on a start row the list never mounts, without writing, then saves normally', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    const rows: DisplayItem[] = [{ kind: 'turn', items: [{ kind: 'single', msg: WITH_NEW[2], idx: 2 }], complete: true }]
    const { rerender, props } = renderLanding(scroller, { getDisplayItems: () => rows })
    for (let i = 0; i < 20; i++) act(runFrame)
    expect(scroller.scrollTopNow()).toBe(scroller.max)
    expect(frames).toHaveLength(0)
    // The wait is over: the reader is at the bottom of what the list shows.
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('does not save while the landing is owed, nor on the stale at-bottom value after it', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    const rows: DisplayItem[] = [{ kind: 'turn', items: [{ kind: 'single', msg: WITH_NEW[2], idx: 2 }], complete: true }]
    const { rerender, props } = renderLanding(scroller, { isAtBottom: true, getDisplayItems: () => rows })
    // The start row is being mounted: the landing is owed, and the entry's
    // at-bottom value is not the reader's, so nothing is saved.
    expect(loadSeenTail(SLOT)).toBe(T[1])
    scroller.addDivider(-900)
    act(runFrame)
    // Landed and moved off the bottom, but the host still says "at bottom":
    // that value is the entry's, so still nothing saved.
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    // The host reports the reader off the bottom, then they read down to it.
    rerender({ ...props, messages: [...WITH_NEW], isAtBottom: false })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, messages: [...WITH_NEW], isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('a settle write after the host reported off-bottom does not re-arm the wait', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-900)
    const { rerender, props } = renderLanding(scroller)
    // The host reports the landing's move before the settle frame runs.
    rerender({ ...props, isAtBottom: false })
    // Rows above re-priced: the settle frame corrects the divider's position.
    scroller.driftDivider(-120)
    const before = scroller.scrollTopNow()
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(before - 120)
    // Reading down to the bottom saves: the corrected write armed nothing.
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('a write the browser clamps does not hold the save hostage', () => {
    // The whole transcript fits: the scroller cannot move, so the host will
    // never report the reader off a bottom they never left.
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-300, { scrollHeight: 500, clientHeight: 500 })
    const { rerender, props } = renderLanding(scroller)
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(0)
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('a write that stays inside the bottom band saves too', () => {
    // One short new message: the divider is already near the top of the view,
    // so the landing moves the scroller a little and the reader is still at
    // the bottom by the follow policy's own threshold.
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(UNREAD_LANDING_PAD_PX - 40)
    const { rerender, props } = renderLanding(scroller)
    expect(scroller.scrollTopNow()).toBe(scroller.max - 40)
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('a caught-up entry is never landed, not even by a message arriving later', () => {
    saveSeenTail(SLOT, T[3])
    const scroller = makeScroller(null)
    const { result, rerender, props } = renderLanding(scroller, { messages: WITH_NEW })
    expect(result.current.unreadRun).toBeNull()
    // The reader scrolls into history; a reply arrives.
    scroller.sc.scrollTop = 400
    scroller.addDivider(900)
    rerender({ ...props, messages: [...WITH_NEW, msg(T[4])], isAtBottom: false })
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(400)
    expect(result.current.unreadRun).toBeNull()
  })

  it("the reader's own saved position outranks the landing; the divider still marks the run", () => {
    saveSeenTail(SLOT, T[1])
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}${ANCHOR_SESSION}`, JSON.stringify({ key: 'a-t0', top: -20 }))
    const scroller = makeScroller(-900)
    // The virtualizer put the reader back mid-history before this hook's
    // effects ran; the host's `isAtBottom` is still the entry's default.
    scroller.sc.scrollTop = 400
    const { result, rerender, props } = renderLanding(scroller, { isAtBottom: true })
    act(runFrame)
    expect(scroller.scrollTopNow()).toBe(400)
    expect(result.current.unreadRun).toEqual({ startId: T[2], count: 2 })
    // Nothing saved from a position that is not the bottom, whatever the prop says.
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    expect(result.current.unreadCount).toBe(2)
    // Reading down to the end saves.
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, messages: [...WITH_NEW], isAtBottom: false })
    rerender({ ...props, messages: [...WITH_NEW], isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe(T[3])
    expect(result.current.unreadCount).toBe(0)
  })

  it('saves nothing while the virtualizer still owns an anchored entry', () => {
    saveSeenTail(SLOT, T[1])
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}${ANCHOR_SESSION}`, JSON.stringify({ key: 'a-t0', top: -20 }))
    const scroller = makeScroller(null)
    let owned = true
    const { rerender, props } = renderLanding(scroller, { isAtBottom: true, restoreOwned: () => owned })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    // The restore gave up and pinned the bottom: the reader is there now.
    owned = false
    rerender({ ...props, messages: [...WITH_NEW] })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('holds the run for the visit and drops the count once read through', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-900)
    const { result, rerender, props } = renderLanding(scroller)
    act(runFrame)
    expect(result.current.unreadCount).toBe(2)
    // A reply during the visit is live, not unseen: the run does not grow.
    const grown = [...WITH_NEW, msg(T[4])]
    rerender({ ...props, messages: grown, isAtBottom: false })
    expect(result.current.unreadRun).toEqual({ startId: T[2], count: 2 })
    // Reaching the bottom saves the newest message and takes the count off
    // the pill; the divider's run stays where the visit started.
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, messages: grown, isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe(T[4])
    expect(result.current.unreadRun).toEqual({ startId: T[2], count: 2 })
    expect(result.current.unreadCount).toBe(0)
  })

  it('saves on a first visit with no marker and lands nothing', () => {
    const scroller = makeScroller(null)
    const { result } = renderLanding(scroller, { isAtBottom: true })
    expect(result.current.unreadRun).toBeNull()
    expect(scroller.scrollTopNow()).toBe(scroller.max)
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('waits for the hydrated transcript before deciding anything', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    // A live frame seeded one new reply before the history fetch landed: the
    // marker's row is not in this partial list, and it must not decide the visit.
    const { result, rerender, props } = renderLanding(scroller, { messages: [msg(T[3])], hydrated: false })
    expect(result.current.unreadRun).toBeNull()
    expect(loadSeenTail(SLOT)).toBe(T[1])
    scroller.addDivider(-900)
    rerender({ ...props, messages: WITH_NEW, hydrated: true })
    expect(result.current.unreadRun).toEqual({ startId: T[2], count: 2 })
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-900 - UNREAD_LANDING_PAD_PX))
  })

  it('a reply landing in a parked window is not read; the window coming back reads it', () => {
    // Caught up, then the window loses focus; the crewmate replies.
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    const { rerender, props } = renderLanding(scroller, { messages: SEEN, isAtBottom: true, shown: false })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    rerender({ ...props, messages: WITH_NEW })
    expect(loadSeenTail(SLOT)).toBe(T[1])
    // Focus returns with the reader still at the bottom: now it is read.
    rerender({ ...props, messages: WITH_NEW, shown: true })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('never saves a send that is still provisional; the last server row is the marker', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(null)
    const sent = { ...msg(T[4], 'user'), meta: { optimistic: true, sendId: 's1' } } as ChatMessage
    renderLanding(scroller, { messages: [...WITH_NEW, sent], isAtBottom: true })
    // The newest row with a server identity, not the optimistic bubble above it.
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('pages back for a marker beyond the hydrated page, then lands on it', () => {
    // A long absence: the marker's row is older than the first page.
    saveSeenTail(SLOT, T[0])
    const scroller = makeScroller(null)
    const load = vi.fn()
    const page1 = [msg(T[2]), msg(T[3])]
    const { result, rerender, props } = renderLanding(scroller, { messages: page1, older: { hasMore: true, loading: false, load } })
    expect(load).toHaveBeenCalledTimes(1)
    expect(result.current.unreadRun).toBeNull()
    // Nothing is saved while the row is being looked for.
    expect(loadSeenTail(SLOT)).toBe(T[0])
    // The older page lands with the marker's row: the run resolves and lands.
    scroller.addDivider(-900)
    rerender({ ...props, messages: [msg(T[0], 'user'), msg(T[1]), ...page1], older: { hasMore: true, loading: false, load } })
    expect(result.current.unreadRun).toEqual({ startId: T[1], count: 3 })
    expect(scroller.scrollTopNow()).toBe(scroller.max + (-900 - UNREAD_LANDING_PAD_PX))
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('a marker never found within the walk holds until the reader first reaches the bottom', () => {
    saveSeenTail(SLOT, 'long-ago')
    const scroller = makeScroller(null)
    const load = vi.fn()
    const older = { hasMore: true, loading: false, load }
    const { result, rerender, props } = renderLanding(scroller, { messages: WITH_NEW, older })
    for (let i = 1; i < MARKER_WALK_MAX_PAGES; i++) {
      rerender({ ...props, messages: [msg(`older-${i}`), ...WITH_NEW], older })
    }
    expect(load).toHaveBeenCalledTimes(MARKER_WALK_MAX_PAGES)
    rerender({ ...props, messages: [msg('older-x'), ...WITH_NEW], older })
    expect(result.current.unreadRun).toBeNull()
    // Walk spent, history still above: the entry's at-bottom is not a read.
    rerender({ ...props, messages: [msg('older-x'), ...WITH_NEW], older, isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe('long-ago')
    // The reader scrolls, then comes back to the bottom: they have seen the
    // newest row, so the unreachable marker is replaced and the slot recovers.
    rerender({ ...props, messages: [msg('older-x'), ...WITH_NEW], older, isAtBottom: false })
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, messages: [msg('older-x'), ...WITH_NEW], older, isAtBottom: true })
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('waits for the last requested page before deciding the run', () => {
    // The marker's row sits on the last page the walk may ask for.
    saveSeenTail(SLOT, T[0])
    const scroller = makeScroller(null)
    const load = vi.fn()
    let messages: ChatMessage[] = [msg(T[3])]
    const { result, rerender, props } = renderLanding(scroller, { messages, older: { hasMore: true, loading: false, load } })
    for (let i = 1; i < MARKER_WALK_MAX_PAGES; i++) {
      messages = [msg(`older-${i}`), ...messages]
      rerender({ ...props, messages, older: { hasMore: true, loading: false, load } })
    }
    expect(load).toHaveBeenCalledTimes(MARKER_WALK_MAX_PAGES)
    // The fourth page is in flight: no decision yet.
    rerender({ ...props, messages, older: { hasMore: true, loading: true, load } })
    expect(result.current.unreadRun).toBeNull()
    expect(loadSeenTail(SLOT)).toBe(T[0])
    // It lands with the marker's row: the run resolves against it.
    scroller.addDivider(-900)
    messages = [msg(T[0], 'user'), msg(T[1]), ...messages]
    rerender({ ...props, messages, older: { hasMore: true, loading: false, load } })
    expect(result.current.unreadRun).toEqual({ startId: T[1], count: messages.length - 1 })
    expect(load).toHaveBeenCalledTimes(MARKER_WALK_MAX_PAGES)
  })

  it('a marker missing with no older history is gone: the default placement, and saving resumes', () => {
    saveSeenTail(SLOT, 'long-ago')
    const scroller = makeScroller(null)
    const { result } = renderLanding(scroller, { messages: WITH_NEW, isAtBottom: true })
    expect(result.current.unreadRun).toBeNull()
    expect(loadSeenTail(SLOT)).toBe(T[3])
  })

  it('does nothing when disabled (a split pane keeps the default placement)', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-900)
    const { result } = renderLanding(scroller, { enabled: false })
    expect(result.current.unreadRun).toBeNull()
    expect(scroller.scrollTopNow()).toBe(scroller.max)
    expect(loadSeenTail(SLOT)).toBe(T[1])
  })

  it("re-arms for the next slot: a switch reads that slot's own marker", () => {
    saveSeenTail(SLOT, T[1])
    saveSeenTail('member-scout', T[2])
    const scroller = makeScroller(-900)
    const { result, rerender, props } = renderLanding(scroller)
    act(runFrame)
    // The next slot's transcript opens at its own end.
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, slotKey: 'member-scout', anchorSessionId: 'pane:member-scout', messages: WITH_NEW })
    expect(result.current.unreadRun).toEqual({ startId: T[3], count: 1 })
    expect(result.current.unreadCount).toBe(1)
  })

  it('a later visit to the same slot starts with the count on again', () => {
    saveSeenTail(SLOT, T[1])
    const scroller = makeScroller(-900)
    const { result, rerender, props } = renderLanding(scroller)
    act(runFrame)
    // Read through: the count drops.
    rerender({ ...props, isAtBottom: false })
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, isAtBottom: true })
    expect(result.current.unreadCount).toBe(0)
    expect(loadSeenTail(SLOT)).toBe(T[3])
    // Away to another crewmate with nothing new; the first one keeps talking.
    rerender({ ...props, slotKey: 'member-scout', anchorSessionId: 'pane:member-scout', messages: WITH_NEW })
    // Back: a fresh visit with a fresh run, counted from the marker just saved.
    scroller.sc.scrollTop = scroller.max
    rerender({ ...props, slotKey: SLOT, anchorSessionId: ANCHOR_SESSION, messages: [...WITH_NEW, msg(T[4])] })
    expect(result.current.unreadRun).toEqual({ startId: T[4], count: 1 })
    expect(result.current.unreadCount).toBe(1)
  })
})

describe('displayIndexOfMessage', () => {
  it('finds a message folded into a turn through the turn', () => {
    const rows: DisplayItem[] = [
      { kind: 'single', msg: msg('a', 'user'), idx: 0 },
      { kind: 'turn', items: [{ kind: 'single', msg: msg('b'), idx: 1 }, { kind: 'group', msgs: [msg('c'), msg('d')], startIdx: 2 }], complete: true },
    ]
    expect(displayIndexOfMessage(rows, 'a')).toBe(0)
    expect(displayIndexOfMessage(rows, 'd')).toBe(1)
    expect(displayIndexOfMessage(rows, 'zz')).toBe(-1)
  })
})
