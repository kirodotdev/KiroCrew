import { describe, expect, it } from 'vitest'
import type { BotPose } from './body'
import { BotMotion, BotWalk, HOP, POSES, POSE_FOR_STATE, Spring, poseFor, rng, seedOf } from './botMotion'

const POSE_NAMES = Object.keys(POSES) as BotPose[]
const FLAGS = { walking: false, speaking: false, still: false }
const asked = { moving: false, carrying: false, listening: false, aiming: false, forced: null, sleeping: false, state: 'idle' as const }

/** Run a Bot for `seconds` at `fps` in one pose, and keep every frame. */
function run(seed: number, pose: BotPose, seconds: number, fps = 72, flags = FLAGS) {
  const m = new BotMotion(seed)
  return Array.from({ length: Math.round(seconds * fps) }, () => m.step(1 / fps, pose, flags))
}

describe('the pose that follows from what was asked', () => {
  it('takes the pose of the state when nothing else is asked', () => {
    expect(poseFor(asked)).toBe('rest')
    expect(poseFor({ ...asked, state: 'working' })).toBe(POSE_FOR_STATE.working)
    expect(poseFor({ ...asked, state: 'needs' })).toBe('ask')
    expect(poseFor({ ...asked, state: 'failed' })).toBe('slump')
  })

  it('puts moving and carrying first, then listening, then pointing, then a pose that was asked for, then a nap', () => {
    const all = { moving: true, carrying: true, listening: true, aiming: true, forced: 'think' as const, sleeping: true, state: 'needs' as const }
    expect(poseFor(all)).toBe('stand')
    expect(poseFor({ ...all, moving: false })).toBe('stand')
    expect(poseFor({ ...all, moving: false, carrying: false })).toBe('listen')
    expect(poseFor({ ...all, moving: false, carrying: false, listening: false })).toBe('point')
    expect(poseFor({ ...all, moving: false, carrying: false, listening: false, aiming: false })).toBe('think')
    expect(poseFor({ ...all, moving: false, carrying: false, listening: false, aiming: false, forced: null })).toBe('sleep')
  })
})

describe('one seed per Bot', () => {
  it('gives a different seed to each name, and the same one to the same name', () => {
    const names = ['atlas', 'juno', 'mira', 'otto', 'pico', 'sage']
    expect(new Set(names.map(seedOf)).size).toBe(names.length)
    expect(seedOf('atlas')).toBe(seedOf('atlas'))
    const a = rng(seedOf('atlas'))
    const b = rng(seedOf('atlas'))
    expect([a(), a(), a()]).toEqual([b(), b(), b()])
  })

  it('moves the same Bot the same way twice', () => {
    expect(run(seedOf('juno'), 'rest', 20)).toEqual(run(seedOf('juno'), 'rest', 20))
  })

  it('never has six Bots blink in step: no frame of two minutes has every eye shut', () => {
    const bots = ['atlas', 'juno', 'mira', 'otto', 'pico', 'sage'].map(n => run(seedOf(n), 'rest', 120))
    const blinks = bots.map(frames => frames.filter((f, i) => f.blink >= 1 && frames[i - 1]?.blink < 1).length)
    // Each Bot blinks about every three seconds, some twice in a row.
    for (const n of blinks) { expect(n).toBeGreaterThan(20); expect(n).toBeLessThan(60) }
    const allShut = bots[0].filter((_f, i) => bots.every(frames => frames[i].blink > 0.5)).length
    expect(allShut).toBe(0)
    // Two Bots do not glance the same way at the same time either.
    const same = bots[0].filter((f, i) => Math.abs(f.lookX - bots[1][i].lookX) < 1e-6 && Math.abs(f.lookX) > 0.05).length
    expect(same).toBe(0)
  })
})

describe('each pose is a place the springs come to rest', () => {
  it.each(POSE_NAMES)('%s: the body settles at its rest, and keeps its volume on the way', pose => {
    const frames = run(7, pose, 6)
    const rest = POSES[pose]
    for (const f of frames) {
      expect(f.up * f.across * f.across).toBeCloseTo(1, 9)
      expect(f.blink).toBeGreaterThanOrEqual(0)
      expect(f.blink).toBeLessThanOrEqual(1)
      expect(Math.abs(f.lookX)).toBeLessThanOrEqual(1.25)
    }
    const last = frames[frames.length - 1]
    expect(last.lean).toBeCloseTo(rest.lean, 2)
    expect(last.tilt).toBeCloseTo(rest.tilt, 2)
    expect(last.wide).toBeCloseTo(rest.wide, 2)
    // The breath and the bob stay small: the body is where the pose puts it.
    expect(Math.abs(last.lift - rest.lift)).toBeLessThanOrEqual(rest.bob + 1e-3)
    expect(Math.abs(1 - last.up - rest.squash)).toBeLessThan(0.02)
    if (pose === 'sleep') expect(frames.every(f => f.blink <= 1) && last.blink).toBeCloseTo(1, 2)
  })

  it('starts a change from what is on screen: no value jumps between two frames', () => {
    const m = new BotMotion(3)
    let prev = m.step(1 / 72, 'rest', FLAGS)
    for (let i = 0; i < 72 * 8; i += 1) {
      const pose = POSE_NAMES[Math.floor(i / 60) % POSE_NAMES.length]
      const f = m.step(1 / 72, pose, FLAGS)
      expect(Math.abs(f.lean - prev.lean)).toBeLessThan(0.05)
      expect(Math.abs(f.lift - prev.lift)).toBeLessThan(0.02)
      expect(Math.abs(f.up - prev.up)).toBeLessThan(0.03)
      prev = f
    }
  })

  it('goes to its rest at once, and stays still, when motion is reduced', () => {
    const frames = run(11, 'ask', 3, 72, { ...FLAGS, still: true })
    for (const f of frames) {
      expect(f.lift).toBe(POSES.ask.lift)
      expect(f.lean).toBe(POSES.ask.lean)
      expect(f.blink).toBe(0)
      expect(f.lookX).toBe(0)
    }
  })
})

describe('the hop, the nod and the hem', () => {
  it('hops once: up to its rise inside its time, then a wobble that dies away', () => {
    const m = new BotMotion(5)
    for (let i = 0; i < 72 * 3; i += 1) m.step(1 / 72, 'stand', FLAGS)
    const calm = m.step(1 / 72, 'stand', FLAGS).lift
    m.hop()
    const frames = Array.from({ length: 72 * 3 }, () => m.step(1 / 72, 'stand', FLAGS))
    const peak = Math.max(...frames.map(f => f.lift))
    expect(peak - calm).toBeGreaterThan(HOP.rise * 0.9)
    expect(peak - calm).toBeLessThan(HOP.rise * 1.1)
    expect(frames.findIndex(f => f.lift === peak) / 72).toBeLessThan(HOP.ms / 1000)
    // It squashes as it lands, and is calm again two and a half seconds on.
    expect(Math.min(...frames.slice(30, 72).map(f => f.up))).toBeLessThan(0.99)
    expect(Math.abs(frames[frames.length - 1].lift - calm)).toBeLessThan(0.02)
  })

  it('hops lower when its work ends than when it takes a card', () => {
    const peak = (kick: (m: BotMotion) => void) => {
      const m = new BotMotion(5)
      for (let i = 0; i < 72 * 3; i += 1) m.step(1 / 72, 'rest', FLAGS)
      kick(m)
      return Math.max(...Array.from({ length: 72 }, () => m.step(1 / 72, 'rest', FLAGS).lift))
    }
    expect(peak(m => m.done())).toBeLessThan(peak(m => m.hop()) - HOP.rise * 0.3)
  })

  it('nods: the head goes down and comes back', () => {
    const m = new BotMotion(5)
    for (let i = 0; i < 72 * 3; i += 1) m.step(1 / 72, 'stand', FLAGS)
    m.nod()
    const heads = Array.from({ length: 72 * 3 }, () => m.step(1 / 72, 'stand', FLAGS).head)
    expect(Math.max(...heads) - POSES.stand.head).toBeGreaterThan(0.12)
    expect(heads[heads.length - 1]).toBeCloseTo(POSES.stand.head, 2)
  })

  it('lets the hem trail a body that sets off, and swing back when it stands', () => {
    const m = new BotMotion(5)
    for (let i = 0; i < 72 * 3; i += 1) m.step(1 / 72, 'stand', FLAGS)
    const going = Array.from({ length: 72 }, () => m.step(1 / 72, 'stand', { ...FLAGS, walking: true }))
    expect(going[going.length - 1].lean).toBeGreaterThan(0.1)
    expect(Math.min(...going.map(f => f.hem))).toBeLessThan(-0.2)
    const after = Array.from({ length: 72 * 3 }, () => m.step(1 / 72, 'stand', FLAGS))
    expect(Math.abs(after[after.length - 1].hem)).toBeLessThan(0.01)
  })

  it('a spring that is stepped late does not blow up', () => {
    const s = new Spring()
    for (let i = 0; i < 40; i += 1) s.step(1, 0.1, 3, 0.32)
    expect(s.x).toBeCloseTo(1, 2)
  })
})

describe('a Bot goes where it is sent', () => {
  it('walks in a straight line at its speed, faces the way it goes, and says when it is there', () => {
    const w = new BotWalk()
    let there = false
    w.walkTo(0.9, 0, () => { there = true })
    w.step(1, 0.45, 0, false)
    expect(w.walking).toBe(true)
    expect(w.x).toBeCloseTo(0.45, 6)
    expect(w.facing).toBeCloseTo(Math.PI / 2, 6)
    for (let i = 0; i < 4 && !there; i += 1) w.step(1, 0.45, 0, false)
    expect(there).toBe(true)
    expect(w.walking).toBe(false)
    expect([w.x, w.z]).toEqual([0.9, 0])
    // Standing, it turns back to the way it should face.
    for (let i = 0; i < 72; i += 1) w.step(1 / 72, 0.45, 0, false)
    expect(w.facing).toBeCloseTo(0, 2)
  })

  it('is there at once when motion is reduced', () => {
    const w = new BotWalk()
    w.walkTo(2, -3)
    w.step(1 / 72, 0.45, 0.5, true)
    expect([w.x, w.z, w.walking]).toEqual([2, -3, false])
    expect(w.facing).toBeCloseTo(0.5, 6)
  })

  it('halts where it stands at once, and the walk it dropped is never told done', () => {
    const w = new BotWalk()
    let done = 0
    w.walkTo(1, 0, () => { done += 1 })
    for (let i = 0; i < 20; i += 1) w.step(1 / 72, 0.45, 0, false)
    const x = w.x
    w.halt()
    w.step(1 / 72, 0.45, 0, false)
    expect([w.walking, w.moving, w.x, done]).toEqual([false, false, x, 0])
    expect(x).toBeGreaterThan(0)
  })
})
