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

  it('keeps the browser pinch by declaring pinch-zoom rather than no touch action', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay } = surfaces(container)
    expect(overlay.className).toContain('touch-pinch-zoom')
    expect(overlay.className).not.toContain('touch-none')
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
