import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Matrix4, PerspectiveCamera, Scene, Vector3 } from 'three'
import type { XrNeed } from '../model'
import { Boards } from './Board'
import { STAY_MOVED, boardFor, boardText, releaseKind, takeBoard, type Board as BoardData } from './board'
import { pinchRoute } from './pinchRoute'
import type { BoardView } from './useBoards'

// The board's own handlers run as they are, on the element React made for its paper; each is handed an event with
// the fields the room's pointer library gives one. The frame loop, the room's camera and the parts that only draw
// (dust, the lowering, the stamp) are stood in for; the session's store is the one the Crew's units use.
vi.mock('@react-three/fiber', () => {
  const state = { camera: new PerspectiveCamera(), scene: new Scene(), gl: { xr: { isPresenting: false, getCamera: () => new PerspectiveCamera() }, compile: () => undefined } }
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
    reset: () => { subs.clear(); state = { visibilityState: 'visible' } },
  }
})
vi.mock('@react-three/xr', () => ({ useXRStore: () => xr }))
vi.mock('./BoardDust', () => ({ BoardDust: () => null, useDustWarm: () => undefined }))
vi.mock('./BoardLower', () => ({ BoardLower: () => null }))
vi.mock('./BoardStamp', async real => ({ ...(await real<typeof import('./BoardStamp')>()), BoardStamp: () => null, useStampWarm: () => undefined }))

const need: XrNeed = {
  id: 'a', kind: 'approval', card: true, bot: 'mira', who: 'Mira', topic: 'release', slot: 's1', action: 'Run the release script', tool: 'shell',
  command: 'npm run release', extra: [], version: 'a|m1|npm run release', purpose: '', origin: 'native', requestId: 'r-a', requestMid: 'm1', questions: [], ts: 1,
}
const viewOf = (b: BoardData): BoardView => ({ ...b, need, label: 'Mira', text: boardText(need), print: null, behind: 0, asked: null, sheets: [] })
const propsOf = (el: Element): Record<string, unknown> => {
  const key = Object.keys(el).find(k => k.startsWith('__reactProps$'))
  return key ? ((el as unknown as Record<string, Record<string, unknown>>)[key] ?? {}) : {}
}
/** A pointer event as the library hands it to a handler on the paper. */
const ev = (pointerId: number, x = 0) => ({ pointerId, pointerType: 'ray', point: new Vector3(x, 0, -1), uv: null, object: { setPointerCapture: () => undefined }, stopPropagation: () => undefined, nativeEvent: { clientX: x, clientY: 0, timeStamp: 0 } })

function stage(where: 'held' | 'bot', inSession = true) {
  const base = boardFor(need)
  const board = where === 'held' ? takeBoard(base, need, 0) : base
  const onRelease = vi.fn((_id: string, _m: unknown) => ({ particles: false }))
  const onTake = vi.fn(() => true)
  const props = {
    boards: [viewOf(board)], crew: () => [], head: new Vector3(0, 1.6, 0), inSession, still: true, onTake, onStamp: vi.fn(), onRelease, onPick: vi.fn(),
    onCorner: vi.fn(), onPage: vi.fn(), onStampBox: vi.fn(), onFoot: vi.fn(), onAway: vi.fn(),
  }
  const { container } = render(<Boards {...props} />)
  const el = [...container.querySelectorAll('mesh')].find(m => propsOf(m).name === 'board-paper')
  if (!el) throw new Error('no board paper was drawn')
  const h = () => propsOf(el) as Record<string, (e: unknown) => void>
  return {
    onRelease, onTake,
    down: (id: number) => act(() => { h().onPointerDown(ev(id)) }),
    move: (id: number, x: number) => act(() => { h().onPointerMove(ev(id, x)) }),
    up: (id: number) => act(() => { h().onPointerUp(ev(id)) }),
    cancel: (id: number) => act(() => { h().onPointerCancel(ev(id)) }),
  }
}

describe('a board\'s press that ends by a cancel or a hidden session, through its own handlers (D192, DASH-17)', () => {
  beforeEach(() => {
    xr.reset()
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    // A group of the room is a host element here: the board's own code reads its place (as the Crew's units do).
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'matrixWorld', { configurable: true, get: () => new Matrix4() })
    // A fast drag of the right hand (0.2 m at 1.5 m/s), as `pinchRoute` reports it at the release.
    ;(pinchRoute as unknown as { owner: unknown }).owner = { hand: 'right', to: 'right', from: [0, 0, 0], at: 0 }
    ;(pinchRoute as unknown as { released: unknown }).released = { hand: 'right', travelM: 0.2, mps: 1.5 }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    ;(pinchRoute as unknown as { owner: unknown }).owner = null
    ;(pinchRoute as unknown as { released: unknown }).released = null
    pinchRoute.ending = null
    delete (Element.prototype as unknown as Record<string, unknown>).position
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).matrixWorld
  })

  it('must not act: a fast drag ended by a cancel that is not his (a lost hand) lets the board go as a stay', () => {
    const s = stage('held')
    s.down(7)
    s.move(7, 200)
    s.cancel(7)
    expect(s.onRelease).toHaveBeenCalledTimes(1)
    expect(s.onRelease.mock.calls[0][1]).toEqual(STAY_MOVED)
    expect(releaseKind(s.onRelease.mock.calls[0][1] as never)).toBe('stay')
  })
  it('must not lose: the same drag ended by his own let-go (a cancel inside his release) is read as his throw', () => {
    const s = stage('held')
    s.down(7)
    s.move(7, 200)
    act(() => { pinchRoute.release('long', () => s.cancel(7)) })
    expect(releaseKind(s.onRelease.mock.calls[0][1] as never)).toBe('throw')
  })
  it('must not act: a hidden session ends the press as a stay; his release after it lets go nothing more', () => {
    const s = stage('held')
    s.down(7)
    s.move(7, 200)
    act(() => xr.set('hidden'))
    expect(s.onRelease).toHaveBeenCalledTimes(1)
    expect(s.onRelease.mock.calls[0][1]).toEqual(STAY_MOVED)
    act(() => xr.set('visible'))
    s.up(7)
    expect(s.onRelease).toHaveBeenCalledTimes(1)
  })
  it('must not act: a hidden session lets go a down on a board at its Bot, so the up after it takes nothing', () => {
    const s = stage('bot')
    s.down(7)
    act(() => xr.set('hidden'))
    act(() => xr.set('visible'))
    s.up(7)
    expect(s.onTake).not.toHaveBeenCalled()
  })
  it('must not lose: a pinch on a board at its Bot takes it; a long one let go by a cancel inside his release too (M47)', () => {
    const s = stage('bot')
    s.down(7)
    s.up(7)
    expect(s.onTake).toHaveBeenCalledTimes(1)
    s.down(7)
    act(() => { pinchRoute.release('long', () => s.cancel(7)) })
    expect(s.onTake).toHaveBeenCalledTimes(2)
  })
  // The flat preview's let-go runs through the room's pointer library, in its own order: `Board.preview.test.tsx`.
})
