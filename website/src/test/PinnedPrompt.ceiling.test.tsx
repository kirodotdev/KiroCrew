import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// The card is an overlay that paints above the composer dock, so its only bound is
// the `maxH` ceiling the host measures (usePinnedPrompt.test.tsx covers the
// measurement). This file pins the card's half of the contract: the ceiling lands
// on the box as `max-height` — which outranks the fold's and the morph's inline
// `height` by CSS rule — and the box's layout lets the ceiling SHRINK the prompt's
// scroll region rather than merely clip it. jsdom lays nothing out, so what can be
// asserted is the structure that makes the browser do the right thing: `max-height`
// on the box, `items-stretch` on it (a single-line flex container clamps its line to
// its max-height and a stretched item takes that as a definite height), a flex
// COLUMN body with `min-h-0`, and `min-h-0` on the paragraph in every state. Remove
// any one and the paragraph keeps its natural height under a capped box, i.e. the
// end of the prompt is cut off instead of scrolled to.
const PREVIEW = 'Line 1 of a long prompt'
const FULL = Array.from({ length: 40 }, (_, i) => `Line ${i + 1} of a long prompt`).join('\n')

function renderCard(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  render(
    <PinnedPrompt
      text={PREVIEW}
      fullText={FULL}
      images={[]}
      bodyBeyondPreview
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onCollapsedHeight={() => {}}
      {...over}
    />,
  )
  const card = screen.getByTestId('pinned-prompt')
  const box = card.firstElementChild as HTMLElement
  const body = box.querySelector('button') as HTMLElement
  const p = box.querySelector('p') as HTMLElement
  return { box, body, p }
}

afterEach(() => { cleanup() })

describe('PinnedPrompt — the host ceiling caps the card', () => {
  it('sets the ceiling as the box\'s max-height', () => {
    const { box } = renderCard({ maxH: 296 })
    expect(box.style.maxHeight).toBe('296px')
  })

  it('leaves the box unbounded when the host has measured no floor', () => {
    const { box } = renderCard()
    expect(box.style.maxHeight).toBe('')
  })

  it('caps the fold too: an inline fold height past the ceiling is still under max-height', () => {
    // The fold writes `height` imperatively; `max-height` wins by CSS rule, so the
    // box carries both and the browser resolves them in the ceiling's favour.
    const { box } = renderCard({ maxH: 296, liveH: 816 })
    expect(box.style.height).toBe('816px')
    expect(box.style.maxHeight).toBe('296px')
  })

  it('lays the box out so the ceiling shrinks the scroll region instead of clipping it', () => {
    for (const expanded of [false, true]) {
      cleanup()
      const { box, body, p } = renderCard({ maxH: 296, expanded })
      expect(box.className, 'single flex line, clamped to max-height, stretched into').toContain('items-stretch')
      expect(box.className).not.toContain('items-start')
      expect(body.className, 'body is a column that can give height back').toContain('flex-col')
      expect(body.className).toContain('min-h-0')
      expect(p.className, `paragraph can shrink (expanded=${expanded})`).toContain('min-h-0')
    }
    // Expanded keeps its own cap on top: a tall pane still gives the card at most 40vh.
    cleanup()
    const { p } = renderCard({ expanded: true })
    expect(p.className).toContain('max-h-[40vh]')
    expect(p.className).toContain('overflow-y-auto')
  })

  it('clips the box at its ceiling, so nothing inside can paint past it', () => {
    // The layout above makes the text shrink; this is the guarantee for whatever
    // does not — an image strip wrapped to more rows than the room allows.
    const { box } = renderCard({ maxH: 160, expanded: true, images: ['/tmp/a.png', '/tmp/b.png', '/tmp/c.png'] })
    expect(box.className).toContain('overflow-hidden')
  })

  it('puts the expanded image strip inside the paragraph, so the card has one scroll region', () => {
    // Beside the paragraph the strip was a second scroll area, and under the
    // ceiling flex shared the loss by base size: a 30-line prompt dwarfs a row
    // of thumbnails, so the strip shrank to a sliver scrolling its own rows. In
    // the paragraph the thumbnails lead the text and scroll with it.
    const { body, p } = renderCard({ expanded: true, images: ['/tmp/a.png', '/tmp/b.png'] })
    const strip = p.querySelector('span.flex-wrap') as HTMLElement
    expect(strip, 'strip is inside the <p>').not.toBeNull()
    expect(strip.querySelectorAll('img')).toHaveLength(2)
    expect(body.querySelector(':scope > span'), 'nothing scrolls beside the paragraph').toBeNull()
    expect(p.className).toContain('overflow-y-auto')
  })

  it('keeps the all-images-gone fallback inside the same region', () => {
    const { p } = renderCard({ expanded: true, images: ['/tmp/gone.png'], fullText: '' , text: '' })
    const img = p.querySelector('img') as HTMLImageElement
    fireEvent.error(img)
    expect(p.querySelector('img')).toBeNull()
    expect(p.querySelector('svg'), 'ImageOff glyph stands in').not.toBeNull()
  })

  it('holds the body at natural height for the morph, then hands back to the stretch', () => {
    // The morph FLIPs the box's height from the old size to the new. Stretched
    // during that, the paragraph would shrink with the animating box and show a
    // scrollbar for 150ms on every expand; the morph parks the box at `flex-start`
    // for its duration and clears it with its other inline values at the end.
    const rects = [40, 300]
    let i = 0
    const spy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const h = this.getAttribute('data-testid') === 'pinned-prompt' || this.className.includes('user-bubble')
        ? rects[Math.min(i++, rects.length - 1)]
        : 0
      return { top: 0, bottom: h, left: 0, right: 0, width: 0, height: h, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    })
    try {
      const props = {
        text: PREVIEW, fullText: FULL, images: [], bodyBeyondPreview: true, pushUp: 0, bannerH: 40,
        onToggleExpanded: () => {}, onJump: () => {}, onCollapsedHeight: () => {},
      }
      const { rerender } = render(<PinnedPrompt {...props} expanded={false} />)
      rerender(<PinnedPrompt {...props} expanded />)
      const box = screen.getByTestId('pinned-prompt').firstElementChild as HTMLElement
      expect(box.style.height, 'morph in flight').toBe('300px')
      expect(box.style.alignItems).toBe('flex-start')
      box.dispatchEvent(Object.assign(new Event('transitionend', { bubbles: true }), { propertyName: 'height' }))
      expect(box.style.height).toBe('')
      expect(box.style.alignItems, 'stretch resumes once the morph has landed').toBe('')
    } finally {
      spy.mockRestore()
    }
  })
})

// The fade is a MASK on the paragraph, set from its own scroll position: shown
// while content continues below the visible edge, cleared at the end and when
// everything fits. jsdom lays nothing out, so the scroll metrics are stubbed on
// the element and the assertion is the class the CSS keys on.
describe('PinnedPrompt — the capped paragraph says when there is more below', () => {
  function metrics(el: HTMLElement, scrollHeight: number, clientHeight: number, scrollTop = 0) {
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
    Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => scrollTop, set: () => {} })
  }

  it('fades the bottom edge while the text runs past it, and drops the fade at the end', () => {
    const { p } = renderCard({ maxH: 160, expanded: true })
    metrics(p, 1464, 155, 0)
    fireEvent.scroll(p)
    expect(p.className).toContain('pinned-scroll-more')
    metrics(p, 1464, 155, 1309)
    fireEvent.scroll(p)
    expect(p.className).not.toContain('pinned-scroll-more')
  })

  it('never fades a paragraph that fits, and never while collapsed', () => {
    const { p } = renderCard({ expanded: true })
    metrics(p, 120, 120, 0)
    fireEvent.scroll(p)
    expect(p.className).not.toContain('pinned-scroll-more')
    cleanup()
    const collapsed = renderCard({ expanded: false })
    metrics(collapsed.p, 1464, 24, 0)
    fireEvent.scroll(collapsed.p)
    expect(collapsed.p.className).not.toContain('pinned-scroll-more')
  })
})
