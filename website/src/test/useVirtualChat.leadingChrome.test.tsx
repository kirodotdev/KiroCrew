/** Chrome ABOVE the rows inside the scroller (the earlier-messages bar) mounting
 *  under a bottom-pinned reader: nothing observes it -- not the row observer,
 *  not the scroller's box, no scroll event -- so without native scroll
 *  anchoring (WebKit) the reader was left the bar's height short of the end.
 *  Measured 46px on the phone rig at every drawer entry into a long session. */
import { act, render } from '@testing-library/react'
import { type RefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

type Item = { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m-${i}` }))

function Harness({ items, scrollerRef, headChrome, runActive }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  /** Height of page chrome sharing the scroller ABOVE the rows. */
  headChrome: number
  runActive: boolean
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'lead', getKey, overscan: 2, externalScrollerRef: scrollerRef, runActive,
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      {headChrome > 0 && <div data-chrome="head" style={{ height: headChrome }} />}
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

const ROW = 100
const CLIENT = 400

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
  Object.defineProperty(proto, 'offsetHeight', {
    configurable: true,
    get(this: HTMLElement) { return this.getAttribute('data-index') !== null ? ROW : 0 },
  })
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => CLIENT })
  Object.defineProperty(scroller, 'scrollHeight', {
    configurable: true,
    get: () => Array.from(scroller.children).reduce((a, c) => a + childHeight(c), 0),
  })
  return () => { proto.getBoundingClientRect = origRect }
}

describe('leading chrome under a bottom-pinned reader', () => {
  let restore: (() => void) | null = null
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { restore?.(); restore = null; vi.useRealTimers() })

  async function mountAtBottom(runActive: boolean) {
    const scrollerRef = { current: null as HTMLDivElement | null }
    const items = mkItems(200)
    const view = render(<Harness items={items} scrollerRef={scrollerRef} headChrome={0} runActive={runActive} />)
    const el = scrollerRef.current as HTMLDivElement
    restore = installFakeLayout(el)
    await act(async () => {
      el.scrollTop = el.scrollHeight - CLIENT
      el.dispatchEvent(new Event('scroll'))
      await vi.advanceTimersByTimeAsync(600)
    })
    // jsdom has no ResizeObserver, so a reprice landing after the settle write is
    // not re-pinned here; the reader may sit a few px above the bottom. That is
    // the harness, not the contract -- what matters below is where the commit
    // that mounts the bar leaves them.
    return { view, el, items, scrollerRef }
  }

  it('the bar mounting above the rows carries a still IDLE reader to the new bottom', async () => {
    const { view, el, items, scrollerRef } = await mountAtBottom(false)
    // No scroll event, no row resize: only the commit that mounts the bar.
    await act(async () => {
      view.rerender(<Harness items={items} scrollerRef={scrollerRef} headChrome={46} runActive={false} />)
    })
    expect(el.scrollHeight - el.scrollTop - CLIENT).toBe(0)
  })

  it('a reader who wheeled since the pin is left where they are', async () => {
    const { view, el, items, scrollerRef } = await mountAtBottom(false)
    const parked = el.scrollTop
    await act(async () => { el.dispatchEvent(new Event('wheel')); await vi.advanceTimersByTimeAsync(200) })
    await act(async () => {
      view.rerender(<Harness items={items} scrollerRef={scrollerRef} headChrome={46} runActive={false} />)
    })
    expect(el.scrollTop).toBe(parked)
  })
})
