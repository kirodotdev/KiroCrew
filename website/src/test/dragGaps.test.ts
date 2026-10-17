/**
 * Tests for computeHeaderDragGaps — the control-free spans of the header band
 * that a remote pane relays so the Electron host can re-add a window-drag
 * region there. The key invariant is failure-safety: each control is padded
 * outward before exclusion, so a gap can only ever shrink off a control, never
 * straddle one.
 */
import { describe, it, expect, afterEach } from 'vitest'

import { computeHeaderDragGaps } from '../lib/dragGaps'

type Span = { left: number; right: number; height?: number }

function stubRect(el: HTMLElement, { left, right, height = 42 }: Span) {
  el.getBoundingClientRect = () =>
    ({ width: right - left, height, top: 0, left, right, bottom: height, x: left, y: 0, toJSON: () => ({}) }) as DOMRect
}

function mountHeader(controls: Span[]): HTMLElement {
  const header = document.createElement('header')
  header.className = 'topbar-glass'
  for (const c of controls) {
    const btn = document.createElement('button')
    stubRect(btn, c)
    header.appendChild(btn)
  }
  document.body.appendChild(header)
  return header
}

afterEach(() => {
  document.querySelectorAll('header.topbar-glass').forEach(h => h.remove())
})

describe('computeHeaderDragGaps', () => {
  it('returns the whole band as one gap when the header has no controls', () => {
    const header = mountHeader([])
    expect(computeHeaderDragGaps(header, 1000)).toEqual([{ x: 0, w: 1000 }])
  })

  it('returns [] for a zero-width band', () => {
    const header = mountHeader([{ left: 100, right: 200 }])
    expect(computeHeaderDragGaps(header, 0)).toEqual([])
  })

  it('leaves a gap on each side of a centered control (padded off it)', () => {
    const header = mountHeader([{ left: 400, right: 600 }])
    // control padded outward by 6 -> blocked [394, 606]
    expect(computeHeaderDragGaps(header, 1000)).toEqual([
      { x: 0, w: 394 },
      { x: 606, w: 394 },
    ])
  })

  it('merges overlapping controls into one blocked span', () => {
    const header = mountHeader([
      { left: 100, right: 300 },
      { left: 250, right: 400 },
    ])
    // padded + merged -> [94, 406]
    expect(computeHeaderDragGaps(header, 1000)).toEqual([
      { x: 0, w: 94 },
      { x: 406, w: 594 },
    ])
  })

  it('drops a gap narrower than the minimum between two controls', () => {
    // A padded right = 106, B padded left = 111 -> a 5px middle gap, dropped.
    const header = mountHeader([
      { left: 40, right: 100 },
      { left: 117, right: 300 },
    ])
    const gaps = computeHeaderDragGaps(header, 1000)
    // No sliver gap around x≈106; only the wide leading + trailing gaps survive.
    expect(gaps.some(g => g.x > 0 && g.x < 306)).toBe(false)
    expect(gaps).toEqual([
      { x: 0, w: 34 },
      { x: 306, w: 694 },
    ])
  })

  it('ignores controls with no box (hidden / not laid out)', () => {
    const header = mountHeader([{ left: 400, right: 600, height: 0 }])
    expect(computeHeaderDragGaps(header, 1000)).toEqual([{ x: 0, w: 1000 }])
  })

  it('clamps a control that overflows the band edges', () => {
    const header = mountHeader([{ left: -20, right: 995 }])
    // padded [<=0, >=1000] -> whole band blocked, no gap wide enough remains.
    expect(computeHeaderDragGaps(header, 1000)).toEqual([])
  })
})
