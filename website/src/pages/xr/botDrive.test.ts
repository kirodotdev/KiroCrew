import { describe, expect, it } from 'vitest'
import { Object3D, Vector3 } from 'three'
import { BotDrive } from './botDrive'

describe('a walk', () => {
  it('starts where the body stands, also before any frame stepped it there', () => {
    const drive = new BotDrive(2)
    drive.root.position.set(0.5, 0.7, -1.2)
    drive.walkTo(new Vector3(0.6, 0, -1.2), undefined, 0.05)
    drive.frame(1 / 60)
    expect(drive.root.position.x).toBeGreaterThan(0.5)
    expect(drive.root.position.x).toBeLessThan(0.51)
    expect(drive.root.position.z).toBeCloseTo(-1.2, 6)
  })
})

describe('what rides with a body', () => {
  it('stands where the body is after each step of a walk, not one step behind', () => {
    const drive = new BotDrive(1)
    const rider = new Object3D()
    drive.riders.add(rider)
    drive.walkTo(new Vector3(1, 0, 0), undefined, 0.5)
    for (let i = 0; i < 10; i += 1) {
      drive.frame(1 / 60)
      expect(rider.position.toArray()).toEqual(drive.root.position.toArray())
    }
    expect(drive.root.position.x).toBeGreaterThan(0)
    drive.riders.delete(rider)
    drive.frame(1 / 60)
    expect(rider.position.x).toBeLessThan(drive.root.position.x)
  })
})

describe('a Bot that the scene moves (M90)', () => {
  const ease = (u: number) => { const c = Math.min(1, Math.max(0, u)); return c * c * (3 - 2 * c) }
  /**
   * Put a Bot 0.3 m tall along x as the room walks it (1 m in 1 s, eased), then hold it there for 2 s, at `rate`
   * frames a second. Returns its most lean while it moves, the poses it took, and how long after the stop it leans.
   */
  function roomWalk(o: { moved: boolean; still?: boolean; rate?: number; put?: (ms: number) => number }) {
    const drive = new BotDrive(3), rate = o.rate ?? 60, dt = 1 / rate
    drive.root.scale.setScalar(0.3 / drive.height)
    drive.update(0, 0, !!o.still)
    const put = o.put ?? ((ms: number) => ease(ms / 1000))
    let lean = 0, leansUntil = -1
    const poses = new Set<string>()
    for (let ms = 0; ms < 3000; ms += 1000 * dt) {
      drive.root.position.x = put(ms)
      drive.setMoved(o.moved)
      const f = drive.frame(dt)
      poses.add(drive.poseNow)
      const tip = Math.hypot(f.lean, f.side)
      if (ms <= 1000) lean = Math.max(lean, tip)
      else if (tip > 0.02) leansUntil = ms - 1000
    }
    return { lean, poses, leansUntil }
  }

  it('leans into its way as on a walk of its own, and stands upright within 0.4 s of the stop', () => {
    const got = roomWalk({ moved: true })
    expect(got.lean).toBeGreaterThan(0.15)
    expect(got.poses.has('stand')).toBe(true)
    expect(got.leansUntil).toBeLessThan(400)
  })

  it.each([30, 15, 8])('does the same at %i frames a second', rate => {
    const got = roomWalk({ moved: true, rate })
    expect(got.lean).toBeGreaterThan(0.15)
    // The stop is seen in the first frame that finds it still: one frame of 8 a second is 125 ms.
    expect(got.leansUntil).toBeLessThan(400 + 1000 / rate)
  })

  it('does not lean when the scene does not say that it moves it, nor with reduced motion', () => {
    expect(roomWalk({ moved: false }).lean).toBe(0)
    expect(roomWalk({ moved: false }).poses).toEqual(new Set(['rest']))
    expect(roomWalk({ moved: true, still: true }).lean).toBe(0)
  })

  it('does not lean for a slow sway in place, nor for a body put somewhere else at once', () => {
    // A sway of 1 cm at 0.3 Hz, as `driftAt` moves a Bot, even if it were said to be moved.
    expect(roomWalk({ moved: true, put: ms => 0.01 * Math.sin((2 * Math.PI * 0.3 * ms) / 1000) }).lean).toBe(0)
    // Laid out again: 2 m away in one frame, then still.
    expect(roomWalk({ moved: true, put: ms => (ms > 500 ? 2 : 0) }).lean).toBe(0)
  })
})

describe('the way a moved Bot leans, and its eyes (D118 2, 4)', () => {
  /** A Bot 0.3 m tall facing +z, put along `dir` (world x, z) at 1 m a second for 0.6 s, then held still for 2 s. */
  function carry(dir: [number, number], o: { yaw?: number; askAtMs?: number; seed?: number; moved?: boolean } = {}) {
    const drive = new BotDrive(o.seed ?? 5), dt = 1 / 60
    drive.root.scale.setScalar(0.3 / drive.height)
    drive.setRest(o.yaw ?? 0)
    drive.update(0, 0, false)
    // It turns to face its rest first, standing.
    for (let i = 0; i < 90; i += 1) drive.frame(dt)
    let ahead = 0, across = 0, upright = true, blinks = 0, shut = false, blinkAfterAsk = -1
    for (let ms = 0; ms < 2600; ms += 1000 * dt) {
      const k = Math.min(ms, 600) / 1000
      if (o.moved !== false) drive.root.position.set(dir[0] * k, 0, dir[1] * k)
      if (o.askAtMs !== undefined && ms >= o.askAtMs && drive.state !== 'needs') drive.setState('needs')
      drive.setMoved(o.moved !== false)
      const f = drive.frame(dt)
      if (ms < 600 && Math.abs(f.lean) > Math.abs(ahead)) ahead = f.lean
      if (ms < 600 && Math.abs(f.side) > Math.abs(across)) across = f.side
      if (ms > 1100 && Math.hypot(f.lean, f.side) > 0.02) upright = false
      if (f.blink > 0.5 && !shut) { blinks += 1; if (o.askAtMs !== undefined && ms >= o.askAtMs && blinkAfterAsk < 0) blinkAfterAsk = ms - o.askAtMs }
      shut = f.blink > 0.5
    }
    return { ahead, across, upright, blinks, blinkAfterAsk }
  }

  it('tips to the side he carries it, whatever it faces, and stands upright when held still', () => {
    // Facing +z, carried to +x: it tips to its +x side, not forward.
    const right = carry([1, 0])
    expect(right.across).toBeGreaterThan(0.15)
    expect(Math.abs(right.ahead)).toBeLessThan(0.03)
    expect(right.upright).toBe(true)
    const left = carry([-1, 0])
    expect(left.across).toBeLessThan(-0.15)
    // Carried the way it faces (toward him, when it faces him): it tips forward; the other way, back.
    expect(carry([0, 1]).ahead).toBeGreaterThan(0.15)
    expect(carry([0, -1]).ahead).toBeLessThan(-0.15)
    // Turned to face +x, carried to +x: forward.
    const turned = carry([1, 0], { yaw: Math.PI / 2 })
    expect(turned.ahead).toBeGreaterThan(0.15)
    expect(Math.abs(turned.across)).toBeLessThan(0.03)
  })

  it('blinks by its own clock while it is moved: no more than a Bot that is not moved (M99)', () => {
    for (const seed of [1, 5, 9, 13]) {
      const moved = carry([1, 0], { seed })
      const still = carry([1, 0], { seed, moved: false })
      expect(moved.blinks).toBeLessThanOrEqual(still.blinks)
    }
  })

  it('still blinks when it starts to ask while it is carried', () => {
    const got = carry([1, 0], { askAtMs: 300 })
    expect(got.blinkAfterAsk).toBeGreaterThanOrEqual(0)
    expect(got.blinkAfterAsk).toBeLessThan(120)
  })
})
