import { describe, expect, it, vi } from 'vitest'
import { ACTIVITY_GLSL } from '../../scripts/cosmos-graph-activity.mjs'
import { byCluster, spread, lightAt, lightEnd, flashAt, afterglow, HitQueue, SPREAD_MS, STRIKE_MS, FADE_MS, TOTAL_MS } from '../pages/overview/memoryGraph/graphActivity'
import { ActivityRunner, type RunnerDeps } from '../pages/overview/memoryGraph/graphActivityRunner'
const items = [{ id: 'a', x: 0, y: 0, cluster: 0 }, { id: 'b', x: 1, y: 0, cluster: 0 }, { id: 'c', x: 2, y: 0, cluster: 0 }, { id: 'd', x: 3, y: 0, cluster: 1 }]
const input = { hit: 0, now: 0, items, members: byCluster(items) }
describe('one outward activation, flashed then faded together', () => {
  it('overshoots once and has exactly two weaker brightness re-strikes', () => {
    expect(flashAt(-1)).toBe(0)
    expect(flashAt(20)).toBeGreaterThan(0.95)
    expect(flashAt(40)).toBeGreaterThan(1.08)
    expect(flashAt(60)).toBe(1)
    expect(flashAt(100)).toBe(0.22)
    expect(flashAt(140)).toBeCloseTo(0.72)
    expect(flashAt(200)).toBe(0.18)
    expect(flashAt(240)).toBeCloseTo(0.5)
    expect(flashAt(320)).toBeCloseTo(0.25)
    expect(flashAt(3320)).toBe(0)
    expect([SPREAD_MS, STRIKE_MS, FADE_MS, TOTAL_MS]).toEqual([200, 320, 3000, 3520])
  })
  it('shares a fast radial spread and three-second exponential afterglow', () => {
    const result = spread(input), lights = [...result.lights.values()]
    expect([...result.lights.keys()]).toEqual([0, 1, 2])
    expect(lights.map(l => l.at)).toEqual([0, 60, 120])
    expect(lightAt(lights[0], 0)).toBeCloseTo(0.999)
    expect(lightAt(lights[0], -1)).toBe(0)
    expect(lightAt(lights[1], 40)).toBe(0)
    expect(lightAt(lights[1], 100)).toBeGreaterThan(0)
    expect(lightAt(lights[2], 100)).toBe(0)
    expect(lights.map(l => lightAt(l, 200))).toEqual([0.999, 0.999, 0.999])
    for (const dt of [0, 200, 500, 1000, 1500, 2400, 3000]) {
      const values = lights.map(l => lightAt(l, 520 + dt))
      expect(values[0]).toBeCloseTo(0.999 * afterglow(dt / 3000))
      expect(values[1]).toBeCloseTo(values[0]); expect(values[2]).toBeCloseTo(values[0])
    }
    expect(afterglow(0.5)).toBeCloseTo(0.2227)
    expect(new Set(lights.map(lightEnd))).toEqual(new Set([3520]))
    expect(result.end).toBe(3520)
  })
  it('keeps the immediate seed compact', () => {
    const dense = Array.from({ length: 100 }, (_, i) => ({ x: i, y: 0, cluster: 0 }))
    const result = spread({ hit: 0, now: 0, items: dense, members: byCluster(dense) })
    expect([...result.lights.values()].filter(l => l.instant)).toHaveLength(5)
    expect([...result.lights.values()].filter(l => lightAt(l, 0) > 0)).toHaveLength(5)
  })
  it('retains the 20 hits per second bound and coalesces queued IDs', () => {
    const queue = new HitQueue()
    queue.push(Array.from({ length: 30 }, (_, i) => String(i)), 'recall', 0)
    expect(queue.take(0)).toHaveLength(20)
    queue.push(['a', 'a', 'b'], 'recall', 0)
    expect(queue.take(0)).toHaveLength(0)
    expect(queue.take(50)).toHaveLength(1)
    expect(queue.take(100)).toHaveLength(1)
    queue.clear(); expect(queue.size).toBe(0)
  })
  it('uploads once for 23k points and requests only one draw per active step', () => {
    let now = 0, tick: FrameRequestCallback = () => {}
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { tick = cb; return 1 })
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    const upload = vi.fn(), render = vi.fn()
    const store = [...items, ...Array.from({ length: 23006 }, (_, i) => ({ id: `other${i}`, x: i, y: 10, cluster: 1 }))]
    const runner = new ActivityRunner({
      graph: () => ({ setPointActivity: upload, requestRender: render }),
      shown: () => ({ mode: 'nodes', items: store }), layout: () => 'L1',
      bounds: () => [0, -1, 23010, 10], dark: () => false, movedAt: () => -1000, now: () => now,
    } as unknown as RunnerDeps)
    try {
      runner.setRunning(true)
      runner.push({ store: '', kind: 'recall', ids: ['a', 'b'], ts: 0 })
      tick(0)
      expect(upload).toHaveBeenCalledTimes(1)
      expect(upload.mock.calls[0][0]).toHaveLength(23010)
      expect([...upload.mock.calls[0][0]].slice(0, 4)).toEqual([0, 60, 120, -1])
      for (now = 20; now <= 3300; now += 20) { const before = render.mock.calls.length; tick(now); expect(render.mock.calls.length - before).toBe(1) }
      expect(upload).toHaveBeenCalledTimes(1)
      runner.push({ store: '', kind: 'recall', ids: ['a'], ts: 4 })
      now = 3320; tick(now)
      expect(upload).toHaveBeenCalledTimes(2)
      expect(upload.mock.calls[1][0][0]).toBe(3320)
      runner.setRunning(false)
      expect(runner.uniforms.clock[2]).toBe(0)
      expect(runner.uniforms.light[2]).toBe(0)
      const stopped = render.mock.calls.length
      now = 3340; tick(now)
      expect(render).toHaveBeenCalledTimes(stopped)
    } finally { runner.dispose(); raf.mockRestore(); cancel.mockRestore() }
  })
  it('retains hue, overshoot and exponential decay without overlay geometry', () => {
    expect(ACTIVITY_GLSL).toContain('age - riseMs - hold')
    expect(ACTIVITY_GLSL).toContain('imageSize < 0.0 ? step(0.0, age)')
    expect(ACTIVITY_GLSL).toContain('float lightness = (high + low) * 0.5')
    expect(ACTIVITY_GLSL).toContain('vec3(lightness) + chroma * (hue - 0.5)')
    expect(ACTIVITY_GLSL).toContain('(exp(-2.5 * fade) - exp(-2.5)) / (1.0 - exp(-2.5))')
    expect(ACTIVITY_GLSL).toContain('2.70158 * p * p * p + 1.70158 * p * p')
    expect(ACTIVITY_GLSL).toContain('0.36 * sizeFlash')
    expect(ACTIVITY_GLSL).not.toContain('wave')
    expect(ACTIVITY_GLSL).not.toContain('kcBloom')
  })
})
