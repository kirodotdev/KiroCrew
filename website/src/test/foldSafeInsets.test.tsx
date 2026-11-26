/**
 * Fold pane widths follow the shell's safe-area insets as they change
 * (hooks/useDrawerSwipe.ts `useSafeInsets`). A landscape turn through 180
 * degrees moves a side inset from one edge to the other while the width and
 * the fold segments stay the same; the sessions list must re-render from the
 * new insets so it still ends at the fold.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))

import { useSafeInsets } from '../hooks/useDrawerSwipe'
import { useSidebarResize } from '../pages/chat-sidebar/resize'

// The probes measure `env(safe-area-inset-*)` through a fixed element's box;
// jsdom has no layout, so the box is stubbed from the inset the device has now.
const device = { left: 0, right: 0, width: 1060 }
const realRect = Element.prototype.getBoundingClientRect
function stubInsets() {
  vi.spyOn(document.documentElement, 'clientWidth', 'get').mockImplementation(() => device.width)
  Element.prototype.getBoundingClientRect = function (this: Element) {
    // jsdom drops the `env()` value itself, so a probe is told by its other
    // styles: a hidden fixed box of no size. Each probe reads its own edge.
    const s = (this as HTMLElement).style
    if (s?.position === 'fixed' && s.visibility === 'hidden' && parseFloat(s.width) === 0) {
      return { left: device.left, right: device.width - device.right } as DOMRect
    }
    return realRect.call(this)
  }
}
function turn(left: number, right: number) {
  device.left = left
  device.right = right
  window.dispatchEvent(new Event('orientationchange'))
}

afterEach(() => {
  cleanup()
  Element.prototype.getBoundingClientRect = realRect
  vi.restoreAllMocks()
  segmentsMock.value = null
  localStorage.clear()
  // Leave the memoized probes at the default for the next file.
  device.left = 0; device.right = 0
  window.dispatchEvent(new Event('orientationchange'))
})

describe('the safe-area insets fold widths are sized from', () => {
  it('re-render with the new insets when a turn moves the inset to the other edge', () => {
    stubInsets()
    turn(0, 42)
    const { result } = renderHook(() => useSafeInsets())
    expect(result.current).toEqual({ left: 0, right: 42 })
    act(() => { turn(42, 0) })
    expect(result.current).toEqual({ left: 42, right: 0 })
  })

  it('keep the sessions list at the fold across that turn', () => {
    stubInsets()
    turn(0, 42)
    // 1060px with a 20px hinge: segments 0-520 and 540-1060.
    segmentsMock.value = [{ x: 0, width: 520 }, { x: 540, width: 520 }]
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false }))
    // No leading inset: the list takes the leading segment less the 236px rail.
    expect(result.current.paintedSidebarWidth).toBe(520 - 236)
    // The inset moves to the left: the shell starts 42px in, so the list narrows
    // and still ends at the fold.
    act(() => { turn(42, 0) })
    expect(result.current.paintedSidebarWidth).toBe(520 - 42 - 236)
  })
})
