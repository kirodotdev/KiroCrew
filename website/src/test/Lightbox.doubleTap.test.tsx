import { render, fireEvent, act } from '@testing-library/react'
import { vi } from 'vitest'
import { Lightbox } from '../components/MarkdownRenderer'

// Double-tap to zoom on the image Lightbox. Issue #6135 brings affordance parity
// between Lightbox and DiagramLightbox so both touch surfaces support pinch and
// double-tap to toggle fit <-> 2.5x.

function open(images: { src: string; alt?: string }[], index = 0) {
  window.dispatchEvent(
    new CustomEvent('lightbox', {
      detail: { images: images.map(i => ({ src: i.src, alt: i.alt ?? '' })), index },
    }),
  )
}

function surfaces(container: HTMLElement) {
  const overlay = container.querySelector('[role="button"]') as HTMLElement
  const img = container.querySelector('img') as HTMLImageElement
  return { overlay, img }
}

const touch = (y: number, x = 100, pointerId = 1) => ({
  pointerType: 'touch',
  pointerId,
  clientX: x,
  clientY: y,
})

function sizeHost(el: HTMLElement, w = 800, h = 600) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: w })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, value: h })
}

describe('Lightbox double-tap to zoom', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 400 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 800 })
  })

  it('toggles fit <-> 2.5x on a double-tap and back', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    expect(img.style.transform).toContain('scale(1)')

    // First double-tap: two quick taps near (200, 400)
    act(() => { fireEvent.pointerDown(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(402, 202, 2)) })

    expect(img.style.transform).toContain('scale(2.5)')

    // Second double-tap: resets back to fit (1x) with pan cleared
    act(() => { fireEvent.pointerUp(overlay, touch(402, 202, 2)) })
    act(() => { fireEvent.pointerDown(overlay, touch(402, 202, 3)) })
    act(() => { fireEvent.pointerUp(overlay, touch(402, 202, 3)) })
    act(() => { fireEvent.pointerDown(overlay, touch(403, 203, 4)) })

    expect(img.style.transform).toContain('scale(1)')
    expect(img.style.transform).toContain('translate(0px, 0px)')
  })

  it('anchors the double-tap zoom at the tapped point rather than the center', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    // Tap at (100, 200), well away from viewport center (200, 400)
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(200, 100, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(200, 100, 2)) })

    expect(img.style.transform).toContain('scale(2.5)')
    // Non-centered tap must offset translate
    expect(img.style.transform).not.toContain('translate(0px, 0px)')
  })

  it('does not read two far-apart taps as one double-tap', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)

    act(() => { fireEvent.pointerDown(overlay, touch(50, 100, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(50, 100, 1)) })
    // Same time window, but beyond DOUBLE_TAP_SLOP (32px)
    act(() => { fireEvent.pointerDown(overlay, touch(300, 700, 2)) })

    expect(img.style.transform).toContain('scale(1)')
  })

  it('does not read two slow taps as a double-tap', () => {
    vi.useFakeTimers()
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)

    act(() => { fireEvent.pointerDown(overlay, touch(300, 400, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 400, 1)) })

    // Advance beyond DOUBLE_TAP_MS (300ms)
    act(() => { vi.advanceTimersByTime(350) })

    act(() => { fireEvent.pointerDown(overlay, touch(300, 400, 2)) })
    expect(img.style.transform).toContain('scale(1)')

    vi.useRealTimers()
  })

  it('does not read a swipe-dismiss drag as a tap candidate', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)

    // Pull down past SLOP (8px) but short of dismiss distance (96px)
    act(() => { fireEvent.pointerDown(overlay, touch(100, 200, 1)) })
    act(() => { fireEvent.pointerMove(overlay, touch(150, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(150, 200, 1)) })

    // A subsequent single tap must not combine with the drag into a double-tap
    act(() => { fireEvent.pointerDown(overlay, touch(100, 200, 2)) })
    expect(img.style.transform).toContain('scale(1)')
  })

  it('does not close the lightbox on the click that follows a double-tap', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)

    act(() => { fireEvent.pointerDown(overlay, touch(300, 400, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 400, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(300, 400, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 400, 2)) })
    // The synthesised click from the second tap must be suppressed
    act(() => { fireEvent.click(overlay) })

    expect(container.querySelector('img')).not.toBeNull()
    expect(img.style.transform).toContain('scale(2.5)')
  })

  it('does not re-arm swipe-to-dismiss when the second tap stays down and drags', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    act(() => { fireEvent.pointerDown(overlay, touch(300, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(300, 200, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(302, 202, 2)) })
    expect(img.style.transform).toContain('scale(2.5)')

    // Holding the second tap and moving past the fit-only dismiss threshold
    // must remain a zoomed gesture. A stale pre-render zoom read used to arm
    // the swipe here and close the viewer on release.
    act(() => { fireEvent.pointerMove(overlay, touch(460, 202, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(460, 202, 2)) })

    expect(container.querySelector('img')).not.toBeNull()
    expect(img.style.transform).toContain('scale(2.5)')
  })

  it('does not claim mouse pointers for double-tap zoom', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)

    const mouse = (y: number, x = 100) => ({
      pointerType: 'mouse',
      pointerId: 1,
      clientX: x,
      clientY: y,
    })

    act(() => { fireEvent.pointerDown(overlay, mouse(300, 400)) })
    act(() => { fireEvent.pointerUp(overlay, mouse(300, 400)) })
    act(() => { fireEvent.pointerDown(overlay, mouse(300, 400)) })

    expect(img.style.transform).toContain('scale(1)')
  })

  it('does not trigger double-tap zoom when rapidly tapping toolbar buttons', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { img } = surfaces(container)
    const zoomInBtn = [...container.querySelectorAll('button')].find(b =>
      /zoom in/i.test(b.getAttribute('aria-label') || ''),
    ) as HTMLButtonElement
    expect(zoomInBtn).not.toBeNull()

    act(() => {
      fireEvent.pointerDown(zoomInBtn, touch(10, 10, 1))
      fireEvent.click(zoomInBtn)
    })
    expect(img.style.transform).toContain('scale(1.5)')

    act(() => {
      fireEvent.pointerDown(zoomInBtn, touch(10, 10, 2))
      fireEvent.click(zoomInBtn)
    })
    expect(img.style.transform).toContain('scale(2)')
  })
})
