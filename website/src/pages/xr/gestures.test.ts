import { describe, expect, it } from 'vitest'
import { BothHands, Dwell, KEY, KeyHold, PALM, PatDetector, palmForward, palmOver, palmToward, rollDeg, type OpenHand } from './gestures'
import type { V3 } from './hand'
import { TUNE } from './tune'

/** A pose that looks along -z, pitched up by `pitch` and then rolled to the right by `roll`, as a quaternion. */
function pose(rollD: number, pitchD = 0): [number, number, number, number] {
  const r = (-rollD * Math.PI) / 360
  const p = (pitchD * Math.PI) / 360
  // q = qPitch(about x) * qRoll(about z)
  const [px, pw] = [Math.sin(p), Math.cos(p)]
  const [rz, rw] = [Math.sin(r), Math.cos(r)]
  return [px * rw, -px * rz, pw * rz, pw * rw]
}

/** A deterministic wobble in -1..1, so "a hand that shakes" is the same on every run. */
const wobble = (i: number): number => Math.sin(i * 12.9898) * Math.cos(i * 4.1414)

describe('turning a key', () => {
  it('reads how far a hand is rolled about the way it points, whatever its pitch', () => {
    for (const pitch of [-50, 0, 35]) for (const roll of [-120, -70, -10, 0, 25, 90, 150]) expect(rollDeg(pose(roll, pitch))).toBeCloseTo(roll, 4)
  })

  it('clicks at a quarter turn, either way, and not before', () => {
    for (const dir of [1, -1]) {
      const k = new KeyHold(10)
      for (let d = 0; d < KEY.TURN_DEG; d += 5) { k.feed(10 + dir * d, 0, 0); expect(k.clicked).toBe(false) }
      expect(k.end).toBe('nothing')
      k.feed(10 + dir * KEY.TURN_DEG, 0, 0)
      expect(k.clicked).toBe(true)
      expect(k.end).toBe('turned')
    }
  })

  it('springs back when the hand lets go before the click: nothing happened', () => {
    const k = new KeyHold(0)
    k.feed(KEY.TURN_DEG - 6, 2, 0.01)
    expect(k.end).toBe('nothing')
    expect(k.turn).toBeCloseTo(KEY.TURN_DEG - 6)
  })

  it('never turns for a hand that only shakes, and never comes out for one', () => {
    // Two hundred frames of a held hand with a tremor of 12 degrees of roll, 4 degrees of aim and 2 cm of travel.
    const k = new KeyHold(0)
    // (Travel up to 2 cm and aim up to 4 degrees are under half the pull marks; roll up to 12 degrees is far under the click.)
    for (let i = 0; i < 200; i += 1) { k.feed(12 * wobble(i), 3.5 * Math.abs(wobble(i + 7)), 0.018 * Math.abs(wobble(i + 3))); expect(k.out).toBe(false) }
    expect(k.end).toBe('nothing')
  })

  it('comes out when the hand pulls away with the key still straight; let go there, that is the no', () => {
    const byRay = new KeyHold(0)
    byRay.feed(3, KEY.PULL_DEG, 0)
    expect(byRay.out).toBe(true)
    expect(byRay.end).toBe('pulled')
    const byHand = new KeyHold(0)
    byHand.feed(-4, 1, KEY.PULL_M)
    expect(byHand.end).toBe('pulled')
  })

  it('has done nothing while the hand still holds the key: pushed back into the lock, the request is as it was', () => {
    const k = new KeyHold(0)
    k.feed(0, 0, KEY.PULL_M * 1.5)
    expect(k.out).toBe(true)
    // A tremor at the mark does not put it back; half way back does.
    k.feed(0, 0, KEY.PULL_M * 0.9)
    expect(k.out).toBe(true)
    k.feed(0, 0, KEY.PULL_M * 0.4)
    expect(k.out).toBe(false)
    expect(k.end).toBe('nothing')
    // Back in its lock it can be turned after all, in the same hold.
    k.feed(KEY.TURN_DEG + 2, 0, KEY.PULL_M * 0.2)
    expect(k.end).toBe('turned')
  })

  it('has one meaning at a time: a key that is being turned cannot come out, and one that is out cannot turn', () => {
    const turning = new KeyHold(0)
    turning.feed(KEY.COMMIT_DEG + 5, 0, 0)
    turning.feed(KEY.COMMIT_DEG + 5, KEY.PULL_DEG * 2, KEY.PULL_M * 2)
    expect(turning.out).toBe(false)
    turning.feed(KEY.TURN_DEG + 1, KEY.PULL_DEG * 2, 0)
    expect(turning.end).toBe('turned')
    const out = new KeyHold(0)
    out.feed(0, KEY.PULL_DEG, 0)
    out.feed(KEY.TURN_DEG + 10, KEY.PULL_DEG, 0)
    expect(out.clicked).toBe(false)
    expect(out.end).toBe('pulled')
  })

  it('needs no reach: the turn is a wrist, and the pull is a few centimetres or a small swing of the hand', () => {
    expect(KEY.PULL_M).toBeLessThanOrEqual(0.05)
    expect(KEY.TURN_DEG).toBeLessThanOrEqual(90)
  })
})

/** A right hand at `at`, with its fingers along `fingers` and its palm facing `palm`. Open unless `curl` is set. */
function hand(at: V3, fingers: V3, palm: V3, curl = false): OpenHand {
  const add = (a: V3, b: V3, k: number): V3 => [a[0] + b[0] * k, a[1] + b[1] * k, a[2] + b[2] * k]
  // On a right hand, index x pinky is the palm normal: so the index side is fingers x palm.
  const side: V3 = [fingers[1] * palm[2] - fingers[2] * palm[1], fingers[2] * palm[0] - fingers[0] * palm[2], fingers[0] * palm[1] - fingers[1] * palm[0]]
  const base = add(at, fingers, 0.03)
  return {
    joints: { wrist: at, index: add(base, side, 0.03), pinky: add(base, side, -0.03) }, handed: 'right',
    shape: curl ? { index: 0.4, others: 0.4 } : { index: 0.97, others: 0.95 }, tip: add(at, fingers, 0.18),
  }
}

describe('an open hand: stop, or thanks', () => {
  const bot: V3 = [0, 0.8, -0.7]
  const chest: V3 = [0.1, 1.15, -0.3]
  const toBot = ((): V3 => { const d: V3 = [bot[0] - chest[0], bot[1] - chest[1], bot[2] - chest[2]]; const l = Math.hypot(...d); return [d[0] / l, d[1] / l, d[2] / l] })()

  it('reads a palm held up at a Bot, from chest height, also when the Bot is small and low on the desk', () => {
    // Fingers up, palm forward: the ordinary "stop" hand. The Bot is forward and below.
    expect(palmToward(hand(chest, [0, 1, 0], [0, 0, -1]), bot)).toBeGreaterThan(PALM.FACING)
    // Tilted to face the Bot squarely is better still.
    const up: V3 = [0, 0.75, -0.66]
    expect(palmToward(hand(chest, up, toBot), bot)).toBeGreaterThan(0.9)
  })

  it('does not read a fist, a hand with its fingers forward, or a palm that faces the person', () => {
    expect(palmToward(hand(chest, [0, 1, 0], [0, 0, -1], true), bot)).toBe(0)
    expect(palmToward(hand(chest, [0, 0, -1], [0, -1, 0]), bot)).toBe(0)
    expect(palmToward(hand(chest, [0, 1, 0], [0, 0, 1]), bot)).toBeLessThan(0)
  })

  it('counts a palm only once it has been held, and once per hold', () => {
    const d = new Dwell(PALM.HOLD_MS)
    const fired: number[] = []
    for (let t = 0; t <= 1000; t += 16) if (d.feed(true, t)) fired.push(t)
    expect(fired).toHaveLength(1)
    expect(fired[0]).toBeGreaterThanOrEqual(PALM.HOLD_MS)
    // A hand that passes through the pose for a few frames is not a palm.
    const e = new Dwell(PALM.HOLD_MS)
    let any = false
    for (let t = 0; t <= 2000; t += 16) any = e.feed(t % 300 < 200, t) || any
    expect(any).toBe(false)
  })

  it('reads a pat as two taps of a palm that is down on top of the Bot, and nothing else', () => {
    const top: V3 = [0, 0.8, -0.7]
    const reach = 0.05
    const over = (dy: number, dx = 0) => palmOver(hand([dx, 0.8 + dy, -0.7 + 0.03], [0, 0, -1], [0, -1, 0]), top, reach)
    expect(over(0.02)).toBeCloseTo(0.02, 2)
    expect(over(0.02, 0.2)).toBeNull()
    expect(palmOver(hand([0, 0.82, -0.67], [0, 1, 0], [0, 0, -1]), top, reach)).toBeNull()
    const pat = new PatDetector()
    const heights = [0.09, 0.02, 0.09, 0.02, 0.09]
    const fired = heights.map((h, i) => pat.feed(over(h), reach, i * 150))
    expect(fired).toEqual([false, false, false, true, false])
    // One tap, a hand that rests there, and two taps that are too far apart in time are not a pat.
    const one = new PatDetector()
    expect([0.09, 0.02, 0.02, 0.02, 0.02].map((h, i) => one.feed(over(h), reach, i * 150)).some(Boolean)).toBe(false)
    const slow = new PatDetector()
    expect([0.09, 0.02, 0.09, 0.02].map((h, i) => slow.feed(over(h), reach, i * (PALM.PAT_MS + 100))).some(Boolean)).toBe(false)
  })

  it('keeps the two apart: a palm at a Bot is not over it, and a palm down on it does not face it upright', () => {
    const top: V3 = [0, 0.8, -0.7]
    expect(palmOver(hand(chest, [0, 1, 0], [0, 0, -1]), top, 0.05)).toBeNull()
    expect(palmToward(hand([0, 0.83, -0.67], [0, 0, -1], [0, -1, 0]), top)).toBe(0)
  })
})

describe('H5: both palms pushed forward stop everyone, both hands pulled back carry on, one palm is neither', () => {
  const head: V3 = [0, 1.5, 0]
  /** The same hand as a left one: its index side and its little-finger side change places. */
  const asLeft = (h: OpenHand): OpenHand => ({ ...h, handed: 'left', joints: { wrist: h.joints.wrist, index: h.joints.pinky, pinky: h.joints.index } })
  /** Both hands up, palms forward, `z` metres in front of the head. */
  const palms = (z: number): { l: OpenHand; r: OpenHand; w: [V3, V3] } => {
    const [lw, rw]: [V3, V3] = [[-0.15, 1.2, -z], [0.15, 1.2, -z]]
    return { l: asLeft(hand(lw, [0, 1, 0], [0, 0, -1])), r: hand(rw, [0, 1, 0], [0, 0, -1]), w: [lw, rw] }
  }
  /** Raise both palms near the body, then push them out and hold them there until `until` ms. What was said, per feed. */
  const push = (both: BothHands, until: number): (string | null)[] => {
    const [near, out] = [palms(0.3), palms(0.4)]
    const said: (string | null)[] = [both.feed(near.l, near.r, near.w, head, 0), both.feed(near.l, near.r, near.w, head, 50)]
    for (let t = 100; t <= until; t += 50) said.push(both.feed(out.l, out.r, out.w, head, t))
    return said
  }

  it('reads a palm that faces away from the head, on either hand', () => {
    const p = palms(0.4)
    expect(palmForward(p.l, head)).toBeGreaterThanOrEqual(PALM.FACING)
    expect(palmForward(p.r, head)).toBeGreaterThanOrEqual(PALM.FACING)
    // A palm that faces the person, and a fist, are not pushed forward.
    expect(palmForward(hand(p.w[1], [0, 1, 0], [0, 0, 1]), head)).toBeLessThan(PALM.FACING)
    expect(palmForward(hand(p.w[1], [0, 1, 0], [0, 0, -1], true), head)).toBe(0)
  })

  it('stops everyone once both palms have been pushed forward and held there, and once per push', () => {
    expect([TUNE.bothPalmsMs, TUNE.palmsPushM]).toEqual([350, 0.06])
    const both = new BothHands()
    const said = push(both, 1000)
    expect(said.filter(Boolean)).toEqual(['stop'])
    // The hold counts from the push: 100 ms in, so not before 450 ms.
    expect(said.indexOf('stop') * 50).toBeGreaterThanOrEqual(100 + TUNE.bothPalmsMs)
    expect(both.both).toBe(true)
  })

  it('does not stop anyone for two open hands that are only held up, however long', () => {
    const both = new BothHands()
    const p = palms(0.4)
    for (let t = 0; t <= 3000; t += 50) expect(both.feed(p.l, p.r, p.w, head, t)).toBeNull()
    // Neither is read as "not now" to one Bot either: two palms up are not one palm.
    expect(both.both).toBe(true)
    // A push that is too short, 3 cm, is not one.
    const little = palms(0.44)
    for (let t = 3050; t <= 4000; t += 50) expect(both.feed(little.l, little.r, little.w, head, t)).toBeNull()
  })

  it('does not stop anyone for one palm that pushes, or for a push that is not held', () => {
    const one = new BothHands()
    const [near, out] = [palms(0.3), palms(0.4)]
    one.feed(null, near.r, near.w, head, 0)
    for (let t = 50; t <= 1000; t += 50) expect(one.feed(null, out.r, out.w, head, t)).toBeNull()
    // One palm is left to mean "not now" to one Bot.
    expect(one.both).toBe(false)
    const brief = new BothHands()
    brief.feed(near.l, near.r, near.w, head, 0)
    for (let t = 50; t <= 250; t += 50) expect(brief.feed(out.l, out.r, out.w, head, t)).toBeNull()
    expect(brief.feed(null, null, out.w, head, 300)).toBeNull()
    expect(brief.feed(null, null, out.w, head, 1000)).toBeNull()
  })

  it('carries on when both hands pull back 6 cm, open or not, and not for one hand or for less', () => {
    expect(TUNE.pullBackM).toBe(0.06)
    const both = new BothHands()
    expect(push(both, 600)).toContain('stop')
    const out = palms(0.4)
    // Hands that drop their pose where they are, pull back a little, or pull back one at a time, change nothing.
    expect(both.feed(null, null, out.w, head, 700)).toBeNull()
    expect(both.feed(null, null, palms(0.36).w, head, 800)).toBeNull()
    const back = palms(0.25).w
    expect(both.feed(null, null, [back[0], out.w[1]], head, 900)).toBeNull()
    expect(both.feed(null, null, back, head, 1000)).toBe('carry-on')
    // It is said once: nothing is stopped any more.
    expect(both.feed(null, null, palms(0.1).w, head, 1100)).toBeNull()
  })

  it('measures the pull back from the furthest the hands pushed', () => {
    const both = new BothHands()
    expect(push(both, 600)).toContain('stop')
    both.feed(null, null, palms(0.5).w, head, 700)
    // Back to where the stop was read is a pull of more than 6 cm from the push that followed it.
    expect(both.feed(null, null, palms(0.42).w, head, 800)).toBe('carry-on')
  })
})
