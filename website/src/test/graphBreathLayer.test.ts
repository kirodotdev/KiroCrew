import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { BreathLayer, breathView } from '../pages/overview/memoryGraph/graphBreathLayer'

/** A WebGL 2 stand-in that counts the calls a breath step would cost. */
function fakeGl() {
  const calls: string[] = []
  const gl = new Proxy({}, {
    get(_t, name: string) {
      if (name === 'getShaderParameter' || name === 'getProgramParameter') return () => true
      if (name === 'getAttribLocation') return () => 0
      if (name === 'getExtension') return () => null
      if (/^[A-Z_0-9]+$/.test(name)) return name
      return (...args: unknown[]) => { calls.push(name === 'pixelStorei' ? `pixelStorei ${args.join(' ')}` : name); return {} }
    },
  })
  return { gl, calls }
}

afterEach(() => { vi.restoreAllMocks() })

describe('the breath layer', () => {
  it('maps the canvas to the layout box the camera shows', () => {
    expect(breathView({ cx: 10, cy: -4, k: 2 }, { width: 400, height: 200 })).toEqual({ x0: -90, y0: -54, w: 200, h: 100 })
  })

  it('is absent without WebGL 2, so the point shader breathes instead', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    expect(BreathLayer.create(document.createElement('div'))).toBeNull()
  })

  it('copies a drawn frame once and draws one quad per breath step, with no buffer upload', () => {
    const { gl, calls } = fakeGl()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(gl as never)
    const host = document.createElement('div')
    const source = Object.assign(document.createElement('canvas'), { width: 8, height: 4 })
    host.appendChild(source)
    document.body.appendChild(host)
    const layer = BreathLayer.create(host)!
    onTestFinished(() => { layer.dispose(); host.remove() })
    expect(host.querySelector('[data-testid="memory-graph-breath"]')).not.toBeNull()
    expect(source).toBeVisible()
    expect(layer.canvas).not.toBeVisible()
    // Kept upright and premultiplied, as cosmos.gl's canvas is.
    expect(calls).toContain('pixelStorei UNPACK_FLIP_Y_WEBGL true')
    expect(calls).toContain('pixelStorei UNPACK_PREMULTIPLY_ALPHA_WEBGL true')
    const setup = calls.length
    const params = { amp: 0.035, phase: 1, perUnit: 0.1, cx: 0, cy: 0, view: { x0: 0, y0: 0, w: 10, h: 10 }, background: [1, 1, 1] as [number, number, number] }
    // Nothing kept yet: even a breath tick must leave cosmos.gl unobscured.
    layer.draw(params)
    expect(calls.slice(setup)).toEqual([])
    expect(layer.canvas).not.toBeVisible()
    expect(source).toBeVisible()
    layer.capture(source)
    expect(calls.slice(setup).filter(c => c === 'texImage2D')).toHaveLength(1)
    expect(layer.canvas).not.toBeVisible()
    const kept = calls.length
    for (let i = 0; i < 20; i++) layer.draw({ ...params, phase: i })
    expect(layer.canvas).toBeVisible()
    const step = calls.slice(kept)
    expect(step.filter(c => c === 'drawArrays')).toHaveLength(20)
    expect(step.filter(c => /^(bufferData|bufferSubData|texImage2D|texSubImage2D|drawArraysInstanced|drawElements)/.test(c))).toEqual([])
    layer.clear()
    expect(layer.canvas).not.toBeVisible()
    const cleared = calls.length
    layer.draw(params)
    expect(calls.slice(cleared)).toEqual([])
    expect(layer.canvas).not.toBeVisible()
    // A fresh, resized frame can make the layer visible again.
    source.width = 16
    layer.capture(source); layer.draw(params)
    expect(layer.canvas.width).toBe(16)
    expect(layer.canvas).toBeVisible()
    source.height = 0
    layer.capture(source)
    expect(layer.canvas).not.toBeVisible()
    const empty = calls.length
    layer.draw(params)
    expect(calls.slice(empty)).toEqual([])
    layer.dispose()
    expect(host.children).toHaveLength(1)
    expect(host.firstChild).toBe(source)
  })
})
