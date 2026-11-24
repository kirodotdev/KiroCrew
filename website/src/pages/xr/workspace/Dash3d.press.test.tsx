import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, PlaneGeometry, Scene, Vector3 } from 'three'
import { Dash3d } from './Dash3d'
import { readLayout } from './dash3d'
import { pinchRoute } from './pinchRoute'
import type { DashView } from './useDash3d'

// REVIEW-73 M243 to M246: a resting tray's own handlers, as React made them on its group, each handed an event with the
// fields the room's pointer library gives one (`pointerId`, `pointerType`, `object`, `nativeEvent`). The frame loop is
// stood in for (so no frame lets a down go: what is tested is the handlers and the store, M245), and so is the room's
// camera; the session's store is the one the Crew's units use.
vi.mock('@react-three/fiber', () => {
  const camera = new PerspectiveCamera()
  const state = { camera, scene: new Scene(), gl: { xr: { isPresenting: false, getCamera: () => camera }, domElement: document.createElement('canvas') }, size: { width: 100, height: 100 } }
  return { useFrame: () => undefined, useThree: (pick: (s: typeof state) => unknown) => pick(state) }
})
const xr = vi.hoisted(() => {
  type State = { visibilityState?: string }
  let state: State = { visibilityState: 'visible' }
  const subs = new Set<(s: State, prev: State) => void>()
  return {
    getState: () => state,
    subscribe: (fn: (s: State, prev: State) => void) => { subs.add(fn); return () => { subs.delete(fn) } },
    set: (visibilityState: string | undefined) => { const prev = state; state = { visibilityState }; for (const fn of [...subs]) fn(state, prev) },
    /** `null`: no session yet, as the flat preview's store reads. */
    reset: (to: string | null = 'visible') => { subs.clear(); state = { visibilityState: to ?? undefined } },
  }
})
vi.mock('@react-three/xr', async () => {
  const { useSyncExternalStore } = await import('react')
  return { useXR: <T,>(pick: (s: { visibilityState?: string }) => T) => useSyncExternalStore(xr.subscribe, () => pick(xr.getState())), useXRStore: () => xr }
})
vi.mock('./RoomMark', () => ({ RoomMark: () => null }))

const view: DashView = { slug: 'mira', label: 'Mira', layout: readLayout({ v: 1, parts: [{ kind: 'ring', name: 'Checks', field: 'checks_done', max: 20 }] }), fields: {}, stale: false } as unknown as DashView
const propsOf = (el: Element): Record<string, unknown> => {
  const key = Object.keys(el).find(k => k.startsWith('__reactProps$'))
  return key ? ((el as unknown as Record<string, Record<string, unknown>>)[key] ?? {}) : {}
}
/** A drawn piece of the tray, as the library's event carries it (`object`): a mesh shown, with its material shown. */
const piece = new Mesh(new PlaneGeometry(0.1, 0.1), new MeshBasicMaterial())
const ev = (pointerId: number, pointerType = 'ray') => ({ pointerId, pointerType, object: piece, stopPropagation: () => undefined, nativeEvent: { clientX: 0, clientY: 0, timeStamp: 0 } })
type Work = { presses: { ev: string; slug: string }[] }

function stage(preview = false) {
  const drag = preview ? { onPointerDown: vi.fn(), onPointerMove: vi.fn(), onPointerUp: vi.fn(), onPointerCancel: vi.fn(), onWheel: vi.fn() } : undefined
  const { container } = render(
    <Dash3d views={[view]} crew={() => []} block={null} boardsUp={new Set()} say={() => undefined} drag={drag as never} still stateColors={{}} report={() => undefined} />,
  )
  const el = [...container.querySelectorAll('group')].find(g => (propsOf(g).userData as { dash3d?: string } | undefined)?.dash3d === 'mira')
  if (!el) throw new Error('no tray was drawn')
  const h = () => propsOf(el) as Record<string, (e: unknown) => void>
  const all = () => ((window as unknown as { __xrDashWork?: Work }).__xrDashWork?.presses ?? []).filter(x => x.ev === 'press' && x.slug === 'mira').length
  // The probe's list is the module's own and outlives a test: each test counts from where it began.
  const from = all()
  const presses = () => all() - from
  const lit = () => (propsOf(el).userData as { lit: boolean }).lit
  const call = (name: string, id: number, type?: string) => act(() => { h()[name](ev(id, type)) })
  return {
    presses, lit,
    enter: (id: number, type?: string) => call('onPointerEnter', id, type),
    leave: (id: number, type?: string) => call('onPointerLeave', id, type),
    down: (id: number) => call('onPointerDown', id),
    up: (id: number) => call('onPointerUp', id),
  }
}

describe('a resting tray\'s own handlers (REVIEW-73 M243 to M246)', () => {
  beforeEach(() => {
    xr.reset()
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    // A group of the room is a host element here; the tray is drawn (its frame shows it).
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
    Object.defineProperty(Element.prototype, 'visible', { configurable: true, get: () => true, set: () => undefined })
    Object.defineProperty(Element.prototype, 'matrixWorld', { configurable: true, get: () => new Matrix4() })
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    ;(pinchRoute as unknown as { owner: unknown }).owner = { hand: 'right', to: 'dot', from: [0, 0, 0], at: 0 }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    for (const k of ['position', 'visible', 'matrixWorld', 'getObjectByName']) delete (Element.prototype as unknown as Record<string, unknown>)[k]
    ;(pinchRoute as unknown as { owner: unknown }).owner = null
  })

  it('must take: down, the ray leaves, comes back, the up: the tray answers once', () => {
    const s = stage()
    s.enter(7); s.down(7); s.leave(7); s.enter(7); s.up(7)
    expect(s.presses()).toBe(1)
  })
  it('must not take: down, the ray leaves, the up lands elsewhere (the tray hears none), and a bare up later in a new pinch (M244)', () => {
    const s = stage()
    s.enter(7); s.down(7); s.leave(7)
    // The other hand pinches while the first one's fingers are open: the first ends and the second begins in one tick.
    pinchRoute.downs += 1
    s.enter(7); s.up(7)
    expect(s.presses()).toBe(0)
  })
  it('must not take: a hidden session heard from the store lets the kept down go, with no frame drawn (M245)', () => {
    const s = stage()
    s.enter(7); s.down(7)
    act(() => xr.set('hidden'))
    act(() => xr.set('visible'))
    s.enter(7); s.up(7)
    expect(s.presses()).toBe(0)
  })
  it('must not lose: after a hidden session a new press takes it', () => {
    const s = stage()
    act(() => xr.set('hidden'))
    act(() => xr.set('visible'))
    s.enter(7); s.down(7); s.up(7)
    expect(s.presses()).toBe(1)
  })
  it('M246: two pointers on it, one leaves: it is still under the other; both leave: not', () => {
    const s = stage()
    s.enter(7); s.enter(8, 'hand')
    s.leave(7)
    expect(s.lit()).toBe(true)
    s.leave(8, 'hand')
    expect(s.lit()).toBe(false)
  })
  it('M246: a preview pointer left on it when a session begins (dropped with no leave) does not keep it under a pointer', () => {
    xr.reset(null)
    const s = stage(true)
    s.enter(1, 'mouse')
    expect(s.lit()).toBe(true)
    act(() => xr.set('visible'))
    expect(s.lit()).toBe(false)
  })
})
