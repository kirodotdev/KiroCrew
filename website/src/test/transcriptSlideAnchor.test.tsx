/**
 * TranscriptScrollShell's slide anchor: while a side panel slides
 * (utils/panelSlide), the line the reader looks at keeps its place in the
 * viewport as the column rewraps, and the virtualizer's own reprice
 * compensation stands down for that scroller. At the bottom it stays out of
 * the way (the transcript follows the bottom); a reader's own vertical wheel
 * ends it.
 *
 * This DOM has no layout: the scroller's box and each block's rect are stood
 * in for, and a block's top answers to the scroll offset as it would in a
 * browser (it moves up by what scrollTop moves down).
 */
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import TranscriptScrollShell from '../pages/chat/TranscriptScrollShell'
import { beginPanelSlide, readingPositionHeld, releaseReadingPosition, retirePanelSlide } from '../utils/panelSlide'

const observers: FakeResizeObserver[] = []
class FakeResizeObserver {
  constructor(readonly callback: ResizeObserverCallback) { observers.push(this) }
  observe() {}
  disconnect() {}
  unobserve() {}
}
const fireAll = () => { for (const o of [...observers]) o.callback([], o as unknown as ResizeObserver) }

/** Each block's layout top at scrollTop 0, px; the reflow moves them. */
let layoutTop: Record<string, number> = {}
const HEIGHT = 60

function Shell() {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const topSentinelRef = useRef<HTMLDivElement | null>(null)
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null)
  return (
    <TranscriptScrollShell scrollerRef={scrollerRef} onScroll={() => {}} loadingOlder={false}
      virt={{ topSentinelRef, bottomSentinelRef, offsetBefore: 0, offsetAfter: 0 }}>
      <div data-display-index="0"><p data-b="a">above the line</p><p data-b="b">the line the reader is on</p><p data-b="c">below</p></div>
    </TranscriptScrollShell>
  )
}

function mount(scrollTop: number) {
  const utils = render(<Shell />)
  const sc = utils.container.querySelector<HTMLDivElement>('.chat-container')!
  Object.defineProperty(sc, 'clientHeight', { configurable: true, value: 600 })
  Object.defineProperty(sc, 'scrollHeight', { configurable: true, value: 5000 })
  sc.scrollTop = scrollTop
  sc.getBoundingClientRect = () => ({ top: 0, bottom: 600, left: 0, right: 800, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  for (const p of sc.querySelectorAll<HTMLElement>('p')) {
    p.getBoundingClientRect = () => {
      const top = layoutTop[p.dataset.b!] - sc.scrollTop
      return { top, bottom: top + HEIGHT, left: 0, right: 800, width: 800, height: HEIGHT, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    }
  }
  return { ...utils, sc, line: sc.querySelector<HTMLElement>('[data-b="b"]')! }
}

describe('transcript slide anchor', () => {
  beforeEach(() => {
    observers.length = 0
    vi.stubGlobal('ResizeObserver', FakeResizeObserver)
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance'] })
    // At scrollTop 1000: "a" ends above the 30% line (180px), "b" crosses it.
    layoutTop = { a: 1000, b: 1150, c: 1210 }
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('keeps the line under the reader in place while the column rewraps, through the slide and just after', () => {
    const { sc, line } = mount(1000)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
    expect(readingPositionHeld(sc)).toBe(true)
    const at = () => line.getBoundingClientRect().top
    expect(at()).toBe(150)
    // The column narrows: the text above rewraps 80px taller.
    layoutTop = { a: 1000, b: 1230, c: 1290 }
    act(() => { fireAll() })
    expect(at()).toBe(150)
    expect(sc.scrollTop).toBe(1080)
    act(() => { end() })
    // The held width lands after the slide: another 40px above.
    layoutTop = { a: 1000, b: 1270, c: 1330 }
    act(() => { vi.advanceTimersByTime(16) })
    expect(at()).toBe(150)
    // It lets go once the slide has settled.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    expect(readingPositionHeld(sc)).toBe(false)
  })

  it('stays out of the way at the bottom: the transcript follows the bottom there', () => {
    const { sc } = mount(4400)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    expect(readingPositionHeld(sc)).toBe(false)
    act(() => { end() })
  })

  it('a scrollbar press or a scrolling key ends it at once, so the next correction cannot undo it', () => {
    for (const input of [
      (sc: HTMLElement) => sc.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })),
      (sc: HTMLElement) => sc.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })),
      (sc: HTMLElement) => sc.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageDown', bubbles: true })),
      (sc: HTMLElement) => sc.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true })),
      (sc: HTMLElement) => sc.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })),
    ]) {
      layoutTop = { a: 1000, b: 1150, c: 1210 }
      const { sc, line, unmount } = mount(1000)
      const end = beginPanelSlide()
      expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
      act(() => { end() })
      // Within the hold after the panel rests: the reader moves the page.
      act(() => { input(sc) })
      expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
      expect(readingPositionHeld(sc)).toBe(false)
      layoutTop = { a: 1000, b: 1230, c: 1290 }
      act(() => { fireAll(); vi.advanceTimersByTime(16) })
      expect(line.getBoundingClientRect().top).toBe(230)
      unmount()
    }
  })

  it('explicit navigation (a jump to the bottom or to a message) takes the position from it', () => {
    const { sc, line } = mount(1000)
    const end = beginPanelSlide()
    act(() => { end() })
    // Within the hold: the jump releases the anchor before it writes...
    act(() => { releaseReadingPosition(sc) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    expect(readingPositionHeld(sc)).toBe(false)
    // ...so the jump's own position stands: nothing pulls the reader back.
    sc.scrollTop = 4400
    layoutTop = { a: 1000, b: 1230, c: 1290 }
    act(() => { fireAll(); vi.advanceTimersByTime(100) })
    expect(sc.scrollTop).toBe(4400)
    expect(line.getBoundingClientRect().top).toBe(1230 - 4400)
  })

  it("a sideways wheel the page's swipe cancelled does not end it", () => {
    const { sc } = mount(1000)
    const claim = (e: Event) => e.preventDefault()
    document.body.addEventListener('wheel', claim)
    const end = beginPanelSlide()
    act(() => { sc.dispatchEvent(new WheelEvent('wheel', { deltaX: -20, deltaY: 2, bubbles: true, cancelable: true })) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
    document.body.removeEventListener('wheel', claim)
    act(() => { end() })
  })

  it('at the bottom, the slide landing is pinned in the same pass: no frame paints unpinned', () => {
    let height = 5000
    const { sc } = mount(4400)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    // Mid-slide the column rewraps: the scroller's resize is corrected in the
    // ResizeObserver pass, after layout and before paint.
    height = 5100
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(4500)
    // The landing publishes the held width; the rows grow by a message's
    // height in that same commit. The pin is already there when end()
    // returns, with no frame or timer in between.
    height = 5280
    end()
    expect(sc.scrollTop).toBe(4680)
    // The virtualizer re-measures a frame later: still corrected pre-paint.
    height = 5300
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(4700)
    act(() => { vi.advanceTimersByTime(1000) })
  })

  it('a slide dropped by a session switch holds nothing past it: the next session keeps its own position', () => {
    let height = 5000
    const { sc } = mount(4400)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    // Session B restores mid-history on the same scroller, then the slide is
    // retired (useChatPanelTrackpadSwipe's retire on the session change).
    sc.scrollTop = 1200
    retirePanelSlide()
    end()
    height = 5300
    act(() => { fireAll(); vi.advanceTimersByTime(100) })
    expect(sc.scrollTop).toBe(1200)
  })

  it("a restore lets go of the bottom hold before it writes", () => {
    let height = 5000
    const { sc } = mount(4400)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    act(() => { end() })
    act(() => { releaseReadingPosition(sc) })
    sc.scrollTop = 1200
    height = 5300
    act(() => { fireAll(); vi.advanceTimersByTime(100) })
    expect(sc.scrollTop).toBe(1200)
  })

  it("a reader's own vertical wheel ends it at once", () => {
    const { sc, line } = mount(1000)
    const end = beginPanelSlide()
    act(() => { sc.dispatchEvent(new WheelEvent('wheel', { deltaY: 40, bubbles: true })) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    layoutTop = { a: 1000, b: 1230, c: 1290 }
    act(() => { fireAll() })
    expect(line.getBoundingClientRect().top).toBe(230)
    act(() => { end() })
  })
})
