// Characterization of the hook's OUTER contract: what it exports, what it
// returns, which returned callbacks keep their identity across a re-render, and
// which option changes tear down and re-register its DOM observers.
//
// Callers rely on these without saying so. VirtualTranscript lists
// `scrollToBottom`, `mountIndex` and `estimateRowTop` in its imperative-handle
// dependencies, and ChatPage re-reads them into refs on every commit, so an
// identity that starts changing on every render re-creates that handle every
// render; one that stops changing on an option change leaves a stale closure. The observer counts are the same
// contract seen from the DOM: a re-subscription resets the scroll listener's
// direction baseline and re-runs its attach-time evaluation, so re-attaching on
// a render that changed nothing (or failing to on one that changed the
// geometry inputs) is observable behaviour, not bookkeeping.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { useRef, type RefObject } from 'react'

import * as virtualChatModule from '../hooks/virtualizer/useVirtualChat'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatReturn } from '../hooks/virtualizer/types'
import { HeightIndex } from '../hooks/virtualizer/HeightIndex'

interface Item { id: string }
const getKey = (it: Item) => it.id
const ITEMS: Item[] = Array.from({ length: 12 }, (_, i) => ({ id: `m${i}` }))

interface HarnessProps {
  sessionId: string
  overscan?: number
  followOutput?: boolean
  bottomThreshold?: number
  heightScopeKey?: string
  estimatedHeight?: number
  prefetchStartIndex?: number
  // The inputs a real host re-creates on every render (ChatPage builds a new
  // rows array and new key closures each time) or flips mid-life.
  items?: Item[]
  getKey?: (it: Item) => string
  getStableId?: (it: Item) => string
  getAltId?: (it: Item) => string | null
  isSticky?: (it: Item) => boolean
  streamingIndex?: number
  runActive?: boolean
  onTopReached?: () => void
  sink: { current: UseVirtualChatReturn<Item> | null }
}

function Harness({
  sessionId, overscan, followOutput, bottomThreshold, heightScopeKey, estimatedHeight, prefetchStartIndex,
  items = ITEMS, getKey: keyOf = getKey, getStableId, getAltId, isSticky, streamingIndex, runActive, onTopReached,
  sink,
}: HarnessProps) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualChat<Item>({
    items,
    getKey: keyOf,
    getStableId,
    getAltId,
    isSticky,
    streamingIndex,
    runActive,
    onTopReached,
    sessionId,
    overscan,
    followOutput,
    bottomThreshold,
    heightScopeKey,
    estimatedHeight,
    prefetchStartIndex,
    externalScrollerRef: scrollerRef as RefObject<HTMLDivElement | null>,
  })
  sink.current = virt
  return (
    <div ref={scrollerRef} data-testid="scroller">
      <div ref={virt.topSentinelRef} />
      {virt.virtualItems.map((vi) =>
        vi.mounted ? <div key={vi.key} ref={virt.measureRef(vi.index)} /> : null,
      )}
      <div ref={virt.bottomSentinelRef} />
    </div>
  )
}

class CountingResizeObserver {
  static created = 0
  static disconnected = 0
  constructor(readonly cb: ResizeObserverCallback) { CountingResizeObserver.created += 1 }
  observe() {}
  unobserve() {}
  disconnect() { CountingResizeObserver.disconnected += 1 }
}

class CountingIntersectionObserver {
  static created = 0
  static disconnected = 0
  constructor(readonly cb: IntersectionObserverCallback, readonly init?: IntersectionObserverInit) {
    CountingIntersectionObserver.created += 1
  }
  observe() {}
  unobserve() {}
  disconnect() { CountingIntersectionObserver.disconnected += 1 }
  takeRecords() { return [] }
}

describe('useVirtualChat: module export surface', () => {
  it('exports exactly the hook and its three public helpers', () => {
    expect(Object.keys(virtualChatModule).sort()).toEqual([
      'SCROLL_SETTLE_MS',
      'pinSuppressedNow',
      'shiftCompensationAllowed',
      'useVirtualChat',
    ])
    expect(virtualChatModule.SCROLL_SETTLE_MS).toBe(150)
  })

  it('keeps the public helpers\' decision tables', () => {
    const { pinSuppressedNow, shiftCompensationAllowed } = virtualChatModule
    expect(shiftCompensationAllowed({ stick: false, settleMeasuring: false })).toBe(true)
    expect(shiftCompensationAllowed({ stick: true, settleMeasuring: false })).toBe(false)
    expect(shiftCompensationAllowed({ stick: false, settleMeasuring: true })).toBe(false)
    // Suppressed inside the settle window after hard input, or while a cascade holds.
    expect(pinSuppressedNow(1000, 900, 0, 150)).toBe(true)
    expect(pinSuppressedNow(1000, 800, 0, 150)).toBe(false)
    expect(pinSuppressedNow(1000, 800, 1001, 150)).toBe(true)
    expect(pinSuppressedNow(1000, Number.NEGATIVE_INFINITY, 0, 150)).toBe(false)
  })
})

describe('useVirtualChat: return surface and callback identity', () => {
  beforeEach(() => { localStorage.clear() })

  it('returns exactly the documented keys', () => {
    const sink: HarnessProps['sink'] = { current: null }
    render(<Harness sessionId="surface-keys" sink={sink} />)
    expect(Object.keys(sink.current!).sort()).toEqual([
      'bottomSentinelRef',
      'contentRef',
      'estimateRowTop',
      'farmIsMeasured',
      'farmRecord',
      'farmRowMounted',
      'getFollow',
      'isAtBottom',
      'measureRef',
      'mountIndex',
      'offsetAfter',
      'offsetBefore',
      'restoreGate',
      'scrollToBottom',
      'scrollToIndex',
      'scrollerRef',
      'topSentinelRef',
      'totalHeight',
      'trailingRef',
      'virtualItems',
    ])
  })

  const FN_KEYS = [
    'scrollToIndex',
    'scrollToBottom',
    'mountIndex',
    'estimateRowTop',
    'measureRef',
    'getFollow',
    'farmRecord',
    'farmIsMeasured',
    'farmRowMounted',
    'scrollerRef',
    'contentRef',
    'topSentinelRef',
    'bottomSentinelRef',
  ] as const
  type FnKey = typeof FN_KEYS[number]

  /** Which returned members change identity when ONE option changes. */
  const CHANGES: Record<string, { next: Partial<HarnessProps>; changed: FnKey[] }> = {
    'nothing': { next: {}, changed: [] },
    'overscan': { next: { overscan: 6 }, changed: ['scrollToIndex', 'scrollToBottom', 'mountIndex'] },
    'followOutput': { next: { followOutput: false }, changed: ['scrollToBottom'] },
    'bottomThreshold': { next: { bottomThreshold: 120 }, changed: [] },
    'heightScopeKey': { next: { heightScopeKey: 'scope-b' }, changed: ['scrollToIndex', 'estimateRowTop'] },
    'sessionId': { next: { sessionId: 'identity-b' }, changed: ['scrollToIndex', 'estimateRowTop'] },
    'estimatedHeight': { next: { estimatedHeight: 120 }, changed: [] },
    'prefetchStartIndex': { next: { prefetchStartIndex: 3 }, changed: [] },
    'items (new array, same rows)': { next: { items: [...ITEMS] }, changed: [] },
    'getKey (new closure)': { next: { getKey: (it) => it.id }, changed: [] },
    'getStableId': { next: { getStableId: (it) => it.id }, changed: [] },
    'getAltId': { next: { getAltId: () => null }, changed: [] },
    'isSticky': { next: { isSticky: () => false }, changed: [] },
    'streamingIndex': { next: { streamingIndex: ITEMS.length - 1 }, changed: [] },
    'runActive': { next: { runActive: true }, changed: [] },
    'onTopReached': { next: { onTopReached: () => {} }, changed: [] },
  }

  for (const [label, { next, changed }] of Object.entries(CHANGES)) {
    it(`a change of ${label} re-creates exactly [${changed.join(', ')}]`, () => {
      const sink: HarnessProps['sink'] = { current: null }
      const base: HarnessProps = { sessionId: 'identity-a', sink }
      const { rerender } = render(<Harness {...base} />)
      // Settle the post-mount commit (the scroller element is promoted to state
      // after the first commit) before sampling identities.
      act(() => { rerender(<Harness {...base} />) })
      const before = sink.current!
      const rowRef = before.measureRef(3)

      act(() => { rerender(<Harness {...base} {...next} />) })
      const after = sink.current!

      const actuallyChanged = FN_KEYS.filter((k) => before[k] !== after[k])
      expect(actuallyChanged).toEqual(changed)
      // Per-index ref callbacks are cached across renders whatever changed.
      expect(after.measureRef(3)).toBe(rowRef)
    })
  }
})

describe('useVirtualChat: the unmount teardown runs once, at unmount', () => {
  // The teardown drops the pending timers and the anchor save and flushes the
  // height cache. Its effect must never re-run mid-life: a re-run would drop a
  // pending anchor save and flush heights on an ordinary commit. The flush is
  // its observable half.
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.restoreAllMocks() })

  it('no re-render flushes the height cache; unmount flushes it once', () => {
    const flush = vi.spyOn(HeightIndex.prototype, 'flush')
    const sink: HarnessProps['sink'] = { current: null }
    const base: HarnessProps = { sessionId: 'teardown', sink }
    const { rerender, unmount } = render(<Harness {...base} />)
    act(() => { rerender(<Harness {...base} />) })
    // Every change that keeps the height scope (a scope change flushes the
    // outgoing cache through the height owner's guard, not the teardown).
    const changes: Partial<HarnessProps>[] = [
      {}, { overscan: 6 }, { followOutput: false }, { bottomThreshold: 120 }, { estimatedHeight: 120 },
      { prefetchStartIndex: 3 }, { items: [...ITEMS] }, { getKey: (it) => it.id },
      { getStableId: (it) => it.id }, { streamingIndex: ITEMS.length - 1 }, { runActive: true },
      { onTopReached: () => {} },
    ]
    for (const next of changes) act(() => { rerender(<Harness {...base} {...next} />) })
    expect(flush).not.toHaveBeenCalled()
    unmount()
    expect(flush).toHaveBeenCalledTimes(1)
  })
})

describe('useVirtualChat: observer registration and re-subscription', () => {
  const originalRO = globalThis.ResizeObserver
  const originalIO = globalThis.IntersectionObserver
  let scrollAdds = 0
  let scrollRemoves = 0
  let visibilityAdds = 0
  let visibilityRemoves = 0
  let watchdogStarts = 0

  beforeEach(() => {
    localStorage.clear()
    CountingResizeObserver.created = 0
    CountingResizeObserver.disconnected = 0
    CountingIntersectionObserver.created = 0
    CountingIntersectionObserver.disconnected = 0
    scrollAdds = scrollRemoves = visibilityAdds = visibilityRemoves = watchdogStarts = 0
    globalThis.ResizeObserver = CountingResizeObserver as unknown as typeof ResizeObserver
    globalThis.IntersectionObserver = CountingIntersectionObserver as unknown as typeof IntersectionObserver

    const elAdd = HTMLElement.prototype.addEventListener
    const elRemove = HTMLElement.prototype.removeEventListener
    vi.spyOn(HTMLElement.prototype, 'addEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (type === 'scroll' && this.dataset.testid === 'scroller') scrollAdds += 1
      return (elAdd as (...a: unknown[]) => void).call(this, type, ...rest)
    })
    vi.spyOn(HTMLElement.prototype, 'removeEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (type === 'scroll' && this.dataset.testid === 'scroller') scrollRemoves += 1
      return (elRemove as (...a: unknown[]) => void).call(this, type, ...rest)
    })
    const docAdd = document.addEventListener.bind(document)
    const docRemove = document.removeEventListener.bind(document)
    vi.spyOn(document, 'addEventListener').mockImplementation((type: string, ...rest: unknown[]) => {
      if (type === 'visibilitychange') visibilityAdds += 1
      return (docAdd as (...a: unknown[]) => void)(type, ...rest)
    })
    vi.spyOn(document, 'removeEventListener').mockImplementation((type: string, ...rest: unknown[]) => {
      if (type === 'visibilitychange') visibilityRemoves += 1
      return (docRemove as (...a: unknown[]) => void)(type, ...rest)
    })
    const realSetInterval = window.setInterval.bind(window)
    vi.spyOn(window, 'setInterval').mockImplementation(((handler: TimerHandler, ms?: number, ...args: unknown[]) => {
      // The viewport-coverage watchdog is the hook's only 500ms interval.
      if (ms === 500) watchdogStarts += 1
      return realSetInterval(handler, ms, ...args)
    }) as typeof window.setInterval)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    globalThis.ResizeObserver = originalRO
    globalThis.IntersectionObserver = originalIO
  })

  const snapshot = () => ({
    scroll: scrollAdds,
    ro: CountingResizeObserver.created,
    io: CountingIntersectionObserver.created,
    visibility: visibilityAdds,
    watchdog: watchdogStarts,
  })

  it('registers each observer once at mount', () => {
    const sink: HarnessProps['sink'] = { current: null }
    const { rerender, unmount } = render(<Harness sessionId="observers-mount" sink={sink} />)
    act(() => { rerender(<Harness sessionId="observers-mount" sink={sink} />) })
    // One scroll listener, one ResizeObserver, the two sentinel
    // IntersectionObservers (per-observer rootMargin), one visibility listener,
    // one watchdog interval.
    expect(snapshot()).toEqual({ scroll: 1, ro: 1, io: 2, visibility: 1, watchdog: 1 })

    unmount()
    // Every registration is torn down with the component.
    expect(scrollRemoves).toBe(scrollAdds)
    expect(visibilityRemoves).toBe(visibilityAdds)
    expect(CountingResizeObserver.disconnected).toBe(CountingResizeObserver.created)
    expect(CountingIntersectionObserver.disconnected).toBe(CountingIntersectionObserver.created)
  })

  /** Registrations added by ONE option change after the mount has settled. */
  const RESUBSCRIBE: Record<string, { next: Partial<HarnessProps>; delta: ReturnType<typeof snapshot> }> = {
    'nothing': { next: {}, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'overscan': { next: { overscan: 6 }, delta: { scroll: 1, ro: 1, io: 2, visibility: 1, watchdog: 1 } },
    'followOutput': { next: { followOutput: false }, delta: { scroll: 1, ro: 0, io: 0, visibility: 1, watchdog: 1 } },
    'bottomThreshold': { next: { bottomThreshold: 120 }, delta: { scroll: 1, ro: 0, io: 0, visibility: 1, watchdog: 0 } },
    'heightScopeKey': { next: { heightScopeKey: 'scope-b' }, delta: { scroll: 1, ro: 1, io: 0, visibility: 0, watchdog: 1 } },
    'sessionId': { next: { sessionId: 'observers-b' }, delta: { scroll: 1, ro: 1, io: 0, visibility: 1, watchdog: 1 } },
    'estimatedHeight': { next: { estimatedHeight: 120 }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'prefetchStartIndex': { next: { prefetchStartIndex: 3 }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'items (new array, same rows)': { next: { items: [...ITEMS] }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'getKey (new closure)': { next: { getKey: (it) => it.id }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'getStableId': { next: { getStableId: (it) => it.id }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'getAltId': { next: { getAltId: () => null }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'isSticky': { next: { isSticky: () => false }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'streamingIndex': { next: { streamingIndex: ITEMS.length - 1 }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'runActive': { next: { runActive: true }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
    'onTopReached': { next: { onTopReached: () => {} }, delta: { scroll: 0, ro: 0, io: 0, visibility: 0, watchdog: 0 } },
  }

  for (const [label, { next, delta }] of Object.entries(RESUBSCRIBE)) {
    it(`a change of ${label} re-registers exactly ${JSON.stringify(delta)}`, () => {
      const sink: HarnessProps['sink'] = { current: null }
      const base: HarnessProps = { sessionId: 'observers-a', sink }
      const { rerender } = render(<Harness {...base} />)
      act(() => { rerender(<Harness {...base} />) })
      const before = snapshot()

      act(() => { rerender(<Harness {...base} {...next} />) })
      const after = snapshot()

      expect({
        scroll: after.scroll - before.scroll,
        ro: after.ro - before.ro,
        io: after.io - before.io,
        visibility: after.visibility - before.visibility,
        watchdog: after.watchdog - before.watchdog,
      }).toEqual(delta)
      // A re-registration always releases the one it replaces.
      expect(scrollAdds - scrollRemoves).toBe(1)
      expect(visibilityAdds - visibilityRemoves).toBe(1)
      expect(CountingResizeObserver.created - CountingResizeObserver.disconnected).toBe(1)
      expect(CountingIntersectionObserver.created - CountingIntersectionObserver.disconnected).toBe(2)
    })
  }
})

describe('useVirtualChat: __vcSnapshot ownership', () => {
  const probe = () => (window as unknown as { __vcSnapshot?: () => Record<string, unknown> }).__vcSnapshot

  beforeEach(() => {
    localStorage.clear()
    vi.stubEnv('DEV', true)
    delete (window as unknown as { __vcSnapshot?: unknown }).__vcSnapshot
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
    delete (window as unknown as { __vcSnapshot?: unknown }).__vcSnapshot
  })

  it('is last-mount-wins, and an older instance unmounting leaves the newer probe installed', () => {
    const a = render(<Harness sessionId="probe-a" sink={{ current: null }} />)
    const probeA = probe()
    const b = render(<Harness sessionId="probe-b" sink={{ current: null }} />)
    const probeB = probe()
    expect(typeof probeA).toBe('function')
    expect(probeB).not.toBe(probeA)

    // The cleanup deletes only its OWN function.
    a.unmount()
    expect(probe()).toBe(probeB)
    b.unmount()
    expect(probe()).toBeUndefined()
  })

  it('reports the documented snapshot fields', () => {
    vi.spyOn(console, 'log').mockImplementation(() => {})
    vi.spyOn(console, 'table').mockImplementation(() => {})
    render(<Harness sessionId="probe-fields" sink={{ current: null }} />)
    const result = probe()!()
    expect(Object.keys(result).sort()).toEqual([
      'children',
      'count',
      'endIsCount',
      'estimated',
      'estimatedHeight',
      'geom',
      'lastWriteTop',
      'measured',
      'mountedRows',
      'offsetAfter',
      'offsetBefore',
      'sessionId',
      'stick',
      'totalHeight',
      'windowRange',
    ])
    expect(result.sessionId).toBe('probe-fields')
    expect(result.count).toBe(ITEMS.length)
  })
})

describe('useVirtualChat: observer registration ORDER', () => {
  // Counts say how many registrations happen; this says in what order. The
  // ResizeObserver observes rows before the scroller, the sentinel observers are
  // created top before bottom, and the visibility listener precedes the scroll
  // listener and its intent listeners -- a sequence, compared as one.
  const originalRO = globalThis.ResizeObserver
  const originalIO = globalThis.IntersectionObserver
  let log: string[] = []

  const tag = (el: Element | Document | null | undefined): string => {
    if (!el) return 'null'
    if (el === document) return 'document'
    const h = el as HTMLElement
    if (h.dataset?.testid === 'scroller') return 'scroller'
    if (h.dataset?.sentinel) return `sentinel-${h.dataset.sentinel}`
    if (h.dataset?.row !== undefined) return `row${h.dataset.row}`
    return h.tagName?.toLowerCase() ?? 'node'
  }

  class LoggingRO {
    constructor(readonly cb: ResizeObserverCallback) { log.push('RO:new') }
    observe(el: Element) { log.push(`RO:observe:${tag(el)}`) }
    unobserve(el: Element) { log.push(`RO:unobserve:${tag(el)}`) }
    disconnect() { log.push('RO:disconnect') }
  }
  class LoggingIO {
    constructor(readonly cb: IntersectionObserverCallback, readonly init?: IntersectionObserverInit) {
      log.push(`IO:new:${init?.rootMargin ?? ''}`)
    }
    observe(el: Element) { log.push(`IO:observe:${tag(el)}`) }
    unobserve(el: Element) { log.push(`IO:unobserve:${tag(el)}`) }
    disconnect() { log.push('IO:disconnect') }
    takeRecords() { return [] }
  }

  function OrderHarness({ overscan, sink }: { overscan?: number; sink: HarnessProps['sink'] }) {
    const scrollerRef = useRef<HTMLDivElement>(null)
    const virt = useVirtualChat<Item>({
      items: ITEMS.slice(0, 4),
      getKey,
      sessionId: 'observer-order',
      overscan,
      externalScrollerRef: scrollerRef as RefObject<HTMLDivElement | null>,
    })
    sink.current = virt
    return (
      <div ref={scrollerRef} data-testid="scroller">
        <div ref={virt.topSentinelRef} data-sentinel="top" />
        {virt.virtualItems.map((vi) =>
          vi.mounted ? <div key={vi.key} ref={virt.measureRef(vi.index)} data-row={vi.index} /> : null,
        )}
        <div ref={virt.bottomSentinelRef} data-sentinel="bottom" />
      </div>
    )
  }

  beforeEach(() => {
    localStorage.clear()
    log = []
    globalThis.ResizeObserver = LoggingRO as unknown as typeof ResizeObserver
    globalThis.IntersectionObserver = LoggingIO as unknown as typeof IntersectionObserver
    const elAdd = HTMLElement.prototype.addEventListener
    const elRemove = HTMLElement.prototype.removeEventListener
    vi.spyOn(HTMLElement.prototype, 'addEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (this.dataset.testid === 'scroller') log.push(`listen:${type}`)
      return (elAdd as (...a: unknown[]) => void).call(this, type, ...rest)
    })
    vi.spyOn(HTMLElement.prototype, 'removeEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (this.dataset.testid === 'scroller') log.push(`unlisten:${type}`)
      return (elRemove as (...a: unknown[]) => void).call(this, type, ...rest)
    })
    const docAdd = document.addEventListener.bind(document)
    const docRemove = document.removeEventListener.bind(document)
    vi.spyOn(document, 'addEventListener').mockImplementation((type: string, ...rest: unknown[]) => {
      if (type === 'visibilitychange') log.push(`doc-listen:${type}`)
      return (docAdd as (...a: unknown[]) => void)(type, ...rest)
    })
    vi.spyOn(document, 'removeEventListener').mockImplementation((type: string, ...rest: unknown[]) => {
      if (type === 'visibilitychange') log.push(`doc-unlisten:${type}`)
      return (docRemove as (...a: unknown[]) => void)(type, ...rest)
    })
    const realSetInterval = window.setInterval.bind(window)
    const realClearInterval = window.clearInterval.bind(window)
    vi.spyOn(window, 'setInterval').mockImplementation(((handler: TimerHandler, ms?: number, ...args: unknown[]) => {
      if (ms === 500) log.push('interval:500')
      return realSetInterval(handler, ms, ...args)
    }) as typeof window.setInterval)
    vi.spyOn(window, 'clearInterval').mockImplementation(((id?: number) => {
      log.push('clearInterval')
      return realClearInterval(id)
    }) as typeof window.clearInterval)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    globalThis.ResizeObserver = originalRO
    globalThis.IntersectionObserver = originalIO
  })

  const INTENT = ['wheel', 'touchstart', 'touchmove', 'touchend', 'touchcancel', 'pointerdown', 'keydown']

  it('registers in a fixed order at mount, on re-subscription and at unmount', () => {
    const sink: HarnessProps['sink'] = { current: null }
    const { rerender, unmount } = render(<OrderHarness sink={sink} />)
    act(() => { rerender(<OrderHarness sink={sink} />) })
    const mount = log
    log = []
    act(() => { rerender(<OrderHarness sink={sink} overscan={6} />) })
    const resubscribe = log
    log = []
    unmount()
    const teardown = log

    expect(mount).toEqual([
      // First commit: the observers that read the scroller through its ref
      // rather than waiting for it to be promoted to state. The visibility
      // listener comes first; the ResizeObserver observes rows before the
      // scroller.
      'doc-listen:visibilitychange',
      'RO:new', 'RO:observe:row0', 'RO:observe:row1', 'RO:observe:row2', 'RO:observe:row3',
      'RO:observe:scroller',
      // Second commit, once the scroller element is in state.
      'listen:scroll', ...INTENT.map((t) => `listen:${t}`),
      'interval:500',
      'RO:observe:scroller',
      'IO:new:200px 0px', 'IO:new:200px 0px', 'IO:observe:sentinel-top', 'IO:observe:sentinel-bottom',
    ])
    expect(resubscribe).toEqual([
      'doc-unlisten:visibilitychange',
      'unlisten:scroll', ...INTENT.map((t) => `unlisten:${t}`),
      'clearInterval',
      'RO:disconnect',
      'IO:disconnect', 'IO:disconnect',
      'doc-listen:visibilitychange',
      'listen:scroll', ...INTENT.map((t) => `listen:${t}`),
      'interval:500',
      'RO:new', 'RO:observe:row0', 'RO:observe:row1', 'RO:observe:row2', 'RO:observe:row3',
      'RO:observe:scroller',
      'IO:new:200px 0px', 'IO:new:200px 0px', 'IO:observe:sentinel-top', 'IO:observe:sentinel-bottom',
    ])
    expect(teardown).toEqual([
      'RO:unobserve:row0', 'RO:unobserve:row1', 'RO:unobserve:row2', 'RO:unobserve:row3',
      'doc-unlisten:visibilitychange',
      'unlisten:scroll', ...INTENT.map((t) => `unlisten:${t}`),
      'clearInterval',
      'RO:disconnect',
      // The late-scroller effect's unobserve finds the observer already torn
      // down by the effect before it, so it records nothing.
      'IO:disconnect', 'IO:disconnect',
    ])
  })
})

describe('useVirtualChat: the rail-collapse window holds back the window recompute', () => {
  // The resize storm a rail collapse produces must not schedule a window
  // recompute per frame; the settle timer runs exactly one when it closes.
  let origRO: typeof ResizeObserver | undefined
  let frames: FrameRequestCallback[] = []

  class ManualRO {
    static last: ManualRO | null = null
    constructor(readonly cb: ResizeObserverCallback) { ManualRO.last = this }
    observe() {}
    unobserve() {}
    disconnect() {}
    fire(entries: Partial<ResizeObserverEntry>[]) { this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver) }
  }

  beforeEach(async () => {
    localStorage.clear()
    const rail = await import('../hooks/useRailWidth')
    rail.__resetRailWidth()
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = ManualRO as unknown as typeof ResizeObserver
    frames = []
    // After the fake timers, which install their own requestAnimationFrame: the
    // recorder has to replace THAT one to see the hook's frame requests.
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length })
  })

  afterEach(async () => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    const rail = await import('../hooks/useRailWidth')
    rail.__resetRailWidth()
  })

  it('schedules no recompute frame from a resize inside the rail window, one outside it', async () => {
    const rail = await import('../hooks/useRailWidth')
    const el = document.createElement('div')
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 400 })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 900 })
    const items = ITEMS.slice(0, 3)
    const scrollerRef = { current: el } as RefObject<HTMLDivElement | null>
    const { result } = await import('@testing-library/react').then(({ renderHook }) =>
      renderHook(() => useVirtualChat<Item>({
        items, getKey, sessionId: 'rail-recompute', externalScrollerRef: scrollerRef,
      })))
    const nodes = items.map((_, i) => {
      const node = document.createElement('div')
      let h = 100
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
      act(() => { result.current.measureRef(i)(node) })
      return { node, grow: (v: number) => { h = v } }
    })
    act(() => { vi.advanceTimersByTime(200) })
    const ro = ManualRO.last!
    frames = []

    act(() => { rail.setRailWidth(74) })
    nodes.forEach((n) => n.grow(140))
    act(() => { ro.fire(nodes.map((n) => ({ target: n.node }))) })
    expect(frames).toHaveLength(0)

    act(() => { vi.advanceTimersByTime(rail.RAIL_SETTLE_MS + 20) })
    expect(rail.isRailSettling()).toBe(false)
    frames = []
    nodes.forEach((n) => n.grow(180))
    act(() => { ro.fire(nodes.map((n) => ({ target: n.node }))) })
    expect(frames).toHaveLength(1)
  })
})

describe('useVirtualChat: swapping the external scroller ref to another element', () => {
  // The one option change that reaches almost every callback. What re-attaches
  // where is pinned as observed: listeners keyed on the scroller ELEMENT stay on
  // the element first promoted to state, while everything keyed on the ref
  // object follows the swap.
  const originalRO = globalThis.ResizeObserver
  const originalIO = globalThis.IntersectionObserver
  let log: string[] = []
  const which = (el: Element | null | undefined) => (el as HTMLElement | null)?.dataset?.which ?? (el as HTMLElement | null)?.dataset?.row ?? 'other'

  class SwapRO {
    constructor(readonly cb: ResizeObserverCallback) { log.push('RO:new') }
    observe(el: Element) { log.push(`RO:observe:${which(el)}`) }
    unobserve(el: Element) { log.push(`RO:unobserve:${which(el)}`) }
    disconnect() { log.push('RO:disconnect') }
  }
  class SwapIO {
    constructor(readonly cb: IntersectionObserverCallback, readonly init?: IntersectionObserverInit) { log.push('IO:new') }
    observe() {}
    unobserve() {}
    disconnect() { log.push('IO:disconnect') }
    takeRecords() { return [] }
  }

  function SwapHarness({ useB, sink }: { useB: boolean; sink: HarnessProps['sink'] }) {
    const refA = useRef<HTMLDivElement>(null)
    const refB = useRef<HTMLDivElement>(null)
    const virt = useVirtualChat<Item>({
      items: ITEMS.slice(0, 3),
      getKey,
      sessionId: 'swap-scroller',
      externalScrollerRef: (useB ? refB : refA) as RefObject<HTMLDivElement | null>,
    })
    sink.current = virt
    return (
      <>
        <div ref={refA} data-which="a">
          <div ref={virt.topSentinelRef} />
          {virt.virtualItems.map((vi) =>
            vi.mounted ? <div key={vi.key} ref={virt.measureRef(vi.index)} data-row={vi.index} /> : null,
          )}
          <div ref={virt.bottomSentinelRef} />
        </div>
        <div ref={refB} data-which="b" />
      </>
    )
  }

  beforeEach(() => {
    localStorage.clear()
    log = []
    globalThis.ResizeObserver = SwapRO as unknown as typeof ResizeObserver
    globalThis.IntersectionObserver = SwapIO as unknown as typeof IntersectionObserver
    const elAdd = HTMLElement.prototype.addEventListener
    const elRemove = HTMLElement.prototype.removeEventListener
    vi.spyOn(HTMLElement.prototype, 'addEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (type === 'scroll') log.push(`listen:scroll:${which(this)}`)
      return (elAdd as (...a: unknown[]) => void).call(this, type, ...rest)
    })
    vi.spyOn(HTMLElement.prototype, 'removeEventListener').mockImplementation(function (
      this: HTMLElement, type: string, ...rest: unknown[]
    ) {
      if (type === 'scroll') log.push(`unlisten:scroll:${which(this)}`)
      return (elRemove as (...a: unknown[]) => void).call(this, type, ...rest)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    globalThis.ResizeObserver = originalRO
    globalThis.IntersectionObserver = originalIO
  })

  it('re-keys the ref-bound callbacks and observers, not the element-bound listeners', () => {
    const sink: HarnessProps['sink'] = { current: null }
    const { rerender } = render(<SwapHarness useB={false} sink={sink} />)
    act(() => { rerender(<SwapHarness useB={false} sink={sink} />) })
    const before = sink.current!
    const rowRef = before.measureRef(1)
    log = []

    act(() => { rerender(<SwapHarness useB sink={sink} />) })
    const after = sink.current!

    const keys = [
      'scrollToIndex', 'scrollToBottom', 'mountIndex', 'estimateRowTop', 'measureRef', 'getFollow',
      'farmRecord', 'farmIsMeasured', 'farmRowMounted', 'scrollerRef', 'contentRef',
      'topSentinelRef', 'bottomSentinelRef',
    ] as const
    expect(keys.filter((k) => before[k] !== after[k])).toEqual([
      'scrollToIndex', 'scrollToBottom', 'estimateRowTop', 'measureRef', 'farmRecord', 'scrollerRef',
    ])
    expect(after.scrollerRef.current).toBe(document.querySelector('[data-which="b"]'))
    // Per-index ref callbacks are cached for the hook's lifetime.
    expect(after.measureRef(1)).toBe(rowRef)
    expect(log).toEqual([
      // The scroll listener re-runs (its callbacks were re-keyed) but stays on
      // the element first promoted to state.
      'unlisten:scroll:a',
      'RO:disconnect',
      'listen:scroll:a',
      // The ResizeObserver follows the ref: rows, then the NEW scroller.
      'RO:new', 'RO:observe:0', 'RO:observe:1', 'RO:observe:2', 'RO:observe:b',
    ])
  })
})

describe('useVirtualChat: StrictMode mount', () => {
  const originalRO = globalThis.ResizeObserver
  const originalIO = globalThis.IntersectionObserver
  beforeEach(() => {
    localStorage.clear()
    CountingResizeObserver.created = 0
    CountingResizeObserver.disconnected = 0
    CountingIntersectionObserver.created = 0
    CountingIntersectionObserver.disconnected = 0
    globalThis.ResizeObserver = CountingResizeObserver as unknown as typeof ResizeObserver
    globalThis.IntersectionObserver = CountingIntersectionObserver as unknown as typeof IntersectionObserver
  })
  afterEach(() => {
    globalThis.ResizeObserver = originalRO
    globalThis.IntersectionObserver = originalIO
  })

  it('double-invoked effects leave exactly one live registration each, all released on unmount', async () => {
    const { StrictMode } = await import('react')
    const sink: HarnessProps['sink'] = { current: null }
    const { rerender, unmount } = render(<StrictMode><Harness sessionId="strict" sink={sink} /></StrictMode>)
    act(() => { rerender(<StrictMode><Harness sessionId="strict" sink={sink} /></StrictMode>) })
    expect({
      roCreated: CountingResizeObserver.created,
      roLive: CountingResizeObserver.created - CountingResizeObserver.disconnected,
      ioCreated: CountingIntersectionObserver.created,
      ioLive: CountingIntersectionObserver.created - CountingIntersectionObserver.disconnected,
    // The sentinel observers wait for the scroller element to reach state, so
    // StrictMode's simulated remount on the first commit never creates them.
    }).toEqual({ roCreated: 2, roLive: 1, ioCreated: 2, ioLive: 2 })
    unmount()
    expect(CountingResizeObserver.created).toBe(CountingResizeObserver.disconnected)
    expect(CountingIntersectionObserver.created).toBe(CountingIntersectionObserver.disconnected)
  })
})
