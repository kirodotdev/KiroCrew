import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { useWalking } from './useWalking'
import { WALK_MIN_DIST, walkDurationMs, walkTiltFor } from './walkMath'

// Animation frames are driven by hand: each `runFrame(at)` delivers every pending
// rAF callback with the clock set to `at`. No timers are involved.
let clock = 0
let frames = new Map<number, FrameRequestCallback>()
let nextId = 1

beforeEach(() => {
  clock = 0
  frames = new Map()
  nextId = 1
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = nextId++
    frames.set(id, cb)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id)
  })
  vi.spyOn(performance, 'now').mockImplementation(() => clock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function runFrame(at: number) {
  clock = at
  const pending = [...frames.entries()]
  frames = new Map()
  act(() => {
    for (const [, cb] of pending) cb(at)
  })
}

function setup(start = { x: 0, y: 0 }) {
  const onWalkEnd = vi.fn()
  const setIsPeeking = vi.fn()
  const setHideEdge = vi.fn()
  const { result } = renderHook(() => {
    const [pos, setPos] = useState(start)
    const walk = useWalking(pos, setPos, onWalkEnd, setIsPeeking, setHideEdge)
    return { ...walk, pos }
  })
  return { result, onWalkEnd, setIsPeeking, setHideEdge }
}

describe('useWalking', () => {
  it('walkTo animates toward the target and reports the end position once', () => {
    const { result, onWalkEnd, setIsPeeking, setHideEdge } = setup()
    const dur = walkDurationMs(0, 0, 300, 400)

    act(() => result.current.walkTo(300, 400))

    expect(result.current.isWalking).toBe(true)
    expect(result.current.walkDir).toBe(1)
    expect(result.current.walkTilt).toBe(walkTiltFor(0, 0, 300, 400))
    expect(setIsPeeking).toHaveBeenCalledWith(false)
    expect(setHideEdge).toHaveBeenCalledWith(null)

    runFrame(dur / 2)
    expect(result.current.pos).toEqual({ x: 150, y: 200 })
    expect(onWalkEnd).not.toHaveBeenCalled()

    runFrame(dur)
    expect(result.current.pos).toEqual({ x: 300, y: 400 })
    expect(result.current.isWalking).toBe(false)
    expect(onWalkEnd).toHaveBeenCalledTimes(1)
    expect(onWalkEnd).toHaveBeenCalledWith({ x: 300, y: 400 })
  })

  it('faces left when the target lies to the left of the companion', () => {
    const { result } = setup({ x: 500, y: 0 })

    act(() => result.current.walkTo(200, 0))

    expect(result.current.walkDir).toBe(-1)
  })

  it('a move shorter than the skip threshold ends at once without animating', () => {
    const { result, onWalkEnd } = setup()

    act(() => result.current.walkTo(WALK_MIN_DIST / 10, 0))

    expect(result.current.isWalking).toBe(false)
    expect(frames.size).toBe(0)
    expect(onWalkEnd).not.toHaveBeenCalled()
  })

  it('walkPath walks each waypoint in order and reports only the final stop', () => {
    const { result, onWalkEnd } = setup()
    const firstLegEnd = walkDurationMs(0, 0, 100, 0)

    act(() => result.current.walkPath([{ x: 100, y: 0 }, { x: 100, y: 200 }]))
    runFrame(firstLegEnd)

    expect(result.current.pos).toEqual({ x: 100, y: 0 })
    expect(result.current.isWalking).toBe(true)
    expect(onWalkEnd).not.toHaveBeenCalled()

    runFrame(firstLegEnd + walkDurationMs(100, 0, 100, 200))

    expect(result.current.pos).toEqual({ x: 100, y: 200 })
    expect(result.current.isWalking).toBe(false)
    expect(onWalkEnd).toHaveBeenCalledTimes(1)
    expect(onWalkEnd).toHaveBeenCalledWith({ x: 100, y: 200 })
  })

  it('walkPath with no waypoints does nothing', () => {
    const { result, onWalkEnd } = setup()

    act(() => result.current.walkPath([]))

    expect(result.current.isWalking).toBe(false)
    expect(frames.size).toBe(0)
    expect(onWalkEnd).not.toHaveBeenCalled()
  })

  it('cancelWalk stops a walk in progress and drops the rest of its path', () => {
    const { result, onWalkEnd } = setup()
    const firstLegEnd = walkDurationMs(0, 0, 100, 0)

    // A two-leg path is cancelled half way through its first leg.
    act(() => result.current.walkPath([{ x: 100, y: 0 }, { x: 100, y: 200 }]))
    runFrame(firstLegEnd / 2)
    act(() => result.current.cancelWalk())

    expect(result.current.isWalking).toBe(false)
    expect(frames.size).toBe(0)

    // A new walk from the stop point must end at its own target. A stale queue
    // would carry the companion on to (100, 200) and never report (300, 400).
    const stopX = result.current.pos.x
    const nextDur = walkDurationMs(stopX, 0, 300, 400)
    act(() => result.current.walkTo(300, 400))
    runFrame(firstLegEnd / 2 + nextDur)

    expect(result.current.isWalking).toBe(false)
    expect(onWalkEnd).toHaveBeenCalledTimes(1)
    expect(onWalkEnd).toHaveBeenCalledWith({ x: 300, y: 400 })
  })
})
