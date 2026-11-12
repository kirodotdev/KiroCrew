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

type Hold = (el: HTMLDivElement, top: number) => void

function Shell({ historyAtBottom = false, liveEdgeAtTop, paddingTop, hold }: { historyAtBottom?: boolean; liveEdgeAtTop?: boolean; paddingTop?: number; hold?: Hold }) {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const topSentinelRef = useRef<HTMLDivElement | null>(null)
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null)
  return (
    <TranscriptScrollShell scrollerRef={scrollerRef} onScroll={() => {}} loadingOlder={false} historyAtBottom={historyAtBottom} liveEdgeAtTop={liveEdgeAtTop}
      scrollerStyle={paddingTop === undefined ? undefined : { paddingTop }}
      virt={{ topSentinelRef, bottomSentinelRef, offsetBefore: 0, offsetAfter: 0, holdScrollTop: hold }}>
      <div data-display-index="0"><p data-b="a">above the line</p><p data-b="b">the line the reader is on</p><p data-b="c">below</p></div>
    </TranscriptScrollShell>
  )
}

function mount(scrollTop: number, historyAtBottom = false, liveEdgeAtTop?: boolean, paddingTop?: number, hold?: Hold) {
  const utils = render(<Shell historyAtBottom={historyAtBottom} liveEdgeAtTop={liveEdgeAtTop} paddingTop={paddingTop} hold={hold} />)
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

  it('top-down layout: a reader at the top, where the newest turn is, stays at scrollTop 0 through the slide', () => {
    layoutTop = { a: 0, b: 150, c: 210 }
    const { sc } = mount(0, true)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    expect(readingPositionHeld(sc)).toBe(false)
    // The column rewraps 80px taller above the 30% line, and the browser's
    // own anchoring moved the scroller: the hold writes it back to 0.
    layoutTop = { a: 0, b: 230, c: 290 }
    sc.scrollTop = 80
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(0)
    act(() => { end() })
    sc.scrollTop = 40
    act(() => { vi.advanceTimersByTime(16) })
    expect(sc.scrollTop).toBe(0)
    act(() => { vi.advanceTimersByTime(1000) })
  })

  it('top-down split pane: a host that places its own paging bar holds the top reader with liveEdgeAtTop alone', () => {
    layoutTop = { a: 0, b: 150, c: 210 }
    const { sc } = mount(0, false, true)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    layoutTop = { a: 0, b: 230, c: 290 }
    sc.scrollTop = 80
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(0)
    act(() => { end(); vi.advanceTimersByTime(1000) })
  })

  it('top-down layout: a reader scrolled into history keeps the block anchor', () => {
    const { sc, line } = mount(1000, true)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
    layoutTop = { a: 1000, b: 1230, c: 1290 }
    act(() => { fireAll() })
    expect(line.getBoundingClientRect().top).toBe(150)
    act(() => { end(); vi.advanceTimersByTime(1000) })
  })

  // The top-edge follower reads a scroll it did not write as the reader's, so
  // in top-down the slide's corrections write through the virtualizer.
  it("top-down layout: the block-anchor correction writes through the virtualizer's holdScrollTop", () => {
    const hold = vi.fn<Hold>((el, top) => { el.scrollTop = top })
    const { sc, line } = mount(1000, true, undefined, undefined, hold)
    const end = beginPanelSlide()
    try {
      layoutTop = { a: 1000, b: 1230, c: 1290 }
      act(() => { fireAll() })
      expect(hold).toHaveBeenCalledWith(sc, 1080)
      expect(line.getBoundingClientRect().top).toBe(150)
    } finally {
      // A failed assertion must not leave the slide open for the next test.
      act(() => { end(); vi.advanceTimersByTime(1000) })
    }
  })

  it("top-down layout: the top pin writes 0 through the virtualizer's holdScrollTop", () => {
    layoutTop = { a: 0, b: 150, c: 210 }
    const hold = vi.fn<Hold>((el, top) => { el.scrollTop = top })
    const { sc } = mount(0, true, undefined, undefined, hold)
    const end = beginPanelSlide()
    try {
      layoutTop = { a: 0, b: 230, c: 290 }
      sc.scrollTop = 80
      act(() => { fireAll() })
      expect(hold).toHaveBeenCalledWith(sc, 0)
      expect(sc.scrollTop).toBe(0)
    } finally {
      act(() => { end(); vi.advanceTimersByTime(1000) })
    }
  })

  it('bottom layout: the block-anchor correction writes scrollTop itself, as before', () => {
    const hold = vi.fn<Hold>((el, top) => { el.scrollTop = top })
    const { sc, line } = mount(1000, false, undefined, undefined, hold)
    const end = beginPanelSlide()
    try {
      layoutTop = { a: 1000, b: 1230, c: 1290 }
      act(() => { fireAll() })
      expect(sc.scrollTop).toBe(1080)
      expect(line.getBoundingClientRect().top).toBe(150)
      expect(hold).not.toHaveBeenCalled()
    } finally {
      act(() => { end(); vi.advanceTimersByTime(1000) })
    }
  })

  it('top-down layout: a reader at the history end keeps their content when an older page lands mid-slide', () => {
    // At the loaded-history bottom: "a" ends above the 30% line, "b" crosses it.
    layoutTop = { a: 4400, b: 4550, c: 4610 }
    let height = 5000
    const { sc, line } = mount(4400, true)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    try {
      expect(line.getBoundingClientRect().top).toBe(150)
      // The pending older page lands below the reader during the hold.
      height = 6000
      act(() => { fireAll() })
      expect(sc.scrollTop).toBe(4400)
      expect(line.getBoundingClientRect().top).toBe(150)
      act(() => { end(); vi.advanceTimersByTime(1000) })
      expect(sc.scrollTop).toBe(4400)
    } finally {
      // A failed assertion must not leave the slide open for the next test.
      act(() => { end(); vi.advanceTimersByTime(1000) })
    }
  })

  it('bottom layout: a reader at the bottom is still pinned there when content grows mid-slide', () => {
    layoutTop = { a: 4400, b: 4550, c: 4610 }
    let height = 5000
    const { sc } = mount(4400)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    try {
      height = 6000
      act(() => { fireAll() })
      expect(sc.scrollTop).toBe(5400)
    } finally {
      act(() => { end(); vi.advanceTimersByTime(1000) })
    }
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

  it("at the bottom, a downward wheel (a trackpad's momentum tail) keeps the pin; an upward one ends it", () => {
    let height = 5000
    const { sc } = mount(4400)
    Object.defineProperty(sc, 'scrollHeight', { configurable: true, get: () => height })
    const end = beginPanelSlide()
    act(() => { sc.dispatchEvent(new WheelEvent('wheel', { deltaY: 3, bubbles: true })) })
    height = 5100
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(4500)
    act(() => { sc.dispatchEvent(new WheelEvent('wheel', { deltaY: -3, bubbles: true })) })
    height = 5200
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(4500)
    act(() => { end(); vi.advanceTimersByTime(1000) })
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

  it('top-down layout: a padding decrease holds a reader at the same content', () => {
    const view = (paddingTop: number) => <Shell liveEdgeAtTop paddingTop={paddingTop} />
    const { container, rerender } = render(view(200))
    const sc = container.querySelector<HTMLDivElement>('.chat-container')!
    let scrollTop = 300
    let heldDuringWrite = false
    const writes: number[] = []
    Object.defineProperty(sc, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (next: number) => {
        scrollTop = next
        heldDuringWrite = readingPositionHeld(sc)
        writes.push(next)
      },
    })

    act(() => { rerender(view(150)) })

    expect(sc.style.paddingTop).toBe('150px')
    expect(scrollTop).toBe(250)
    expect(writes).toEqual([250])
    expect(heldDuringWrite).toBe(false)
  })

  it('top-down layout: a padding change keeps a follower at scrollTop 0', () => {
    const view = (paddingTop: number) => <Shell liveEdgeAtTop paddingTop={paddingTop} />
    const { container, rerender } = render(view(200))
    const sc = container.querySelector<HTMLDivElement>('.chat-container')!
    let writes = 0
    Object.defineProperty(sc, 'scrollTop', {
      configurable: true,
      get: () => 0,
      set: () => { writes += 1 },
    })

    act(() => { rerender(view(150)) })

    expect(sc.style.paddingTop).toBe('150px')
    expect(sc.scrollTop).toBe(0)
    expect(writes).toBe(0)
  })

  it('clears effect-owned top padding when the live edge moves back to the bottom', () => {
    const { container, rerender } = render(<Shell liveEdgeAtTop paddingTop={200} />)
    const sc = container.querySelector<HTMLDivElement>('.chat-container')!

    act(() => { rerender(<Shell liveEdgeAtTop={false} />) })

    expect(sc.style.paddingTop).toBe('')
  })

  it('keeps the slide anchor in control when top padding changes during a slide', () => {
    const { sc, rerender } = mount(1000, false, true, 200)
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
    expect(readingPositionHeld(sc)).toBe(true)

    act(() => { rerender(<Shell liveEdgeAtTop paddingTop={150} />) })

    expect(sc.style.paddingTop).toBe('150px')
    expect(sc.scrollTop).toBe(950)
    expect(readingPositionHeld(sc)).toBe(true)
    act(() => { releaseReadingPosition(sc) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    expect(readingPositionHeld(sc)).toBe(false)
    act(() => { end() })
  })

  it('a padding change before and during a slide leaves the slide anchor its hold and its release', () => {
    const { sc, rerender } = mount(1000, false, true, 200)
    act(() => { rerender(<Shell liveEdgeAtTop paddingTop={180} />) })
    const end = beginPanelSlide()
    expect(sc.hasAttribute('data-slide-anchor')).toBe(true)
    act(() => { rerender(<Shell liveEdgeAtTop paddingTop={150} />) })
    act(() => { vi.advanceTimersByTime(16) })
    expect(readingPositionHeld(sc)).toBe(true)
    act(() => { releaseReadingPosition(sc) })
    expect(sc.hasAttribute('data-slide-anchor')).toBe(false)
    act(() => { end() })
  })

  // Top-down has no history-end pin (see the older-page test above), so a
  // padding change there during a slide leaves nothing that holds the reader.
  it('a padding change at the history end during a slide pins nothing there', () => {
    const { sc, rerender } = mount(4400, false, true, 200)
    const end = beginPanelSlide()
    expect(readingPositionHeld(sc)).toBe(false)
    act(() => { rerender(<Shell liveEdgeAtTop paddingTop={150} />) })
    expect(readingPositionHeld(sc)).toBe(false)
    act(() => { vi.advanceTimersByTime(16) })
    act(() => { releaseReadingPosition(sc) })
    sc.scrollTop = 1200
    act(() => { fireAll() })
    expect(sc.scrollTop).toBe(1200)
    act(() => { end() })
  })

  it('bottom layout leaves scrollTop to the browser when padding changes', () => {
    const view = (paddingTop: number) => <Shell liveEdgeAtTop={false} paddingTop={paddingTop} />
    const { container, rerender } = render(view(200))
    const sc = container.querySelector<HTMLDivElement>('.chat-container')!
    let scrollTop = 300
    let writes = 0
    Object.defineProperty(sc, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (next: number) => { scrollTop = next; writes += 1 },
    })

    act(() => { rerender(view(150)) })

    expect(sc.style.paddingTop).toBe('150px')
    expect(scrollTop).toBe(300)
    expect(writes).toBe(0)
  })
})
