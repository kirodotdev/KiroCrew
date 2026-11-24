import { act, render } from '@testing-library/react'
import { forwardHtmlEvents } from '@pmndrs/pointer-events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, PlaneGeometry, Scene, Vector3 } from 'three'
import type { XrNeed } from '../model'
import { Boards } from './Board'
import { boardFor, boardText, type Board as BoardData } from './board'
import type { BoardView } from './useBoards'

// REVIEW-73 M242, M243: the flat preview's click on a board at its Bot, through the board's own handlers and the
// room's own pointer library. The screen's events go through `forwardHtmlEvents` as `@react-three/xr`'s
// `PointerEvents` sets it up under a canvas that always renders (`events.js:11`: `batchEvents: alwaysRendering`), so
// they reach the scene at the next frame (`update`, which the room's loop runs before it draws). A frame here is that
// `update`, then the callbacks asked for with `requestAnimationFrame` since the last frame, in the order a browser runs
// them: the room's loop asks for its next frame first thing (`@react-three/fiber` `loop`: `frame =
// requestAnimationFrame(loop)`, then `flushGlobalEffects('before')`), so a callback asked for between frames runs
// after the loop's `update` in the next one.
vi.mock('@react-three/fiber', () => {
  const state = { camera: new PerspectiveCamera(), scene: new Scene(), gl: { xr: { isPresenting: false, getCamera: () => new PerspectiveCamera() }, compile: () => undefined } }
  return { useFrame: () => undefined, useThree: (pick: (s: typeof state) => unknown) => pick(state) }
})
const xr = vi.hoisted(() => {
  type State = { visibilityState?: string }
  let state: State = { visibilityState: undefined }
  const subs = new Set<(s: State, prev: State) => void>()
  return {
    getState: () => state,
    subscribe: (fn: (s: State, prev: State) => void) => { subs.add(fn); return () => { subs.delete(fn) } },
    set: (visibilityState: string | undefined) => { const prev = state; state = { visibilityState }; for (const fn of [...subs]) fn(state, prev) },
    reset: () => { subs.clear(); state = { visibilityState: undefined } },
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

function stage(where: 'bot' | 'floor' = 'bot') {
  const base = boardFor(need)
  const board: BoardData = where === 'floor' ? { ...base, where: 'floor' } : base
  const onTake = vi.fn(() => true)
  const onPick = vi.fn()
  const props = {
    boards: [viewOf(board)], crew: () => [], head: new Vector3(0, 1.6, 0), inSession: false, still: true, onTake, onStamp: vi.fn(), onRelease: vi.fn(() => ({ particles: false })), onPick,
    onCorner: vi.fn(), onPage: vi.fn(), onStampBox: vi.fn(), onFoot: vi.fn(), onAway: vi.fn(),
  }
  const { container } = render(<Boards {...props} />)
  const el = [...container.querySelectorAll('mesh')].find(m => propsOf(m).name === 'board-paper')
  if (!el) throw new Error('no board paper was drawn')
  // The paper's handlers on a real mesh, as `@react-three/fiber` hands them to the library (`__r3f.handlers`).
  const scene = new Scene()
  const paper = new Mesh(new PlaneGeometry(0.4, 0.4), new MeshBasicMaterial())
  paper.position.set(0, 0, -1)
  Object.defineProperty(paper, '__r3f', { get: () => { const handlers = propsOf(el); return { handlers, eventCount: Object.keys(handlers).filter(k => /^onPointer|^onWheel/.test(k)).length } } })
  // A tray beside it: its handlers stop the event, as Dash3d's drawn tray does.
  const tray = new Mesh(new PlaneGeometry(0.4, 0.4), new MeshBasicMaterial())
  tray.position.set(-0.6, 0, -1)
  scene.add(paper, tray)
  scene.updateMatrixWorld()
  const camera = new PerspectiveCamera(90, 1, 0.1, 10)
  camera.updateMatrixWorld()
  const canvas = document.createElement('canvas')
  document.body.appendChild(canvas)
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100, x: 0, y: 0, toJSON: () => ({}) })
  const fw = forwardHtmlEvents(canvas, () => camera, scene, { batchEvents: true, pointerTypePrefix: 'screen-' })
  const asked: FrameRequestCallback[] = []
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { asked.push(cb); return asked.length })
  const frame = () => act(() => { fw.update(); const now = asked.splice(0); for (const cb of now) cb(performance.now()) })
  /** A screen event on the canvas at (x, y) of 100 by 100, as the browser sends it; it bubbles to the window. */
  const fire = (type: string, x = 50, { id = 1, kind = 'mouse', button = 0 } = {}) => act(() => {
    const e = new MouseEvent(type, { bubbles: true, clientX: x, clientY: 50, button, buttons: type === 'pointerdown' ? 1 << button : 0 })
    Object.defineProperty(e, 'pointerId', { value: id }); Object.defineProperty(e, 'pointerType', { value: kind })
    canvas.dispatchEvent(e)
  })
  /** A click held `frames` frames, at (x, 50). */
  const click = (frames: number, x = 50, o = {}) => { fire('pointermove', x, o); frame(); fire('pointerdown', x, o); for (let i = 0; i < frames; i += 1) frame(); fire('pointerup', x, o); frame(); frame() }
  return { onTake, onPick, fire, frame, click, done: () => { fw.destroy(); canvas.remove() } }
}

describe('a click on a board in the flat preview, through the room\'s pointer library (REVIEW-73 M242)', () => {
  beforeEach(() => {
    xr.reset()
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const ctx = new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => undefined) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ctx as never)
    Object.defineProperty(Element.prototype, 'position', { configurable: true, get: () => new Vector3() })
    Object.defineProperty(Element.prototype, 'getObjectByName', { configurable: true, value: () => undefined })
    Object.defineProperty(Element.prototype, 'matrixWorld', { configurable: true, get: () => new Matrix4() })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    delete (Element.prototype as unknown as Record<string, unknown>).position
    delete (Element.prototype as unknown as Record<string, unknown>).getObjectByName
    delete (Element.prototype as unknown as Record<string, unknown>).matrixWorld
  })

  for (const frames of [1, 3, 30]) {
    it(`must take: a click held ${frames} frame${frames > 1 ? 's' : ''} on a board at its Bot`, () => {
      const s = stage()
      s.click(frames)
      expect(s.onTake).toHaveBeenCalledTimes(1)
      s.done()
    })
  }
  it('must take: a click with no frame between its down and its up (a tool\'s click)', () => {
    const s = stage()
    s.fire('pointermove'); s.frame()
    s.fire('pointerdown'); s.fire('pointerup'); s.frame(); s.frame()
    expect(s.onTake).toHaveBeenCalledTimes(1)
    s.done()
  })
  it('must take: two clicks, the second one\'s down before the frame that hands on the first one\'s up', () => {
    const s = stage()
    s.fire('pointermove'); s.frame()
    s.fire('pointerdown'); s.frame()
    s.fire('pointerup'); s.fire('pointerdown'); s.frame(); s.frame()
    s.fire('pointerup'); s.frame(); s.frame()
    expect(s.onTake).toHaveBeenCalledTimes(2)
    s.done()
  })
  it('must take: a pick from the floor, held 3 frames', () => {
    const s = stage('floor')
    s.click(3)
    expect(s.onPick).toHaveBeenCalledTimes(1)
    s.done()
  })
  it('must take: a touch held 3 frames', () => {
    const s = stage()
    s.click(3, 50, { id: 9, kind: 'touch' })
    expect(s.onTake).toHaveBeenCalledTimes(1)
    s.done()
  })
  it('must not take: a down beside it (the tray\'s place), the up on the board', () => {
    const s = stage()
    s.fire('pointermove', 15); s.frame()
    s.fire('pointerdown', 15); s.frame(); s.frame()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerup', 50); s.frame(); s.frame()
    expect(s.onTake).not.toHaveBeenCalled()
    s.done()
  })
  it('must not take: a down on the board, the up off it, then a bare up on the board', () => {
    const s = stage()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerdown', 50); s.frame(); s.frame()
    s.fire('pointermove', 95); s.frame()
    s.fire('pointerup', 95); s.frame(); s.frame()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerup', 50); s.frame(); s.frame()
    expect(s.onTake).not.toHaveBeenCalled()
    s.done()
  })
  it('must not take: a down on the board, the pointer leaves the canvas (the library exits and cancels), a later bare up', () => {
    const s = stage()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerdown', 50); s.frame(); s.frame()
    s.fire('pointerleave', 50); s.frame()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerup', 50); s.frame(); s.frame()
    expect(s.onTake).not.toHaveBeenCalled()
    s.done()
  })
  it('must not take: a session that begins while a preview down is kept lets it go', () => {
    const s = stage()
    s.fire('pointermove', 50); s.frame()
    s.fire('pointerdown', 50); s.frame(); s.frame()
    act(() => xr.set('visible'))
    s.fire('pointerup', 50); s.frame(); s.frame()
    expect(s.onTake).not.toHaveBeenCalled()
    s.done()
  })
})
