/**
 * `useScrollEdgesY` derives a top/bottom fade cue from a vertical scroller's own
 * geometry, the same way `useScrollEdges` does for a horizontal one. These pin
 * the behaviour a fixed-height panel needs: a column that fits shows no cue, a
 * clipped column offers the cue only on the hidden side, and the flags follow
 * the column as it scrolls (which needs a listener bound to the node, not a
 * one-shot read at mount).
 *
 * jsdom does no layout, so scroll geometry is stubbed on the element — that stub
 * is what makes the derivation testable at all.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useScrollEdgesY } from '../hooks/useScrollEdges'

/** Give a node a fixed viewport and a taller content, `scrolled` px past the top. */
function stubGeometry(el: HTMLElement, { hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 300 })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 300 + hidden })
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => scrolled })
}

describe('useScrollEdgesY', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { document.body.replaceChildren() })

  it('shows no cue when the column fits', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    stubGeometry(el, { hidden: 0 })
    const { result } = renderHook(() => useScrollEdgesY<HTMLDivElement>())
    act(() => result.current[0](el))
    expect(result.current[1]).toEqual({ top: false, bottom: false })
  })

  it('cues only the hidden side at the top of a clipped column', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    stubGeometry(el, { hidden: 240 })
    const { result } = renderHook(() => useScrollEdgesY<HTMLDivElement>())
    act(() => result.current[0](el))
    // At the top nothing is hidden above, so only the bottom cue shows.
    expect(result.current[1]).toEqual({ top: false, bottom: true })
  })

  it('follows the column as it scrolls', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    stubGeometry(el, { hidden: 240 })
    const { result } = renderHook(() => useScrollEdgesY<HTMLDivElement>())
    act(() => result.current[0](el))
    expect(result.current[1]).toEqual({ top: false, bottom: true })

    // Scrolled to the far end: the hidden side flips.
    stubGeometry(el, { hidden: 240, scrolled: 240 })
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(result.current[1]).toEqual({ top: true, bottom: false })

    // Scrolled to the middle: both edges clip.
    stubGeometry(el, { hidden: 240, scrolled: 120 })
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(result.current[1]).toEqual({ top: true, bottom: true })
  })

  it('clears the cue when the scroller detaches', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    stubGeometry(el, { hidden: 240 })
    const { result } = renderHook(() => useScrollEdgesY<HTMLDivElement>())
    act(() => result.current[0](el))
    expect(result.current[1]).toEqual({ top: false, bottom: true })
    // A null node means nothing is clipped; a surviving cue would point at
    // content that is not there.
    act(() => result.current[0](null))
    expect(result.current[1]).toEqual({ top: false, bottom: false })
  })
})
