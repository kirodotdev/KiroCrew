/** Viewport-coverage watchdog with the live edge at the TOP. A follower whose
 *  window silently stops covering the viewport (no scroll, no resize) is
 *  re-pinned to the live edge, scrollTop 0. A released reader keeps their
 *  position and the window is re-covered where they are. Modelled on
 *  useVirtualChat.coverageWatchdog.test.tsx. */
import { act, render } from '@testing-library/react'
import { type RefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

type Item = { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m-${i}` }))

const ROW = 100
const CLIENT = 400

function Harness({ items, scrollerRef }: { items: Item[]; scrollerRef: RefObject<HTMLDivElement | null> }) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'watchdog-top', getKey, overscan: 2, externalScrollerRef: scrollerRef, followEdge: 'top',
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <div key={it.key} data-index={it.index} ref={v.measureRef(it.index)} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

/** Rows report ROW from their first render (the test DOM's own offsetHeight
 *  is 0, and a row that reads 0 at mount is never recorded). */
function installRowHeights(): () => void {
  const proto = HTMLElement.prototype
  const original = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')
  Object.defineProperty(proto, 'offsetHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return this.getAttribute('data-index') !== null ? ROW : 0
    },
  })
  return () => {
    if (original) Object.defineProperty(proto, 'offsetHeight', original)
    else Reflect.deleteProperty(proto, 'offsetHeight')
  }
}

function installFakeLayout(scroller: HTMLElement) {
  const proto = HTMLElement.prototype
  const origRect = proto.getBoundingClientRect
  const childHeight = (child: Element): number => {
    if ((child as HTMLElement).getAttribute('data-index') !== null) return ROW
    const h = (child as HTMLElement).style?.height
    return h ? parseFloat(h) : 0
  }
  proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
    const rect = (y: number, h: number) =>
      ({ top: y, bottom: y + h, height: h, left: 0, right: 390, width: 390, x: 0, y, toJSON: () => ({}) }) as DOMRect
    if (this === scroller) return rect(0, CLIENT)
    if (this.parentElement === scroller) {
      let y = 0
      for (const sib of Array.from(scroller.children)) {
        if (sib === this) break
        y += childHeight(sib)
      }
      return rect(y - scroller.scrollTop, childHeight(this))
    }
    return origRect.call(this)
  }
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => CLIENT })
  Object.defineProperty(scroller, 'scrollHeight', {
    configurable: true,
    get: () => Array.from(scroller.children).reduce((a, c) => a + childHeight(c), 0),
  })
  return () => {
    proto.getBoundingClientRect = origRect
  }
}

function mountedSpan(el: HTMLElement): [number, number] {
  const rows = [...el.querySelectorAll('[data-index]')].map((r) => Number(r.getAttribute('data-index')))
  return rows.length ? [Math.min(...rows), Math.max(...rows)] : [-1, -1]
}

describe('viewport-coverage watchdog, followEdge top', () => {
  let restore: (() => void) | null = null
  let restoreRowHeights: (() => void) | null = null
  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    restoreRowHeights = installRowHeights()
  })
  afterEach(() => {
    restore?.()
    restore = null
    restoreRowHeights?.()
    restoreRowHeights = null
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  function mountAtTop() {
    const scrollerRef = { current: null as HTMLDivElement | null }
    render(<Harness items={mkItems(200)} scrollerRef={scrollerRef as RefObject<HTMLDivElement | null>} />)
    const el = scrollerRef.current as HTMLDivElement
    restore = installFakeLayout(el)
    return el
  }

  it('a FOLLOWER silently displaced off the head window is re-pinned to scrollTop 0', async () => {
    const el = mountAtTop()
    await act(async () => {
      el.scrollTop = 0
      el.dispatchEvent(new Event('scroll'))
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(mountedSpan(el)[0]).toBe(0) // following => head-anchored window
    // A displacement with no scroll event: nothing but the watchdog will see it.
    await act(async () => {
      el.scrollTop = 20 * ROW
      await vi.advanceTimersByTimeAsync(1200 + 600)
    })
    expect(el.scrollTop).toBe(0)
    expect(mountedSpan(el)[0]).toBe(0)
  })

  it('a RELEASED reader is not moved; the window is re-covered where they are', async () => {
    const el = mountAtTop()
    // Scrolling DOWN from the live edge releases follow.
    await act(async () => {
      el.scrollTop = 0
      el.dispatchEvent(new Event('scroll'))
      await vi.advanceTimersByTimeAsync(50)
      el.scrollTop = 10 * ROW
      el.dispatchEvent(new Event('scroll'))
      await vi.advanceTimersByTimeAsync(600)
    })
    await act(async () => {
      el.scrollTop = 60 * ROW
      await vi.advanceTimersByTimeAsync(1200 + 600)
    })
    expect(el.scrollTop).toBe(60 * ROW)
    const [lo, hi] = mountedSpan(el)
    expect(lo).toBeLessThanOrEqual(60)
    expect(hi).toBeGreaterThanOrEqual(63)
  })
})
