import { render, fireEvent, act } from '@testing-library/react'
import { Lightbox } from '../components/MarkdownRenderer'

// Swipe-down-to-dismiss on the image lightbox. The gesture lives on the overlay
// (so a drag anywhere counts, not just on the image), is gated to non-mouse
// pointers, and is gated to fit zoom because above fit the same drag pans.

function open(images: { src: string; alt?: string }[], index = 0) {
  window.dispatchEvent(new CustomEvent('lightbox', {
    detail: { images: images.map(i => ({ src: i.src, alt: i.alt ?? '' })), index },
  }))
}

/** The overlay is the Clickable root; the swipe transform lives on its child. */
function surfaces(container: HTMLElement) {
  const overlay = container.querySelector('[role="button"]') as HTMLElement
  return { overlay, inner: overlay.firstElementChild as HTMLElement }
}

const touch = (y: number, x = 100, pointerId = 1) => ({ pointerType: 'touch', pointerId, clientX: x, clientY: y })

describe('Lightbox swipe-to-dismiss', () => {
  it('follows a downward touch drag and dismisses past the release threshold', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    // Below the slop the gesture is still a candidate tap — nothing moves.
    act(() => { fireEvent.pointerMove(overlay, touch(104)) })
    expect(inner.getAttribute('style')).toBeNull()
    // Past the slop the image tracks the finger and the backdrop dims.
    act(() => { fireEvent.pointerMove(overlay, touch(160)) })
    expect(inner.getAttribute('style')).toContain('translateY(60.0px)')
    expect(overlay.getAttribute('style')).toContain('rgba(0, 0, 0, ')
    // Released past LIGHTBOX_DISMISS_DISTANCE (96px) the viewer closes.
    act(() => { fireEvent.pointerMove(overlay, touch(240)) })
    act(() => { fireEvent.pointerUp(overlay, touch(240)) })
    expect(container.firstChild).toBeNull()
  })

  it('springs back when released short of the threshold, without closing via the click', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(150)) })
    expect(inner.getAttribute('style')).toContain('translateY(50.0px)')
    act(() => { fireEvent.pointerUp(overlay, touch(150)) })
    expect(inner.getAttribute('style')).toBeNull()
    // The click a real drag generates must not fall through to backdrop-close.
    act(() => { fireEvent.click(overlay) })
    expect(container.querySelector('img')).not.toBeNull()
    // …but the NEXT plain tap still closes: the suppression is one-shot.
    act(() => { fireEvent.pointerDown(overlay, touch(10, 100, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(10, 100, 2)) })
    act(() => { fireEvent.click(overlay) })
    expect(container.firstChild).toBeNull()
  })

  it('rubber-bands an upward drag and never dismisses on it', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(300)) })
    act(() => { fireEvent.pointerMove(overlay, touch(100)) })
    // -200px of pull renders as -50px, and the backdrop keeps its full dim.
    expect(inner.getAttribute('style')).toContain('translateY(-50.0px)')
    expect(overlay.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(100)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('drops the gesture when the drag is horizontal', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(130, 220)) })
    expect(inner.getAttribute('style')).toBeNull()
    // Once dropped, continuing downward does not resurrect the drag.
    act(() => { fireEvent.pointerMove(overlay, touch(400, 220)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(400, 220)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('restores the image when the touch is cancelled mid-drag', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(300)) })
    expect(inner.getAttribute('style')).toContain('translateY(200.0px)')
    // Past the dismiss distance, but a cancel is an abort, not a release.
    act(() => { fireEvent.pointerCancel(overlay, touch(300)) })
    expect(container.querySelector('img')).not.toBeNull()
    expect(inner.getAttribute('style')).toBeNull()
  })

  // ── multi-touch: a pinch must never read as a dismiss ─────────────────────

  it('abandons the drag when a second finger lands, so a pinch cannot dismiss', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(200)) })
    expect(inner.getAttribute('style')).toContain('translateY(100.0px)')
    // Second finger: the gesture is a pinch, so the image returns to rest…
    act(() => { fireEvent.pointerDown(overlay, touch(600, 100, 2)) })
    expect(inner.getAttribute('style')).toBeNull()
    // …and neither finger's continued travel or release can dismiss it, even
    // well past the 96px threshold.
    act(() => { fireEvent.pointerMove(overlay, touch(400)) })
    act(() => { fireEvent.pointerMove(overlay, touch(300, 100, 2)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(400)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 100, 2)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('ignores a second finger that never owned the drag', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    // A stray move/up from an id that never started a drag cannot steer or end
    // the live one.
    act(() => { fireEvent.pointerMove(overlay, touch(500, 100, 9)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(500, 100, 9)) })
    act(() => { fireEvent.pointerMove(overlay, touch(250)) })
    expect(inner.getAttribute('style')).toContain('translateY(150.0px)')
    act(() => { fireEvent.pointerUp(overlay, touch(250)) })
    expect(container.firstChild).toBeNull()
  })

  it('owns every gesture on the overlay rather than leaving one to the browser', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    // Page zoom is off across the touch shell, so `pinch-zoom` here would ask for
    // a behaviour the root `touch-action` has already withheld — and get a dead
    // gesture. The viewer scales its own transform instead (see the pinch block).
    expect(overlay.className).toContain('touch-none')
    expect(overlay.className).not.toContain('touch-pinch-zoom')
  })

  // ── pinch-to-zoom: the viewer owns it, because the shell has no page zoom ──

  it('scales the image by the ratio the fingers spread', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    const img = () => container.querySelector('img') as HTMLImageElement
    expect(img().getAttribute('style')).toContain('scale(1)')
    // Two fingers 100px apart, spread to 250px → 2.5x.
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    act(() => { fireEvent.pointerMove(overlay, touch(325, 100, 2)) })
    expect(img().getAttribute('style')).toContain('scale(2.25)')
    // Pinching back in returns to fit and is clamped there, not below.
    act(() => { fireEvent.pointerMove(overlay, touch(120, 100, 2)) })
    expect(img().getAttribute('style')).toContain('scale(1)')
    act(() => { fireEvent.pointerUp(overlay, touch(120, 100, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(100, 100, 1)) })
  })

  it('is bounded by the same max the toolbar is', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(110, 100, 2)) })
    // 10px → 900px is 90x; LIGHTBOX_ZOOM_MAX is 5.
    act(() => { fireEvent.pointerMove(overlay, touch(1000, 100, 2)) })
    expect((container.querySelector('img') as HTMLImageElement).getAttribute('style')).toContain('scale(5)')
  })

  it('keeps the pinch alive when a finger lands while the image is already zoomed', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    // Zoom past fit first: at this point the <img> pan owns one-finger drags, and
    // recording the contact only after that bail-out would lose the second finger.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    // 100px → 200px doubles the zoom the gesture STARTED from (1.5), not from fit.
    act(() => { fireEvent.pointerMove(overlay, touch(300, 100, 2)) })
    expect((container.querySelector('img') as HTMLImageElement).getAttribute('style')).toContain('scale(3)')
  })

  it('does not let the click after a pinch close the viewer', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    act(() => { fireEvent.pointerMove(overlay, touch(400, 100, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(400, 100, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.click(overlay) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('ignores a two-finger mouse-typed sequence, so no desktop path is armed', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    const mouse = (y: number, id: number) => ({ pointerType: 'mouse', pointerId: id, clientX: 100, clientY: y })
    act(() => { fireEvent.pointerDown(overlay, mouse(100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, mouse(200, 2)) })
    act(() => { fireEvent.pointerMove(overlay, mouse(500, 2)) })
    expect((container.querySelector('img') as HTMLImageElement).getAttribute('style')).toContain('scale(1)')
  })

  it('drops the contacts when the viewer closes mid-pinch', () => {    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    act(() => { fireEvent.pointerMove(overlay, touch(400, 100, 2)) })
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(container.firstChild).toBeNull()
    // Reopened, a single finger is a dismiss-drag again — not the survivor of a
    // pair whose partner is still recorded from the previous open.
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const next = surfaces(container)
    act(() => { fireEvent.pointerDown(next.overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerMove(next.overlay, touch(160, 100, 1)) })
    expect(next.inner.getAttribute('style')).toContain('translateY(60.0px)')
  })

  // The pinch pair is DERIVED from the live contacts rather than stored as two
  // pointer ids. These two specs are what that invariant buys: both describe a
  // contact set changing in a way a `size < 2` test cannot see.

  it('keeps scaling when a third finger joins and an original one lifts', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    const scale = () => (container.querySelector('img') as HTMLImageElement).getAttribute('style')
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    act(() => { fireEvent.pointerMove(overlay, touch(300, 100, 2)) })
    expect(scale()).toContain('scale(2)')
    // A third finger lands, then finger 1 — a member of the measured pair — lifts.
    // Two contacts remain, so nothing resets, and an id-based pinch would still be
    // pointing at the lifted pointer: every later move would read `undefined` for
    // it and silently stop scaling until the user lifted everything.
    act(() => { fireEvent.pointerDown(overlay, touch(400, 100, 3)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 100, 1)) })
    // The first move after the pair changes only RE-SEATS the baseline (measuring
    // from the current zoom, so nothing jumps); the one after it must scale.
    act(() => { fireEvent.pointerMove(overlay, touch(700, 100, 3)) })
    act(() => { fireEvent.pointerMove(overlay, touch(1100, 100, 3)) })
    expect(scale()).toContain('scale(4)')
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('re-seats the baseline on a pair change instead of jumping the zoom', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    const scale = () => (container.querySelector('img') as HTMLImageElement).getAttribute('style')
    act(() => { fireEvent.pointerDown(overlay, touch(100, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })
    act(() => { fireEvent.pointerMove(overlay, touch(300, 100, 2)) })
    expect(scale()).toContain('scale(2)')
    // A third finger at the SAME separation as the live pair. Re-seating measures
    // the new baseline from the current zoom, so the very next frame must not
    // recompute the scale from the old pair's distance — the zoom holds at 2.
    act(() => { fireEvent.pointerDown(overlay, touch(500, 100, 3)) })
    act(() => { fireEvent.pointerMove(overlay, touch(500, 100, 3)) })
    expect(scale()).toContain('scale(2)')
  })

  // ── the gesture must not take anything away ───────────────────────────────

  it('ignores a mouse drag so desktop click-to-close is unchanged', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, { pointerType: 'mouse', pointerId: 1, clientX: 100, clientY: 100 }) })
    act(() => { fireEvent.pointerMove(overlay, { pointerType: 'mouse', pointerId: 1, clientX: 100, clientY: 400 }) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, { pointerType: 'mouse', pointerId: 1, clientX: 100, clientY: 400 }) })
    // No suppression was armed, so the click still closes.
    act(() => { fireEvent.click(overlay) })
    expect(container.firstChild).toBeNull()
  })

  it('yields to pan once the image is zoomed past fit', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(400)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(400)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('does not start a drag from a toolbar control', () => {
    const { container, getByLabelText } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { inner } = surfaces(container)
    const zoomIn = getByLabelText('Zoom in (+)')
    act(() => { fireEvent.pointerDown(zoomIn, touch(100)) })
    act(() => { fireEvent.pointerMove(zoomIn, touch(300)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(zoomIn, touch(300)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('starts each newly shown image undragged', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }, { src: 'b.png', alt: 'b' }]))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(160)) })
    expect(inner.getAttribute('style')).toContain('translateY(60.0px)')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(surfaces(container).inner.getAttribute('style')).toBeNull()
  })
})
