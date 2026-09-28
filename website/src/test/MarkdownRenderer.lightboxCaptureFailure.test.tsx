import { render, fireEvent, act } from '@testing-library/react'
import { Lightbox } from '../components/MarkdownRenderer'

// Capture-failure fallback on the image lightbox pan.
//
// When `setPointerCapture` throws on the pan pointer-down (NotFoundError on a
// disconnected element), the drag never receives a `lostpointercapture` and the
// release can land outside the image, so a window-level pointerup/pointercancel
// fallback is armed to terminate the pan. That pan pointer-down also bubbles to
// the overlay, which records the contact in the pinch tracker. The window
// fallback must therefore drop that contact (`trackPointerUp`) BEFORE it
// terminates the pan — otherwise the contact latches forever and the very next
// single-finger touch is read as a second pinch contact, corrupting zoom/pan.
// See the GPT review finding on this PR (MarkdownRenderer window fallback: call
// trackPointerUp before terminatePan) and Design Review's request for a test
// that forces the throw path.

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

describe('Lightbox pan capture-failure window fallback', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 400 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 800 })
  })

  it('drops the tracked contact on the window fallback so a later touch is not a ghost pinch', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    // Zoom above fit with a double-tap so the <img> pan path is live.
    act(() => { fireEvent.pointerDown(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(402, 202, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(402, 202, 2)) })
    expect(img.style.transform).toContain('scale(2.5)')

    // Make setPointerCapture throw so the window fallback arms on the pan press.
    img.setPointerCapture = () => {
      throw new DOMException('InvalidPointerId', 'NotFoundError')
    }

    // Pan press: capture throws (arming the window fallback) and the same press
    // bubbles to the overlay, which records this contact in the pinch tracker.
    const panPointer = 5
    act(() => { fireEvent.pointerDown(img, touch(300, 300, panPointer)) })

    // Release lands on window (outside the image) — the fallback fires.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: panPointer }))
    })

    // A fresh single-finger touch. With the contact correctly dropped, the
    // tracker holds one contact, so no pinch seats and a move does not rescale.
    const nextPointer = 6
    act(() => { fireEvent.pointerDown(overlay, touch(500, 500, nextPointer)) })
    act(() => { fireEvent.pointerMove(overlay, touch(500, 300, nextPointer)) })

    // No ghost pinch: the zoom is unchanged by a single-finger gesture. If the
    // stale contact had latched, this move would have scaled against it.
    expect(img.style.transform).toContain('scale(2.5)')

    act(() => { fireEvent.pointerUp(overlay, touch(500, 300, nextPointer)) })
  })

  it('keeps the fallback armed when a pinch takes over, so the uncaptured pan pointer still drops its contact', () => {
    // Distinct from the case above: here a SECOND contact seats a pinch while
    // the uncaptured pan pointer is still down, firing onPinchStart. onPinchStart
    // must NOT disarm that pointer's window fallback — if it does, and the pan
    // finger then lifts outside the overlay, its contact is never dropped and the
    // next single touch seats a ghost pinch. See the GPT review finding on this
    // PR (MarkdownRenderer.tsx onPinchStart disarms the sole live fallback).
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    // Zoom above fit so the <img> pan path is live.
    act(() => { fireEvent.pointerDown(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(402, 202, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(402, 202, 2)) })
    expect(img.style.transform).toContain('scale(2.5)')

    // Make setPointerCapture throw so the window fallback arms on the pan press.
    img.setPointerCapture = () => {
      throw new DOMException('InvalidPointerId', 'NotFoundError')
    }

    // Pan press (uncaptured, fallback armed); the press bubbles to the overlay
    // and records this contact in the pinch tracker.
    const panPointer = 5
    act(() => { fireEvent.pointerDown(img, touch(300, 300, panPointer)) })

    // A second contact arrives on the overlay and seats a pinch -> onPinchStart
    // fires (tears down the pan) while the pan pointer is still down.
    const pinchPointer = 7
    act(() => { fireEvent.pointerDown(overlay, touch(320, 500, pinchPointer)) })
    // The second finger lifts, ending the pinch.
    act(() => { fireEvent.pointerUp(overlay, touch(320, 500, pinchPointer)) })

    // The uncaptured pan finger now lifts OUTSIDE the overlay (window only).
    // With the fallback still armed, this drops its contact; if onPinchStart had
    // disarmed it, the contact would latch here.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: panPointer }))
    })

    const scaleAfterGesture = img.style.transform

    // A fresh single-finger touch + move must NOT seat a ghost pinch.
    const nextPointer = 9
    act(() => { fireEvent.pointerDown(overlay, touch(500, 500, nextPointer)) })
    act(() => { fireEvent.pointerMove(overlay, touch(500, 300, nextPointer)) })

    // Zoom unchanged by the single-finger gesture: no stale contact latched.
    expect(img.style.transform).toBe(scaleAfterGesture)

    act(() => { fireEvent.pointerUp(overlay, touch(500, 300, nextPointer)) })
  })

  it('keeps each uncaptured pointer its own fallback so a second image press does not strand the first', () => {
    // The two-uncaptured-touches case: both image presses have setPointerCapture
    // throw, so each arms a window fallback. A single-slot fallback let the
    // SECOND press evict the FIRST pointer's listeners — then the first finger
    // lifting outside the overlay was never heard and its contact stranded. With
    // a per-pointer registry each fallback survives until its own up/cancel.
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const { overlay, img } = surfaces(container)
    sizeHost(img)

    // Zoom above fit so the <img> pan path is live.
    act(() => { fireEvent.pointerDown(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerUp(overlay, touch(400, 200, 1)) })
    act(() => { fireEvent.pointerDown(overlay, touch(402, 202, 2)) })
    act(() => { fireEvent.pointerUp(overlay, touch(402, 202, 2)) })
    expect(img.style.transform).toContain('scale(2.5)')

    // Every image press has capture throw, arming a fallback per pointer.
    img.setPointerCapture = () => {
      throw new DOMException('InvalidPointerId', 'NotFoundError')
    }

    // First uncaptured image press (fallback armed for pointer A); its press
    // bubbles to the overlay, recording contact A in the pinch tracker.
    const pointerA = 11
    act(() => { fireEvent.pointerDown(img, touch(300, 300, pointerA)) })

    // Second uncaptured image press (pointer B) — under the old single slot this
    // disarmed pointer A's fallback. It also records contact B.
    const pointerB = 12
    act(() => { fireEvent.pointerDown(img, touch(320, 320, pointerB)) })

    // Pointer A now lifts OUTSIDE the overlay (window only). Its fallback must
    // still be armed so contact A is dropped here.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: pointerA }))
    })
    // Pointer B lifts too (also outside).
    act(() => {
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: pointerB }))
    })

    const scaleAfterGesture = img.style.transform

    // A fresh single-finger touch + move must NOT seat a ghost pinch: with both
    // contacts correctly dropped, the tracker holds one, so no rescale.
    const nextPointer = 13
    act(() => { fireEvent.pointerDown(overlay, touch(500, 500, nextPointer)) })
    act(() => { fireEvent.pointerMove(overlay, touch(500, 300, nextPointer)) })

    expect(img.style.transform).toBe(scaleAfterGesture)

    act(() => { fireEvent.pointerUp(overlay, touch(500, 300, nextPointer)) })
  })
})
