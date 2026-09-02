/**
 * `useDrag` — the companion's pointer handling, including the paths that only
 * the desktop's main process can normally trigger.
 *
 * Everything worth testing here is a boundary: the saved position is clamped to
 * the current screen and can dock the pet at either edge; a plain click must NOT
 * flash the "held" pose (the 6px threshold); a swallowed mouseup must still end
 * the drag (the 2s stuck timer); and the cross-display path arrives as
 * `onDragUpdate` / `onDragEnded` callbacks from outside the renderer entirely.
 *
 * The bridge is stubbed so those callbacks can be invoked directly, and timers
 * are faked — the hook's first read is behind a 300ms delay and the stuck guard
 * is 2s, neither of which a test may actually wait for.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createRef } from 'react'

const bridge = {
  getWindowPosition: vi.fn(),
  savePosition: vi.fn(),
  dragStart: vi.fn(),
  dragEnd: vi.fn(),
  onDragUpdate: vi.fn(),
  onDragEnded: vi.fn(),
}
vi.mock('../apps/crew-companion/petBridge', () => ({ petBridge: bridge }))

const { useDrag } = await import('../apps/crew-companion/useDrag')
const { PET_W, PET_H } = await import('../apps/crew-companion/constants')

/** Callbacks the hook registered with the main process. */
let dragUpdateCb: ((x: number, y: number) => void) | null = null
let dragEndedCb: ((x: number, y: number) => void) | null = null

function options(over: Partial<Parameters<typeof useDrag>[1]> = {}) {
  const isPeekingRef = createRef<boolean>() as React.MutableRefObject<boolean>
  isPeekingRef.current = false
  return {
    clearPersistentMood: vi.fn(),
    displayState: 'idle' as const,
    setDisplayState: vi.fn(),
    isPeekingRef,
    setIsPeeking: vi.fn((v: boolean) => { isPeekingRef.current = v }),
    setHideEdge: vi.fn(),
    allowPeek: true,
    getGrip: () => ({ x: 10, y: 20 }),
    ...over,
  }
}

/** Mount the hook and let its 300ms position read settle. */
async function mount(opts = options()) {
  const view = renderHook(() => useDrag({ x: 5, y: 5 }, opts))
  await act(async () => {
    vi.advanceTimersByTime(300)
    await Promise.resolve()
    await Promise.resolve()
  })
  return { ...view, opts }
}

function mouseDown(clientX: number, clientY: number, button = 0) {
  return { button, clientX, clientY, preventDefault: vi.fn() } as unknown as React.MouseEvent
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  dragUpdateCb = null
  dragEndedCb = null
  bridge.onDragUpdate.mockImplementation((cb: (x: number, y: number) => void) => {
    dragUpdateCb = cb
    return () => { dragUpdateCb = null }
  })
  bridge.onDragEnded.mockImplementation((cb: (x: number, y: number) => void) => {
    dragEndedCb = cb
    return () => { dragEndedCb = null }
  })
  bridge.getWindowPosition.mockResolvedValue({ x: 400, y: 300 })
  // A predictable screen for every clamp assertion below.
  window.innerWidth = 1000
  window.innerHeight = 800
})

afterEach(() => {
  vi.useRealTimers()
})

describe('crew-companion/useDrag — restoring the saved position', () => {
  it('clamps the saved position onto the current screen and reports it ready', async () => {
    bridge.getWindowPosition.mockResolvedValue({ x: 5000, y: -50 })
    const { result } = await mount()
    expect(result.current.posReady).toBe(true)
    expect(result.current.pos).toEqual({ x: 1000 - PET_W, y: 0 })
  })

  it('docks at the left edge when the saved position is against it', async () => {
    bridge.getWindowPosition.mockResolvedValue({ x: 10, y: 100 })
    const { opts } = await mount()
    expect(opts.setHideEdge).toHaveBeenCalledWith('left')
    expect(opts.setIsPeeking).toHaveBeenCalledWith(true)
  })

  it('docks at the right edge when the saved position is against it', async () => {
    bridge.getWindowPosition.mockResolvedValue({ x: 1000 - PET_W - 5, y: 100 })
    const { opts } = await mount()
    expect(opts.setHideEdge).toHaveBeenCalledWith('right')
  })

  it('never docks a custom pack, which has no peek pose', async () => {
    bridge.getWindowPosition.mockResolvedValue({ x: 0, y: 100 })
    const { opts } = await mount(options({ allowPeek: false }))
    expect(opts.setHideEdge).not.toHaveBeenCalled()
    expect(opts.setIsPeeking).not.toHaveBeenCalled()
  })

  it('falls back to the bottom-left with no saved position', async () => {
    bridge.getWindowPosition.mockResolvedValue(null)
    const { result, opts } = await mount()
    expect(result.current.pos).toEqual({ x: 0, y: Math.floor(800 - PET_H - 80) })
    expect(opts.setHideEdge).toHaveBeenCalledWith('left')
    expect(result.current.posReady).toBe(true)
  })

  it('falls back the same way when the read itself fails', async () => {
    bridge.getWindowPosition.mockRejectedValue(new Error('zz-no-bridge'))
    const { result, opts } = await mount()
    expect(result.current.pos).toEqual({ x: 0, y: Math.floor(800 - PET_H - 80) })
    expect(result.current.posReady).toBe(true)
    expect(opts.setIsPeeking).toHaveBeenCalledWith(true)
  })

  it('leaves the fallback undocked for a custom pack', async () => {
    bridge.getWindowPosition.mockResolvedValue(null)
    const { opts } = await mount(options({ allowPeek: false }))
    expect(opts.setHideEdge).not.toHaveBeenCalled()
  })
})

describe('crew-companion/useDrag — pointer drag', () => {
  it('ignores a non-primary button', async () => {
    const { result, opts } = await mount()
    act(() => result.current.onMouseDown(mouseDown(100, 100, 2)))
    expect(result.current.dragging.current).toBe(false)
    expect(opts.clearPersistentMood).not.toHaveBeenCalled()
  })

  it('arms the drag on mousedown and wakes an offline pet', async () => {
    const opts = options({ displayState: 'offline' as const })
    const { result } = await mount(opts)
    const e = mouseDown(100, 100)
    act(() => result.current.onMouseDown(e))
    expect(result.current.dragging.current).toBe(true)
    expect(result.current.isDragging).toBe(false) // not yet — a click must not flash the pose
    expect(opts.clearPersistentMood).toHaveBeenCalled()
    expect(opts.setDisplayState).toHaveBeenCalledWith('idle')
  })

  it('does not enter the held pose until the pointer clears the threshold', async () => {
    const { result } = await mount()
    act(() => result.current.onMouseDown(mouseDown(100, 100)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 103, clientY: 100 })) })
    expect(result.current.isDragging).toBe(false)
    expect(bridge.dragStart).not.toHaveBeenCalled()

    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 140, clientY: 100 })) })
    expect(result.current.isDragging).toBe(true)
    // Grip-by-tip: the offset comes from getGrip, not from where the click landed.
    expect(bridge.dragStart).toHaveBeenCalledWith(10, 20)
    expect(result.current.pos).toEqual({ x: 130, y: 80 })
  })

  it('clamps the dragged position to half-off each edge', async () => {
    const { result } = await mount()
    act(() => result.current.onMouseDown(mouseDown(100, 100)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 140, clientY: 100 })) })
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: -500, clientY: -500 })) })
    expect(result.current.pos).toEqual({ x: -PET_W / 2, y: 0 })
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 5000, clientY: 5000 })) })
    expect(result.current.pos).toEqual({ x: 1000 - PET_W / 2, y: 800 - PET_H })
  })

  it('ignores a mousemove that no mousedown started', async () => {
    const { result } = await mount()
    const before = result.current.pos
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 900, clientY: 700 })) })
    expect(result.current.pos).toEqual(before)
  })

  it('snaps to the left edge on release and saves the position', async () => {
    const { result, opts } = await mount()
    act(() => result.current.onMouseDown(mouseDown(100, 100)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 25, clientY: 300 })) })
    act(() => { window.dispatchEvent(new MouseEvent('mouseup')) })
    expect(result.current.pos.x).toBe(0)
    expect(bridge.savePosition).toHaveBeenCalledWith(0, result.current.pos.y)
    expect(bridge.dragEnd).toHaveBeenCalled()
    expect(opts.setHideEdge).toHaveBeenLastCalledWith('left')
    expect(result.current.dragging.current).toBe(false)
    expect(result.current.isDragging).toBe(false)
  })

  it('snaps to the right edge on release', async () => {
    const { result, opts } = await mount()
    act(() => result.current.onMouseDown(mouseDown(500, 300)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 990, clientY: 300 })) })
    act(() => { window.dispatchEvent(new MouseEvent('mouseup')) })
    expect(result.current.pos.x).toBe(1000 - PET_W)
    expect(opts.setHideEdge).toHaveBeenLastCalledWith('right')
  })

  it('un-peeks when released away from either edge', async () => {
    const opts = options()
    opts.isPeekingRef.current = true
    const { result } = await mount(opts)
    act(() => result.current.onMouseDown(mouseDown(400, 300)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 500, clientY: 300 })) })
    act(() => { window.dispatchEvent(new MouseEvent('mouseup')) })
    expect(opts.setIsPeeking).toHaveBeenLastCalledWith(false)
    expect(opts.setHideEdge).toHaveBeenLastCalledWith(null)
  })

  it('ends a drag whose mouseup was swallowed, after the stuck timeout', async () => {
    const { result } = await mount()
    act(() => result.current.onMouseDown(mouseDown(400, 300)))
    act(() => { window.dispatchEvent(new MouseEvent('mousemove', { clientX: 500, clientY: 300 })) })
    expect(result.current.dragging.current).toBe(true)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.dragging.current).toBe(false)
    expect(bridge.dragEnd).toHaveBeenCalled()
  })
})

describe('crew-companion/useDrag — cross-display drag from the main process', () => {
  it('follows position updates only while a drag is in flight', async () => {
    const opts = options()
    opts.isPeekingRef.current = true
    const { result } = await mount(opts)
    expect(dragUpdateCb).toBeTypeOf('function')

    act(() => dragUpdateCb?.(700, 600))
    // Not dragging: the update is ignored outright.
    expect(result.current.pos).not.toEqual({ x: 700, y: 600 })

    act(() => result.current.onMouseDown(mouseDown(400, 300)))
    act(() => dragUpdateCb?.(700, 600))
    expect(result.current.pos).toEqual({ x: 700, y: 600 })
    // A peeking pet un-peeks as soon as it is picked up.
    expect(opts.setIsPeeking).toHaveBeenCalledWith(false)
    expect(opts.setHideEdge).toHaveBeenCalledWith(null)
  })

  it('ends the drag, clamps, docks and saves when the main process reports the end', async () => {
    const { result, opts } = await mount()
    act(() => result.current.onMouseDown(mouseDown(400, 300)))
    act(() => dragEndedCb?.(-999, 5000))
    expect(result.current.pos).toEqual({ x: 0, y: 800 - PET_H })
    expect(result.current.dragging.current).toBe(false)
    expect(opts.setHideEdge).toHaveBeenLastCalledWith('left')
    expect(bridge.savePosition).toHaveBeenCalledWith(0, 800 - PET_H)
  })

  it('un-peeks on a main-process end away from the edges', async () => {
    const opts = options()
    opts.isPeekingRef.current = true
    const { result } = await mount(opts)
    act(() => result.current.onMouseDown(mouseDown(400, 300)))
    act(() => dragEndedCb?.(500, 300))
    expect(result.current.pos).toEqual({ x: 500, y: 300 })
    expect(opts.setIsPeeking).toHaveBeenLastCalledWith(false)
  })

  it('unsubscribes from both bridge channels on unmount', async () => {
    const { unmount } = await mount()
    unmount()
    expect(dragUpdateCb).toBeNull()
    expect(dragEndedCb).toBeNull()
  })
})
