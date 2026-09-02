/**
 * useScrollManager — the paths the existing far-jump regression test leaves
 * cold: scrollToBottom (with and without a scroller, and the no-scrollTo
 * fallback), and scrollToDisplayIndex's alignment arithmetic — 'center' vs
 * 'end' vs 'start' + offset — plus its clamp to the scrollable range and its
 * own scrollTop fallback.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useScrollManager } from '../pages/chat/useScrollManager'

/** A stand-in scroller with real geometry: happy-dom reports 0 for every
 *  layout metric, so the numbers the hook does arithmetic on are installed
 *  explicitly. */
function makeScroller(opts: {
  scrollHeight?: number
  clientHeight?: number
  withScrollTo?: boolean
} = {}) {
  const { scrollHeight = 1000, clientHeight = 400, withScrollTo = true } = opts
  const el = document.createElement('div')
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  el.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) })
  const scrollTo = vi.fn()
  if (withScrollTo) (el as unknown as { scrollTo: unknown }).scrollTo = scrollTo
  else (el as unknown as { scrollTo: unknown }).scrollTo = undefined
  return { el, scrollTo }
}

/** A mounted row at a known offset with a known height. */
function addRow(el: HTMLElement, index: number, top: number, height: number) {
  const row = document.createElement('div')
  row.setAttribute('data-display-index', String(index))
  Object.defineProperty(row, 'offsetHeight', { value: height, configurable: true })
  row.getBoundingClientRect = () => ({ top, left: 0, bottom: top + height, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}) })
  el.appendChild(row)
  return row
}

function mount(el: HTMLElement) {
  const { result } = renderHook(() => useScrollManager())
  ;(result.current.scrollerRef as { current: HTMLElement | null }).current = el
  return result
}

describe('useScrollManager.scrollToBottom', () => {
  it('does nothing when there is no scroller element', () => {
    const { result } = renderHook(() => useScrollManager())
    expect(() => result.current.scrollToBottom()).not.toThrow()
  })

  it('scrolls to scrollHeight with the requested behavior', () => {
    const { el, scrollTo } = makeScroller({ scrollHeight: 1234 })
    mount(el).current.scrollToBottom('auto')
    expect(scrollTo).toHaveBeenCalledWith({ top: 1234, behavior: 'auto' })
  })

  it('defaults to smooth behavior', () => {
    const { el, scrollTo } = makeScroller({ scrollHeight: 500 })
    mount(el).current.scrollToBottom()
    expect(scrollTo).toHaveBeenCalledWith({ top: 500, behavior: 'smooth' })
  })

  it('falls back to writing scrollTop when scrollTo is unavailable', () => {
    const { el } = makeScroller({ scrollHeight: 777, withScrollTo: false })
    mount(el).current.scrollToBottom('auto')
    expect(el.scrollTop).toBe(777)
  })
})

describe('useScrollManager.scrollToDisplayIndex alignment', () => {
  it("centers the row for align 'center'", () => {
    const { el, scrollTo } = makeScroller({ scrollHeight: 2000, clientHeight: 400 })
    addRow(el, 3, 500, 100)
    const scrolled = mount(el).current.scrollToDisplayIndex(3, { behavior: 'auto', align: 'center' })
    expect(scrolled).toBe(true)
    // elTop 500 − clientHeight/2 (200) + rowHeight/2 (50) = 350
    expect(scrollTo).toHaveBeenCalledWith({ top: 350, behavior: 'auto' })
  })

  it("puts the row bottom at the viewport bottom for align 'end'", () => {
    const { el, scrollTo } = makeScroller({ scrollHeight: 2000, clientHeight: 400 })
    addRow(el, 3, 800, 100)
    mount(el).current.scrollToDisplayIndex(3, { behavior: 'auto', align: 'end' })
    // elTop 800 − clientHeight 400 + rowHeight 100 = 500
    expect(scrollTo).toHaveBeenCalledWith({ top: 500, behavior: 'auto' })
  })

  it("applies the offset for align 'start' so the header is cleared", () => {
    const { el, scrollTo } = makeScroller({ scrollHeight: 2000, clientHeight: 400 })
    addRow(el, 3, 600, 100)
    mount(el).current.scrollToDisplayIndex(3, { behavior: 'auto', align: 'start', offset: -60 })
    expect(scrollTo).toHaveBeenCalledWith({ top: 540, behavior: 'auto' })
  })

  it('clamps below zero and above the scrollable range', () => {
    const low = makeScroller({ scrollHeight: 2000, clientHeight: 400 })
    addRow(low.el, 1, 10, 20)
    mount(low.el).current.scrollToDisplayIndex(1, { behavior: 'auto', align: 'start', offset: -500 })
    expect(low.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'auto' })

    const high = makeScroller({ scrollHeight: 1000, clientHeight: 400 })
    addRow(high.el, 2, 5000, 40)
    mount(high.el).current.scrollToDisplayIndex(2, { behavior: 'auto', align: 'start' })
    // max = scrollHeight − clientHeight = 600
    expect(high.scrollTo).toHaveBeenCalledWith({ top: 600, behavior: 'auto' })
  })

  it('falls back to writing scrollTop when the scroller has no scrollTo', () => {
    const { el } = makeScroller({ scrollHeight: 2000, clientHeight: 400, withScrollTo: false })
    addRow(el, 4, 700, 100)
    const scrolled = mount(el).current.scrollToDisplayIndex(4, { align: 'end' })
    expect(scrolled).toBe(true)
    expect(el.scrollTop).toBe(400)
  })
})
