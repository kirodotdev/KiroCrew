/**
 * usePinnedScrollOrigin: a fixed-layout `overflow: hidden` box that the engine
 * scrolls to reveal a caret (an undone large paste in the composer) is put back
 * at 0,0, so the chat column does not stay shifted under the window-drag band.
 *
 * jsdom keeps no scroll offset of its own, so each box gets a plain own
 * `scrollTop`/`scrollLeft` value and the test fires the `scroll` event the
 * engine would fire after moving it.
 */
import { useRef } from 'react'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { usePinnedScrollOrigin } from './usePinnedScrollOrigin'

function scrollable(el: HTMLElement) {
  let top = 0
  let left = 0
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
  Object.defineProperty(el, 'scrollLeft', { configurable: true, get: () => left, set: (v: number) => { left = v } })
  return el
}

function RefBox({ enabled }: { enabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  usePinnedScrollOrigin(ref, enabled)
  return <div ref={ref} data-testid="box" />
}

function ElementBox({ el }: { el: HTMLElement | null }) {
  usePinnedScrollOrigin(el)
  return null
}

describe('usePinnedScrollOrigin', () => {
  it('puts a box scrolled by a caret reveal back at 0,0', () => {
    const { getByTestId } = render(<RefBox />)
    const box = scrollable(getByTestId('box'))
    box.scrollTop = 1180
    box.scrollLeft = 24
    box.dispatchEvent(new Event('scroll'))
    expect(box.scrollTop).toBe(0)
    expect(box.scrollLeft).toBe(0)
  })

  it('pins an element passed directly, and a box already scrolled when it mounts', () => {
    const el = scrollable(document.createElement('div'))
    el.scrollTop = 300
    render(<ElementBox el={el} />)
    expect(el.scrollTop).toBe(0)
    el.scrollTop = 90
    el.dispatchEvent(new Event('scroll'))
    expect(el.scrollTop).toBe(0)
  })

  it('leaves the box alone when disabled, so a page that scrolls keeps its offset', () => {
    const { getByTestId } = render(<RefBox enabled={false} />)
    const box = scrollable(getByTestId('box'))
    box.scrollTop = 500
    box.dispatchEvent(new Event('scroll'))
    expect(box.scrollTop).toBe(500)
  })

  it('stops pinning once unmounted', () => {
    const el = scrollable(document.createElement('div'))
    const { unmount } = render(<ElementBox el={el} />)
    unmount()
    el.scrollTop = 70
    el.dispatchEvent(new Event('scroll'))
    expect(el.scrollTop).toBe(70)
  })
})
