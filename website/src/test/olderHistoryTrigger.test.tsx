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
import { api } from '../api/client'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `i${i}` }))

// A paged resume ships the last RESUME_RAW raw rows of TOTAL, so the cursor arms
// at the oldest raw index it loaded, not at the total.
const TOTAL = 240
const RESUME_RAW = 200
const OLDEST = TOTAL - RESUME_RAW

function Harness({ onTopReached, scrollerRef }: {
  onTopReached: () => void
  scrollerRef: RefObject<HTMLDivElement | null>
}) {
  const v = useVirtualChat<Item>({
    items: mkItems(30), getKey, sessionId: 'older-history', overscan: 2,
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

interface FakeIOInst { cb: IntersectionObserverCallback }

/** Replace IntersectionObserver with one whose callbacks the test fires by hand. */
function installFakeIO() {
  const instances: FakeIOInst[] = []
  class FakeIO {
    cb: IntersectionObserverCallback
    constructor(cb: IntersectionObserverCallback) { this.cb = cb; instances.push(this) }
    observe() {}
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
      await waitFor(() => expect(detail).toHaveBeenCalledWith('slot-1', 100, OLDEST, expect.any(AbortSignal)))
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
    expect(detail).toHaveBeenCalledWith('slot-1', 100, OLDEST, expect.any(AbortSignal))
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
