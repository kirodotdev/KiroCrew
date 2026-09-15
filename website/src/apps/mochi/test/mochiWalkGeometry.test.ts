/**
 * Walk geometry — ported from the original main/index.ts perform_pet_action.
 *
 * This arithmetic took a long time to get right upstream and every failure mode
 * is silent: a wrong clamp walks the pet behind the Dock, a missing dead-band
 * makes it twitch on every planner tick, and a missing cancel makes two walks
 * fight. The geometry moved from the main process to the renderer (the shell
 * cannot subscribe to the gateway event bus), so it needs its own pins.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../panel/panelBridge', () => ({
  subscribeAppEvent: () => () => {},
  onColorMapChanged: () => () => {},
  getPetState: vi.fn(),
  onStateChange: () => () => {},
  onMood: () => () => {},
  galleryGetPackDetail: vi.fn(),
  galleryPackFileUrl: () => '',
  presetsGetColorMap: vi.fn(),
}))

const PET_W = 128
const PET_H = 128
const PET_BOTTOM_MARGIN = 140
const WA = { width: 1000, height: 800 }
/** The clamp ceiling the original used: work area minus the pet and the Dock. */
const MAX_Y = WA.height - PET_H - PET_BOTTOM_MARGIN

let bridge: typeof import('../pet/petBridge')
const walks: [number, number][] = []
const paths: { x: number; y: number }[][] = []
const appends: { x: number; y: number }[][] = []
let cancels = 0

beforeEach(async () => {
  vi.resetModules()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
  bridge = await import('../pet/petBridge')
  walks.length = 0
  paths.length = 0
  appends.length = 0
  cancels = 0
  bridge.onWalk((x, y) => walks.push([x, y]))
  bridge.onWalkPath((p) => paths.push(p))
  bridge.onWalkAppend((p) => appends.push(p))
  bridge.onWalkCancel(() => {
    cancels += 1
  })
  bridge._setWorkAreaForTest(WA)
  bridge._setLastPosForTest({ x: 500, y: 300 })
})

describe('explicit target', () => {
  it('clamps to the work area, keeping the pet off the Dock', () => {
    bridge.handleMove({ x: 99999, y: 99999 })
    expect(walks).toEqual([[WA.width - PET_W, MAX_Y]])
  })

  it('clamps negatives to the top-left corner', () => {
    bridge.handleMove({ x: -500, y: -500 })
    expect(walks).toEqual([[0, 0]])
  })

  it('ignores a move inside the 20px dead-band', () => {
    // A planner nudging the pet a few pixels must not trigger a walk animation.
    bridge.handleMove({ x: 505, y: 305 })
    expect(walks).toEqual([])
  })

  it('acts on a move just outside the dead-band', () => {
    bridge.handleMove({ x: 530, y: 300 })
    expect(walks).toEqual([[530, 300]])
  })
})

describe('behaviours', () => {
  it('hide_left keeps the current y', () => {
    bridge.handleMove({ behavior: 'hide_left' })
    expect(walks).toEqual([[0, 300]])
  })

  it('hide_right parks one pet-width from the edge, same y', () => {
    bridge.handleMove({ behavior: 'hide_right' })
    expect(walks).toEqual([[WA.width - PET_W, 300]])
  })

  it('return centres horizontally and above the Dock margin', () => {
    bridge.handleMove({ behavior: 'return' })
    expect(walks).toEqual([[500, Math.floor((WA.height - PET_BOTTOM_MARGIN) / 2)]])
  })
})

describe('waypoints', () => {
  it('clamps every point and emits one path', () => {
    bridge.handleMove({ waypoints: [{ x: -10, y: 10 }, { x: 99999, y: 99999 }] })
    expect(paths).toEqual([[{ x: 0, y: 10 }, { x: WA.width - PET_W, y: MAX_Y }]])
  })

  it('interrupt:false APPENDS instead of replacing, and does not cancel', () => {
    bridge.handleMove({ waypoints: [{ x: 100, y: 100 }], interrupt: false })
    expect(appends).toHaveLength(1)
    expect(paths).toHaveLength(0)
    expect(cancels).toBe(0)
  })

  it('a normal move cancels the walk in flight first', () => {
    bridge.handleMove({ waypoints: [{ x: 100, y: 100 }] })
    expect(cancels).toBe(1)
  })
})

describe('non-moves', () => {
  it('a query-shaped action moves nothing and cancels nothing', () => {
    bridge.handleMove({})
    expect(walks).toEqual([])
    expect(paths).toEqual([])
    expect(cancels).toBe(0)
  })
})

describe('reports', () => {
  it('walk-distance is dropped when non-positive, posted otherwise', () => {
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>
    fetchMock.mockClear()
    bridge.reportWalkDistance(0)
    expect(fetchMock).not.toHaveBeenCalled()
    bridge.reportWalkDistance(640)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/apps/mochi/walk-distance',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
