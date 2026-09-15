// Feature: chat transcript — #7045 bubble-vanish diagnostic probe.
//
// The probe must be FREE when the flag is off (no observer constructed) and,
// when on, must log exactly on a drop in mounted-row count with the store and
// display counts captured at that instant.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import {
  useBubbleVanishProbe,
  BUBBLE_PROBE_FLAG,
} from '../pages/chat/useBubbleVanishProbe'

function makeScrollerWithRows(n: number) {
  const el = document.createElement('div')
  for (let i = 0; i < n; i++) {
    const row = document.createElement('div')
    row.setAttribute('data-display-index', String(i))
    el.appendChild(row)
  }
  document.body.appendChild(el)
  return el
}

/**
 * Scroller mirroring the REAL transcript nesting: `data-display-index` sits on
 * the virtualizer's row wrapper, and the collapsible container (the shape
 * CollapsibleSection produces) renders INSIDE the row. Toggling
 * `data-collapsed` on that inner container hides the row's content in place
 * without removing a single node.
 */
function makeScrollerWithCollapsible(nPlainRows: number, nFoldedRows: number) {
  const el = document.createElement('div')
  for (let i = 0; i < nPlainRows; i++) {
    const row = document.createElement('div')
    row.setAttribute('data-display-index', String(i))
    el.appendChild(row)
  }
  const containers: HTMLElement[] = []
  for (let i = 0; i < nFoldedRows; i++) {
    const row = document.createElement('div')
    row.setAttribute('data-display-index', String(nPlainRows + i))
    const fold = document.createElement('div') // CollapsibleSection's motion.div
    fold.appendChild(document.createElement('div')) // the folded turn content
    row.appendChild(fold)
    el.appendChild(row)
    containers.push(fold)
  }
  document.body.appendChild(el)
  return { el, containers }
}

describe('useBubbleVanishProbe', () => {
  let warnSpy: ReturnType<typeof vi.spyOn>
  let rafSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    localStorage.clear()
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // Run the coalescing frame synchronously so MutationObserver microtasks
    // and the probe's reading happen inside the same act() block.
    rafSpy = vi
      .spyOn(globalThis, 'requestAnimationFrame')
      .mockImplementation(((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame)
  })

  afterEach(() => {
    warnSpy.mockRestore()
    rafSpy.mockRestore()
    document.body.replaceChildren()
  })

  it('constructs no observer when the flag is off', () => {
    const observeSpy = vi.spyOn(MutationObserver.prototype, 'observe')
    const el = makeScrollerWithRows(3)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    renderHook(() => useBubbleVanishProbe(ref, () => ({ store: 3, display: 3 })))
    expect(observeSpy).not.toHaveBeenCalled()
    observeSpy.mockRestore()
  })

  it('logs a snapshot when the mounted-row count drops, not when it grows', async () => {
    localStorage.setItem(BUBBLE_PROBE_FLAG, '1')
    const el = makeScrollerWithRows(4)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const counts = { store: 10, display: 4 }
    renderHook(() => useBubbleVanishProbe(ref, () => ({ ...counts })))

    // Growth: no log.
    await act(async () => {
      const row = document.createElement('div')
      row.setAttribute('data-display-index', '4')
      el.appendChild(row)
      await Promise.resolve() // flush the MutationObserver microtask
    })
    expect(warnSpy).not.toHaveBeenCalled()

    // Drop: two rows unmount → one snapshot with the live counts.
    counts.display = 3
    await act(async () => {
      el.removeChild(el.lastChild!)
      el.removeChild(el.lastChild!)
      await Promise.resolve()
    })
    expect(warnSpy).toHaveBeenCalledTimes(1)
    const payload = warnSpy.mock.calls[0][1] as Record<string, unknown>
    expect(payload.kind).toBe('mounted-drop')
    expect(payload.mountedBefore).toBe(5)
    expect(payload.mountedAfter).toBe(3)
    expect(payload.storeMessages).toBe(10)
    expect(payload.displayItems).toBe(3)
  })

  it('reports the hidden-in-place bucket when a collapse hides a row\'s content without removing nodes', async () => {
    localStorage.setItem(BUBBLE_PROBE_FLAG, '1')
    // Real nesting: 2 plain rows + 3 rows each wrapping a collapsible fold.
    const { el, containers } = makeScrollerWithCollapsible(2, 3)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    renderHook(() => useBubbleVanishProbe(ref, () => ({ store: 5, display: 5 })))

    // Collapse one turn: the attribute flips on the fold INSIDE its row,
    // no node is removed anywhere.
    await act(async () => {
      containers[0].setAttribute('data-collapsed', 'true')
      await Promise.resolve()
    })
    expect(warnSpy).toHaveBeenCalledTimes(1)
    expect(warnSpy.mock.calls[0][0]).toContain('hidden in place')
    const payload = warnSpy.mock.calls[0][1] as Record<string, unknown>
    expect(payload.kind).toBe('hidden-in-place')
    expect(payload.mountedBefore).toBe(payload.mountedAfter) // mounted held
    expect(payload.mountedAfter).toBe(5)
    expect(payload.visibleBefore).toBe(5)
    expect(payload.visibleAfter).toBe(4) // the collapsed row left the visible count
  })

  it('re-expanding (attribute removed) produces no reading — only drops are reported', async () => {
    localStorage.setItem(BUBBLE_PROBE_FLAG, '1')
    const { el, containers } = makeScrollerWithCollapsible(2, 3)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    renderHook(() => useBubbleVanishProbe(ref, () => ({ store: 5, display: 5 })))

    await act(async () => {
      containers[0].setAttribute('data-collapsed', 'true')
      await Promise.resolve()
    })
    expect(warnSpy).toHaveBeenCalledTimes(1) // the collapse itself

    await act(async () => {
      containers[0].removeAttribute('data-collapsed')
      await Promise.resolve()
    })
    expect(warnSpy).toHaveBeenCalledTimes(1) // re-expand added nothing
  })
})
