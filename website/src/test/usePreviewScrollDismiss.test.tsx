import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { renderHook, cleanup } from '@testing-library/react'
import { usePreviewScrollDismiss } from '../components/usePreviewScrollDismiss'

// The rule shared by the composer's and the sent bubble's paste previews: a
// resize closes the preview, a scroll closes it only when it moved the anchor.

let frames: FrameRequestCallback[] = []
const runFrames = () => { const due = frames; frames = []; due.forEach(cb => cb(0)) }

function anchorAt(pos: { left: number; top: number }): HTMLElement {
  const el = document.createElement('span')
  document.body.appendChild(el)
  el.getBoundingClientRect = () => ({ left: pos.left, top: pos.top, right: pos.left + 40, bottom: pos.top + 16, width: 40, height: 16, x: pos.left, y: pos.top, toJSON: () => ({}) }) as DOMRect
  return el
}

beforeEach(() => {
  frames = []
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length })
  vi.stubGlobal('cancelAnimationFrame', () => {})
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.body.replaceChildren() })

describe('usePreviewScrollDismiss', () => {
  it('keeps the preview on a scroll that leaves its anchor in place', () => {
    const pos = { left: 10, top: 20 }
    const el = anchorAt(pos)
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(true, () => el, close))
    window.dispatchEvent(new Event('scroll'))
    runFrames()
    expect(close).not.toHaveBeenCalled()
  })

  it('closes at once on a scroll that moved its anchor', () => {
    const pos = { left: 10, top: 20 }
    const el = anchorAt(pos)
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(true, () => el, close))
    pos.top = 60
    window.dispatchEvent(new Event('scroll'))
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('closes on the next frame when the anchor only moves after the scroll event', () => {
    const pos = { left: 10, top: 20 }
    const el = anchorAt(pos)
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(true, () => el, close))
    window.dispatchEvent(new Event('scroll'))
    expect(close).not.toHaveBeenCalled()
    pos.top = 4
    runFrames()
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('closes on a scroll once the anchor is gone', () => {
    const el = anchorAt({ left: 10, top: 20 })
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(true, () => el, close))
    el.remove()
    window.dispatchEvent(new Event('scroll'))
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('closes on resize even when the anchor did not move', () => {
    const el = anchorAt({ left: 10, top: 20 })
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(true, () => el, close))
    window.dispatchEvent(new Event('resize'))
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('listens to nothing while the preview is closed', () => {
    const el = anchorAt({ left: 10, top: 20 })
    const close = vi.fn()
    renderHook(() => usePreviewScrollDismiss(false, () => el, close))
    el.remove()
    window.dispatchEvent(new Event('scroll'))
    window.dispatchEvent(new Event('resize'))
    expect(close).not.toHaveBeenCalled()
  })
})
