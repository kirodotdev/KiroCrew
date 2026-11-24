import { act, render } from '@testing-library/react'
import { CombinedPointer, createRayPointer, type Pointer } from '@pmndrs/pointer-events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Mesh, MeshBasicMaterial, Object3D, PerspectiveCamera, PlaneGeometry, Scene, Vector3 } from 'three'
import type { XrBot } from '../model'

// The Crew's own code runs as it is. The frame loop, the Bots' art and the signs' canvases are stood in for, and its
// box's handlers are handed to a real mesh that the real pointer library (`@pmndrs/pointer-events`, the one the
// session's dot and rays are made of) presses, as `@react-three/fiber` hands them (`__r3f.handlers`, `event.js:228`).
vi.mock('@react-three/fiber', () => ({ useFrame: () => undefined }))
vi.mock('../sceneParts', () => ({ loadFace: () => new Promise(() => undefined) }))
vi.mock('../character', async importOriginal => ({ ...(await importOriginal<object>()), tintOf: () => new Promise(() => undefined) }))
vi.mock('../faces', () => ({ faceArtFor: () => ({ layers: [] }) }))
vi.mock('../ModelGuard', () => ({ ModelGuard: ({ children }: { children: unknown }) => children }))
vi.mock('./BotNote', () => ({ BotNote: () => null }))
vi.mock('./PlaceDesk', () => ({ PlaceDesks: () => null }))
vi.mock('./SinceLight', () => ({ SincePool: () => null }))
// The session's store, as far as the Crew and the pointers read it: its `visibilityState`. The real
// `setupSyncIsVisible` (`@pmndrs/xr`, what `@react-three/xr`'s `RootCombinedPointer` runs) reads the same store and
// turns the pointers off and on; the Crew hears the same store (`useXRStore`), as it does under `<XR store>`.
const xr = vi.hoisted(() => {
  type State = { visibilityState?: string }
  let state: State = { visibilityState: 'visible' }
  const subs = new Set<(s: State, prev: State) => void>()
  return {
    getState: () => state,
    subscribe: (fn: (s: State, prev: State) => void) => { subs.add(fn); return () => { subs.delete(fn) } },
    set: (visibilityState: string | undefined) => { const prev = state; state = { visibilityState }; for (const fn of [...subs]) fn(state, prev) },
    reset: () => { subs.clear(); state = { visibilityState: 'visible' } },
  }
})
vi.mock('@react-three/xr', async () => {
  const { useSyncExternalStore } = await import('react')
  return { useXR: <T,>(pick: (s: { visibilityState?: string }) => T) => useSyncExternalStore(xr.subscribe, () => pick(xr.getState())), useXRStore: () => xr }
})

import { setupSyncIsVisible } from '@pmndrs/xr'
import { Crew, type CrewProbe } from './Crew'
import type { EmptyHand } from './RoomWalls'

const BOT = { slug: 'mira', name: 'mira', label: 'Mira', avatar: null, state: 'idle', line: '', things: [], needs: [], thread: '' } as unknown as XrBot
const COLORS = { state: { idle: 0x888888, working: 0x44aa44, needs: 0xffaa00, failed: 0xff0000, done: 0x4488ff } } as never
// The screen place stays one: a ray's events carry what they are given, and a press that the preview sees travel is a drag.
const at = () => ({ button: 0, timeStamp: performance.now(), clientX: 0, clientY: 0 }) as never

/** React keeps a host element's props on the element itself, under a key of its own. */
const propsOf = (el: Element): Record<string, unknown> => {
  const key = Object.keys(el).find(k => k.startsWith('__reactProps$'))
  return key ? ((el as unknown as Record<string, Record<string, unknown>>)[key] ?? {}) : {}
}

interface Ray { pointer: Pointer; aim: (x: number) => void }
interface Stage extends Ray {
  read: () => CrewProbe | undefined; onHold: ReturnType<typeof vi.fn>; onPlace: ReturnType<typeof vi.fn>; box: Mesh; ray: (type: string) => Ray; combined: CombinedPointer
  /** The room's quiet time begins (or ends): the Crew is drawn again with `hushed`. */
  hush: (on: boolean) => void
  /** The head's ray moves on the box with the screen place `px` to the side (the preview reads a drag by it). */
  slide: (px: number) => void
}

/** The Crew of one Bot, with the `drag` that a session gives (`EmptySpace.tsx:53`: `undefined`) or the preview's. */
function stage(drag: EmptyHand | undefined, withHold: boolean): Stage {
  let probe: (() => CrewProbe[]) | null = null
  const onHold = vi.fn(() => true)
  const onPlace = vi.fn()
  const crew = (hushed: boolean) => (
    <Crew bots={[BOT]} head={new Vector3(0, 1.6, 0)} colors={COLORS} still hushed={hushed} known onTap={() => undefined} drag={drag} report={read => { probe = read }} onPlace={onPlace} {...(withHold ? { onHold } : {})} />
  )
  const { container, rerender } = render(crew(false))
  const el = [...container.querySelectorAll('mesh')].find(m => (propsOf(m).userData as { xrPress?: string } | undefined)?.xrPress === 'bot')
  if (!el) throw new Error('no Bot box was drawn')
  const scene = new Scene()
  const box = new Mesh(new PlaneGeometry(0.2, 0.2), new MeshBasicMaterial())
  box.position.set(0, 0, -1)
  // The box's handlers as they are at each event (a render hands new ones).
  Object.defineProperty(box, '__r3f', { get: () => { const handlers = propsOf(el); return { handlers, eventCount: Object.keys(handlers).filter(k => /^onPointer|^onWheel/.test(k)).length } } })
  scene.add(box)
  const cam = new PerspectiveCamera()
  // The session's pointers sit in one combined pointer (`@react-three/xr` `RootCombinedPointer`, `xr.js:64-66`).
  const combined = new CombinedPointer(true)
  const ray = (type: string) => {
    const space = new Object3D()
    scene.add(space)
    const pointer = createRayPointer(() => cam, { current: space }, {}, {}, type)
    combined.register(pointer)
    const aim = (x: number) => act(() => { space.lookAt(-x, 0, 1); scene.updateMatrixWorld(true); pointer.move(scene, at()) })
    return { pointer, aim }
  }
  const { pointer, aim } = ray('head')
  // Only a session's pointers sit under the store's visibility; the preview's screen pointer does not (`forward.js`).
  if (drag === undefined) setupSyncIsVisible(xr as never, visible => combined.setEnabled(visible, at()))
  const hush = (on: boolean) => act(() => { rerender(crew(on)) })
  const slide = (px: number) => act(() => { pointer.move(scene, { ...(at() as object), clientX: px } as never) })
  return { pointer, aim, read: () => probe?.().find(b => b.slug === 'mira'), onHold, onPlace, box, ray, combined, hush, slide }
}

describe('a session press on a Bot\'s box, through the Crew and the real pointer library (M202)', () => {
  beforeEach(() => {
    xr.reset()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    // The signs draw on a canvas: the test's document has none, so it is given a 2D context that takes every call.
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    // A group of the room is a host element here: the probe reads its place and its children by name.
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).position
  })

  it('down on the box, the ray leaves, the up off it: the press ends at the leave, the light goes out, no tap', () => {
    const s = stage(undefined, true)
    s.aim(0)
    expect(s.read()?.lit).toBe(true)
    act(() => s.pointer.down(at()))
    expect(s.read()?.pressing).toMatchObject({ type: 'head', held: false, holding: false, lifted: false })
    s.aim(0.9)
    expect(s.read()?.pressing).toBeNull()
    expect(s.read()?.lit).toBe(false)
    act(() => s.pointer.up(at()))
    expect(s.read()?.taps).toBe(0)
    expect(s.onHold).not.toHaveBeenCalled()
  })

  it('down, held 500 ms (a hold: the box takes the pointer), the ray leaves, the up: the hold is let go at the box, then nothing stays', () => {
    const s = stage(undefined, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => { vi.advanceTimersByTime(500) })
    expect(s.onHold).toHaveBeenLastCalledWith('mira', 'start')
    expect(s.read()?.pressing).toMatchObject({ holding: true, held: true })
    s.aim(0.9)
    // The box holds the pointer: no leave comes (`pointer.js:103-106`), and the hold goes on.
    expect(s.read()?.pressing).toMatchObject({ holding: true, held: true })
    act(() => s.pointer.up(at()))
    expect(s.onHold).toHaveBeenLastCalledWith('mira', 'let')
    expect(s.read()?.pressing).toBeNull()
    s.aim(0.9)
    expect(s.read()?.lit).toBe(false)
    expect(s.read()?.taps).toBe(0)
  })

  it('the same tap with the ray kept on the box is a tap (the other side)', () => {
    const s = stage(undefined, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => s.pointer.up(at()))
    expect(s.read()?.taps).toBe(1)
    expect(s.read()?.pressing).toBeNull()
  })

  it('a second pointer that leaves the box ends nothing of the first one\'s press', () => {
    const s = stage(undefined, true)
    const hand = s.ray('ray')
    s.aim(0)
    hand.aim(0)
    act(() => s.pointer.down(at()))
    hand.aim(0.9)
    expect(s.read()?.pressing).toMatchObject({ type: 'head', holding: false })
    act(() => s.pointer.up(at()))
    expect(s.read()?.taps).toBe(1)
  })

  it('a lifted Bot (no hold in this room): the box takes the pointer at the lift, a leave cannot drop it, the up puts it down', () => {
    const s = stage(undefined, false)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => { vi.advanceTimersByTime(500) })
    expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true, held: true } })
    s.aim(0.9)
    expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true, held: true } })
    act(() => s.pointer.up(at()))
    expect(s.read()).toMatchObject({ carried: false, pressing: null })
  })

  it('the flat preview: `drag` is given, so the box takes the pointer at the down', () => {
    const hand = { onPointerDown: vi.fn(), onPointerMove: vi.fn(), onPointerUp: vi.fn(), onPointerCancel: vi.fn(), onWheel: vi.fn() } as unknown as EmptyHand
    const s = stage(hand, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    expect(s.read()?.pressing).toMatchObject({ held: true })
    s.aim(0.9)
    expect(s.read()?.pressing).toMatchObject({ held: true })
    act(() => s.pointer.up(at()))
    expect(s.read()?.taps).toBe(1)
  })
})

// D185: when the session stops being visible while a Bot's press is under way, the Crew does what its quiet time does.
// The store goes `visible-blurred` or `hidden`; the real `setupSyncIsVisible` turns the pointers off (the box gets a
// leave, the release never comes) and the Crew reads the same store.
describe('a press on a Bot when the session stops being visible (D185)', () => {
  beforeEach(() => {
    xr.reset()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).position
  })
  const see = (state: string) => act(() => xr.set(state))
  /** Down on the box and held 500 ms: a hold where the room has `onHold`, a lift where it has none. */
  const held = (s: Stage) => { s.aim(0); act(() => s.pointer.down(at())); act(() => { vi.advanceTimersByTime(500) }) }

  for (const away of ['visible-blurred', 'hidden']) {
    it(`must end: a hold, the session ${away}, his release, visible again: no press, no light, told cancel once; then a tap is a tap`, () => {
      const s = stage(undefined, true)
      held(s)
      expect(s.read()?.pressing).toMatchObject({ holding: true, held: true })
      see(away)
      act(() => s.pointer.up(at()))
      see('visible')
      s.aim(0.9)
      expect(s.read()).toMatchObject({ pressing: null, lit: false })
      expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'cancel'])
      s.aim(0)
      act(() => s.pointer.down(at()))
      act(() => s.pointer.up(at()))
      expect(s.read()?.taps).toBe(1)
      expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'cancel'])
    })

    it(`must end: a carried Bot, the session ${away}, his release, visible again: it is put back, not lit, no press; a tap is a tap, a long press lifts it`, () => {
      const s = stage(undefined, false)
      held(s)
      expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true, held: true } })
      see(away)
      act(() => s.pointer.up(at()))
      see('visible')
      s.aim(0.9)
      expect(s.read()).toMatchObject({ carried: false, pressing: null, lit: false })
      // Back to its own place: no place of the row is kept for it (`putDown(slug, false)`).
      expect(s.onPlace).not.toHaveBeenCalled()
      s.aim(0)
      act(() => s.pointer.down(at()))
      act(() => s.pointer.up(at()))
      expect(s.read()).toMatchObject({ carried: false, taps: 1 })
      held(s)
      expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true } })
    })
  }

  it('must end: the session ends while he carries a Bot (the store reads nothing): it is put back, no press, no light', () => {
    const s = stage(undefined, false)
    held(s)
    expect(s.read()?.carried).toBe(true)
    act(() => xr.set(undefined))
    act(() => s.pointer.cancel(at()))
    expect(s.read()).toMatchObject({ carried: false, pressing: null, lit: false })
    expect(s.onPlace).not.toHaveBeenCalled()
  })

  it('the probe keeps the box\'s last events and the session\'s visibility changes (DASH-15 C)', () => {
    const s = stage(undefined, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    see('visible-blurred')
    see('visible')
    const r = s.read()
    expect(r?.boxEvents?.map(x => `${x.type}:${x.pointer}`)).toEqual(['pointerenter:head', 'pointerdown:head', 'pointerleave:head', 'pointerenter:head'])
    expect(r?.visibility?.map(x => x.state)).toEqual(['visible', 'visible-blurred', 'visible'])
  })

  it('must end: a plain press, the session hidden: it ends and its light goes out, as before', () => {
    const s = stage(undefined, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    see('hidden')
    expect(s.read()).toMatchObject({ pressing: null, lit: false })
  })

  it('must not end: a carry whose ray leaves the box while the session stays visible goes on', () => {
    const s = stage(undefined, false)
    held(s)
    s.aim(0.9)
    expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true, held: true } })
    act(() => s.pointer.up(at()))
    expect(s.read()).toMatchObject({ carried: false, pressing: null })
    // His release puts it down where he let go: the row keeps that place.
    expect(s.onPlace).toHaveBeenCalledTimes(1)
  })

  it('must not end: a hold whose ray drifts off within the hold, the session visible, is let go at his release', () => {
    const s = stage(undefined, true)
    held(s)
    s.aim(0.9)
    expect(s.read()?.pressing).toMatchObject({ holding: true, held: true })
    act(() => s.pointer.up(at()))
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'let'])
  })

  it('must not end: the flat preview has no session; a carry there goes on while its store says nothing', () => {
    const hand = { onPointerDown: vi.fn(), onPointerMove: vi.fn(), onPointerUp: vi.fn(), onPointerCancel: vi.fn(), onWheel: vi.fn() } as unknown as EmptyHand
    act(() => xr.set(undefined))
    const s = stage(hand, false)
    held(s)
    expect(s.read()).toMatchObject({ carried: true, pressing: { lifted: true, held: true } })
    s.aim(0.9)
    act(() => s.pointer.up(at()))
    expect(s.read()).toMatchObject({ carried: false, pressing: null })
  })

  it('must not end: a second pointer turned off while the dot holds the press', () => {
    const s = stage(undefined, true)
    const hand = s.ray('ray')
    hand.aim(0)
    held(s)
    act(() => hand.pointer.setEnabled(false, at()))
    expect(s.read()?.pressing).toMatchObject({ holding: true, held: true })
    act(() => s.pointer.up(at()))
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'let'])
  })
})

// M224: the quiet time takes a press by its own call (`if (hushed) pressTaken()`), not only by the session's
// visibility; and a press that had become the preview's drag lets the room go when it is taken (V9).
describe('a press on a Bot that the quiet time takes (M224)', () => {
  beforeEach(() => {
    xr.reset()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).position
  })

  it('must end: a hold, the quiet time begins while the session stays visible: told cancel, no press, no light; his release then does nothing', () => {
    const s = stage(undefined, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => { vi.advanceTimersByTime(500) })
    expect(s.read()?.pressing).toMatchObject({ holding: true })
    s.hush(true)
    expect(s.read()).toMatchObject({ pressing: null, lit: false })
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'cancel'])
    act(() => s.pointer.up(at()))
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'cancel'])
    expect(s.read()?.taps).toBe(0)
  })

  it('must end: a carried Bot, the quiet time begins: it is put back, nothing kept', () => {
    const s = stage(undefined, false)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => { vi.advanceTimersByTime(500) })
    expect(s.read()?.carried).toBe(true)
    s.hush(true)
    expect(s.read()).toMatchObject({ carried: false, pressing: null })
    act(() => s.pointer.up(at()))
    expect(s.onPlace).not.toHaveBeenCalled()
  })

  it('must not end: the quiet time that ends takes nothing; a press after it is a tap', () => {
    const s = stage(undefined, true)
    s.hush(true)
    s.hush(false)
    s.aim(0)
    act(() => s.pointer.down(at()))
    act(() => s.pointer.up(at()))
    expect(s.read()?.taps).toBe(1)
  })

  it('V9: a press that became the preview\'s drag, taken by the quiet time: the room is let go once, by that pointer', () => {
    const hand = { onPointerDown: vi.fn(), onPointerMove: vi.fn(), onPointerUp: vi.fn(), onPointerCancel: vi.fn(), onWheel: vi.fn() } as unknown as EmptyHand
    act(() => xr.set(undefined))
    const s = stage(hand, true)
    s.aim(0)
    act(() => s.pointer.down(at()))
    s.slide(40)
    expect(hand.onPointerDown).toHaveBeenCalledTimes(1)
    expect(s.read()?.pressing).toMatchObject({ dragging: true })
    s.hush(true)
    expect(hand.onPointerUp).toHaveBeenCalledTimes(1)
    expect((hand.onPointerUp as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({ pointerId: s.pointer.id })
    expect(s.read()?.pressing).toBeNull()
  })
})

// M221 (probes Pa, Pb): the store changes and comes back inside one task, before React draws again. The library
// turns the pointers off and on; the Crew hears both changes, so the press is taken as for a longer layer.
// M223 (probe Pe): a session that begins takes a press under way in the preview; a store change that keeps
// `visible` takes nothing (V2).
describe('a press on a Bot and the store\'s own changes (M221, M223)', () => {
  beforeEach(() => {
    xr.reset()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).position
  })
  const held = (s: Stage) => { s.aim(0); act(() => s.pointer.down(at())); act(() => { vi.advanceTimersByTime(500) }) }
  const flicker = () => act(() => { xr.set('hidden'); xr.set('visible') })

  it('Pa: a hold, the session hidden and visible again inside one task: told cancel, no press; lit only by the ray on it', () => {
    const s = stage(undefined, true)
    held(s)
    expect(s.read()?.pressing).toMatchObject({ holding: true })
    flicker()
    expect(s.read()?.pressing).toBeNull()
    // The pointers came back on with the ray still on the box: it is lit by that, and goes dark when the ray leaves.
    s.aim(0.9)
    expect(s.read()).toMatchObject({ pressing: null, lit: false })
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'cancel'])
    expect(s.read()?.visibility?.map(x => x.state)).toEqual(['visible', 'hidden', 'visible'])
  })

  it('Pb: a carried Bot, the same flicker: it is put back, nothing kept', () => {
    const s = stage(undefined, false)
    held(s)
    expect(s.read()?.carried).toBe(true)
    flicker()
    expect(s.read()).toMatchObject({ carried: false, pressing: null })
    act(() => s.pointer.up(at()))
    expect(s.onPlace).not.toHaveBeenCalled()
  })

  it('Pe: a Bot carried in the flat preview, then a session begins: it is put back, no press, no light', () => {
    const hand = { onPointerDown: vi.fn(), onPointerMove: vi.fn(), onPointerUp: vi.fn(), onPointerCancel: vi.fn(), onWheel: vi.fn() } as unknown as EmptyHand
    act(() => xr.set(undefined))
    const s = stage(hand, false)
    held(s)
    expect(s.read()?.carried).toBe(true)
    act(() => xr.set('visible'))
    expect(s.read()).toMatchObject({ carried: false, pressing: null, lit: false })
    expect(s.onPlace).not.toHaveBeenCalled()
  })

  it('V2, must not end: a store change that keeps `visible` takes nothing of a hold or a carry he still has', () => {
    const s = stage(undefined, true)
    held(s)
    act(() => xr.set('visible'))
    expect(s.read()?.pressing).toMatchObject({ holding: true })
    act(() => s.pointer.up(at()))
    expect(s.onHold.mock.calls.map(c => c[1])).toEqual(['start', 'let'])
    const c = stage(undefined, false)
    held(c)
    act(() => xr.set('visible'))
    expect(c.read()).toMatchObject({ carried: true, pressing: { lifted: true } })
  })
})
