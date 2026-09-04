/**
 * useMouseForward — the hook that tells the main process WHERE the pet is.
 *
 * Everything here is a click-through bug that has actually shipped:
 *
 *  - an INACTIVE overlay must report `null`, not its own position: there is one
 *    hitbox slot but one overlay per display, so a second reporter made the two
 *    overwrite each other and the shell's poll flipped click-through every tick;
 *  - it must NOT gate on `dragging`, because a latched drag flag (a press whose
 *    mouseup was lost) then froze the hitbox forever, which reads to the user as
 *    "the pet stopped responding to clicks" with nothing in any log;
 *  - the post-drag and post-mouseup re-sends exist for short drags where the main
 *    process never starts drag-polling, so nothing else would re-send.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { MutableRefObject } from 'react'

const updateHitbox = vi.fn()
const onDragEnded = vi.fn<(cb: () => void) => () => void>()

vi.mock('../src/mochiApi', () => ({
  api: {
    get updateHitbox() {
      return updateHitbox
    },
    get onDragEnded() {
      return onDragEnded
    },
  },
}))

import { PET_H, PET_W, BUBBLE_W } from '../src/shared/constants'
import { useMouseForward, type UseMouseForwardParams } from '../src/renderer/hooks/useMouseForward'

function ref<T>(value: T): MutableRefObject<T> {
  return { current: value }
}

function params(over: Partial<UseMouseForwardParams> = {}): UseMouseForwardParams {
  return {
    pos: { x: 10, y: 20 },
    visualPos: { x: 10, y: 20 },
    bubble: null,
    bubbleX: 300,
    bubbleAbove: false,
    bubbleY: 40,
    bubbleHeight: 120,
    isPeekingForSvgRef: ref(false),
    hideEdge: null,
    dragging: ref(false),
    isActiveRef: ref(true),
    ...over,
  }
}

let offDrag: ReturnType<typeof vi.fn>

beforeEach(() => {
  updateHitbox.mockReset()
  offDrag = vi.fn()
  onDragEnded.mockReset().mockReturnValue(offDrag)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useMouseForward — reporting the pet box', () => {
  it('reports the pet rect at the VISUAL position, with no bubble', () => {
    renderHook(() => useMouseForward(params()))
    expect(updateHitbox).toHaveBeenCalledWith({ x: 10, y: 20, w: PET_W, h: PET_H }, null)
  })

  it('says nothing but null while the overlay is inactive', () => {
    renderHook(() => useMouseForward(params({ isActiveRef: ref(false) })))
    // One call, and it is the CLEAR — never this display's own position.
    expect(updateHitbox).toHaveBeenCalledTimes(1)
    expect(updateHitbox).toHaveBeenCalledWith(null, null)
  })

  it('keeps reporting while a drag is in flight — the flag must not gate the send', () => {
    renderHook(() => useMouseForward(params({ dragging: ref(true) })))
    expect(updateHitbox).toHaveBeenCalledWith({ x: 10, y: 20, w: PET_W, h: PET_H }, null)
  })

  it('does not re-send an unchanged position', () => {
    const p = params()
    const { rerender } = renderHook(() => useMouseForward(p))
    expect(updateHitbox).toHaveBeenCalledTimes(1)
    rerender()
    expect(updateHitbox).toHaveBeenCalledTimes(1)
  })

  it('re-sends when the pet moves', () => {
    let current = params()
    const { rerender } = renderHook(() => useMouseForward(current))
    current = params({ pos: { x: 99, y: 20 }, visualPos: { x: 99, y: 20 } })
    rerender()
    expect(updateHitbox).toHaveBeenLastCalledWith({ x: 99, y: 20, w: PET_W, h: PET_H }, null)
    expect(updateHitbox).toHaveBeenCalledTimes(2)
  })
})

describe('useMouseForward — the bubble box', () => {
  it('uses the laid-out bubble x and pads the height for the tail', () => {
    renderHook(() => useMouseForward(params({ bubble: 'zzq text' })))
    expect(updateHitbox).toHaveBeenLastCalledWith(
      { x: 10, y: 20, w: PET_W, h: PET_H },
      { x: 300, y: 40, w: BUBBLE_W, h: 140 },
    )
  })

  it('falls back to a default height when the bubble has not measured yet', () => {
    renderHook(() => useMouseForward(params({ bubble: 'zzq text', bubbleHeight: 0 })))
    expect(updateHitbox.mock.calls.at(-1)?.[1]).toMatchObject({ h: 220 })
  })

  it('anchors the bubble to the pet while peeking from the left edge', () => {
    renderHook(() =>
      useMouseForward(
        params({ bubble: 'zzq', isPeekingForSvgRef: ref(true), hideEdge: 'left' }),
      ),
    )
    expect(updateHitbox.mock.calls.at(-1)?.[1]).toMatchObject({ x: 10 + PET_W * 0.45 })
  })

  it('anchors it the other way while peeking from the right edge', () => {
    renderHook(() =>
      useMouseForward(
        params({ bubble: 'zzq', isPeekingForSvgRef: ref(true), hideEdge: 'right' }),
      ),
    )
    expect(updateHitbox.mock.calls.at(-1)?.[1]).toMatchObject({
      x: 10 + PET_W * 0.55 - BUBBLE_W,
    })
  })

  it('re-sends the hitbox periodically while the bubble is up, then stops on unmount', async () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => useMouseForward(params({ bubble: 'zzq' })))
    expect(updateHitbox).toHaveBeenCalledTimes(1)

    // The tick exists because a sleep/wake can lose the main process's copy of
    // the hitbox while nothing about the pet's position changes.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(updateHitbox).toHaveBeenCalledTimes(2)

    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(updateHitbox).toHaveBeenCalledTimes(2)
  })

  it('does not run the re-send timer with no bubble', async () => {
    vi.useFakeTimers()
    renderHook(() => useMouseForward(params()))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(updateHitbox).toHaveBeenCalledTimes(1)
  })
})

describe('useMouseForward — drag end', () => {
  it('re-sends after the shell reports the drag ended, once the position settles', async () => {
    vi.useFakeTimers()
    renderHook(() => useMouseForward(params({ visualPos: { x: 55, y: 66 } })))
    updateHitbox.mockClear()

    const cb = onDragEnded.mock.calls[0][0]
    await act(async () => {
      cb()
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).toHaveBeenCalledWith({ x: 55, y: 66, w: PET_W, h: PET_H }, null)
  })

  it('skips the re-send when the overlay went inactive or a new drag started', async () => {
    vi.useFakeTimers()
    const inactive = params({ isActiveRef: ref(false) })
    renderHook(() => useMouseForward(inactive))
    let cb = onDragEnded.mock.calls[0][0]
    updateHitbox.mockClear()
    await act(async () => {
      cb()
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).not.toHaveBeenCalled()

    onDragEnded.mockClear()
    renderHook(() => useMouseForward(params({ dragging: ref(true) })))
    cb = onDragEnded.mock.calls[0][0]
    updateHitbox.mockClear()
    await act(async () => {
      cb()
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).not.toHaveBeenCalled()
  })

  it('unsubscribes from the shell on unmount', () => {
    const { unmount } = renderHook(() => useMouseForward(params()))
    unmount()
    expect(offDrag).toHaveBeenCalledTimes(1)
  })
})

describe('useMouseForward — mouseup safety net', () => {
  it('re-sends after a mouseup that drag-polling never saw', async () => {
    vi.useFakeTimers()
    renderHook(() => useMouseForward(params({ visualPos: { x: 7, y: 8 } })))
    updateHitbox.mockClear()

    await act(async () => {
      window.dispatchEvent(new Event('mouseup'))
      // The handler defers to the next frame so React has re-rendered with the
      // final position from useDrag's own mouseup.
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).toHaveBeenCalledWith({ x: 7, y: 8, w: PET_W, h: PET_H }, null)
  })

  it('sends nothing on mouseup when a drag is still latched', async () => {
    vi.useFakeTimers()
    renderHook(() => useMouseForward(params({ dragging: ref(true) })))
    updateHitbox.mockClear()
    await act(async () => {
      window.dispatchEvent(new Event('mouseup'))
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).not.toHaveBeenCalled()
  })

  it('detaches the listener on unmount', async () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => useMouseForward(params()))
    unmount()
    updateHitbox.mockClear()
    await act(async () => {
      window.dispatchEvent(new Event('mouseup'))
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(updateHitbox).not.toHaveBeenCalled()
  })
})
