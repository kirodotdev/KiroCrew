import { render, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ScreenshotGallery } from '../pages/AppDetailPage'

// Screenshot-viewer magnification. #6000 turned page zoom off shell-wide on the
// rule that "a surface that must magnify owns its own zoom", and the AppDetail
// screenshot viewer was the third overlay bound by it — after the image viewer
// and DiagramLightbox — yet the guard sweep that shipped with the second fix
// (#6117) globbed `components/**` only, so this overlay in `pages/` shipped
// without a gesture and was carried in the sweep as a named exception (#6162).
// These specs pin the gesture it now owns, and the two interactions the other
// viewers do not: prev/next navigation (a pinch must not read as navigation,
// and `selected` changing must reset zoom/pan) and click-to-dismiss on the
// backdrop (a finished pinch must not close the viewer it just zoomed into).

const S1 = '/shots/a.png'
const S2 = '/shots/b.png'

/** jsdom gives every element a zero layout box, so the pan clamp would pin every
 *  candidate to 0 and no spec could observe a pan. Give the lightbox img a real
 *  box. */
function sizeImg(el: HTMLElement, w = 800, h = 600) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: w })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, value: h })
}

/** The lightbox's <img> — the transform target, and the only img inside the
 *  dialog (the thumbnails live outside it). */
function lightboxImg(): HTMLImageElement {
  return document.querySelector('[role="dialog"] img') as HTMLImageElement
}

function openLightbox() {
  render(<MemoryRouter><ScreenshotGallery screenshots={[S1, S2]} /></MemoryRouter>)
  fireEvent.click(document.querySelector('button[aria-label="Open screenshot 1"]')!)
  const img = lightboxImg()
  sizeImg(img)
  return img
}

const touch = (x: number, y: number, pointerId = 1) =>
  ({ pointerType: 'touch', pointerId, clientX: x, clientY: y })

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
})

describe('ScreenshotGallery lightbox magnification', () => {
  it('starts at fit and scales on a two-finger pinch', () => {
    const img = openLightbox()
    expect(img.getAttribute('style')).toContain('scale(1)')
    act(() => { fireEvent.pointerDown(img, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(img, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(img, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(img, touch(300, 400, 2)) })
    expect(img.getAttribute('style')).toContain('scale(2)')
  })

  it('toggles fit <-> 2.5x on a double-tap and back', () => {
    const img = openLightbox()
    act(() => { fireEvent.pointerDown(img, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerUp(img, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerDown(img, touch(202, 402, 2)) })
    expect(img.getAttribute('style')).toContain('scale(2.5)')
    act(() => { fireEvent.pointerUp(img, touch(202, 402, 2)) })
    act(() => { fireEvent.pointerDown(img, touch(203, 403, 3)) })
    act(() => { fireEvent.pointerUp(img, touch(203, 403, 3)) })
    act(() => { fireEvent.pointerDown(img, touch(204, 404, 4)) })
    expect(img.getAttribute('style')).toContain('scale(1)')
  })

  it('does not let a pinch be read as navigation, and navigation resets the zoom', () => {
    const img = openLightbox()
    // Pinch to zoom in.
    act(() => { fireEvent.pointerDown(img, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(img, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(img, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(img, touch(300, 400, 2)) })
    expect(img.getAttribute('style')).toContain('scale(2)')
    // A completed pinch leaves the viewer open and on the SAME screenshot.
    act(() => { fireEvent.pointerUp(img, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerUp(img, touch(300, 400, 2)) })
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
    expect(lightboxImg().getAttribute('src')).toBe(S1)
    // Navigating to the next screenshot resets zoom/pan via the hook's reset().
    act(() => { fireEvent.keyDown(document.querySelector('[role="dialog"]')!, { key: 'ArrowRight' }) })
    expect(lightboxImg().getAttribute('src')).toBe(S2)
    expect(lightboxImg().getAttribute('style')).toContain('scale(1)')
  })

  it('does not close on the click that follows a pinch on the backdrop', () => {
    const img = openLightbox()
    // End the pinch OUTSIDE the image (on the backdrop): the finger lift
    // synthesises a click there, which must not dismiss the viewer.
    act(() => { fireEvent.pointerDown(img, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(img, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(img, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(img, touch(300, 400, 2)) })
    act(() => { fireEvent.pointerUp(img, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerUp(img, touch(300, 400, 2)) })
    // The click synthesised after the last finger lifts is gesture residue.
    act(() => { fireEvent.click(document.querySelector('[role="dialog"]')!) })
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('pans a zoomed screenshot on a one-finger drag, and not at fit', () => {
    const img = openLightbox()
    // At fit a drag must not pan — there is nothing off-screen to reach.
    act(() => { fireEvent.pointerDown(img, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerMove(img, touch(260, 400, 1)) })
    expect(img.getAttribute('style')).toContain('translate(0px, 0px)')
    act(() => { fireEvent.pointerUp(img, touch(260, 400, 1)) })
    // Double-tap to zoom in, then drag: now the pan moves.
    act(() => { fireEvent.pointerDown(img, touch(200, 400, 2)) })
    act(() => { fireEvent.pointerUp(img, touch(200, 400, 2)) })
    act(() => { fireEvent.pointerDown(img, touch(201, 401, 3)) })
    expect(img.getAttribute('style')).toContain('scale(2.5)')
    act(() => { fireEvent.pointerUp(img, touch(201, 401, 3)) })
    act(() => { fireEvent.pointerDown(img, touch(200, 400, 4)) })
    act(() => { fireEvent.pointerMove(img, touch(240, 400, 4)) })
    expect(img.getAttribute('style')).not.toContain('translate(0px, 0px)')
  })

  it('does not claim mouse pointers, so desktop click-out is untouched', () => {
    const img = openLightbox()
    act(() => { fireEvent.pointerDown(img, { pointerType: 'mouse', pointerId: 1, clientX: 200, clientY: 400 }) })
    act(() => { fireEvent.pointerDown(img, { pointerType: 'mouse', pointerId: 1, clientX: 202, clientY: 402 }) })
    expect(img.getAttribute('style')).toContain('scale(1)')
    // A plain click on the backdrop still dismisses (desktop affordance intact).
    act(() => { fireEvent.click(document.querySelector('[role="dialog"]')!) })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('opts the lightbox img out of the root pan-only touch-action', () => {
    const img = openLightbox()
    expect(img.className).toContain('touch-none')
  })
})
