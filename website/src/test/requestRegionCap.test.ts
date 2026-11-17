import { describe, it, expect, afterEach, vi, onTestFinished } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { REQUEST_REGION_MIN_PX, requestRegionCap, topBound, useRequestRegionCap } from '../components/chat-input/requestRegionCap'

const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 100, width: 100, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect

afterEach(() => { document.body.replaceChildren() })

/** A pane (clipping, top at `paneTop`) holding a card whose region spans the
 *  given box; the region's content is `content` px tall (its box by default). */
function layout({ paneTop, cardTop, regionTop, regionBottom, content }: { paneTop: number; cardTop: number; regionTop: number; regionBottom: number; content?: number }) {
  const pane = document.createElement('div')
  pane.style.overflowY = 'hidden'
  pane.getBoundingClientRect = () => box(paneTop, 1000)
  const card = document.createElement('div')
  card.getBoundingClientRect = () => box(cardTop, regionBottom + 40)
  const region = document.createElement('div')
  region.getBoundingClientRect = () => box(regionTop, regionBottom)
  Object.defineProperty(region, 'scrollHeight', { configurable: true, value: content ?? regionBottom - regionTop })
  card.appendChild(region)
  pane.appendChild(card)
  document.body.appendChild(pane)
  return { card, region }
}

describe('requestRegionCap', () => {
  it('keeps the card below the top of the pane that clips it', () => {
    const { card, region } = layout({ paneTop: 100, cardTop: 50, regionTop: 90, regionBottom: 600 })
    expect(topBound(card)).toBe(100)
    // 600 - 100 - (90 - 50) = 460
    expect(requestRegionCap(card, region)).toBe(460)
  })

  it('keeps the card below the title overlay when that is lower', () => {
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const overlay = document.createElement('div')
    overlay.setAttribute('data-transcript-top-overlay', '')
    overlay.getBoundingClientRect = () => box(0, 72)
    document.body.appendChild(overlay)
    // Overlay bottom 72 + 8px gap = 80; 400 - 80 - 40 = 280
    expect(topBound(card)).toBe(80)
    expect(requestRegionCap(card, region)).toBe(280)
  })

  it('in split view, only the card\'s own pane\'s overlay bounds it', () => {
    // Upper pane (top 0) holds the card; the lower pane's pinned prompt ends at
    // 530px, below the upper card, and covers nothing of it.
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 360 })
    card.parentElement!.setAttribute('data-chat-pane', '')
    const lower = document.createElement('div')
    lower.setAttribute('data-chat-pane', 'focused')
    const banner = document.createElement('div')
    banner.setAttribute('data-testid', 'pinned-prompt')
    banner.getBoundingClientRect = () => box(480, 530)
    lower.appendChild(banner)
    document.body.appendChild(lower)
    expect(topBound(card)).toBe(0)
    // 360 - 0 - 40
    expect(requestRegionCap(card, region)).toBe(320)
  })

  it('keeps the card below the visual viewport\'s top when an iOS keyboard pans the page', () => {
    const vv = Object.assign(new EventTarget(), { offsetTop: 0, height: 400 })
    vi.stubGlobal('visualViewport', vv)
    onTestFinished(() => { vi.unstubAllGlobals() })
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const { result } = renderHook(() => useRequestRegionCap(card, region))
    expect(result.current).toEqual({ cap: 360, cramped: false })
    // The keyboard opens and the page pans up 300px: 400 - 300 - 40 < 64.
    vv.offsetTop = 300
    act(() => { vv.dispatchEvent(new Event('scroll')) })
    expect(topBound(card)).toBe(300)
    expect(result.current).toEqual({ cap: REQUEST_REGION_MIN_PX, cramped: true })
  })

  it('in split view, keeps the card below its pane\'s own title row', () => {
    // The pane renders no transcript overlay; its title row (z-50) paints over the dock.
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    pane.setAttribute('data-chat-pane', '')
    const title = document.createElement('div')
    title.setAttribute('data-pane-title-row', '')
    title.getBoundingClientRect = () => box(0, 44)
    pane.prepend(title)
    // 44 + 8 gap = 52; 400 - 52 - 40
    expect(topBound(card)).toBe(52)
    expect(requestRegionCap(card, region)).toBe(308)
  })

  it('keeps the card below a host band floating over its pane (the Members DM header)', () => {
    const { card } = layout({ paneTop: 100, cardTop: 110, regionTop: 150, regionBottom: 500 })
    const pane = card.parentElement!
    pane.setAttribute('data-chat-pane', '')
    pane.setAttribute('data-pane-top-inset', '56')
    // 100 + 56 + 8
    expect(topBound(card)).toBe(164)
  })

  it('a forged title row placed as the pane\'s own child but inside untrusted content bounds nothing', () => {
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    pane.setAttribute('data-chat-pane', '')
    const fake = document.createElement('div')
    fake.setAttribute('data-guide-untrusted', '')
    fake.setAttribute('data-pane-title-row', '')
    fake.getBoundingClientRect = () => box(300, 420)
    pane.prepend(fake)
    expect(topBound(card)).toBe(0)
    expect(requestRegionCap(card, region)).toBe(360)
  })

  it('in a frameless pane, a message carrying the title-row marker bounds nothing', () => {
    // A frameless pane renders no title row; a transcript message (untrusted
    // markdown, nested well below the pane root) carries the marker instead.
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    pane.setAttribute('data-chat-pane', '')
    const transcript = document.createElement('div')
    const message = document.createElement('div')
    message.setAttribute('data-guide-untrusted', '')
    const fake = document.createElement('div')
    fake.setAttribute('data-pane-title-row', '')
    fake.getBoundingClientRect = () => box(300, 420)
    message.appendChild(fake)
    transcript.appendChild(message)
    pane.prepend(transcript)
    expect(topBound(card)).toBe(0)
    expect(requestRegionCap(card, region)).toBe(360)
  })

  /** A rendered message (the markdown renderer's untrusted root) holding one
   *  element that carries a forged marker, its box low enough to cut the card. */
  function forgedInMessage(attr: string, value: string, into: ParentNode = document.body) {
    const message = document.createElement('div')
    message.setAttribute('data-guide-untrusted', '')
    const fake = document.createElement('div')
    fake.setAttribute(attr, value)
    fake.getBoundingClientRect = () => box(300, 420)
    message.appendChild(fake)
    into.appendChild(message)
    return fake
  }

  // Forged markers in a message: every marker the cap looks up, one test each.
  it.each([
    ['the title overlay', 'data-transcript-top-overlay', ''],
    ['the pinned prompt', 'data-testid', 'pinned-prompt'],
  ])('a message forging %s bounds nothing', (_name, attr, value) => {
    // A message's markdown keeps data attributes, so it can carry the overlay's
    // own markers; inside the renderer's untrusted root they bound nothing.
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    forgedInMessage(attr, value)
    expect(topBound(card)).toBe(0)
    expect(requestRegionCap(card, region)).toBe(360)
  })

  it('a message forging a split-view pane (with a top inset) around the card is not taken as its pane', () => {
    // Contrived on purpose: untrusted content cannot wrap the composer today,
    // but the pane lookup still refuses a pane inside it, and with it the
    // inset that pane would carry.
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    const message = document.createElement('div')
    message.setAttribute('data-guide-untrusted', '')
    message.setAttribute('data-chat-pane', '')
    message.setAttribute('data-pane-top-inset', '300')
    message.getBoundingClientRect = () => box(0, 1000)
    pane.appendChild(message)
    message.appendChild(card)
    expect(topBound(card)).toBe(0)
    expect(requestRegionCap(card, region)).toBe(360)
  })

  it('a message forging the composer dock around the card is not observed as its dock', () => {
    const observed: Element[] = []
    class FakeRO { observe(el: Element) { observed.push(el) } disconnect() {} unobserve() {} }
    vi.stubGlobal('ResizeObserver', FakeRO)
    onTestFinished(() => { vi.unstubAllGlobals() })
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    const forged = document.createElement('div')
    forged.setAttribute('data-guide-untrusted', '')
    forged.setAttribute('data-testid', 'composer-dock-root')
    const wrapper = document.createElement('div')
    pane.appendChild(forged)
    forged.appendChild(wrapper)
    wrapper.appendChild(card)
    renderHook(() => useRequestRegionCap(card, region))
    expect(observed).not.toContain(forged)
    expect(observed).toContain(wrapper)
  })

  it('does not count a top overlay that holds the card itself (its own dock in a top-down layout)', () => {
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    // The dock is marked as a top overlay and wraps the card; its box ends below the card.
    const dock = document.createElement('div')
    dock.setAttribute('data-transcript-top-overlay', '')
    dock.getBoundingClientRect = () => box(0, 460)
    card.parentElement!.appendChild(dock)
    dock.appendChild(card)
    expect(topBound(card)).toBe(0)
    // 400 - 0 - 40
    expect(requestRegionCap(card, region)).toBe(360)
  })

  it('re-measures when a pane that clips the card resizes (a split-view divider)', () => {
    const observers: { cb: () => void; els: Element[] }[] = []
    class FakeRO {
      els: Element[] = []
      constructor(public cb: () => void) { observers.push(this) }
      observe(el: Element) { this.els.push(el) }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal('ResizeObserver', FakeRO)
    onTestFinished(() => { vi.unstubAllGlobals() })
    let paneTop = 0
    const { card, region } = layout({ paneTop: 0, cardTop: 10, regionTop: 50, regionBottom: 400 })
    const pane = card.parentElement!
    pane.getBoundingClientRect = () => box(paneTop, 1000)
    // The dock between them, as the page nests it: the dock's own box does not change.
    const dock = document.createElement('div')
    dock.setAttribute('data-testid', 'composer-dock-root')
    pane.appendChild(dock)
    dock.appendChild(card)
    const { result } = renderHook(() => useRequestRegionCap(card, region))
    expect(result.current).toEqual({ cap: 360, cramped: false })
    const ro = observers.find(o => o.els.includes(pane))
    expect(ro).toBeTruthy()
    // The divider moves the pane's top down to 380px: 400 - 380 - 40 < 64.
    paneTop = 380
    act(() => { ro!.cb() })
    expect(result.current).toEqual({ cap: REQUEST_REGION_MIN_PX, cramped: true })
  })

  it('a short request that is fully on screen is not cramped, however little room is left above it', () => {
    // A call with no input: a 38px region with the card's top 12px below the
    // pane's. Room 400 - 350 - 0 = 50, under the 64px floor, but the region
    // needs only 38px of it, so nothing is cut.
    const { card, region } = layout({ paneTop: 350, cardTop: 362, regionTop: 362, regionBottom: 400 })
    const { result } = renderHook(() => useRequestRegionCap(card, region))
    expect(result.current).toEqual({ cap: REQUEST_REGION_MIN_PX, cramped: false })
  })

  it('never caps below a few legible lines', () => {
    const { card, region } = layout({ paneTop: 380, cardTop: 0, regionTop: 40, regionBottom: 400 })
    expect(requestRegionCap(card, region)).toBe(REQUEST_REGION_MIN_PX)
  })
})
