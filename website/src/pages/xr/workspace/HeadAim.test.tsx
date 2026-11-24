import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Quaternion, Vector3 } from 'three'

// The framework is stood in for: the dot's own code runs as it is, and the test tells it the frames.
const stage = vi.hoisted(() => ({ frame: null as (() => void) | null, xr: { getViewerPose: () => ({}) } as { getViewerPose: () => object | null } | undefined, session: {} as object | null, camera: { p: [0, 1.6, 0] as [number, number, number], q: [0, 0, 0, 1] as [number, number, number, number] }, pointer: { down: vi.fn(), up: vi.fn(), cancel: vi.fn(), getEnabled: () => true, setEnabled: vi.fn(), getIntersection: () => undefined } }))

vi.mock('@react-three/fiber', () => ({
  // A frame of the session: the framework hands the dot's code the frame, which says whether the session has his head's pose.
  useFrame: (cb: (s: unknown, dt: number, frame?: unknown) => void) => { stage.frame = () => cb({}, 0, stage.xr) },
  useThree: (pick: (s: unknown) => unknown) => pick({ gl: { xr: { getReferenceSpace: () => ({}) } }, camera: { getWorldPosition: (v: Vector3) => v.set(...stage.camera.p), getWorldQuaternion: (q: Quaternion) => q.set(...stage.camera.q) } }),
}))
vi.mock('@react-three/xr', () => ({
  useRayPointer: () => stage.pointer,
  useXR: (pick: (s: unknown) => unknown) => pick({ session: stage.session }),
}))

import { HeadAim, headPress } from './HeadAim'
import { PINCH, pinchRoute } from './pinchRoute'

/** How far the dot's ray is turned from straight ahead, in radians. */
const turned = (q: Quaternion) => new Vector3(0, 0, -1).applyQuaternion(q).angleTo(new Vector3(0, 0, -1))

describe('the held dot', () => {
  let ray: { position: Vector3; quaternion: Quaternion }
  beforeEach(() => {
    // The test's document has no scene: the two groups of the dot are given what a group of the room has.
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { container } = render(<HeadAim />)
    const [space, dot] = [...container.querySelectorAll('group')] as unknown as Record<string, unknown>[]
    Object.assign(space, { position: new Vector3(), quaternion: new Quaternion(), updateMatrixWorld: () => undefined })
    Object.assign(dot, { position: new Vector3(), quaternion: new Quaternion(), scale: new Vector3(), visible: false })
    ray = space as unknown as typeof ray
    pinchRoute.owner = null
    pinchRoute.quietUntil = 0
  })
  afterEach(() => { if (pinchRoute.owner) pinchRoute.lost(pinchRoute.owner.hand); vi.restoreAllMocks() })

  it('turns by the hand\'s travel while the pinch is held, and stays where the fingers opened', () => {
    stage.frame?.()
    pinchRoute.move('right', 0, [0, 1, 0])
    expect(pinchRoute.start('right', 0, [0, 1, 0], 'page:a')).toBe('dot')
    headPress.down()
    // The hand goes 10 cm down: the ray turns down by half a radian a metre past the still bound (a page scrolls).
    pinchRoute.move('right', 300, [0, 0.9, 0])
    stage.frame?.()
    const atOpening = turned(ray.quaternion)
    expect(atOpening).toBeCloseTo(PINCH.turnPerM * (0.1 - PINCH.stillM), 6)
    // The fingers open; in the 70 ms until the page lets the pinch go the hand drops 5 cm more.
    pinchRoute.opened('right', 300, [0, 0.9, 0])
    pinchRoute.move('right', 335, [0, 0.875, 0])
    stage.frame?.()
    pinchRoute.move('right', 370, [0, 0.85, 0])
    stage.frame?.()
    expect(turned(ray.quaternion)).toBeCloseTo(atOpening, 9)
    // The same pinch, back after a dropped frame, drags on.
    pinchRoute.reclosed('right')
    stage.frame?.()
    expect(turned(ray.quaternion)).toBeCloseTo(PINCH.turnPerM * (0.15 - PINCH.stillM), 6)
  })
})

describe('the dot when a session begins (P21i): on only once its ray holds a pose the session gave', () => {
  const flat = new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), 0.3)
  const head = new Quaternion().setFromAxisAngle(new Vector3(1, 0, 0), (-34 * Math.PI) / 180)
  const cam = (q: Quaternion, p: [number, number, number] = [0, 1.6, 0]) => { stage.camera = { p, q: [q.x, q.y, q.z, q.w] } }
  /** The dot drawn, with a pointer that says whether it is on; `turned`: each time it was turned on or off, and where the ray stood then. */
  const drawn = () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const view = render(<HeadAim />)
    const [space, dot] = [...view.container.querySelectorAll('group')] as unknown as Record<string, unknown>[]
    Object.assign(space, { position: new Vector3(), quaternion: new Quaternion(), updateMatrixWorld: () => undefined })
    Object.assign(dot, { position: new Vector3(), quaternion: new Quaternion(), scale: new Vector3(), visible: false })
    const ray = space as unknown as { position: Vector3; quaternion: Quaternion }
    const state = { on: false }
    const turned: { on: boolean; ray: Quaternion }[] = []
    stage.pointer.getEnabled = () => state.on
    stage.pointer.setEnabled.mockImplementation((on: boolean) => { state.on = on; turned.push({ on, ray: ray.quaternion.clone() }) })
    return { ray, state, turned, view }
  }
  beforeEach(() => { pinchRoute.owner = null; pinchRoute.quietUntil = 0; pinchRoute.tipNear = false; stage.session = {}; stage.xr = { getViewerPose: () => ({}) }; cam(flat, [0.3, 1.2, 2]) })
  afterEach(() => { stage.session = {}; stage.xr = { getViewerPose: () => ({}) }; stage.camera = { p: [0, 1.6, 0], q: [0, 0, 0, 1] }; stage.pointer.getEnabled = () => true; stage.pointer.setEnabled.mockReset(); pinchRoute.hands.right.raised = false; pinchRoute.hands.right.onPanel = false; vi.restoreAllMocks() })

  it('must not send: in the frames in which its ray holds the flat preview\'s pose the pointer is off; it comes on a frame after the ray took his head\'s', () => {
    const { ray, state, turned } = drawn()
    // Frame 1 of the session: the camera is still the flat preview's (the renderer gives it his head's pose as it draws).
    stage.frame?.()
    expect(state.on).toBe(false)
    expect(ray.quaternion.angleTo(flat)).toBeCloseTo(0, 6)
    // Frame 2: the camera holds his head's pose; the ray takes it. The library moved the pointer BEFORE this, from the
    // flat pose: it is still off, so that move told nothing.
    cam(head)
    stage.frame?.()
    expect(state.on).toBe(false)
    expect(ray.quaternion.angleTo(head)).toBeCloseTo(0, 6)
    // Frame 3: the library moved it from his head's pose; now it is turned on, and what it lies on is told of him.
    stage.frame?.()
    expect(state.on).toBe(true)
    // Whenever it was turned on, the ray stood at his head's pose already, a frame before.
    expect(turned.filter(t => t.on).length).toBe(1)
    expect(turned.filter(t => t.on)[0].ray.angleTo(head)).toBeCloseTo(0, 6)
  })

  it('must not send, though the library turns every pointer on when a session shows: until its ray holds his head\'s pose the ray has no pose', () => {
    stage.session = null
    const { ray, view } = drawn()
    const ready = () => (ray as unknown as { transformReady?: boolean }).transformReady
    // The flat preview: no pose of his, in every frame.
    stage.frame?.()
    expect(ready()).toBe(false)
    // The session begins. The library's own switch turns the pointer on and moves it before each frame below:
    // that move finds a ray with no pose, and lies on nothing.
    stage.session = {}
    view.rerender(<HeadAim />)
    stage.frame?.()
    expect(ready()).toBe(false)
    // The camera holds his head's pose; the ray takes it, and has a pose from here on (a head held still too).
    cam(head)
    stage.frame?.()
    expect(ready()).toBe(true)
    expect(ray.quaternion.angleTo(head)).toBeCloseTo(0, 6)
    stage.frame?.()
    expect(ready()).toBe(true)
    // The session ends: none again.
    stage.session = null
    view.rerender(<HeadAim />)
    stage.frame?.()
    expect(ready()).toBe(false)
  })

  it('must not lose: a head held still is a pose from the session too, and the dot is on at the third frame', () => {
    const { state } = drawn()
    // The session's camera never moves, and stands as the flat one stood.
    for (let i = 0; i < 2; i += 1) { stage.frame?.(); expect(state.on).toBe(false) }
    stage.frame?.()
    expect(state.on).toBe(true)
  })

  it('waits while the session has no pose of his head yet, and while no frame of the session comes (the page hidden)', () => {
    const { state } = drawn()
    stage.xr = { getViewerPose: () => null }
    for (let i = 0; i < 5; i += 1) stage.frame?.()
    expect(state.on).toBe(false)
    // A frame that is not the session's (the window's own, while the session's are held back).
    stage.xr = undefined
    for (let i = 0; i < 5; i += 1) stage.frame?.()
    expect(state.on).toBe(false)
    stage.xr = { getViewerPose: () => ({}) }
    cam(head)
    stage.frame?.(); stage.frame?.()
    expect(state.on).toBe(false)
    stage.frame?.()
    expect(state.on).toBe(true)
  })

  it('a hand\'s ray has the light at the start and gives it back: the dot is on in that very frame', () => {
    const { state } = drawn()
    pinchRoute.hands.right.raised = true; pinchRoute.hands.right.onPanel = true
    cam(head)
    for (let i = 0; i < 6; i += 1) stage.frame?.()
    expect(state.on).toBe(false)
    pinchRoute.hands.right.onPanel = false
    stage.frame?.()
    expect(state.on).toBe(true)
  })

  it('a session that ends and starts again waits anew; with no session (the flat preview) the dot is never on', () => {
    const { state, turned, view } = drawn()
    cam(head)
    for (let i = 0; i < 3; i += 1) stage.frame?.()
    expect(state.on).toBe(true)
    stage.session = null
    view.rerender(<HeadAim />)
    cam(flat, [0.3, 1.2, 2])
    for (let i = 0; i < 4; i += 1) stage.frame?.()
    expect(state.on).toBe(false)
    const before = turned.length
    stage.session = {}
    view.rerender(<HeadAim />)
    stage.frame?.()
    cam(head)
    stage.frame?.()
    expect(state.on).toBe(false)
    stage.frame?.()
    expect(state.on).toBe(true)
    const again = turned.slice(before).filter(t => t.on)
    expect(again.length).toBe(1)
    expect(again[0].ray.angleTo(head)).toBeCloseTo(0, 6)
  })
})

describe('the dot\'s own frames, as the room\'s rules count them (M171)', () => {
  it('says each frame after it has turned the pointer on or off, and two frames since a time count as "on again"', () => {
    const order: string[] = []
    stage.pointer.setEnabled.mockImplementation(() => { order.push('set') })
    const real = pinchRoute.dotFrame.bind(pinchRoute)
    vi.spyOn(pinchRoute, 'dotFrame').mockImplementation((at: number) => { order.push('said'); real(at) })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { container } = render(<HeadAim />)
    const [space, dot] = [...container.querySelectorAll('group')] as unknown as Record<string, unknown>[]
    Object.assign(space, { position: new Vector3(), quaternion: new Quaternion(), updateMatrixWorld: () => undefined })
    Object.assign(dot, { position: new Vector3(), quaternion: new Quaternion(), scale: new Vector3(), visible: false })
    let now = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    // The pointer is on (the stand-in says so) and the dot is not the light's: the frame turns it off, then says.
    pinchRoute.quietUntil = 2000
    stage.frame?.()
    expect(order).toEqual(['set', 'said'])
    // One frame after the quiet end is not yet "on again"; the second is.
    now = 2150
    stage.frame?.()
    expect(pinchRoute.dotSeenSince(2000)).toBe(false)
    now = 2161
    stage.frame?.()
    expect(pinchRoute.dotSeenSince(2000)).toBe(true)
    expect(pinchRoute.dotSeenSince(2155)).toBe(false)
    pinchRoute.quietUntil = 0
    vi.restoreAllMocks()
  })
})
