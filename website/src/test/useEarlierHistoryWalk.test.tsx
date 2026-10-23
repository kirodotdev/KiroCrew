/**
 * The automatic older-history walk a `VirtualTranscript` host gets from its
 * `earlier` model (crewmate DM pane first: "scrolling up needs a click per
 * page" was the defect). Each test is one door the walk must hold shut, or the
 * one case it must open: the gates are the main page's, so the fixture drives
 * the same signals that page reads -- real input on the scroller, settle time,
 * follow state, bar visibility, row measurement, transcript height.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { useRef, type MutableRefObject } from 'react'
import { useEarlierHistoryWalk, type EarlierWalkSource } from '../chat-core/transcript/useEarlierHistoryWalk'
import {
  OLDER_TOP_POLL_MS, OLDER_LANDING_SETTLE_MS, OLDER_WALK_ACTIVE_MS, OLDER_WALK_MAX_PAGES_PER_INPUT, OLDER_FILL_SLACK_PX, EARLIER_BAR_SELECTOR,
} from '../pages/chat/pagination'

const VIEWPORT = 600

interface Geometry { scrollHeight: number; scrollTop: number; barInView: boolean }

function Harness({ sessionId, earlier, geom, follow, measured, mountedIndices, itemCount, elRef }: {
  sessionId: string
  earlier: EarlierWalkSource | undefined
  geom: Geometry
  follow: boolean
  measured: (index: number) => boolean
  mountedIndices: readonly number[]
  itemCount: number
  elRef: MutableRefObject<HTMLDivElement | null>
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  // jsdom has no layout: the scroller's geometry is pinned on the element so
  // the hook reads the numbers the test names.
  const bind = (el: HTMLDivElement | null) => {
    ref.current = el
    elRef.current = el
    if (!el) return
    Object.defineProperty(el, 'clientHeight', { value: VIEWPORT, configurable: true })
    Object.defineProperty(el, 'scrollHeight', { value: geom.scrollHeight, configurable: true })
    Object.defineProperty(el, 'scrollTop', { value: geom.scrollTop, configurable: true, writable: true })
    el.getBoundingClientRect = () => ({ top: 0, bottom: VIEWPORT, left: 0, right: 400, width: 400, height: VIEWPORT, x: 0, y: 0, toJSON: () => ({}) })
    const bar = el.querySelector(EARLIER_BAR_SELECTOR) as HTMLElement | null
    if (bar) {
      const top = geom.barInView ? 10 : -5000
      bar.getBoundingClientRect = () => ({ top, bottom: top + 30, left: 0, right: 400, width: 400, height: 30, x: 0, y: top, toJSON: () => ({}) })
    }
  }
  useEarlierHistoryWalk({
    scrollerRef: ref,
    sessionId,
    earlier,
    getFollow: () => follow,
    getMountedIndices: () => mountedIndices,
    farmIsMeasured: measured,
    itemCount,
  })
  return (
    <div ref={bind}>
      {earlier?.hasMore && <button type="button" data-testid="load-earlier-messages">more</button>}
    </div>
  )
}

const TALL: Geometry = { scrollHeight: VIEWPORT * 10, scrollTop: 0, barInView: true }

function mount(over: Partial<Parameters<typeof Harness>[0]> = {}) {
  const onLoad = vi.fn()
  const elRef: MutableRefObject<HTMLDivElement | null> = { current: null }
  const props = {
    sessionId: 'pane:thread-a',
    earlier: { hasMore: true, loading: false, failed: false, onLoad } as EarlierWalkSource,
    geom: TALL, follow: false, measured: () => true, mountedIndices: [0, 1, 2], itemCount: 12, elRef,
    ...over,
  }
  const view = render(<Harness {...props} />)
  const rerender = (next: Partial<typeof props>) => view.rerender(<Harness {...props} {...next} />)
  return { onLoad, elRef, rerender, props }
}

/** A real gesture on the scroller, then enough quiet for a landing but not so
 *  much that the reader has "stopped climbing". */
function gestureAndSettle(el: HTMLElement) {
  act(() => {
    el.dispatchEvent(new Event('wheel'))
    el.dispatchEvent(new Event('scroll'))
  })
  act(() => { vi.advanceTimersByTime(OLDER_LANDING_SETTLE_MS) })
}

function ticks(n = 1) {
  act(() => { vi.advanceTimersByTime(OLDER_TOP_POLL_MS * n) })
}

afterEach(() => { vi.useRealTimers() })

describe('useEarlierHistoryWalk', () => {
  it('pages once the reader has climbed to the bar and the scroller has settled', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount()
    gestureAndSettle(elRef.current!)
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  it('never pages a reader who has not touched the scroller', () => {
    // A parked reader after a refresh: the bar is in view and the top is near,
    // but no wheel/touch/key/pointer ever happened. This is the door that once
    // walked a whole archive over a phone.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad } = mount()
    ticks(20)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('requires a new gesture after switching sessions on the same scroller', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef, rerender, props } = mount()
    const el = elRef.current!
    gestureAndSettle(el)
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(1)

    const loadThreadB = vi.fn()
    rerender({ sessionId: 'pane:thread-b', earlier: { ...props.earlier, onLoad: loadThreadB } })
    expect(elRef.current).toBe(el)
    // Restoring the incoming thread can scroll, but carries no reader intent.
    act(() => { el.dispatchEvent(new Event('scroll')) })
    ticks()
    expect(loadThreadB).not.toHaveBeenCalled()
    expect(onLoad).toHaveBeenCalledTimes(1)

    gestureAndSettle(el)
    ticks()
    expect(loadThreadB).toHaveBeenCalledTimes(1)
  })

  it('never pages a bottom-followed reader', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount({ follow: true })
    gestureAndSettle(elRef.current!)
    ticks(3)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('waits for the bar to be on screen', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount({ geom: { ...TALL, scrollTop: 300, barInView: false } })
    gestureAndSettle(elRef.current!)
    ticks(3)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('does not land a page while the scroller is still moving', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount()
    const el = elRef.current!
    act(() => { el.dispatchEvent(new Event('wheel')) })
    // Momentum: a scroll event every poll, never OLDER_LANDING_SETTLE_MS of quiet.
    for (let i = 0; i < 4; i++) {
      act(() => { el.dispatchEvent(new Event('scroll')) })
      act(() => { vi.advanceTimersByTime(OLDER_LANDING_SETTLE_MS - 50) })
    }
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('stops once the reader has stopped climbing', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount()
    const el = elRef.current!
    act(() => { el.dispatchEvent(new Event('wheel')); el.dispatchEvent(new Event('scroll')) })
    // Let the clock run past the active window without a poll firing in it, so
    // the first poll the walk gets sees a reader who let go a while ago.
    vi.setSystemTime(Date.now() + OLDER_WALK_ACTIVE_MS + 1)
    ticks(3)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('spends a bounded page budget per gesture, refilled by the next gesture', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef, rerender, props } = mount()
    const el = elRef.current!
    gestureAndSettle(el)
    // Each landing: the host flips loading on, then off, and the prepend's
    // compensation fires a scroll event (no wheel: that is our write, not the
    // reader's). The walk keeps going -- the landing postdates the gesture --
    // until the budget is spent.
    for (let i = 0; i < OLDER_WALK_MAX_PAGES_PER_INPUT + 3; i++) {
      ticks()
      rerender({ earlier: { ...props.earlier, loading: true } })
      ticks()
      rerender({ earlier: { ...props.earlier, loading: false } })
      act(() => { el.dispatchEvent(new Event('scroll')) })
      act(() => { vi.advanceTimersByTime(OLDER_LANDING_SETTLE_MS) })
    }
    expect(onLoad).toHaveBeenCalledTimes(OLDER_WALK_MAX_PAGES_PER_INPUT)
    // A fresh gesture refills the budget: more pages, and at most one more
    // budget's worth of them.
    gestureAndSettle(el)
    ticks(2)
    expect(onLoad.mock.calls.length).toBeGreaterThan(OLDER_WALK_MAX_PAGES_PER_INPUT)
    expect(onLoad.mock.calls.length).toBeLessThanOrEqual(OLDER_WALK_MAX_PAGES_PER_INPUT * 2)
  })

  it('does not fetch while a page is in flight or after one failed', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef, rerender, props } = mount({ earlier: { hasMore: true, loading: true, failed: false, onLoad: vi.fn() } })
    const el = elRef.current!
    gestureAndSettle(el)
    ticks(2)
    expect(props.earlier.onLoad).not.toHaveBeenCalled()
    // The failed page parks the walk on the bar's retry.
    rerender({ earlier: { ...props.earlier, loading: false, failed: true } })
    gestureAndSettle(el)
    ticks(2)
    expect(props.earlier.onLoad).not.toHaveBeenCalled()
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('waits for every mounted row to be measured', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef, rerender } = mount({ measured: index => index !== 2 })
    gestureAndSettle(elRef.current!)
    ticks(3)
    expect(onLoad).not.toHaveBeenCalled()

    rerender({ measured: () => true })
    gestureAndSettle(elRef.current!)
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  it('pages when mounted rows are measured even if an unmounted row is not', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const measured = vi.fn((index: number) => index !== 6)
    const { onLoad, elRef } = mount({ measured, mountedIndices: [0, 1, 2] })
    gestureAndSettle(elRef.current!)
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(1)
    expect(measured.mock.calls).toEqual([[0], [1], [2]])
  })

  it('fills a transcript too short to scroll without a gesture, but never an empty one', () => {
    // A patroller's thread filters down to a few bubbles: no scrollbar exists,
    // so nothing else could ever load its history. An EMPTY list is "not
    // loaded yet", not "short", and must stay shut.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const short: Geometry = { scrollHeight: VIEWPORT + OLDER_FILL_SLACK_PX - 1, scrollTop: 0, barInView: true }
    const empty = mount({ geom: short, itemCount: 0, mountedIndices: [] })
    ticks(3)
    expect(empty.onLoad).not.toHaveBeenCalled()
    const few = mount({ geom: short, itemCount: 3 })
    ticks()
    expect(few.onLoad).toHaveBeenCalledTimes(1)
  })

  it('bounds the no-gesture fill when landed pages leave the transcript just as short', () => {
    // A crewmate DM filters a page of patrol rows down to nothing: the page
    // lands, the transcript is exactly as short, and geometry alone would page
    // the whole history with nobody reading. The fill spends one budget, and a
    // gesture on the scroller that cannot scroll buys the next.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const short: Geometry = { scrollHeight: VIEWPORT + OLDER_FILL_SLACK_PX - 1, scrollTop: 0, barInView: true }
    const { onLoad, elRef, rerender, props } = mount({ geom: short, itemCount: 3 })
    for (let i = 0; i < OLDER_WALK_MAX_PAGES_PER_INPUT + 3; i++) {
      ticks()
      rerender({ earlier: { ...props.earlier, loading: true } })
      ticks()
      rerender({ earlier: { ...props.earlier, loading: false } })
    }
    expect(onLoad).toHaveBeenCalledTimes(OLDER_WALK_MAX_PAGES_PER_INPUT)
    act(() => { elRef.current!.dispatchEvent(new Event('wheel')) })
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(OLDER_WALK_MAX_PAGES_PER_INPUT + 1)
  })

  it('counts a wheel against the top edge as activity even though nothing scrolls', () => {
    // Parked at scrollTop 0, an upward wheel fires no scroll event. The reader
    // is still asking for history, so the active window reads real input too.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const { onLoad, elRef } = mount()
    const el = elRef.current!
    gestureAndSettle(el)
    vi.setSystemTime(Date.now() + OLDER_WALK_ACTIVE_MS + 1)
    ticks(2)
    expect(onLoad).not.toHaveBeenCalled()
    act(() => { el.dispatchEvent(new Event('wheel')) })
    ticks()
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  it('is disarmed with no paging model or no more history', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-23T00:00:00Z'))
    const none = mount({ earlier: undefined })
    const done = mount({ earlier: { hasMore: false, loading: false, failed: false, onLoad: vi.fn() } })
    for (const { elRef } of [none, done]) {
      const el = elRef.current!
      act(() => { el.dispatchEvent(new Event('wheel')) })
    }
    ticks(3)
    expect(none.onLoad).not.toHaveBeenCalled()
    expect(done.props.earlier?.onLoad).not.toHaveBeenCalled()
  })
})
