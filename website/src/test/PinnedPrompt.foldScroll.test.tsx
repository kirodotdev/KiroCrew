import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// The pinned card lives in a `pointer-events-none` overlay that is a SIBLING of the
// transcript scroller, never an ancestor. An interactive box there is the target of a
// wheel, and the browser then hunts for a scrollable ANCESTOR of that box — the
// overlay, then the page — so the transcript never moves. Verified in a browser: a
// wheel over such a box left the sibling scroller's scrollTop at 0 while the same
// wheel over bare scroller moved it 400px.
//
// Going inert dodges that and costs too much. While the fold holds the card over
// lines the reader has not finished, an inert card cannot be selected, copied or
// clicked, and its jump and expand buttons keep their hover styling while doing
// nothing. So the card STAYS interactive and forwards the gesture instead.
//
// jsdom does not scroll, so what these assert is the delta reaching the host and the
// default being prevented — the two things the forwarder is responsible for. That the
// host's `scrollTop +=` then moves the transcript is the browser's job, measured by
// the probe above.
function renderCard(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  const scrollTranscriptBy = vi.fn()
  render(
    <PinnedPrompt
      text="a prompt long enough that one line cannot hold it"
      fullText={'a prompt long enough that one line cannot hold it\nsecond line'}
      images={[]}
      bodyBeyondPreview
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onCollapsedHeight={() => {}}
      scrollTranscriptBy={scrollTranscriptBy}
      {...over}
    />,
  )
  const card = screen.getByTestId('pinned-prompt')
  const box = card.firstElementChild
  if (!box) throw new Error('pinned card rendered no box')
  return { card, box, scrollTranscriptBy }
}

function wheel(box: Element, deltaY: number, deltaMode = 0) {
  const e = new WheelEvent('wheel', { deltaY, deltaMode, cancelable: true })
  box.dispatchEvent(e)
  return e
}

// happy-dom's `WheelEvent` constructor drops the mouse-modifier fields: it keeps
// `deltaY` but leaves `ctrlKey` undefined, while `MouseEvent` honours it. Verified in
// the same happy-dom version the suite runs. So the zoom gesture has to be set on the
// event object, the way `touches` is below — passing it in the init dict would build an
// event that silently is not the gesture, and the test would pass whether or not the
// component checked.
function zoomWheel(box: Element, deltaY: number) {
  const e = new WheelEvent('wheel', { deltaY, cancelable: true })
  Object.defineProperty(e, 'ctrlKey', { value: true, configurable: true })
  box.dispatchEvent(e)
  return e
}

function touch(box: Element, type: string, clientY: number) {
  const e = new Event(type, { cancelable: true })
  Object.defineProperty(e, 'touches', { value: [{ clientY }], configurable: true })
  box.dispatchEvent(e)
  return e
}

describe('PinnedPrompt — the card stays interactive and forwards the scroll', () => {
  it('keeps pointer events at its resting height', () => {
    const { card } = renderCard()
    expect(card.className).toContain('pointer-events-auto')
    expect(card.className).not.toContain('pointer-events-none')
  })

  it('keeps pointer events while the fold holds it grown', () => {
    const { card } = renderCard({ liveH: 816 })
    expect(card.className).toContain('pointer-events-auto')
    expect(card.className).not.toContain('pointer-events-none')
  })

  it('forwards a wheel delta to the transcript instead of swallowing it', () => {
    const { box, scrollTranscriptBy } = renderCard({ liveH: 816 })
    const e = wheel(box, 120)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(120)
    expect(e.defaultPrevented).toBe(true)
  })

  it('forwards at the resting height too, where the band would also swallow', () => {
    const { box, scrollTranscriptBy } = renderCard()
    wheel(box, 40)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(40)
  })

  it('converts a line-mode delta rather than treating lines as pixels', () => {
    const { box, scrollTranscriptBy } = renderCard()
    wheel(box, 3, 1)
    // No CSS in jsdom, so the computed line height is unparseable and the documented
    // 24px fallback applies: 3 lines = 72px, not 3px.
    expect(scrollTranscriptBy).toHaveBeenCalledWith(72)
  })

  it('ignores a zero delta and leaves the event alone', () => {
    const { box, scrollTranscriptBy } = renderCard()
    const e = wheel(box, 0)
    expect(scrollTranscriptBy).not.toHaveBeenCalled()
    expect(e.defaultPrevented).toBe(false)
  })

  it('forwards a one-finger drag as the distance since the last move, inverted', () => {
    const { box, scrollTranscriptBy } = renderCard({ liveH: 816 })
    touch(box, 'touchstart', 500)
    touch(box, 'touchmove', 460)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(40)
  })

  it('does not forward a drag that never started on the card', () => {
    const { box, scrollTranscriptBy } = renderCard()
    touch(box, 'touchmove', 460)
    expect(scrollTranscriptBy).not.toHaveBeenCalled()
  })

  it('renders without a forwarder, so a host that omits it does not crash', () => {
    cleanup()
    expect(() => renderCard({ scrollTranscriptBy: undefined })).not.toThrow()
  })
})

describe('PinnedPrompt — the browser zoom gesture is not taken', () => {
  it('lets ctrl+wheel through instead of scrolling the transcript', () => {
    const { box, scrollTranscriptBy } = renderCard({ liveH: 816 })
    const e = zoomWheel(box, 120)
    // Zoom is a low-vision path. Taking it would make the card the one place on the
    // page that cannot be zoomed.
    expect(scrollTranscriptBy).not.toHaveBeenCalled()
    expect(e.defaultPrevented).toBe(false)
  })

  it('still forwards a plain wheel, so the zoom exemption is not a hole', () => {
    const { box, scrollTranscriptBy } = renderCard({ liveH: 816 })
    wheel(box, 120)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(120)
  })
})

// The card can hold its OWN scroll region: while `expanded` the paragraph is
// `max-h-[40vh] overflow-y-auto`. Forwarding there cancels the native scroll the
// reader is using to get through the prompt they just expanded, and moving the
// transcript underneath recomputes the pin, so the card can collapse or swap while
// they are inside it. Design, UX and Opus each raised this on the same head.
//
// happy-dom reports no layout, so the scrollable region is described explicitly:
// the geometry and `overflowY` are what the forwarder reads.
function makeScrollable(el: Element, opts: { scrollHeight?: number, clientHeight?: number, scrollTop?: number } = {}) {
  const { scrollHeight = 1000, clientHeight = 300, scrollTop = 100 } = opts
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  let top = scrollTop
  Object.defineProperty(el, 'scrollTop', { get: () => top, set: (v: number) => { top = v }, configurable: true })
  ;(el as HTMLElement).style.overflowY = 'auto'
}

function wheelOn(target: Element, deltaY: number) {
  const e = new WheelEvent('wheel', { deltaY, cancelable: true, bubbles: true })
  target.dispatchEvent(e)
  return e
}

describe('PinnedPrompt — the forwarder yields to the card own scroll region', () => {
  function expandedCard() {
    const r = renderCard({ expanded: true, liveH: undefined })
    const p = r.card.querySelector('p')
    if (!p) throw new Error('pinned card rendered no paragraph')
    return { ...r, p }
  }

  it('leaves a wheel alone while the expanded text still has room to scroll', () => {
    const { p, scrollTranscriptBy } = expandedCard()
    makeScrollable(p, { scrollTop: 100 })
    const e = wheelOn(p, 120)
    expect(scrollTranscriptBy).not.toHaveBeenCalled()
    expect(e.defaultPrevented).toBe(false)
  })

  it('claims the wheel once that text is at its bottom edge', () => {
    const { p, scrollTranscriptBy } = expandedCard()
    // scrollTop + clientHeight == scrollHeight: nothing left to give downward.
    makeScrollable(p, { scrollHeight: 1000, clientHeight: 300, scrollTop: 700 })
    wheelOn(p, 120)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(120)
  })

  it('claims an upward wheel once that text is at its top edge', () => {
    const { p, scrollTranscriptBy } = expandedCard()
    makeScrollable(p, { scrollTop: 0 })
    wheelOn(p, -120)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(-120)
  })

  it('still forwards everything while folding, where the text is overflow-hidden', () => {
    const { card, scrollTranscriptBy } = renderCard({ liveH: 816 })
    const p = card.querySelector('p')
    if (!p) throw new Error('pinned card rendered no paragraph')
    // Tall content, but the fold clips rather than scrolls, so the region cannot
    // consume the gesture and the transcript must still move.
    makeScrollable(p, { scrollTop: 100 })
    ;(p as HTMLElement).style.overflowY = 'hidden'
    wheelOn(p, 120)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(120)
  })

  it('leaves a touch drag alone inside the expanded text', () => {
    const { p, scrollTranscriptBy } = expandedCard()
    makeScrollable(p, { scrollTop: 100 })
    const start = new Event('touchstart', { cancelable: true, bubbles: true })
    Object.defineProperty(start, 'touches', { value: [{ clientY: 500 }], configurable: true })
    p.dispatchEvent(start)
    const move = new Event('touchmove', { cancelable: true, bubbles: true })
    Object.defineProperty(move, 'touches', { value: [{ clientY: 400 }], configurable: true })
    p.dispatchEvent(move)
    expect(scrollTranscriptBy).not.toHaveBeenCalled()
    expect(move.defaultPrevented).toBe(false)
  })
})

// A line-mode wheel (Firefox) is converted with a line height, and WHICH element that
// is read from is the whole question. `box` is the `.user-bubble` div, whose `text-sm`
// sets line-height 1.25rem (20px); the paragraph is `my-1 leading-6` (24px). The
// earlier line-mode test could not catch a box read, because happy-dom computes no
// line-height at all and the 24px fallback answered either way. So this test states
// both values and checks the delta.
describe('PinnedPrompt — a line-mode wheel uses the paragraph line height', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('converts 3 lines as 72px, not the box 60px', () => {
    vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => ({
      lineHeight: el.tagName === 'P' ? '24px' : '20px',
      // The forwarder also asks about overflow when deciding whether to yield; nothing
      // here is scrollable, so it must not yield.
      overflowY: 'visible',
    }) as unknown as CSSStyleDeclaration)
    const { box, scrollTranscriptBy } = renderCard()
    wheel(box, 3, 1)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(72)
  })
})
