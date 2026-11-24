/**
 * The layer path, against fakes of the few parts of WebXR's layers and of WebGL that it calls. No session here
 * grants layers, and no device has run this: the tests hold which calls are made, in which order, and how often.
 */
import { describe, expect, it } from 'vitest'
import { KEEP_LAYER_DEG, MOST_PAGE_LAYERS, layerBudget, offGaze, orderLayers, pickLayers, type LayerPage } from './layerPick'
import { LayerDirector, PageLayer, type LayerBinding, type LayerEntry, type LayerGl, type LayerHost, type LayerPanel, type LayerPicture, type LayerSource, type QuadLayer, type QuadLayerInit } from './pageLayer'

const page = (id: string, offGazeDeg: number, o: Partial<LayerPage> = {}): LayerPage => ({ id, offGazeDeg, distanceM: 1.8, inView: true, ready: true, ...o })

describe('how many pages may have a layer', () => {
  it('is none in a session that did not grant layers', () => {
    expect(layerBudget(undefined, 16)).toBe(0)
    expect(layerBudget(null, 16)).toBe(0)
    expect(layerBudget(['hand-tracking', 'local-floor'], 16)).toBe(0)
  })

  it('is the session\'s limit less one for the scene, and never more than six', () => {
    expect(layerBudget(['layers'], 7)).toBe(6)
    expect(layerBudget(['layers'], 4)).toBe(3)
    expect(layerBudget(['layers'], 1)).toBe(0)
    expect(layerBudget(['layers'], 16)).toBe(MOST_PAGE_LAYERS)
    expect(layerBudget(['layers'], 3.9)).toBe(2)
  })

  it('is six when the session grants layers and names no limit', () => {
    expect(layerBudget(['layers'], undefined)).toBe(6)
    expect(layerBudget(['layers'], Number.NaN)).toBe(6)
  })
})

describe('which pages get a layer', () => {
  it('takes the pages nearest to where the person looks, up to the budget', () => {
    const pages = [page('far-left', 80), page('ahead', 2), page('right', 40), page('left', 35), page('behind', 170)]
    expect(pickLayers(pages, 3)).toEqual(['ahead', 'left', 'right'])
    expect(pickLayers(pages, 0)).toEqual([])
    expect(pickLayers(pages, 99)).toHaveLength(5)
  })

  it('gives none to a page that is out of view, so a page that leaves the view gives its layer back', () => {
    const held = new Set(['a', 'b'])
    expect(pickLayers([page('a', 5), page('b', 30, { inView: false })], 6, held)).toEqual(['a'])
  })

  it('gives none to a page with no picture a layer can show', () => {
    expect(pickLayers([page('still', 1, { ready: false }), page('live', 20)], 6)).toEqual(['live'])
  })

  it('lets a page keep its layer until another is clearly nearer to the gaze', () => {
    const held = new Set(['a'])
    // The head rests between two pages: the one that has the layer keeps it.
    expect(pickLayers([page('a', 31), page('b', 29)], 1, held)).toEqual(['a'])
    expect(pickLayers([page('a', 31 + KEEP_LAYER_DEG), page('b', 29)], 1, held)).toEqual(['b'])
    // With nothing held, the nearer one wins, and two at the same angle go by name, the same way every time.
    expect(pickLayers([page('a', 31), page('b', 29)], 1)).toEqual(['b'])
    expect(pickLayers([page('b', 10), page('a', 10)], 1)).toEqual(['a'])
  })

  it('orders the layers with the farthest first, so a nearer page lies over a farther one', () => {
    expect(orderLayers([{ id: 'near', distanceM: 1.1 }, { id: 'far', distanceM: 4 }, { id: 'mid', distanceM: 1.8 }])).toEqual(['far', 'mid', 'near'])
    expect(orderLayers([{ id: 'b', distanceM: 2 }, { id: 'a', distanceM: 2 }])).toEqual(['a', 'b'])
  })

  it('measures how far a page is from the gaze', () => {
    expect(offGaze([0, 1.6, 0], [0, 0, -1], [0, 1.6, -2])).toBeCloseTo(0, 6)
    expect(offGaze([0, 1.6, 0], [0, 0, -1], [2, 1.6, 0])).toBeCloseTo(90, 6)
    expect(offGaze([0, 1.6, 0], [0, 0, -5], [0, 1.6, 3])).toBeCloseTo(180, 6)
  })
})

/* ---------------- fakes ---------------- */

type Call = [string, ...unknown[]]
function fakes() {
  const calls: Call[] = []
  let made = 0
  const binding: LayerBinding = {
    createQuadLayer(init: QuadLayerInit) { made += 1; const layer = { name: `L${made}`, destroy() { calls.push(['destroy', (this as { name: string }).name]) } }; calls.push(['createQuadLayer', layer.name, init]); return layer as unknown as QuadLayer },
    getSubImage(layer, frame) { const name = (layer as unknown as { name: string }).name; calls.push(['getSubImage', name, frame]); return { colorTexture: `texture-of-${name}` } },
  }
  const gl: LayerGl = { TEXTURE_2D: 0x0de1, RGBA: 0x1908, UNSIGNED_BYTE: 0x1401, texSubImage2D: (...args) => { calls.push(['texSubImage2D', ...args]) } }
  const bind = (target: number, texture: unknown) => { calls.push(['bindTexture', target, texture]) }
  return { calls, binding, gl, bind }
}
const bitmap = (name: string, closed: string[]) => ({ name, width: 2048, height: 1440, close: () => { closed.push(name) } }) as unknown as ImageBitmap
const picture = (name: string, closed: string[], o: Partial<LayerPicture> = {}): LayerPicture => ({ bitmap: bitmap(name, closed), width: 2048, height: 1440, flipped: true, ...o })
const names = (calls: Call[]) => calls.map(c => c[0])

describe('a page\'s layer', () => {
  it('is made once, in the picture\'s own size, as a layer that can be written on any frame', () => {
    const f = fakes()
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'the-space', 2048, 1440)
    expect(f.calls).toEqual([['createQuadLayer', 'L1', { space: 'the-space', viewPixelWidth: 2048, viewPixelHeight: 1440, layout: 'mono', isStatic: false, textureType: 'texture', mipLevels: 1 }]])
    expect([layer.width, layer.height]).toEqual([2048, 1440])
  })

  it('makes no call at all in a frame with no new picture', () => {
    const f = fakes()
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    f.calls.length = 0
    for (let i = 0; i < 90; i += 1) expect(layer.write(`frame-${i}`)).toBe(false)
    expect(f.calls).toEqual([])
    expect(layer.writes).toBe(0)
  })

  it('writes one new picture with one getSubImage, one bind and one texSubImage2D, in that order, and then closes it', () => {
    const f = fakes()
    const closed: string[] = []
    const done: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    f.calls.length = 0
    const p = picture('p1', closed, { done: () => done.push('p1') })
    expect(layer.offer(p)).toBe(true)
    // Nothing happens until a frame.
    expect(f.calls).toEqual([])
    expect(layer.write('frame-1')).toBe(true)
    expect(f.calls).toEqual([
      ['getSubImage', 'L1', 'frame-1'],
      ['bindTexture', f.gl.TEXTURE_2D, 'texture-of-L1'],
      ['texSubImage2D', f.gl.TEXTURE_2D, 0, 0, 0, f.gl.RGBA, f.gl.UNSIGNED_BYTE, p.bitmap],
    ])
    expect(closed).toEqual(['p1'])
    expect(done).toEqual(['p1'])
    // The frames after it: nothing again.
    f.calls.length = 0
    expect(layer.write('frame-2')).toBe(false)
    expect(f.calls).toEqual([])
    expect(layer.writes).toBe(1)
  })

  it('writes only the newest of two pictures that arrive between two frames', () => {
    const f = fakes()
    const closed: string[] = []
    const done: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    layer.offer(picture('older', closed, { done: () => done.push('older') }))
    layer.offer(picture('newer', closed, { done: () => done.push('newer') }))
    expect(closed).toEqual(['older'])
    f.calls.length = 0
    layer.write('frame')
    expect(names(f.calls)).toEqual(['getSubImage', 'bindTexture', 'texSubImage2D'])
    expect((f.calls[2][7] as { name: string }).name).toBe('newer')
    expect(done).toEqual(['newer'])
  })

  it('refuses a picture that would stand on its head: one decoded with no options', () => {
    const f = fakes()
    const closed: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    expect(layer.offer(picture('upright-for-a-texture', closed, { flipped: false }))).toBe(false)
    f.calls.length = 0
    expect(layer.write('frame')).toBe(false)
    expect(f.calls).toEqual([])
    // It is the caller's still: not closed here.
    expect(closed).toEqual([])
  })

  it('refuses a picture of another size: a layer has one size for its life', () => {
    const f = fakes()
    const closed: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    expect(layer.offer(picture('smaller', closed, { width: 1434, height: 1008 }))).toBe(false)
    expect(closed).toEqual([])
  })

  it('writes nothing outside a frame, and keeps the picture for the next one', () => {
    const f = fakes()
    const closed: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    layer.offer(picture('p', closed))
    f.calls.length = 0
    expect(layer.write(null)).toBe(false)
    expect(f.calls).toEqual([])
    expect(layer.write('frame')).toBe(true)
  })

  it('is destroyed once, closes a picture that still waits, and takes nothing after that', () => {
    const f = fakes()
    const closed: string[] = []
    const layer = new PageLayer(f.binding, f.gl, f.bind, 'space', 2048, 1440)
    layer.offer(picture('waiting', closed))
    f.calls.length = 0
    layer.dispose()
    layer.dispose()
    expect(f.calls).toEqual([['destroy', 'L1']])
    expect(closed).toEqual(['waiting'])
    expect(layer.offer(picture('late', closed))).toBe(false)
    expect(layer.write('frame')).toBe(false)
  })
})

/* ---------------- the director ---------------- */

function world(features: readonly string[] | undefined = ['layers'], maxRenderLayers: number | undefined = 7) {
  const f = fakes()
  const list: LayerEntry[] = []
  const log: string[] = []
  const state = { session: { enabledFeatures: features, maxRenderLayers } as { enabledFeatures?: readonly string[]; maxRenderLayers?: number } | null, bindings: 0 }
  const host: LayerHost = {
    session: () => state.session,
    binding: () => { state.bindings += 1; return f.binding },
    gl: () => f.gl,
    bind: f.bind,
    space: () => 'reference-space',
    add: e => { list.push(e); log.push(`add ${(e.layer as unknown as { name: string }).name}`) },
    remove: e => { list.splice(list.indexOf(e), 1); log.push(`remove ${(e.layer as unknown as { name: string }).name}`) },
    place: (layer, anchor) => { f.calls.push(['place', (layer as unknown as { name: string }).name, anchor]) },
  }
  return { ...f, list, log, state, host }
}
function panel(id: string, at: [number, number, number], o: { inView?: boolean; size?: [number, number] | null } = {}) {
  const told: (PageLayer | null)[] = []
  const state = { inView: o.inView ?? true, size: o.size === undefined ? [2048, 1440] as [number, number] : o.size, at }
  const source: LayerSource = { toLayer: l => { told.push(l) }, pictureSize: () => state.size }
  const p: LayerPanel = { id, source, position: () => state.at, inView: () => state.inView, anchor: `mesh-of-${id}` }
  return { p, told, state }
}
const HEAD: [number, number, number] = [0, 1.6, 0]
const AHEAD: [number, number, number] = [0, 0, -1]

describe('the director of the layers', () => {
  it('does nothing in a session with no layers: no binding is asked for, no layer is made, no source is told', () => {
    const w = world(['hand-tracking'])
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    for (let i = 0; i < 5; i += 1) d.frame(`frame-${i}`, HEAD, AHEAD, [a.p])
    expect(w.calls).toEqual([])
    expect(w.state.bindings).toBe(0)
    expect(a.told).toEqual([])
    expect([...d.onLayer]).toEqual([])
  })

  it('does nothing outside a session, and nothing in a frame that is not a session\'s', () => {
    const w = world()
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    d.frame(null, HEAD, AHEAD, [a.p])
    w.state.session = null
    d.frame('frame', HEAD, AHEAD, [a.p])
    expect(w.calls).toEqual([])
    expect(a.told).toEqual([])
  })

  it('gives a page in view a layer of its picture\'s size, hands it to the framework, and tells the page\'s source', () => {
    const w = world()
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    d.frame('frame-1', HEAD, AHEAD, [a.p])
    expect(names(w.calls)).toEqual(['createQuadLayer', 'place'])
    expect((w.calls[0][2] as QuadLayerInit).viewPixelWidth).toBe(2048)
    expect((w.calls[0][2] as QuadLayerInit).space).toBe('reference-space')
    expect(w.list).toHaveLength(1)
    expect(w.list[0].object3D).toBe('mesh-of-a')
    expect(a.told).toHaveLength(1)
    // The layer has no picture yet: the page's mesh goes on showing the page.
    expect([...d.onLayer]).toEqual([])
    // The source hands the picture over; the next frame writes it, and the mesh then draws for depth only.
    const closed: string[] = []
    a.told[0]!.offer(picture('first', closed))
    w.calls.length = 0
    d.frame('frame-2', HEAD, AHEAD, [a.p])
    expect(names(w.calls)).toEqual(['place', 'getSubImage', 'bindTexture', 'texSubImage2D'])
    expect([...d.onLayer]).toEqual(['a'])
    expect(d.writes()).toEqual({ a: 1 })
    // A page at rest: only its place is looked at, and no picture is written.
    w.calls.length = 0
    for (let i = 3; i < 60; i += 1) d.frame(`frame-${i}`, HEAD, AHEAD, [a.p])
    expect(new Set(names(w.calls))).toEqual(new Set(['place']))
    expect(d.writes()).toEqual({ a: 1 })
  })

  it('gives layers to the pages nearest the gaze only, and leaves the others on their meshes', () => {
    const w = world(['layers'], 3)
    const ring = [0, 60, -60, 120].map((yaw, i) => panel(`p${i}`, [Math.sin((yaw * Math.PI) / 180) * 1.8, 1.6, -Math.cos((yaw * Math.PI) / 180) * 1.8]))
    const d = new LayerDirector(w.host)
    d.frame('frame', HEAD, AHEAD, ring.map(r => r.p))
    // A limit of three is two pages and the scene.
    expect(w.list).toHaveLength(2)
    expect(ring.map(r => r.told.length)).toEqual([1, 1, 0, 0])
  })

  it('takes a page\'s layer out of the list when the page leaves the view, sends its pictures back to the mesh, and frees the layer a frame later', () => {
    const w = world()
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    d.frame('frame-1', HEAD, AHEAD, [a.p])
    a.told[0]!.offer(picture('first', []))
    d.frame('frame-2', HEAD, AHEAD, [a.p])
    a.state.inView = false
    w.calls.length = 0
    d.frame('frame-3', HEAD, AHEAD, [a.p])
    expect(w.list).toEqual([])
    expect(a.told[1]).toBeNull()
    expect([...d.onLayer]).toEqual([])
    // Not destroyed in the frame that still had it in the session's list.
    expect(names(w.calls)).not.toContain('destroy')
    d.frame('frame-4', HEAD, AHEAD, [a.p])
    expect(w.calls).toContainEqual(['destroy', 'L1'])
  })

  it('makes a new layer when the picture\'s size changes, and the old one leaves the list first', () => {
    const w = world()
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    d.frame('frame-1', HEAD, AHEAD, [a.p])
    a.told[0]!.offer(picture('big', []))
    d.frame('frame-2', HEAD, AHEAD, [a.p])
    a.state.size = [1434, 1008]
    w.log.length = 0
    d.frame('frame-3', HEAD, AHEAD, [a.p])
    expect(w.log).toEqual(['remove L1', 'add L2'])
    expect(a.told).toHaveLength(2)
    expect([a.told[1]!.width, a.told[1]!.height]).toEqual([1434, 1008])
    // Until the new layer has a picture, the mesh shows the page.
    expect([...d.onLayer]).toEqual([])
  })

  it('orders the layers with the farthest first', () => {
    const w = world()
    const near = panel('near', [0.2, 1.6, -1.0])
    const far = panel('far', [-0.2, 1.6, -4.0])
    const d = new LayerDirector(w.host)
    d.frame('frame', HEAD, AHEAD, [near.p, far.p])
    const order = Object.fromEntries(w.list.map(e => [e.object3D as string, e.renderOrder]))
    expect(order['mesh-of-far']).toBeLessThan(order['mesh-of-near'])
  })

  it('gives every layer back when the session ends', () => {
    const w = world()
    const a = panel('a', [0, 1.6, -1.8])
    const d = new LayerDirector(w.host)
    d.frame('frame-1', HEAD, AHEAD, [a.p])
    w.state.session = null
    d.frame(null, HEAD, AHEAD, [a.p])
    expect(w.list).toEqual([])
    expect(a.told[1]).toBeNull()
    d.dispose()
    expect(w.calls).toContainEqual(['destroy', 'L1'])
  })

  it('leaves a page with no picture, a still or a sheet, on its mesh', () => {
    const w = world()
    const blank = panel('blank', [0, 1.6, -1.8], { size: null })
    const sheet: LayerPanel = { id: 'sheet', source: null, position: () => [0.5, 1.6, -1.8], inView: () => true, anchor: 'mesh-of-sheet' }
    const d = new LayerDirector(w.host)
    d.frame('frame', HEAD, AHEAD, [blank.p, sheet])
    expect(w.list).toEqual([])
    expect(names(w.calls)).toEqual([])
  })
})
