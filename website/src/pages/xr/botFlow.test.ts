import { describe, expect, it } from 'vitest'
import { Bone, BufferGeometry, Float32BufferAttribute, Group, MeshBasicMaterial, Skeleton, SkinnedMesh, Uint16BufferAttribute, Vector3 } from 'three'
import { bendAt, FLOW, flowInto, Follower, Pace, WaveFlow } from './botFlow'

describe('the bend of the lower body', () => {
  it('takes nothing at the top, the middle\'s lag half way down and the hem\'s at the hem', () => {
    const near = (got: [number, number], want: [number, number]) => { expect(got[0]).toBeCloseTo(want[0], 12); expect(got[1]).toBeCloseTo(want[1], 12) }
    near(bendAt(0), [0, 0])
    near(bendAt(0.5), [1, 0])
    near(bendAt(1), [0, 1])
  })

  it('starts with no bend at all, so the outline has no corner where the giving starts', () => {
    const [a, b] = bendAt(0.01)
    expect(Math.abs(a)).toBeLessThan(1e-4)
    expect(Math.abs(b)).toBeLessThan(1e-4)
  })
})

describe('a follower', () => {
  /** Carry a follower behind a body that goes at `speed` along x for `frames` frames of `dt`. Returns how far behind it is. */
  function lagBehind(f: Follower, speed: number, dt: number, frames: number, from = 0): number {
    const to = new Vector3()
    for (let i = 0; i <= frames; i += 1) { to.set(from + speed * i * dt, 0, 0); f.step(to, dt) }
    return to.x - f.x.x
  }

  it.each([
    ['the middle', FLOW.midHz, FLOW.midAir],
    ['the hem', FLOW.hemHz, FLOW.hemAir],
    ['the flick', FLOW.flickHz, FLOW.flickAir],
  ] as const)('%s lags behind a steady body by speed x air / (pi x swings)', (_name, hz, air) => {
    // Frames of a thousandth of a second: the body then moves as good as steadily, which is what the rule is for.
    const speed = 1.4
    expect(lagBehind(new Follower(hz, air), speed, 1 / 1000, 6000)).toBeCloseTo(speed * air / (Math.PI * hz), 2)
  })

  it.each([30, 60, 90])('swings on when the body stops and settles at its place, at %i frames a second', rate => {
    const f = new Follower(FLOW.hemHz, FLOW.hemAir), dt = 1 / rate, speed = 1.4
    const behind = lagBehind(f, speed, dt, 3 * rate)
    expect(behind).toBeGreaterThan(0.05)
    // The body stands where it stopped. The follower goes past its place, comes back, and is still.
    const stop = new Vector3(speed * 3, 0, 0)
    let past = 0
    for (let i = 0; i < 4 * rate; i += 1) { f.step(stop, dt); past = Math.max(past, f.x.x - stop.x) }
    expect(past).toBeGreaterThan(0.1 * behind)
    expect(past).toBeLessThan(behind)
    expect(Math.abs(f.x.x - stop.x)).toBeLessThan(1e-3)
  })

  it('starts at its place, and is held near it when the body is put somewhere else', () => {
    const f = new Follower(FLOW.hemHz, FLOW.hemAir)
    f.step(new Vector3(1, 2, 3), 1 / 60)
    expect(f.x.toArray()).toEqual([1, 2, 3])
    const far = new Vector3(1.5, 2, 3)
    f.hold(far, 0.3)
    expect(f.x.distanceTo(far)).toBeCloseTo(0.3, 9)
    // Put much further than a body can go in a frame: it is there at once.
    const gone = new Vector3(50, 2, 3)
    f.hold(gone, 0.3)
    expect(f.x.toArray()).toEqual([50, 2, 3])
  })
})

describe('the lower body at the pace it moves (M89)', () => {
  const ease = (u: number) => { const c = Math.min(1, Math.max(0, u)); return c * c * (3 - 2 * c) }
  /**
   * A body one unit tall put along x as the room of places walks it (`length` body heights in `ms`, eased), at `rate`
   * frames a second, with a hem that hangs from a middle as `WaveFlow.sense` hangs it. Returns the hem's most trail
   * while it moves, and after the stop how far it swings past its place and how often it crosses it.
   */
  function walk(length: number, ms: number, rate: number, fitted = true, moved = false) {
    const mid = new Follower(FLOW.midHz, FLOW.midAir, 1), hem = new Follower(FLOW.hemHz, FLOW.hemAir, 2), pace = new Pace()
    const dt = 1 / rate, drop = new Vector3(...FLOW.hem).sub(new Vector3(...FLOW.mid)), at = new Vector3(), to = new Vector3()
    let trail = 0, past = 0, crossings = 0, side = 0
    for (let wall = 0; wall < ms + 4000; wall += 1000 * dt) {
      const x = length * ease(wall / ms), fit = pace.feel(x, 0, dt, 1, FLOW, moved)
      at.set(x, 0, 0)
      mid.step(at, dt, 0, 0, fitted ? fit : undefined); mid.hold(at, FLOW.most)
      hem.step(to.copy(drop).add(mid.x), dt, 0, 0, fitted ? fit : undefined); hem.hold(to, FLOW.most)
      const lag = hem.x.x - (x + drop.x)
      if (wall <= ms) trail = Math.max(trail, -lag)
      else {
        past = Math.max(past, lag)
        const s = Math.abs(lag) < 0.004 ? 0 : Math.sign(lag)
        if (s && side && s !== side) crossings += 1
        if (s) side = s
      }
    }
    return { trail, past, crossings }
  }

  it('fits nothing at rest or at the land\'s walk, and fully at the room\'s walk', () => {
    const pace = new Pace()
    for (let i = 0; i < 60; i += 1) expect(pace.feel(0, 0, 1 / 60, 1).share).toBe(0)
    const land = new Pace()
    for (let i = 0; i < 120; i += 1) expect(land.feel((1.38 * i) / 60, 0, 1 / 60, 1).share).toBe(0)
    const room = new Pace()
    let fit = room.feel(0, 0, 1 / 60, 1)
    for (let i = 1; i < 30; i += 1) fit = room.feel((3.33 * i) / 60, 0, 1 / 60, 1)
    expect(fit.share).toBe(1)
    expect(fit.mps).toBeCloseTo(FLOW.fitMps, 9)
    // After the stop the pace fades: half a second on it still fits, two seconds on it does not.
    for (let i = 0; i < 30; i += 1) fit = room.feel(3.33 * 0.5, 0, 1 / 60, 1)
    expect(fit.share).toBeGreaterThan(0)
    for (let i = 0; i < 90; i += 1) fit = room.feel(3.33 * 0.5, 0, 1 / 60, 1)
    expect(fit.share).toBe(0)
  })

  it('reads no pace from a body that was put far away at once', () => {
    const pace = new Pace()
    pace.feel(0, 0, 1 / 60, 1)
    expect(pace.feel(5, 0, 1 / 60, 1).share).toBe(0)
    expect(pace.felt).toBe(0)
  })

  it.each([60, 30, 15, 8])('trails at most 0.25 at the room\'s walk, swings past at most 0.06 and crosses once, at %i frames a second', rate => {
    const got = walk(3.33, 1000, rate)
    expect(got.trail).toBeLessThanOrEqual(0.25)
    expect(got.past).toBeLessThanOrEqual(0.06)
    expect(got.crossings).toBeLessThanOrEqual(1)
    // Unfitted, the same walk trails twice as far and swings on.
    const before = walk(3.33, 1000, rate, false)
    expect(before.trail).toBeGreaterThan(0.4)
    // The trail is the same at every frame rate, within a fifth.
    expect(Math.abs(got.trail - walk(3.33, 1000, 60).trail)).toBeLessThan(0.2 * got.trail)
  })

  it.each([[0.1, 400], [0.15, 400], [0.2, 400], [0.5, 1000], [1.67, 1000]])('settles within the bars after any walk the room makes: %f body heights in %i ms (M101)', (length, ms) => {
    // 0.15 m of a Bot 0.3 m tall is half a body height; the room paces a short way at 400 ms at least.
    const got = walk(length / 0.3 * 0.3, ms, 60, true, true)
    expect(got.trail).toBeLessThanOrEqual(0.3)
    expect(got.past).toBeLessThanOrEqual(0.08)
    expect(got.crossings).toBeLessThanOrEqual(2)
  })

  it('is the same follower as before at the land\'s walk: the fit then changes nothing', () => {
    const a = new Follower(FLOW.hemHz, FLOW.hemAir), b = new Follower(FLOW.hemHz, FLOW.hemAir), pace = new Pace(), to = new Vector3()
    for (let i = 0; i < 200; i += 1) {
      const x = i < 120 ? (1.38 * i) / 60 : 1.38 * 2
      to.set(x, 0, 0)
      a.step(to, 1 / 60, 0, 0, pace.feel(x, 0, 1 / 60, 1)); b.step(to, 1 / 60)
      expect(a.x.x).toBe(b.x.x)
    }
  })
})

describe('the bend in a material', () => {
  const SHADER = '#include <common>\nvoid main() {\n#include <beginnormal_vertex>\n#include <begin_vertex>\n}'
  const compile = (m: MeshBasicMaterial) => {
    const shader = { uniforms: {} as Record<string, unknown>, vertexShader: SHADER, fragmentShader: '' }
    m.onBeforeCompile(shader as never, null as never)
    return shader
  }

  it('goes into the vertex shader once, however often it is asked for', () => {
    const m = new MeshBasicMaterial()
    const u = flowInto(m)
    expect(flowInto(m)).toBe(u)
    const shader = compile(m)
    expect(shader.vertexShader.match(/vec3 flowAt\(/g)).toHaveLength(1)
    expect(shader.vertexShader.match(/transformed = flowP;/g)).toHaveLength(1)
    expect(shader.uniforms.flowMid).toBe(u.flowMid)
  })

  it('gives the flat art the body\'s own numbers', () => {
    const body = new MeshBasicMaterial(), art = new MeshBasicMaterial()
    const u = flowInto(body)
    expect(flowInto(art, FLOW, u)).toBe(u)
    expect(compile(art).uniforms.flowHem).toBe(u.flowHem)
    expect(art.customProgramCacheKey()).not.toBe(new MeshBasicMaterial().customProgramCacheKey())
  })

  it('refuses a shader that has no place for it', () => {
    const m = new MeshBasicMaterial()
    flowInto(m)
    expect(() => m.onBeforeCompile({ uniforms: {}, vertexShader: 'void main(){}', fragmentShader: '' } as never, null as never)).toThrow()
  })
})

describe('the two flicks of a body', () => {
  /** A body of three points: one on the belly, and the tip of a flick on each side. `two` gives it the second flick's bone. */
  function bot(two: boolean) {
    const names = two ? ['belly', 'head', 'hem', 'flick', 'flick2'] : ['belly', 'head', 'hem', 'flick']
    const at: Record<string, [number, number, number]> = { belly: [0, 0.4, 0], head: [0, 0.6, 0], hem: [0, 0.24, 0], flick: [-0.38, 0.47, 0], flick2: [0.38, 0.47, 0] }
    const bones = names.map(n => { const b = new Bone(); b.name = n; b.position.set(...at[n]); return b })
    const root = new Group()
    for (const b of bones) root.add(b)
    const g = new BufferGeometry()
    g.setAttribute('position', new Float32BufferAttribute([0, 0.4, 0, -0.45, 0.25, 0, 0.45, 0.25, 0], 3))
    g.setAttribute('skinIndex', new Uint16BufferAttribute([0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, two ? 4 : 3], 4))
    g.setAttribute('skinWeight', new Float32BufferAttribute([1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, two ? 1 : 0], 4))
    const body = new SkinnedMesh(g, new MeshBasicMaterial())
    root.add(body)
    root.updateWorldMatrix(true, true)
    body.bind(new Skeleton(bones))
    const pick = (n: string) => bones.find(b => b.name === n) ?? null
    return { root, flow: new WaveFlow({ root, body, belly: bones[0], flick: pick('flick'), flick2: pick('flick2') }) }
  }
  /** Carry the body forward for a second, and read how far each flick's tip is behind. */
  function carried(two: boolean) {
    const { root, flow } = bot(two)
    flow.step(1 / 60, 'rest')
    for (let i = 1; i <= 60; i += 1) { root.position.z = i * 0.03; flow.step(1 / 60, 'rest') }
    return flow
  }

  it('hangs the second from the mirror place of the first, and each tip moves on its own when the body is carried', () => {
    const { u } = carried(true)
    expect(u.flowHinge2.value.x).toBeCloseTo(-u.flowHinge.value.x, 6)
    expect(u.flowHinge2.value.w).toBeCloseTo(u.flowHinge.value.w, 6)
    // The air sways each tip in its own time, so the two are not each other's mirror at one moment: each has moved.
    expect(u.flowFlick.value.length()).toBeGreaterThan(1e-4)
    expect(u.flowFlick2.value.length()).toBeGreaterThan(1e-4)
  })

  it('leaves the second at rest in a body file that has one flick, and puts both back with reduced motion', () => {
    expect(carried(false).u.flowFlick2.value.length()).toBe(0)
    const flow = carried(true)
    flow.rest()
    expect(flow.u.flowFlick.value.length() + flow.u.flowFlick2.value.length()).toBe(0)
  })
})
