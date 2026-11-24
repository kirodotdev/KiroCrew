import { render } from '@testing-library/react'
import { forwardRef, useImperativeHandle, type ComponentType } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PAGES } from './layout'

// The framework is stood in for: the hand's own code runs as it is, told of the session's events and frames by the test.
const stage = vi.hoisted(() => ({
  hand: [0, 1, 0] as [number, number, number],
  frames: [] as { cb: (s: unknown, dt: number) => void; priority: number }[],
  hit: undefined as unknown,
  ray: { down: null as unknown, up: null as unknown, cancel: null as unknown },
  on: {} as Record<string, () => void>,
  dot: { down: vi.fn(), up: vi.fn(), cancel: vi.fn() },
}))

vi.mock('@react-three/fiber', () => ({
  useFrame: (cb: (s: unknown, dt: number) => void, priority = 0) => { stage.frames.push({ cb, priority }) },
  useThree: (pick: (s: unknown) => unknown) => pick({ camera: { getWorldPosition: (v: { set: (x: number, y: number, z: number) => void }) => v.set(0, 1.6, 0) } }),
}))
vi.mock('@react-three/xr', () => ({
  XRHandModel: () => null,
  XRSpace: forwardRef((_props: unknown, ref) => {
    useImperativeHandle(ref, () => ({
      transformReady: true,
      get matrix() { return new Matrix4().setPosition(...stage.hand) },
      getWorldPosition: (v: { set: (x: number, y: number, z: number) => void }) => v.set(...stage.hand),
    }))
    return null
  }),
  createXRStore: (options: unknown) => options,
  useRayPointer: () => ({ down: stage.ray.down, up: stage.ray.up, cancel: stage.ray.cancel, getEnabled: () => false, setEnabled: vi.fn(), getIntersection: () => stage.hit }),
  useXR: (pick: (s: unknown) => unknown) => pick({ mode: 'immersive-ar', originReferenceSpace: null }),
  useXRInputSourceEvent: (_source: unknown, name: string, fn: () => void) => { stage.on[name] = fn },
  useXRInputSourceStateContext: () => ({ inputSource: { handedness: 'right' } }),
}))
vi.mock('../ModelGuard', () => ({ ModelGuard: ({ children }: { children: unknown }) => children }))
vi.mock('./HeadAim', () => ({ headPress: stage.dot }))

import { Matrix4, Vector3 } from 'three'
import { makePagesStore } from './hands'
import { pinchRoute } from './pinchRoute'

/** The hand's ray and pinch, as the session's store is given it. */
const Aim = (makePagesStore() as unknown as { controller: ComponentType }).controller

describe('a hand\'s pinch that the device sends', () => {
  let now = 0
  /** One frame of the session at `ms`, with the hand at `x` metres to the right of where it began. */
  const frame = (ms: number, x = 0, y = 1) => {
    now = ms
    stage.hand = [x, y, 0]
    // Each render subscribes anew: the last render's callbacks, in the order of their priority.
    const last = stage.frames.slice(-2)
    for (const f of [...last].sort((a, b) => a.priority - b.priority)) f.cb({}, 0)
  }
  /** The hand's part, with the ray's own group given what a group of the room has. */
  const mount = () => {
    stage.frames.length = 0
    const { container } = render(<Aim />)
    const group = container.querySelector('group') as unknown as { matrix: Matrix4; transformReady?: boolean }
    group.matrix = new Matrix4()
    return { rayX: () => new Vector3().setFromMatrixPosition(group.matrix).x }
  }
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    stage.hit = undefined
    stage.ray = { down: vi.fn(), up: vi.fn(), cancel: vi.fn() }
    pinchRoute.hands.right.select = false
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    pinchRoute.owner = null
    pinchRoute.quietUntil = 0
    pinchRoute.veil = null
    Object.values(stage.dot).forEach(f => f.mockClear())
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('is a click when the hand moves 21 mm in the time between the fingers\' opening and the gate\'s release', () => {
    expect(PAGES.pinchOpenMs).toBe(70)
    mount()
    frame(1000)
    stage.on.selectstart()
    expect(stage.dot.down).toHaveBeenCalledTimes(1)
    frame(1100)
    frame(1200)
    // The fingers open with the hand where it was; the page lets the pinch go once they stayed open for 70 ms.
    stage.on.select()
    stage.on.selectend()
    frame(1235, 0.0105)
    expect(stage.dot.up).not.toHaveBeenCalled()
    expect(pinchRoute.travel()).toBe(0)
    frame(1270, 0.021)
    expect(stage.dot.up).toHaveBeenCalledTimes(1)
    expect(stage.dot.up).toHaveBeenCalledWith('click')
    expect(pinchRoute.probe.last).toMatchObject({ hand: 'right', kind: 'click', ms: 200 })
    expect(pinchRoute.released).toMatchObject({ place: [0, 1, 0], travelM: 0, mps: 0 })
  })

  it('ends a pinch that stayed open at its own time and place when the next begins before a frame let it go (D55 6, small 1)', () => {
    mount()
    let atUp: unknown = null
    stage.dot.up.mockImplementation(() => { atUp = { ...pinchRoute.released, ms: pinchRoute.probe.last?.ms } })
    frame(3000)
    stage.on.selectstart()
    frame(3100)
    // The fingers open at 3,120 ms with the hand still; no frame runs before the next pinch, 80 ms later, with the
    // hand 2 cm on. The first pinch ends where and when its fingers opened: a click of 120 ms, not a drag of 200.
    now = 3120
    stage.on.select()
    stage.on.selectend()
    stage.hand = [0.02, 1, 0]
    now = 3200
    stage.on.selectstart()
    expect(stage.dot.up).toHaveBeenCalledTimes(1)
    expect(stage.dot.up).toHaveBeenCalledWith('click')
    expect(atUp).toMatchObject({ place: [0, 1, 0], travelM: 0, ms: 120 })
    // And the new pinch is its own: a second down.
    expect(stage.dot.down).toHaveBeenCalledTimes(2)
  })

  it('goes on when the tracker drops it for a frame: the hand\'s way after that counts', () => {
    mount()
    frame(2000)
    stage.on.selectstart()
    frame(2100)
    stage.on.selectend()
    frame(2116)
    stage.on.selectstart()
    frame(2300, 0.03)
    expect(pinchRoute.travel()).toBeCloseTo(0.03, 6)
    stage.on.select()
    stage.on.selectend()
    frame(2370, 0.03)
    expect(stage.dot.up).toHaveBeenCalledWith('drag')
  })

  it('holds a raised hand\'s ray where the fingers opened until the pinch is let go, so what it moves lands there', () => {
    const { rayX } = mount()
    // The hand is raised (20 cm under the eyes) and its ray lies on a thing of the room: its pinch presses its own ray.
    stage.hit = { distance: 1, point: new Vector3(0, 1.4, -1), object: { userData: {}, name: 'thing', parent: null } }
    frame(3000, 0, 1.4)
    stage.on.selectstart()
    expect(stage.ray.down).toHaveBeenCalledTimes(1)
    frame(3100, 0.1, 1.4)
    expect(rayX()).toBeCloseTo(0.1, 9)
    // The fingers open at 10 cm; the hand goes on to 14 cm in the 70 ms until the page lets the pinch go.
    stage.on.select()
    stage.on.selectend()
    frame(3135, 0.12, 1.4)
    expect(rayX()).toBeCloseTo(0.1, 9)
    expect(stage.ray.up).not.toHaveBeenCalled()
    frame(3170, 0.14, 1.4)
    // The release is handed on in this frame, with the ray still where the fingers opened.
    expect(stage.ray.up).toHaveBeenCalledTimes(1)
    expect(rayX()).toBeCloseTo(0.1, 9)
    // After it the ray is the hand's again.
    frame(3190, 0.15, 1.4)
    expect(rayX()).toBeCloseTo(0.15, 9)
  })

  it('presses nothing with a raised hand\'s ray on a point that the readout\'s plate hides, and presses a thing beside it', () => {
    mount()
    // The plate takes presses and hides what the eye sees to the left of straight ahead (a stand-in for its shape).
    pinchRoute.veil = point => point[0] < 0
    const thing = { userData: {}, name: 'thing', parent: null }
    stage.hit = { distance: 0.5, point: new Vector3(-0.1, 1.4, -0.5), object: thing }
    frame(5000, 0, 1.4)
    expect(pinchRoute.hands.right.point).toEqual([-0.1, 1.4, -0.5])
    stage.on.selectstart()
    stage.on.select()
    stage.on.selectend()
    frame(5200, 0, 1.4)
    expect(stage.ray.down).not.toHaveBeenCalled()
    expect(stage.ray.up).not.toHaveBeenCalled()
    expect(stage.dot.down).not.toHaveBeenCalled()
    expect(pinchRoute.probe.last).toMatchObject({ hand: 'right', to: 'right', down: 'hidden', kind: 'cancel' })
    // The same ray on a thing beside the plate: the pinch presses it.
    stage.hit = { distance: 0.5, point: new Vector3(0.1, 1.4, -0.5), object: thing }
    frame(6000, 0, 1.4)
    stage.on.selectstart()
    expect(stage.ray.down).toHaveBeenCalledTimes(1)
    stage.on.select()
    stage.on.selectend()
    frame(6200, 0, 1.4)
    expect(stage.ray.up).toHaveBeenCalledTimes(1)
    // A ray on nothing keeps no point.
    stage.hit = undefined
    frame(7000, 0, 1.4)
    expect(pinchRoute.hands.right.point).toBeNull()
  })

  it('after the quiet time (P21j): a pinch before the raised hand\'s first frame waits for it and goes down on its ray, never on the dot', () => {
    mount()
    const thing = { userData: {}, name: 'key', parent: null }
    stage.hit = { distance: 0.5, point: new Vector3(0.1, 1.4, -0.5), object: thing }
    frame(900, 0, 1.4)
    pinchRoute.hush(1000, 900, false)
    // A frame in the quiet time: the ray is off, the book says "on no panel".
    frame(1500, 0, 1.4)
    expect(pinchRoute.hands.right).toMatchObject({ raised: true, onPanel: false })
    // The quiet time is over; no frame has come. The device sends the pinch.
    now = 1901
    stage.on.selectstart()
    expect(stage.dot.down).not.toHaveBeenCalled()
    expect(stage.ray.down).not.toHaveBeenCalled()
    // The hand's frame: its book is read, and the pinch goes down on its ray, once.
    frame(1930, 0, 1.4)
    expect(pinchRoute.hands.right).toMatchObject({ onPanel: true, readAt: 1930 })
    expect(stage.ray.down).toHaveBeenCalledTimes(1)
    frame(1960, 0, 1.4)
    expect(stage.ray.down).toHaveBeenCalledTimes(1)
    stage.on.select()
    stage.on.selectend()
    frame(2100, 0, 1.4)
    expect(stage.ray.up).toHaveBeenCalledTimes(1)
    expect(stage.dot.down).not.toHaveBeenCalled()
    expect(stage.dot.up).not.toHaveBeenCalled()
    expect(pinchRoute.probe.last).toMatchObject({ hand: 'right', to: 'right', kind: 'click' })
  })
})
