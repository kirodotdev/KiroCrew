/**
 * A bulk PREPEND while followed at the bottom must NOT take the hydration
 * force-pin path.
 *
 * The bulk-growth branch exists for hydration (a thin optimistic list
 * REPLACED by the full conversation) and force-pins to the bottom. The idle
 * history prefetch introduced a second legitimate bulk growth: hundreds of
 * rows PREPENDED above a reader who is followed at the bottom, reading or
 * typing. Routing that through force-pin remounted the tail window under the
 * reader (a visible flash while typing) and raced the async scroll event of a
 * reader who had JUST started scrolling up — yanking them back to the bottom
 * ("滑着滑着弹回底部"). The discriminator is TAIL IDENTITY: a prepend keeps
 * the tail item, a replace does not.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import React, { useRef, type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id

const writes: number[] = []

function Harness({ items, scrollerRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'bulk-prepend', getKey, overscan: 2, externalScrollerRef: scrollerRef,
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <div key={it.key} data-index={it.index} data-key={it.key} ref={v.measureRef(it.index)} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

function Wrapper({ items }: { items: Item[] }) {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  return <Harness items={items} scrollerRef={scrollerRef} />
}

/** jsdom elements have no layout; give the scroller live-ish scroll geometry
 *  and record every programmatic scrollTo. */
function instrumentScroller(container: HTMLElement) {
  const el = container.querySelector('[data-scroller]') as HTMLElement
  let top = 0
  Object.defineProperty(el, 'scrollTop', {
    get: () => top,
    set: (v: number) => { top = v },
    configurable: true,
  })
  Object.defineProperty(el, 'scrollHeight', { get: () => 10000, configurable: true })
  Object.defineProperty(el, 'clientHeight', { get: () => 600, configurable: true })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
    writes.push(Math.round(typeof o === 'object' ? o.top : (o as number)))
    top = typeof o === 'object' ? o.top : (o as number)
  }
  return el
}

describe('bulk growth: prepend vs hydration', () => {
  beforeEach(() => { writes.length = 0; vi.restoreAllMocks() })

  it('a bulk PREPEND never yanks a reader who scrolled UP off the bottom', () => {
    const initial = Array.from({ length: 6 }, (_, i) => ({ id: `m${i}` }))
    const { container, rerender } = render(<Wrapper items={initial} />)
    const el = instrumentScroller(container)
    // Reader scrolls up: an upward non-self move releases follow. This is
    // the '滑着滑着弹回底部' shape the guard exists for.
    act(() => { el.scrollTop = 9400; el.dispatchEvent(new Event('scroll')) })
    act(() => { el.scrollTop = 4000; el.dispatchEvent(new Event('scroll')) })
    writes.length = 0
    const prepended = [
      ...Array.from({ length: 300 }, (_, i) => ({ id: `old${i}` })),
      ...initial,
    ]
    act(() => { rerender(<Wrapper items={prepended} />) })
    // The reader is HELD, not yanked: the only write allowed is the
    // anchor-miss fallback's position-holding advance -- scrollTop moved by
    // exactly the inserted block's tree height (300 rows at the 80px
    // estimate; the mocked scroller never measures), keeping the same rows
    // on screen. A snap to the bottom band -- the yank this guard exists
    // for -- would be a different value entirely.
    expect(writes).toEqual([4000 + 300 * 80])
  })

  it('a bulk PREPEND holds a FOLLOWED reader at the bottom with a pin write', () => {
    const initial = Array.from({ length: 6 }, (_, i) => ({ id: `m${i}` }))
    const { container, rerender } = render(<Wrapper items={initial} />)
    const el = instrumentScroller(container)
    // Parked at the bottom: follow armed (non-self scroll landing at 0 from
    // the bottom band re-arms stick).
    act(() => { el.scrollTop = 9400; el.dispatchEvent(new Event('scroll')) })
    writes.length = 0
    const prepended = [
      ...Array.from({ length: 300 }, (_, i) => ({ id: `old${i}` })),
      ...initial,
    ]
    act(() => { rerender(<Wrapper items={prepended} />) })
    // The parked view is held by a pre-paint bottom re-target.
    expect(writes.some((w) => w >= 9000)).toBe(true)
  })

  it('a bulk REPLACE (tail changed) still takes the hydration force-pin', () => {
    const initial = [{ id: 'optimistic-tail' }]
    const { container, rerender } = render(<Wrapper items={initial} />)
    instrumentScroller(container)
    writes.length = 0
    const hydrated = Array.from({ length: 50 }, (_, i) => ({ id: `h${i}` }))
    act(() => { rerender(<Wrapper items={hydrated} />) })
    // Hydration force-pins: at least one write lands at/near the bottom.
    expect(writes.some((w) => w >= 9000)).toBe(true)
  })
})
