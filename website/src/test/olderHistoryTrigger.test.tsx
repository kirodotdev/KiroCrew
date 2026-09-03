/**
 * Wiring for the "load older history" trigger, tested where each layer can be
 * observed.
 *
 * The virtualizer calls `onTopReached` when the top sentinel comes into view, and
 * that callback — composed with the real gate and the real thunk — fetches when
 * the server reported more history and stays quiet when it did not. The page's
 * own wiring is asserted against its source instead: the page renders its list
 * through this virtualizer, which mounts an empty window with no layout engine,
 * so a full-page render never produces a sentinel to intersect. That matches the
 * convention the other page-level wiring tests already follow.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import type { RefObject } from 'react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { createTestStore } from './helpers'
import { loadOlderMessages, resumeFromHistory } from '../store/chatSlice'
import { shouldPaginateOlder } from '../pages/chat/pagination'
import { shouldAutoFillOlder } from '../pages/ChatPage'
import { api } from '../api/client'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `i${i}` }))

// A paged resume ships the last RESUME_RAW raw rows of TOTAL, so the cursor arms
// at the oldest raw index it loaded, not at the total.
const TOTAL = 240
const RESUME_RAW = 200
const OLDEST = TOTAL - RESUME_RAW

function Harness({ onTopReached, scrollerRef, items }: {
  onTopReached: () => void
  scrollerRef: RefObject<HTMLDivElement | null>
  items?: Item[]
}) {
  const v = useVirtualChat<Item>({
    items: items ?? mkItems(30), getKey, sessionId: 'older-history', overscan: 2,
    externalScrollerRef: scrollerRef, onTopReached,
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      {v.virtualItems.map((it) => (
        <div key={it.key} data-index={it.index} ref={v.measureRef(it.index)} />
      ))}
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

interface FakeIOInst { cb: IntersectionObserverCallback; opts?: IntersectionObserverInit; observed: Element[] }

/** Replace IntersectionObserver with one whose callbacks the test fires by hand.
 *  Records the constructor options and observed targets too, so a test can ask
 *  which sentinel an observer watches and with how much lead. */
function installFakeIO() {
  const instances: FakeIOInst[] = []
  class FakeIO {
    cb: IntersectionObserverCallback
    opts?: IntersectionObserverInit
    observed: Element[] = []
    constructor(cb: IntersectionObserverCallback, opts?: IntersectionObserverInit) {
      this.cb = cb; this.opts = opts; instances.push(this)
    }
    observe(el: Element) { this.observed.push(el) }
    unobserve() {}
    disconnect() {}
    takeRecords() { return [] }
    root: Element | null = null
    rootMargin = ''
    thresholds: number[] = []
  }
  const original = globalThis.IntersectionObserver
  globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver
  return { instances, restore: () => { globalThis.IntersectionObserver = original } }
}

function fireIntersection(inst: FakeIOInst, target: HTMLElement) {
  act(() => {
    inst.cb(
      [{ isIntersecting: true, target } as unknown as IntersectionObserverEntry],
      inst as unknown as IntersectionObserver,
    )
  })
}

describe('older-history trigger — virtualizer callback', () => {
  it('prefetches on the row-lead crossing, without the sentinel, and cannot self-loop', () => {
    // The user-stated contract: start the fetch while ~two user turns still
    // sit above the window, so a landing's remount churn stays off-screen
    // (the hated blank-to-background flash happens when the reader is parked
    // ON the seam). The trigger is the DOWNWARD CROSSING of a row count —
    // pixels were tried and self-oscillated. Driven here by scroll recompute
    // alone: the sentinel never fires, so every call is the prefetch's.
    const { restore } = installFakeIO()
    const onTopReached = vi.fn()
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    // The scroll-driven window recompute is rAF-coalesced; run frames inline
    // so each dispatched scroll settles synchronously.
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    // Sentinel-free harness ON PURPOSE: with zero-layout (jsdom) sentinels
    // mounted, the window recompute derives position from their rects — all
    // 0 — and start pins at 0, so no crossing can be exercised. The sentinel
    // interplay is covered by the tests above; this one isolates the
    // scroll-driven crossing + anti-loop.
    function LeanHarness({ items }: { items?: Item[] }) {
      const v = useVirtualChat<Item>({
        items: items ?? mkItems(30), getKey, sessionId: 'older-history-lean', overscan: 2,
        externalScrollerRef: scrollerRef, onTopReached,
      })
      return (
        <div ref={scrollerRef as RefObject<HTMLDivElement>}>
          {v.virtualItems.map((it) => (
            <div key={it.key} data-index={it.index} ref={v.measureRef(it.index)} />
          ))}
        </div>
      )
    }
    try {
      const view = render(<LeanHarness />)
      const el = scrollerRef.current!
      let scrollTop = 0
      Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } })
      Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 400 })
      Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 2400 })
      expect(onTopReached).not.toHaveBeenCalled()

      // Park mid-transcript: start recomputes ABOVE the lead. No crossing yet.
      act(() => { scrollTop = 1600; el.dispatchEvent(new Event('scroll')) })
      expect(onTopReached).not.toHaveBeenCalled()

      // Travel upward past the lead: the crossing fires the prefetch — once.
      act(() => { scrollTop = 100; el.dispatchEvent(new Event('scroll')) })
      expect(onTopReached).toHaveBeenCalledTimes(1)

      // Lingering inside the lead does not re-fire (crossing, not presence).
      act(() => { scrollTop = 60; el.dispatchEvent(new Event('scroll')) })
      expect(onTopReached).toHaveBeenCalledTimes(1)

      // Anti-loop: the page lands (indices +100, window re-based far away);
      // the landing itself must not fire again.
      act(() => {
        view.rerender(
          <LeanHarness items={[...mkItems(100, 'older'), ...mkItems(30)]} />,
        )
      })
      expect(onTopReached).toHaveBeenCalledTimes(1)
    } finally {
      globalThis.requestAnimationFrame = origRaf
      restore()
    }
  })

  it('keeps the history-fetch lead below a page\'s rendered height', () => {
    // The top sentinel starts a NETWORK fetch; the bottom one only expands the
    // mounted window over rows already in memory. Separate observers let their
    // leads differ — but the fetch lead has a CEILING, learned the hard way: it
    // was raised to 1500px to hide fetch latency, and because tool-call grouping
    // can collapse a 100-message page into a few hundred px of display rows, the
    // sentinel stayed inside the margin after every insert. `shouldPaginateOlder`
    // gates concurrency, not recurrence, so page after page fired serially —
    // "loads nonstop near Load-earlier" on a real phone, each landing with its
    // own estimate-to-real settle under the reader. The margin must stay below
    // the height a typical page renders at, or pagination self-oscillates.
    const { instances, restore } = installFakeIO()
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    try {
      const { container } = render(<Harness onTopReached={() => {}} scrollerRef={scrollerRef} />)
      const top = container.querySelector('[data-sentinel="top"]') as HTMLElement
      const bottom = container.querySelector('[data-sentinel="bottom"]') as HTMLElement
      const px = (i?: FakeIOInst) => Number.parseInt(String(i?.opts?.rootMargin ?? '0'), 10)

      const topIO = instances.find(i => i.observed.includes(top))
      const bottomIO = instances.find(i => i.observed.includes(bottom))
      expect(topIO).toBeTruthy()
      expect(bottomIO).toBeTruthy()
      expect(topIO).not.toBe(bottomIO)
      // Ceiling: a short grouped page must still escape the margin when it
      // lands, or the next fetch fires immediately and the loop never ends.
      expect(px(topIO)).toBeGreaterThan(0)
      expect(px(topIO)).toBeLessThanOrEqual(400)
    } finally {
      restore()
    }
  })

  it('calls onTopReached when the top sentinel comes into view', () => {
    const { instances, restore } = installFakeIO()
    const onTopReached = vi.fn()
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    try {
      const { container } = render(<Harness onTopReached={onTopReached} scrollerRef={scrollerRef} />)
      const top = container.querySelector('[data-sentinel="top"]') as HTMLElement
      expect(onTopReached).not.toHaveBeenCalled()
      fireIntersection(instances[0], top)
      expect(onTopReached).toHaveBeenCalledTimes(1)
    } finally {
      restore()
    }
  })

  it('does not call onTopReached for the bottom sentinel', () => {
    const { instances, restore } = installFakeIO()
    const onTopReached = vi.fn()
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    try {
      const { container } = render(<Harness onTopReached={onTopReached} scrollerRef={scrollerRef} />)
      const bottom = container.querySelector('[data-sentinel="bottom"]') as HTMLElement
      fireIntersection(instances[0], bottom)
      expect(onTopReached).not.toHaveBeenCalled()
    } finally {
      restore()
    }
  })
})

describe('older-history trigger — fetch through the gate', () => {
  afterEach(() => { vi.restoreAllMocks() })

  /** Seed a resumed session through the real reducer path that reads `has_more`. */
  function resumedStore(hasMore: boolean) {
    const store = createTestStore()
    store.dispatch(resumeFromHistory.fulfilled(
      { ok: true, key: 'slot-1', messages: [], hasMore, nextBefore: OLDEST, rawCount: RESUME_RAW, total: TOTAL },
      'req-1',
      { key: 'slot-1', title: 'slot-1' },
    ))
    return store
  }

  function renderWired(store: ReturnType<typeof resumedStore>) {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    const onTopReached = () => {
      const chat = store.getState().chat
      if (!shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })) return
      void store.dispatch(loadOlderMessages())
    }
    const io = installFakeIO()
    const { container } = render(<Harness onTopReached={onTopReached} scrollerRef={scrollerRef} />)
    return { io, container }
  }

  it('fetches older messages when the server reported more history', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockResolvedValue({ messages: [], has_more: false, total: TOTAL } as never)
    const store = resumedStore(true)
    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotOldestIndex).toBe(OLDEST)
    const { io, container } = renderWired(store)
    try {
      const top = container.querySelector('[data-sentinel="top"]') as HTMLElement
      fireIntersection(io.instances[0], top)
      await waitFor(() => expect(detail).toHaveBeenCalledWith('slot-1', 300, OLDEST, expect.any(AbortSignal)))
    } finally {
      io.restore()
    }
  })

  it('does not fetch when the server reported no more history', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockResolvedValue({ messages: [], has_more: false, total: TOTAL } as never)
    const store = resumedStore(false)
    expect(store.getState().chat.slotHasMore).toBe(false)
    const { io, container } = renderWired(store)
    try {
      const top = container.querySelector('[data-sentinel="top"]') as HTMLElement
      fireIntersection(io.instances[0], top)
      await act(async () => {})
      expect(detail).not.toHaveBeenCalled()
    } finally {
      io.restore()
    }
  })

  // The existing suite only asserted the refusal cases, which pass whether or not
  // the thunk ever fetches. These pin both directions.
  it('fetches on a resumed session with a page left to load', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockResolvedValue({ messages: [], has_more: true, total: TOTAL } as never)
    const store = resumedStore(true)
    const result = await store.dispatch(loadOlderMessages())
    expect(detail).toHaveBeenCalledWith('slot-1', 300, OLDEST, expect.any(AbortSignal))
    expect(result.payload).not.toBeNull()
  })

  it('still returns the null sentinel when there is nothing left to load', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockResolvedValue({ messages: [], has_more: false, total: TOTAL } as never)
    const store = resumedStore(false)
    const result = await store.dispatch(loadOlderMessages())
    expect(detail).not.toHaveBeenCalled()
    expect(result.payload).toBeNull()
  })

  it('records a rejected fetch so the bar can surface it', async () => {
    vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new Error('network down'))
    const store = resumedStore(true)
    expect(store.getState().chat.slotOlderError).toBe(false)
    await store.dispatch(loadOlderMessages())
    expect(store.getState().chat.slotOlderError).toBe(true)
    expect(store.getState().chat.loadingOlder).toBe(false)
    // The bar must stay mounted for the retry to be reachable.
    expect(store.getState().chat.slotHasMore).toBe(true)
  })

  it('clears the failure once a retry succeeds', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new Error('network down'))
    const store = resumedStore(true)
    await store.dispatch(loadOlderMessages())
    expect(store.getState().chat.slotOlderError).toBe(true)
    detail.mockResolvedValue({ messages: [], has_more: true, total: TOTAL } as never)
    await store.dispatch(loadOlderMessages())
    expect(store.getState().chat.slotOlderError).toBe(false)
  })
})

describe('older-history trigger — ChatPage wiring contract', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const chatPageSrc = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')

  it('hands the virtualizer a top-reached callback', () => {
    expect(chatPageSrc).toMatch(/onTopReached:\s*handleTopReached/)
  })

  it('gates that callback on the shared predicate rather than inline logic', () => {
    expect(chatPageSrc).toMatch(/shouldPaginateOlder\(\{/)
    expect(chatPageSrc).toMatch(/dispatch\(loadOlderMessages\(\)\)/)
  })

  it('reads live store state, so a stale render cannot suppress the fetch', () => {
    expect(chatPageSrc).toMatch(/store\.getState\(\)\.chat/)
  })


  it('renders the affordance only when the server reported unloaded history AND the cursor is this chat\'s', () => {
    expect(chatPageSrc).toMatch(/slotHasMore && cursorIsForActiveSlot && \(\s*<EarlierMessagesBar/)
    expect(chatPageSrc).toMatch(/loading=\{loadingOlder\}/)
  })

  it('gives the explicit control its own handler, so a click bypasses the gate', () => {
    expect(chatPageSrc).toMatch(/onLoad=\{handleLoadEarlier\}/)
    expect(chatPageSrc).toMatch(/const handleLoadEarlier = useCallback/)
  })




})

describe('auto-fill gate for a reader who did not climb', () => {
  // The top sentinel/crossing can fire WITHOUT reader input: boot-phase
  // estimate pricing keeps total height under a viewport for a beat, and a
  // spacer collapse pulls the top within reach. Each self-issued landing
  // re-fires the transition -- a page chain that walked a multi-megabyte
  // transcript over a bottom-followed phone right after refresh. The gate:
  // a followed reader on a scrollable transcript never auto-fetches; the
  // short-transcript fill (no scrollbar exists) stays load-bearing.
  it('refuses a scrollable transcript with no reader input this session', () => {
    // Includes the boot-restored-anchor shape: follow already released,
    // reader has touched nothing. Follow state is deliberately NOT an
    // authorization signal (the replica probe walked a page per ~8s
    // through the follow-released door with zero input events).
    expect(shouldAutoFillOlder({ scrollHeight: 5000, clientHeight: 800, sawInput: false })).toBe(false)
  })
  it('fills a transcript too short to scroll, even with no input', () => {
    expect(shouldAutoFillOlder({ scrollHeight: 820, clientHeight: 800, sawInput: false })).toBe(true)
  })
  it('serves a reader who actually scrolled (input is the request)', () => {
    expect(shouldAutoFillOlder({ scrollHeight: 5000, clientHeight: 800, sawInput: true })).toBe(true)
  })
})
