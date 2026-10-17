/**
 * SessionGridLayout divider resize via Pointer Events (mouse + touch + pen).
 *
 * The per-split dividers use the shared usePointerDrag hook, keyed by
 * data-divider-index, so a touch drag resizes a split exactly like a mouse drag.
 *
 * Locks:
 *  (1) each divider is a role="separator" with the correct aria-orientation and
 *      touch-action:none (so a touch drag resizes instead of scrolling);
 *  (2) a pointer drag reports the fractional delta to onResize for the right
 *      split id + divider index — a col split tracks clientX, a row split clientY.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import SessionGridLayout from '../components/SessionGridLayout'
import type { GridSplit } from '../hooks/useSessionGrid'

// jsdom has no layout: stub getBoundingClientRect so the split's measured extent
// (width for a col split, height for a row split) is a deterministic 200px, and
// the reported fraction is exact.
let rectSpy: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, top: 0, left: 0, right: 200, bottom: 200, width: 200, height: 200, toJSON: () => ({}),
  } as DOMRect)
})
afterEach(() => { rectSpy.mockRestore() })

const leaf = (id: string) => ({ type: 'leaf' as const, id, kind: 'placeholder' as const })

function renderSplit(dir: 'col' | 'row', onResize = vi.fn()) {
  const node: GridSplit = { type: 'split', id: 'root', dir, children: [leaf('a'), leaf('b')], sizes: [1, 1] }
  const utils = render(
    <SessionGridLayout node={node} renderLeaf={(l) => <div data-leaf={l.id} />} onResize={onResize} />,
  )
  const divider = utils.container.querySelector('[data-divider-index="0"]') as HTMLElement
  return { ...utils, divider, onResize }
}

describe('SessionGridLayout — pointer/touch divider resize', () => {
  it('a col split divider is a vertical separator with touch-action:none', () => {
    const { divider } = renderSplit('col')
    expect(divider).toBeTruthy()
    expect(divider.getAttribute('role')).toBe('separator')
    expect(divider.getAttribute('aria-orientation')).toBe('vertical')
    expect(divider.style.touchAction).toBe('none')
  })

  it('a pointer drag on a col divider reports the horizontal fractional delta to onResize', () => {
    const { divider, onResize } = renderSplit('col')
    fireEvent.pointerDown(divider, { clientX: 100, clientY: 50, pointerId: 1 })
    act(() => { fireEvent.pointerMove(divider, { clientX: 150, clientY: 50, pointerId: 1 }) })
    act(() => { fireEvent.pointerUp(divider, { clientX: 150, clientY: 50, pointerId: 1 }) })
    // extent = 200 (stubbed width); moved +50px → +0.25 fraction for split 'root', divider 0.
    expect(onResize).toHaveBeenCalledWith('root', 0, 0.25)
  })

  it('a row split divider is a horizontal separator and tracks clientY', () => {
    const { divider, onResize } = renderSplit('row')
    expect(divider.getAttribute('aria-orientation')).toBe('horizontal')
    fireEvent.pointerDown(divider, { clientX: 10, clientY: 100, pointerId: 1 })
    act(() => { fireEvent.pointerMove(divider, { clientX: 10, clientY: 130, pointerId: 1 }) })
    act(() => { fireEvent.pointerUp(divider, { clientX: 10, clientY: 130, pointerId: 1 }) })
    // extent = 200 (stubbed height); moved +30px → +0.15 fraction.
    expect(onResize).toHaveBeenCalledWith('root', 0, 30 / 200)
  })
})
