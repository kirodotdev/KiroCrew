/**
 * The doors that page older history into the transcript
 * (pages/chat/page/transcriptPaging.ts), driven directly against a stub
 * virtualizer and a scroller whose geometry the test sets: the top sentinel,
 * the earlier-messages bar, the level-triggered walk poll and the retry on a
 * failed page. Every automatic door needs the bar in view, measured rows and a
 * recent real gesture; a short transcript is the one fill that needs no gesture.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

const h = vi.hoisted(() => ({
  chat: {} as Record<string, unknown>,
  virtOpts: null as null | { onTopReached?: () => void },
  virt: null as null | Record<string, unknown>,
}))
vi.mock('../store', async (orig) => ({
  ...(await orig<typeof import('../store')>()),
  store: { getState: () => ({ chat: h.chat }), dispatch: vi.fn(), subscribe: vi.fn() },
}))
vi.mock('../store/chatSlice', async (orig) => ({
  ...(await orig<typeof import('../store/chatSlice')>()),
  loadOlderMessages: vi.fn(() => 'LOAD_OLDER'),
  abortActiveOlderFetch: vi.fn(),
}))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { onTopReached?: () => void }) => { h.virtOpts = opts; return h.virt },
}))

import { abortActiveOlderFetch } from '../store/chatSlice'
import { useTranscriptPaging, useTranscriptScrollRefs } from './chat/page/transcriptPaging'
import { slashRefusal } from './chat/page/turnActions'

type Props = Parameters<typeof useTranscriptPaging>[0]

const T0 = new Date('2026-03-01T12:00:00Z')
/** How long one real gesture authorizes automatic paging (REAL_GESTURE_AUTH_MS in the owner). */
const GESTURE_WINDOW_MS = 20_000

/** A scroller of the given geometry holding the earlier-messages bar at `barTop`. */
function scroller({ clientHeight = 500, scrollHeight = 5000, barTop = 0 } = {}) {
  const el = document.createElement('div')
  const bar = document.createElement('div')
  bar.setAttribute('data-testid', 'load-earlier-messages')
  el.appendChild(bar)
  let top = 0
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
  el.getBoundingClientRect = () => ({ top: 0, bottom: clientHeight, left: 0, right: 0, width: 0, height: clientHeight, x: 0, y: 0, toJSON: () => ({}) })
  bar.getBoundingClientRect = () => ({ top: barTop, bottom: barTop + 24, left: 0, right: 0, width: 0, height: 24, x: 0, y: barTop, toJSON: () => ({}) })
  document.body.appendChild(el)
  return el
}

function props(el: HTMLElement, over: Partial<Props> = {}): Props {
  return {
    dispatch: vi.fn() as never, activeSlot: 's1', slotRunning: false, slotHasMore: true,
    scrollerRef: { current: el as HTMLDivElement }, canMeasure: () => true, scrollerWidthBucket: 1,
    vGetFollowRef: { current: () => true }, vScrollerElRef: { current: el }, lastRealInputAtRef: { current: 0 },
    sentinelPagesSinceInputRef: { current: 0 }, walkPagesSinceInputRef: { current: 0 }, walkLastInputAtRef: { current: Number.NEGATIVE_INFINITY },
    vScrollToBottomRef: { current: () => {} }, vFarmIsMeasuredRef: { current: () => true }, earlierBarInViewRef: { current: () => false },
    mountIndexRef: { current: () => false }, estimateRowTopRef: { current: () => null }, displayItemsRef: { current: [{ kind: 'single' } as never] },
    setPinExpanded: vi.fn(), setPinned: vi.fn(), updatePinnedPrompt: vi.fn(), isStreaming: false, setRefusedPress: vi.fn(),
    renderedDisplayItems: [], anchorAltId: () => '', prefetchStartIndex: undefined, stableAnchorId: () => '', virtualKey: (_it, i) => String(i),
    ...over,
  }
}

function mount(el: HTMLElement, over: Partial<Props> = {}, { follow = false, measured = true } = {}) {
  h.virt = {
    scrollerRef: { current: el }, getFollow: () => follow, scrollToBottom: vi.fn(), farmIsMeasured: () => measured,
    mountIndex: () => false, estimateRowTop: () => null, isAtBottom: true, virtualItems: [], retainRange: vi.fn(),
  }
  const p = props(el, over)
  const r = renderHook(() => useTranscriptPaging(p))
  return { ...r, p }
}

const loads = (p: Props) => vi.mocked(p.dispatch).mock.calls.filter(([a]) => a === 'LOAD_OLDER').length

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', 'Date', 'requestAnimationFrame', 'cancelAnimationFrame'] })
  vi.setSystemTime(T0)
  h.chat = { loadingOlder: false, slotHasMore: true, slotOlderError: null, slotCursorKey: 's1', activeSlot: 's1' }
  vi.mocked(abortActiveOlderFetch).mockClear()
})
afterEach(() => {
  vi.useRealTimers()
  document.body.replaceChildren()
})

describe('the top sentinel (onTopReached)', () => {
  const reach = () => act(() => { h.virtOpts!.onTopReached!() })

  it('fills a transcript too short to scroll without any gesture', () => {
    const { p } = mount(scroller({ scrollHeight: 520 }))
    reach()
    expect(loads(p)).toBe(1)
  })

  it('refuses while a page is in flight, with the bar off screen, on an empty transcript, and on unmeasured rows', () => {
    h.chat.loadingOlder = true
    const a = mount(scroller({ scrollHeight: 520 }))
    reach()
    expect(loads(a.p)).toBe(0)
    h.chat.loadingOlder = false
    const b = mount(scroller({ scrollHeight: 520, barTop: -5000 }))
    reach()
    expect(loads(b.p)).toBe(0)
    const c = mount(scroller({ scrollHeight: 520 }), { displayItemsRef: { current: [] } })
    reach()
    expect(loads(c.p)).toBe(0)
    const d = mount(scroller({ scrollHeight: 520 }), {}, { measured: false })
    reach()
    expect(loads(d.p)).toBe(0)
  })

  it('pages a scrollable transcript only inside a recent gesture, and only a bounded run per gesture', () => {
    const { p } = mount(scroller())
    reach()
    expect(loads(p)).toBe(0)
    p.lastRealInputAtRef.current = Date.now()
    reach(); reach(); reach()
    expect(loads(p)).toBe(2)
    expect(p.sentinelPagesSinceInputRef.current).toBe(2)
    vi.setSystemTime(T0.getTime() + GESTURE_WINDOW_MS + 1)
    p.sentinelPagesSinceInputRef.current = 0
    reach()
    expect(loads(p)).toBe(2)
  })
})

describe('the earlier-messages bar', () => {
  it('pages on a click unless a page is already in flight', () => {
    const { result, p } = mount(scroller())
    act(() => { result.current.handleLoadEarlier() })
    expect(loads(p)).toBe(1)
    h.chat.loadingOlder = true
    act(() => { result.current.handleLoadEarlier() })
    expect(loads(p)).toBe(1)
  })
})

describe('the walk poll', () => {
  const climb = (el: HTMLElement) => {
    el.dispatchEvent(new Event('wheel'))
    el.dispatchEvent(new Event('scroll'))
  }

  it('pages a reader climbing at the top, once the scroller has settled', () => {
    const el = scroller()
    const { p } = mount(el)
    climb(el)
    expect(abortActiveOlderFetch).toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(700) })
    expect(loads(p)).toBe(1)
  })

  it('stops once the reader stops climbing, even with page budget left', () => {
    const el = scroller()
    const { p } = mount(el)
    climb(el)
    act(() => { vi.advanceTimersByTime(2100) })
    // One page per settled tick while the climb is recent, capped by the per-gesture budget.
    expect(loads(p)).toBe(2)
    p.walkPagesSinceInputRef.current = 0
    act(() => { vi.advanceTimersByTime(2100) })
    expect(loads(p)).toBe(2)
  })

  it('never pages a bottom-followed reader, a failed history, or another chat cursor', () => {
    const a = scroller()
    const followed = mount(a, {}, { follow: true })
    climb(a)
    act(() => { vi.advanceTimersByTime(700) })
    expect(loads(followed.p)).toBe(0)
    followed.unmount()

    h.chat.slotOlderError = 'boom'
    const b = scroller()
    const failed = mount(b)
    climb(b)
    act(() => { vi.advanceTimersByTime(700) })
    expect(loads(failed.p)).toBe(0)
    failed.unmount()

    h.chat.slotOlderError = null
    h.chat.slotCursorKey = 'other'
    const c = scroller()
    const switching = mount(c)
    climb(c)
    act(() => { vi.advanceTimersByTime(700) })
    expect(loads(switching.p)).toBe(0)
  })

  it('needs a real gesture: scrolling alone is not a request for history', () => {
    const el = scroller()
    const { p } = mount(el)
    el.dispatchEvent(new Event('scroll'))
    act(() => { vi.advanceTimersByTime(700) })
    expect(loads(p)).toBe(0)
  })
})

describe('the retry on a failed page', () => {
  it('treats a further upward scroll as retry intent, one request per cooldown', () => {
    h.chat.slotOlderError = 'boom'
    const el = scroller()
    const { p } = mount(el)
    el.scrollTop = 300
    el.dispatchEvent(new Event('scroll'))
    el.scrollTop = 200
    el.dispatchEvent(new Event('scroll'))
    expect(loads(p)).toBe(1)
    el.scrollTop = 100
    el.dispatchEvent(new Event('scroll'))
    expect(loads(p)).toBe(1)
    vi.setSystemTime(T0.getTime() + 1500)
    el.scrollTop = 50
    el.dispatchEvent(new Event('scroll'))
    expect(loads(p)).toBe(2)
  })

  it('does not retry on a downward scroll or without a failure', () => {
    const el = scroller()
    const { p } = mount(el)
    el.scrollTop = 100
    el.dispatchEvent(new Event('scroll'))
    el.scrollTop = 50
    el.dispatchEvent(new Event('scroll'))
    expect(loads(p)).toBe(0)
  })
})

describe('slot entry and the slash refusal report', () => {
  it('resets the pinned prompt on a slot switch and reports a refused slash command', () => {
    const { result, p } = mount(scroller())
    expect(p.setPinned).toHaveBeenCalledWith(null)
    expect(p.setPinExpanded).toHaveBeenCalledWith(false)
    result.current.reportSlashRefusal({ stage: 'agent', error: 'no such agent' })
    expect(p.setRefusedPress).toHaveBeenCalledWith(slashRefusal({ stage: 'agent', error: 'no such agent' }))
  })

  it('never lets a new session inherit the last one gesture authorization', () => {
    const { result, rerender } = renderHook(({ activeSlot }: { activeSlot: string | null }) => useTranscriptScrollRefs({ activeSlot }), {
      initialProps: { activeSlot: 'a' as string | null },
    })
    result.current.lastRealInputAtRef.current = 123
    result.current.walkPagesSinceInputRef.current = 2
    result.current.sentinelPagesSinceInputRef.current = 2
    result.current.walkLastInputAtRef.current = 456
    rerender({ activeSlot: 'b' })
    expect(result.current.lastRealInputAtRef.current).toBe(0)
    expect(result.current.walkPagesSinceInputRef.current).toBe(0)
    expect(result.current.sentinelPagesSinceInputRef.current).toBe(0)
    expect(result.current.walkLastInputAtRef.current).toBe(Number.NEGATIVE_INFINITY)
  })
})
