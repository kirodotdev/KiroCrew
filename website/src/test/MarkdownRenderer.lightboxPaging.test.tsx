import { render, fireEvent, act } from '@testing-library/react'
import { Lightbox } from '../components/MarkdownRenderer'

// Horizontal swipe paging on the image viewer. The viewer opens with a SET, and
// until this existed the only way through it was ArrowLeft/ArrowRight — so on a
// phone every image after the first was unreachable. The gesture is the mirror of
// swipe-to-dismiss: overlay-wide, touch only, fit zoom only, distance-committed.

function open(count: number, index = 0) {
  window.dispatchEvent(new CustomEvent('lightbox', {
    detail: {
      images: Array.from({ length: count }, (_, i) => ({ src: `p${i}.png`, alt: `p${i}` })),
      index,
    },
  }))
}

/** The overlay is the Clickable root; the swipe transform lives on its child. */
function surfaces(container: HTMLElement) {
  const overlay = container.querySelector('[role="button"]') as HTMLElement
  return { overlay, inner: overlay.firstElementChild as HTMLElement }
}

const shown = (c: HTMLElement) => (c.querySelector('img') as HTMLImageElement).getAttribute('src')
/** y is held constant so the axis lock reads the drag as horizontal. */
const touch = (x: number, y = 400, pointerId = 1) => ({ pointerType: 'touch', pointerId, clientX: x, clientY: y })

/** One complete horizontal drag: down at `from`, one move to `to`, release. */
function drag(overlay: HTMLElement, from: number, to: number, y = 400) {
  act(() => { fireEvent.pointerDown(overlay, touch(from, y)) })
  act(() => { fireEvent.pointerMove(overlay, touch(to, y)) })
  act(() => { fireEvent.pointerUp(overlay, touch(to, y)) })
}

describe('Lightbox horizontal swipe paging', () => {
  it('pages forward on a leftward drag past the threshold', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    expect(shown(container)).toBe('p0.png')
    drag(surfaces(container).overlay, 200, 80)
    expect(shown(container)).toBe('p1.png')
  })

  it('pages back on a rightward drag past the threshold', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4, 2))
    drag(surfaces(container).overlay, 200, 320)
    expect(shown(container)).toBe('p1.png')
  })

  it('tracks the finger while the drag is live', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    // Below the slop the gesture is still a candidate tap — nothing moves.
    act(() => { fireEvent.pointerMove(overlay, touch(195)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerMove(overlay, touch(120)) })
    expect(inner.getAttribute('style')).toContain('translateX(-80.0px)')
    // Paging is not a dismiss, so the backdrop keeps its full dim.
    expect(overlay.getAttribute('style')).toBeNull()
  })

  it('springs back without paging when released short of the threshold', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    drag(overlay, 200, 160)
    expect(shown(container)).toBe('p0.png')
    expect(inner.getAttribute('style')).toBeNull()
    // The click a real drag generates must not fall through to backdrop-close.
    act(() => { fireEvent.click(overlay) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('rubber-bands at the first image rather than going dead', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4, 0))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    act(() => { fireEvent.pointerMove(overlay, touch(320)) })
    // 120px of pull renders as 30px: the image still answers the finger, which
    // reads as "start of the set" instead of a broken gesture.
    expect(inner.getAttribute('style')).toContain('translateX(30.0px)')
    act(() => { fireEvent.pointerUp(overlay, touch(320)) })
    expect(shown(container)).toBe('p0.png')
    expect(inner.getAttribute('style')).toBeNull()
  })

  it('rubber-bands at the last image and never pages past the end', () => {
    const { container } = render(<Lightbox />)
    act(() => open(3, 2))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    act(() => { fireEvent.pointerMove(overlay, touch(80)) })
    expect(inner.getAttribute('style')).toContain('translateX(-30.0px)')
    act(() => { fireEvent.pointerUp(overlay, touch(80)) })
    expect(shown(container)).toBe('p2.png')
  })

  it('drops a horizontal drag when the set has only one image', () => {
    const { container } = render(<Lightbox />)
    act(() => open(1))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    act(() => { fireEvent.pointerMove(overlay, touch(80)) })
    // Nothing to page to, so the gesture is dropped outright — not rubber-banded.
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerUp(overlay, touch(80)) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('leaves a vertical drag to the dismiss gesture', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100)) })
    act(() => { fireEvent.pointerMove(overlay, touch(200, 260)) })
    expect(inner.getAttribute('style')).toContain('translateY(160.0px)')
    act(() => { fireEvent.pointerUp(overlay, touch(200, 260)) })
    expect(container.firstChild).toBeNull()
  })

  it('yields to pan once the image is zoomed past fit', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    drag(overlay, 200, 80)
    expect(inner.getAttribute('style')).toBeNull()
    expect(shown(container)).toBe('p0.png')
  })

  it('ignores a mouse drag so desktop click-to-close is unchanged', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay } = surfaces(container)
    const mouse = (x: number) => ({ pointerType: 'mouse', pointerId: 1, clientX: x, clientY: 400 })
    act(() => { fireEvent.pointerDown(overlay, mouse(200)) })
    act(() => { fireEvent.pointerMove(overlay, mouse(80)) })
    act(() => { fireEvent.pointerUp(overlay, mouse(80)) })
    expect(shown(container)).toBe('p0.png')
    // No suppression was armed, so the click still closes.
    act(() => { fireEvent.click(overlay) })
    expect(container.firstChild).toBeNull()
  })

  it('does not page from a toolbar control', () => {
    const { container, getByLabelText } = render(<Lightbox />)
    act(() => open(4))
    const zoomIn = getByLabelText('Zoom in (+)')
    act(() => { fireEvent.pointerDown(zoomIn, touch(200)) })
    act(() => { fireEvent.pointerMove(zoomIn, touch(80)) })
    act(() => { fireEvent.pointerUp(zoomIn, touch(80)) })
    expect(shown(container)).toBe('p0.png')
  })

  it('abandons paging when a second finger lands, so a pinch cannot page', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    act(() => { fireEvent.pointerMove(overlay, touch(120)) })
    expect(inner.getAttribute('style')).toContain('translateX(-80.0px)')
    act(() => { fireEvent.pointerDown(overlay, touch(600, 400, 2)) })
    expect(inner.getAttribute('style')).toBeNull()
    act(() => { fireEvent.pointerMove(overlay, touch(20)) })
    act(() => { fireEvent.pointerUp(overlay, touch(20)) })
    expect(shown(container)).toBe('p0.png')
  })

  it('restores the image when a paging touch is cancelled', () => {
    const { container } = render(<Lightbox />)
    act(() => open(4))
    const { overlay, inner } = surfaces(container)
    act(() => { fireEvent.pointerDown(overlay, touch(200)) })
    act(() => { fireEvent.pointerMove(overlay, touch(60)) })
    expect(inner.getAttribute('style')).toContain('translateX(-140.0px)')
    // Past the threshold, but a cancel is an abort, not a release.
    act(() => { fireEvent.pointerCancel(overlay, touch(60)) })
    expect(shown(container)).toBe('p0.png')
    expect(inner.getAttribute('style')).toBeNull()
  })

  it('keeps the keyboard path working alongside the gesture', () => {
    const { container } = render(<Lightbox />)
    act(() => open(3))
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(shown(container)).toBe('p1.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(shown(container)).toBe('p0.png')
  })

  // ── discoverability: a gesture nothing hints at is a gesture nobody finds ──

  it('announces the position in the set, politely', () => {
    const { container, getByText } = render(<Lightbox />)
    act(() => open(4, 1))
    const counter = getByText('2 of 4')
    expect(counter.getAttribute('aria-live')).toBe('polite')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(getByText('3 of 4')).toBeTruthy()
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('shows no counter for a single image', () => {
    const { queryByText } = render(<Lightbox />)
    act(() => open(1))
    expect(queryByText('1 of 1')).toBeNull()
  })
})
