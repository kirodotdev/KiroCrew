// Feature: chat-virtualizer — hidden-ancestor resize reports must not poison
// the height cache.
//
// ResizeObserver delivers a 0×0 content box for every observed row the moment
// an ancestor goes display:none (hidden tab, collapsed panel) — that is the
// observer reporting visibility, not a row height. The RO write path must skip
// those entries: writing them caches 0 for every mounted row (and HeightCache
// persists per session), so on return the offset tree prices the region at
// heightAt's 1px floor, offsetBefore collapses, and the transcript shows the
// "blank area above" symptom until every row remounts and re-measures. The
// measureRef seed path already applies an h > 0 floor; this suite pins the
// same floor on the ResizeObserver path.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id

class FakeRO {
  static instances: FakeRO[] = []
  constructor(readonly cb: ResizeObserverCallback) { FakeRO.instances.push(this) }
  observe() {}
  unobserve() {}
  disconnect() {}
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

/** A row node whose measured height the test controls after registration. */
function mkRowNode(initialH: number): { node: HTMLDivElement; setH: (h: number) => void } {
  const node = document.createElement('div')
  let h = initialH
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
  return { node, setH: (v: number) => { h = v } }
}

describe('useVirtualChat: ResizeObserver zero-height guard', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    FakeRO.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
  })

  function mount(sessionId: string, rowCount: number) {
    const el = document.createElement('div')
    Object.defineProperty(el, 'scrollTop', { configurable: true, value: 0, writable: true })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 3000 })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 400 })
    ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = () => {}
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = Array.from({ length: rowCount }, (_, i) => ({ id: `m${i}` }))
    const view = renderHook(() =>
      useVirtualChat<Item>({ items, sessionId, getKey, externalScrollerRef: ref }),
    )
    const rows = items.map((_, i) => {
      const row = mkRowNode(100)
      act(() => { view.result.current.measureRef(i)(row.node) })
      return row
    })
    // Settle the seed measurements' debounced sync so the baseline is the
    // committed post-mount tree, not a mid-debounce snapshot.
    act(() => { vi.advanceTimersByTime(150) })
    return { view, rows }
  }

  it('ignores 0-height entries (hidden ancestor) instead of caching them', () => {
    const { view, rows } = mount('zero-h-guard', 5)
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    const baseline = view.result.current.totalHeight
    expect(baseline).toBeGreaterThan(0)

    // The transcript's ancestor goes display:none: the observer reports every
    // mounted row at 0 in one tick.
    act(() => {
      rows.forEach((r) => r.setH(0))
      ro.fire(rows.map((r) => ({ target: r.node })))
    })
    act(() => { vi.advanceTimersByTime(300) })

    // Nothing was written: the tree still prices every row at its real height.
    expect(view.result.current.totalHeight).toBe(baseline)
  })

  it('still applies a genuine resize after the hidden interval ends', () => {
    const { view, rows } = mount('zero-h-recover', 5)
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    const baseline = view.result.current.totalHeight

    // Hidden tick (skipped), then the ancestor is shown again and row 0 comes
    // back GROWN — the guard must not swallow the real follow-up measurement.
    act(() => {
      rows.forEach((r) => r.setH(0))
      ro.fire(rows.map((r) => ({ target: r.node })))
    })
    act(() => { vi.advanceTimersByTime(300) })
    act(() => {
      rows.forEach((r) => r.setH(100))
      rows[0].setH(140)
      ro.fire(rows.map((r) => ({ target: r.node })))
    })
    act(() => { vi.advanceTimersByTime(300) })

    expect(view.result.current.totalHeight).toBe(baseline + 40)
  })
})
