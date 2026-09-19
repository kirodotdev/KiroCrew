import { describe, it, expect, vi } from 'vitest'
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
