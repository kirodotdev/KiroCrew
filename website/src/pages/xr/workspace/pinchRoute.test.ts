import { describe, expect, it } from 'vitest'
import { Group, Mesh, PlaneGeometry, Vector3 } from 'three'
import { LONG_LOOK_DEG, PINCH, PinchRoute, TipsPinch, downAt, headStillOn, heldTurn, holdsLong, pinchKind, raised, releaseCancels, type V3 } from './pinchRoute'

describe('pinchRoute', () => {
  it('a hand points only while its ray begins less than 0.35 m below the eyes', () => {
    expect(raised(1.6, 1.3)).toBe(true)
    expect(raised(1.6, 1.6 - PINCH.raisedM)).toBe(false)
    expect(raised(1.6, 1.0)).toBe(false)
    // With a desk: raised only 15 cm over its top, wherever the eyes are.
    expect(raised(1.6, 1.3, 1.2)).toBe(false)
    expect(raised(1.6, 1.2 + PINCH.deskRaisedM + 0.01, 1.2)).toBe(true)
    expect(raised(1.3, 1.0, 0.9)).toBe(false)
  })

  it('a pinch is a click when short and still, a long press when held still, a drag when the hand travels', () => {
    expect(pinchKind(200, 0.005)).toBe('click')
    expect(pinchKind(PINCH.clickMs, 0.005)).toBe('long')
    expect(pinchKind(200, PINCH.stillM)).toBe('drag')
  })

  it('the held dot turns only once the hand travels past stillness, down for a hand that goes down', () => {
    expect(heldTurn([0.01, 0, 0])).toEqual([0, 0])
    const [yaw, pitch] = heldTurn([0, -0.1, 0])
    expect(yaw === 0).toBe(true)
    expect(pitch).toBeLessThan(0)
    expect(heldTurn([0.1, 0, 0])[0]).toBeLessThan(0)
  })

  it('the dot owns a resting hand\'s pinch; a raised hand on a panel owns its own', () => {
    const r = new PinchRoute()
    expect(r.light()).toBe('dot')
    expect(r.start('right', 0, [0, 1, 0], 'page:a')).toBe('dot')
    // The first down owns the button: the other hand's pinch does nothing meanwhile.
    expect(r.start('left', 10, [0, 1, 0], 'page:a')).toBeNull()
    expect(r.end('left', 20, [0, 1, 0], 'page:a')).toBeNull()
    expect(r.end('right', 100, [0, 1, 0], 'page:a')).toEqual({ to: 'dot', kind: 'click' })
    r.hands.left.raised = true
    r.hands.left.onPanel = true
    expect(r.light()).toBe('left')
    expect(r.start('left', 200, [0, 1.4, 0], 'page:a')).toBe('left')
    expect(r.lost('left')).toBe('left')
    expect(r.probe.last).toMatchObject({ hand: 'left', to: 'left', kind: 'cancel' })
    // A resting hand's pinch presses the one lit thing: the raised hand's ray.
    // While a pinch is held its light stays, however the hands move across the line.
    r.hands.left.raised = false
    expect(r.start('right', 250, [0, 1, 0], 'page:a')).toBe('dot')
    r.hands.left.raised = true
    expect(r.light()).toBe('dot')
    expect(r.end('right', 280, [0, 1, 0], 'page:a')).toEqual({ to: 'dot', kind: 'click' })
    expect(r.light()).toBe('left')
    expect(r.start('right', 300, [0, 1, 0], 'page:a')).toBe('left')
    expect(r.end('right', 1000, [0, 1, 0], 'page:a')).toEqual({ to: 'left', kind: 'long' })
    expect(r.owner).toBeNull()
  })

  it('takes a pinch of the other hand after the first hand\'s fingers opened as its own pinch, before the gate let the first go (D55 6, small 2)', () => {
    const r = new PinchRoute()
    const ended: string[] = []
    r.finishers.left = () => { const e = r.end('left', 999, [0, 1, 0], 'page:a'); if (e) ended.push(e.kind) }
    expect(r.start('left', 0, [0, 1, 0], 'page:a')).toBe('dot')
    // Held: the other hand's pinch does nothing meanwhile, as before.
    expect(r.start('right', 50, [0, 1, 0], 'page:a')).toBeNull()
    // The left fingers open at 100 ms; the right hand pinches 80 ms later, before any frame let the left go.
    r.opened('left', 100, [0, 1, 0])
    expect(r.start('right', 180, [0, 1, 0], 'page:a')).toBe('dot')
    expect(ended).toEqual(['click'])
    expect(r.probe.last).toMatchObject({ hand: 'right' })
    expect(r.owner?.hand).toBe('right')
  })

  it('the tips begin a pinch close, end it farther apart, and lose it when the joints go', () => {
    const t = new TipsPinch()
    expect(t.read(0.05)).toBeNull()
    expect(t.read(0.01)).toBe('down')
    expect(t.read(0.02)).toBeNull()
    expect(t.read(0.04)).toBe('up')
    expect(t.read(0.01)).toBe('down')
    expect(t.read(null)).toBe('lost')
    expect(t.read(null)).toBeNull()
  })
})

describe('the finger tips on a desk, frame by frame at 90 Hz', () => {
  const FRAME = 1000 / 90
  type Step = { d: number | null; wrist?: V3 | null; flat?: boolean }
  const REST: V3 = [0.1, 0.8, -0.4]
  /** Runs the frames with a desk (or none); says every change and the time of each from the start (ms). */
  const run = (frames: Step[], desk = true): string[] => {
    const t = new TipsPinch()
    const out: string[] = []
    frames.forEach((f, i) => {
      const c = t.read(f.d, 10_000 + i * FRAME, desk ? { flat: f.flat ?? false, wrist: f.wrist === undefined ? REST : f.wrist } : null)
      if (c) out.push(`${c}@${Math.round(i * FRAME)}`)
    })
    return out
  }
  const n = (ms: number) => Math.max(1, Math.round(ms / FRAME))
  const hold = (d: number | null, ms: number, more: Partial<Step> = {}): Step[] => Array.from({ length: n(ms) }, () => ({ d, ...more }))
  const ramp = (from: number, to: number, ms: number): Step[] => Array.from({ length: n(ms) }, (_, i) => ({ d: from + ((to - from) * (i + 1)) / n(ms) }))
  const downs = (c: string[]) => c.filter(x => x.startsWith('down')).length

  it('takes a brisk pinch from a hand at rest on the desk as one down', () => {
    const c = run([...hold(0.05, 1000), ...ramp(0.05, 0.01, 80), ...hold(0.01, 300)])
    expect(downs(c)).toBe(1)
  })
  it('takes no down from a hand that comes to rest curled', () => {
    // The wrist comes down 10 cm in 400 ms as the tips curl, and stops as they close; then the hand rests.
    for (const curl of [80, 150, 300]) {
      const coming = Array.from({ length: n(400) }, (_, i): Step => {
        const k = (i + 1) / n(400)
        const left = n(400) - i - 1
        return { d: left < n(curl) ? 0.01 + (0.045 - 0.01) * (left / n(curl)) : 0.045, wrist: [REST[0], REST[1] + 0.1 * (1 - k), REST[2]] }
      })
      expect(run([...hold(0.045, 500, { wrist: [REST[0], REST[1] + 0.1, REST[2]] }), ...coming, ...hold(0.01, 1500)])).toEqual([])
    }
  })
  it('takes no down from a slow curl: the tips cross from 3 cm to 1.5 cm in 0.5 s or in 1.3 s', () => {
    for (const ms of [500, 1300]) expect(run([...hold(0.05, 2000), ...ramp(0.03, 0.0149, ms), ...hold(0.01, 500)])).toEqual([])
  })
  it('takes no down from a hand found as its tips close, or found closed', () => {
    expect(run([...ramp(0.04, 0.01, 60), ...hold(0.01, 1500)])).toEqual([])
    expect(run(hold(0.01, 2000))).toEqual([])
    // Found closed, then apart for 200 ms and closed briskly at 800 ms: tracked 400 ms by then, so that close is one.
    expect(downs(run([...hold(0.01, 600), ...hold(0.04, 200), ...ramp(0.04, 0.01, 40), ...hold(0.01, 300)]))).toBe(1)
  })
  it('takes no down from tips that flicker apart for under 150 ms', () => {
    // Closed, a flicker apart of 100 ms, closed again; and stretches of 100 ms split by one frame under 3 cm.
    expect(run([...hold(0.01, 800), ...hold(0.04, 100), ...ramp(0.04, 0.01, 40), ...hold(0.01, 300)])).toEqual([])
    expect(run([...hold(0.02, 800), ...hold(0.04, 100), ...hold(0.025, FRAME), ...hold(0.04, 100), ...ramp(0.04, 0.01, 40), ...hold(0.01, 300)])).toEqual([])
  })
  it('takes a held pinch after a good close as one down and one up', () => {
    const c = run([...hold(0.05, 1000), ...ramp(0.05, 0.01, 80), ...hold(0.01, 2000), ...ramp(0.01, 0.05, 80), ...hold(0.05, 300)])
    expect(c.map(x => x.split('@')[0])).toEqual(['down', 'up'])
  })
  it('takes no down from a flat hand, nor from one untracked in the last 400 ms', () => {
    expect(run([...hold(0.05, 1000, { flat: true }), ...ramp(0.05, 0.01, 80).map(s => ({ ...s, flat: true })), ...hold(0.01, 300, { flat: true })])).toEqual([])
    expect(run([...hold(0.05, 900), { d: null }, ...hold(0.05, 200), ...ramp(0.05, 0.01, 80), ...hold(0.01, 300)])).toEqual([])
    expect(run([...hold(0.05, 900), { d: 0.05, wrist: null }, ...hold(0.05, 200), ...ramp(0.05, 0.01, 80), ...hold(0.01, 300)])).toEqual([])
  })
  it('pinches as it always did with no desk', () => {
    expect(run(hold(0.01, 500), false)).toEqual(['down@0'])
    expect(run([...hold(0.05, 2000), ...ramp(0.05, 0.01, 4000), ...hold(0.01, 500)], false).map(x => x.split('@')[0])).toEqual(['down'])
  })
})

describe('how far the hand of a held pinch moved', () => {
  it('is the distance from its down, and none with no pinch: 20 mm is past the still bound, so a Bot is not lifted', () => {
    const r = new PinchRoute()
    expect(r.travel()).toBe(0)
    r.start('right', 0, [0, 1, -0.3], 'room')
    r.at.right = [0.02, 1, -0.3]
    expect(r.travel()).toBeCloseTo(0.02, 9)
    expect(r.travel()).toBeGreaterThanOrEqual(PINCH.stillM)
    r.at.right = [0.005, 1, -0.3]
    expect(r.travel()).toBeLessThan(PINCH.stillM)
    r.end('right', 600, [0.005, 1, -0.3], 'room')
    expect(r.travel()).toBe(0)
  })
})

describe('the probe\'s list of the moves on a page', () => {
  it('says whose pointer moved on which panel and when, and keeps the last 12', () => {
    const r = new PinchRoute()
    expect(r.probe.moves).toEqual([])
    r.movedOn('p1', 23412, 'head', [0.12345, 1.6, -1.5], 1000.4)
    r.movedOn('p2', 23413, 'ray', null, 1016.6)
    expect(r.probe.moves).toEqual([{ ms: 1000, panel: 'p1', pointerId: 23412, pointerType: 'head', at: [0.123, 1.6, -1.5] }, { ms: 1017, panel: 'p2', pointerId: 23413, pointerType: 'ray', at: null }])
    for (let i = 0; i < 20; i += 1) r.movedOn('p1', 1, 'screen-mouse', null, 2000 + i)
    expect(r.probe.moves.length).toBe(12)
    expect(r.probe.moves[0].ms).toBe(2008)
    expect(r.probe.moves.every(m => m.pointerType === 'screen-mouse')).toBe(true)
  })
})

describe('the quiet time of a session', () => {
  it('presses nothing with a pinch in it, starts anew at each such pinch after a close, and presses again after it', () => {
    const r = new PinchRoute()
    r.hush(1000, 600, true)
    expect(r.start('left', 1300, [0, 1, 0], 'page:b')).toBeNull()
    expect(r.probe.last).toMatchObject({ down: 'quiet', kind: 'cancel' })
    // The pinch at 1300 started it anew: 1700 is still in it.
    expect(r.start('left', 1700, [0, 1, 0], 'page:b')).toBeNull()
    expect(r.start('left', 2400, [0, 1, 0], 'page:b')).toBe('dot')
  })
  it('does not start anew when no close began it', () => {
    const r = new PinchRoute()
    r.hush(1000, 600, false)
    expect(r.start('left', 1300, [0, 1, 0], 'page:b')).toBeNull()
    expect(r.start('left', 1650, [0, 1, 0], 'page:b')).toBe('dot')
  })
})

describe('the light in the quiet time of a session', () => {
  it('lights nothing while a pinch would press nothing, and the same aim is lit again after it with no move', () => {
    const r = new PinchRoute()
    r.probe.head = { origin: [0, 1.6, 0], direction: [0, 0, -1], on: { panel: 'b', x: 10, y: 10, what: 'page:b' }, enabled: true }
    expect([r.light(900), r.lit(900)]).toEqual(['dot', 'page:b'])
    r.hush(1000, 500, true)
    expect([r.light(1200), r.lit(1200)]).toEqual([null, 'nothing'])
    r.hands.right = { raised: true, onPanel: true, on: 'page:b', select: false }
    expect(r.light(1200)).toBeNull()
    expect([r.light(1600), r.lit(1600)]).toEqual(['right', 'page:b'])
  })

  // A pinch that the device sends is let go 70 ms after the fingers open (`pinchGate.ts`): the hand is on its way by then.
  describe('a pinch counts to where and when its fingers opened', () => {
    /** A pinch held still for 200 ms; then the fingers open, and the hand moves 21 mm in the 70 ms until the gate lets go. */
    const brisk = () => {
      const r = new PinchRoute()
      r.move('right', 0, [0, 1, 0])
      r.start('right', 0, [0, 1, 0], 'page:a')
      for (let t = 10; t <= 200; t += 10) r.move('right', t, [0, 1, 0])
      r.opened('right', 200, [0, 1, 0])
      for (let t = 210; t <= 270; t += 10) r.move('right', t, [((t - 200) / 70) * 0.021, 1, 0])
      return r
    }

    it('a hand that moves 21 mm in the 70 ms after the fingers open is still a click', () => {
      const r = brisk()
      expect(r.end('right', 270, [0.021, 1, 0], 'page:a')).toEqual({ to: 'dot', kind: 'click' })
      expect(r.probe.last).toMatchObject({ kind: 'click', ms: 200 })
      // With no word of the opening the same hand reads as a drag: the release's own place and time count.
      const late = new PinchRoute()
      late.start('right', 0, [0, 1, 0], 'page:a')
      expect(late.end('right', 270, [0.021, 1, 0], 'page:a')).toEqual({ to: 'dot', kind: 'drag' })
    })

    it('the travel at the up does not count the way after the fingers opened', () => {
      const r = brisk()
      expect(r.at.right[0]).toBeCloseTo(0.021, 6)
      expect(r.travel()).toBe(0)
      expect(r.place()).toEqual([0, 1, 0])
      r.end('right', 270, [0.021, 1, 0], 'page:a')
      expect(r.released).toMatchObject({ hand: 'right', place: [0, 1, 0], travelM: 0 })
    })

    it('the hand\'s speed read at the up is its speed at the opening', () => {
      // A flick: 1.2 m/s to the right until the fingers open; in the 70 ms after, the hand slows to a stop.
      const r = new PinchRoute()
      r.start('left', 0, [0, 1, 0], 'board')
      for (let t = 10; t <= 300; t += 10) r.move('left', t, [t * 0.0012, 1, 0])
      expect(r.velocity()[0]).toBeCloseTo(1.2, 6)
      r.opened('left', 300, [0.36, 1, 0])
      for (let t = 310; t <= 370; t += 10) r.move('left', t, [0.36 + 0.02 * (1 - ((370 - t) / 70) ** 2), 1, 0])
      expect(r.velocity()[0]).toBeCloseTo(1.2, 6)
      expect(r.travel()).toBeCloseTo(0.36, 6)
      r.end('left', 370, [0.38, 1, 0], 'board')
      expect(r.released?.mps).toBeCloseTo(1.2, 6)
      expect(r.released?.velocity[0]).toBeCloseTo(1.2, 6)
      expect(r.released?.travelM).toBeCloseTo(0.36, 6)
      // The owner is gone at the up: what is read there is what the route kept.
      expect(r.travel()).toBe(0)
      expect(r.velocity()).toEqual([0, 0, 0])
      // The next pinch's down forgets it; a pinch that is lost leaves none.
      r.start('left', 500, [0, 1, 0], 'board')
      expect(r.released).toBeNull()
      r.lost('left')
      expect(r.released).toBeNull()
    })

    it('a pinch that closes again before the gate lets go goes on as if it had never opened', () => {
      const r = new PinchRoute()
      r.start('right', 0, [0, 1, 0], 'page:a')
      r.opened('right', 100, [0, 1, 0])
      r.reclosed('right')
      r.move('right', 150, [0.05, 1, 0])
      expect(r.travel()).toBeCloseTo(0.05, 6)
      expect(r.end('right', 600, [0.05, 1, 0], 'page:a')).toEqual({ to: 'dot', kind: 'drag' })
      expect(r.probe.last?.ms).toBe(600)
    })
  })
})

describe('a hand\'s ray on what a plate that takes presses hides', () => {
  /** A stand-in for the plate's shape: it hides every point to the left of straight ahead, however far. */
  const veil = (point: V3) => point[0] < 0
  const on = (r: PinchRoute, hand: 'left' | 'right', point: V3) => { r.hands[hand] = { raised: true, onPanel: true, on: 'press:close', select: false, point } }

  it('presses nothing, owns nothing, and says so; beside the plate the same pinch presses', () => {
    const r = new PinchRoute()
    r.veil = veil
    // A thing nearer than the plate (0.5 m) and one behind it (1.75 m): both are hidden, both are refused.
    for (const point of [[-0.1, 1.3, -0.5], [-0.3, 1.9, -1.75]] as V3[]) {
      on(r, 'right', point)
      expect(r.start('right', 100, [0.2, 1.3, -0.3], 'press:close')).toBeNull()
      expect(r.owner).toBeNull()
      expect(r.probe.last).toMatchObject({ hand: 'right', to: 'right', down: 'hidden', up: 'hidden', kind: 'cancel' })
      expect(r.end('right', 200, [0.2, 1.3, -0.3], 'press:close')).toBeNull()
    }
    on(r, 'right', [0.1, 1.3, -0.5])
    expect(r.start('right', 300, [0.2, 1.3, -0.3], 'press:palette')).toBe('right')
    expect(r.end('right', 400, [0.2, 1.3, -0.3], 'press:palette')).toEqual({ to: 'right', kind: 'click' })
  })

  it('holds for the left hand, and for a resting hand\'s pinch that would press the lit ray of the other', () => {
    const r = new PinchRoute()
    r.veil = veil
    on(r, 'left', [-0.2, 1.5, -1.75])
    expect(r.start('left', 100, [-0.2, 1.3, -0.3], 'press:close')).toBeNull()
    // The right hand rests: its pinch is the lit thing's, which is the left ray's hidden point.
    expect(r.start('right', 200, [0.2, 1, 0], 'press:close')).toBeNull()
    expect(r.probe.last).toMatchObject({ hand: 'right', to: 'left', down: 'hidden' })
    expect(r.owner).toBeNull()
  })

  it('refuses nothing while no plate takes presses, and never the dot\'s pinch', () => {
    const r = new PinchRoute()
    on(r, 'right', [-0.1, 1.3, -0.5])
    // The plate is free: it tells the route nothing.
    expect(r.start('right', 100, [0.2, 1.3, -0.3], 'press:palette')).toBe('right')
    r.end('right', 200, [0.2, 1.3, -0.3], 'press:palette')
    // The plate takes presses; the hand rests, with the last point of its ray still in its book: the pinch is the dot's.
    r.veil = veil
    r.hands.right.raised = false
    expect(r.start('right', 300, [0.2, 1, 0], 'page:a')).toBe('dot')
  })

  it('a pinch that begins beside the plate and ends under it keeps its target: the down decided', () => {
    const r = new PinchRoute()
    r.veil = veil
    on(r, 'right', [0.01, 1.3, -1.75])
    expect(r.start('right', 100, [0.2, 1.3, -0.3], 'page:a')).toBe('right')
    on(r, 'right', [-0.01, 1.3, -1.75])
    expect(r.end('right', 200, [0.2, 1.3, -0.3], 'page:a')).toEqual({ to: 'right', kind: 'click' })
    // And one that begins under it presses nothing, wherever it ends.
    expect(r.start('right', 300, [0.2, 1.3, -0.3], 'page:a')).toBeNull()
    on(r, 'right', [0.01, 1.3, -1.75])
    expect(r.end('right', 400, [0.2, 1.3, -0.3], 'page:a')).toBeNull()
  })
})

describe('a long hold keeps its own use only where it has one (D84 2, D86 5)', () => {
  const on = (userData: Record<string, unknown>) => { const g = new Group(); g.userData = userData; const m = new Mesh(); g.add(m); return m }
  it('names the stamp, a Bot and the inside of a page, and nothing else', () => {
    expect(holdsLong(on({ xrPress: 'stamp' }))).toBe(true)
    expect(holdsLong(on({ xrPress: 'bot' }))).toBe(true)
    expect(holdsLong(on({ xrPage: 'p1' }))).toBe(true)
    // A tile, Home's names, a page's close mark and frame, the palette: the release of a long pinch presses there.
    expect(holdsLong(on({ xrPress: 'tile' }))).toBe(false)
    expect(holdsLong(on({ xrPress: 'home' }))).toBe(false)
    expect(holdsLong(on({ xrFrame: 'close' }))).toBe(false)
    expect(holdsLong(on({ xrPress: 'palette' }))).toBe(false)
    expect(holdsLong(null)).toBe(false)
  })
})

describe('a long pinch presses only what his head still looks at (M47)', () => {
  it('reads his head\'s own forward line against the point the pinch went down on, within 3 degrees (M54)', () => {
    const tile = new Mesh(new PlaneGeometry(0.1, 0.1))
    tile.position.set(0, 0, -1)
    tile.updateMatrixWorld()
    const down = downAt({ object: tile, point: new Vector3(0, 0, -1) })
    const eye = new Group()
    eye.updateMatrixWorld()
    expect(headStillOn(eye, down)).toBe(true)
    // A head that drifts 1 degree: still on it. 20 degrees away: not.
    eye.rotation.y = (1 * Math.PI) / 180
    eye.updateMatrixWorld()
    expect(headStillOn(eye, down)).toBe(true)
    eye.rotation.y = (LONG_LOOK_DEG + 0.5) * (Math.PI / 180)
    eye.updateMatrixWorld()
    expect(headStillOn(eye, down)).toBe(false)
    eye.rotation.y = (20 * Math.PI) / 180
    eye.updateMatrixWorld()
    expect(headStillOn(eye, down)).toBe(false)
    expect(headStillOn(eye, null)).toBe(false)
    // The point is kept in the thing's own frame: a tile that moves 2 cm with his palm carries it.
    eye.rotation.y = 0
    eye.updateMatrixWorld()
    tile.position.x += 0.02
    tile.updateMatrixWorld()
    expect(headStillOn(eye, down)).toBe(true)
  })
})

describe('which release is a cancel (M47, M53)', () => {
  it('asks the head only for the dot\'s long pinch; a raised hand\'s ray goes where the hand points', () => {
    const away = () => false
    // The dot: a long pinch whose head left the point is a cancel; one still on it is an up.
    expect(releaseCancels('long', false, true, away)).toBe(true)
    expect(releaseCancels('long', false, true, () => true)).toBe(false)
    // A raised hand's ray: the head is never asked, so a long pinch with his head away still presses.
    let asked = false
    expect(releaseCancels('long', false, false, () => { asked = true; return false })).toBe(false)
    expect(asked).toBe(false)
    // A thing with its own long hold: a cancel for both. A short pinch: an up for both.
    expect(releaseCancels('long', true, false, away)).toBe(true)
    expect(releaseCancels('click', false, true, away)).toBe(false)
  })
})

describe('the light after the quiet time (P21j, D194): nobody\'s until each raised hand had a frame with its ray on', () => {
  /** A route whose quiet time ends at 1900, with the right hand as its frame leaves it in the quiet time: raised, its ray off, so "on no panel". */
  const after = (closed = false) => {
    const r = new PinchRoute()
    r.probe.head = { origin: [0, 1.6, 0], direction: [0, 0, -1], on: { panel: null, x: null, y: null, what: 'chat-card' }, enabled: false }
    r.hush(1000, 900, closed)
    Object.assign(r.hands.right, { raised: true, onPanel: false, on: 'nothing', point: null })
    return r
  }
  /** The hand's frame at `ms`: its ray is on, and its book is read. */
  const framed = (r: PinchRoute, hand: 'right' | 'left', ms: number, on: string | null) => Object.assign(r.hands[hand], { raised: true, onPanel: on !== null, on: on ?? 'nothing', point: on ? [0, 1.4, -0.5] : null, readAt: ms })

  it('must not take: in the gap the light is not the dot\'s, and a pinch that starts in it is never the dot\'s', () => {
    const r = after()
    expect(r.light(1899)).toBeNull()
    expect([r.awaited(1901), r.light(1901), r.lit(1901)]).toEqual(['right', null, 'nothing'])
    // A pinch in the gap: no pointer is pressed yet; it waits for the hand's frame, as the hand's own.
    expect(r.start('right', 1901, [0, 1.3, 0], 'chat-card')).toBeNull()
    expect(r.owner).toMatchObject({ hand: 'right', to: 'right', held: true })
    expect(r.probe.last).toMatchObject({ to: 'right', down: 'held' })
    expect(r.light(1905)).toBe('right')
    // The hand's frame: its ray lies on a key. The pinch goes down there, one frame after it began.
    framed(r, 'right', 1930, 'room')
    expect(r.taken('right', 'room')).toBe(true)
    expect(r.taken('right', 'room')).toBe(false)
    expect(r.owner).toMatchObject({ to: 'right', held: false })
    expect(r.probe.last).toMatchObject({ to: 'right', down: 'room' })
    expect(r.end('right', 2000, [0, 1.3, 0], 'room')).toEqual({ to: 'right', kind: 'click' })
    expect(r.light(2001)).toBe('right')
  })

  it('must not lose: with no hand raised the dot\'s light is there at the first instant; with a hand, once its frame came', () => {
    const none = after()
    none.hands.right.raised = false
    expect([none.awaited(1900), none.light(1900)]).toEqual([null, 'dot'])
    expect(none.start('left', 1900, [0, 1, 0], 'chat-card')).toBe('dot')
    // A raised hand whose frame came: the hand's when its ray lies on a thing, else the dot's, from that frame on.
    const on = after()
    framed(on, 'right', 1905, 'room')
    expect([on.awaited(1906), on.light(1906)]).toEqual([null, 'right'])
    expect(on.start('right', 1906, [0, 1.3, 0], 'room')).toBe('right')
    const off = after()
    framed(off, 'right', 1905, null)
    expect(off.light(1906)).toBe('dot')
    expect(off.start('right', 1906, [0, 1.3, 0], 'chat-card')).toBe('dot')
  })

  it('must not hang: a raised hand with no frame holds the light back for `lightWaitMs`, not longer; a hand lowered or come in the gap', () => {
    expect(PINCH.lightWaitMs).toBe(100)
    const lost = after()
    expect(lost.light(1999)).toBeNull()
    expect(lost.light(2000)).toBe('dot')
    expect(lost.start('left', 2000, [0, 1, 0], 'chat-card')).toBe('dot')
    // Lowered in the gap (its frame says so): the dot's at once.
    const lowered = after()
    lowered.hands.right.raised = false
    expect(lowered.light(1910)).toBe('dot')
    // Another hand comes in the gap, raised, with no frame of its ray yet: waited for too, then read.
    const came = after()
    framed(came, 'right', 1905, 'room')
    Object.assign(came.hands.left, { raised: true, onPanel: false })
    expect([came.awaited(1910), came.light(1910)]).toEqual(['left', null])
    framed(came, 'left', 1930, 'room')
    expect(came.light(1931)).toBe('right')
  })

  it('a pinch that waited: over with nothing pressed when the hand had no frame, was lowered, or its ray lies on nothing or on a hidden point', () => {
    // No frame of the hand before the fingers open: no down was given, so no up is.
    const never = after()
    expect(never.start('right', 1901, [0, 1.3, 0], 'chat-card')).toBeNull()
    expect(never.end('right', 1950, [0, 1.3, 0], 'chat-card')).toBeNull()
    expect(never.owner).toBeNull()
    expect(never.probe.last).toMatchObject({ down: 'gap', up: 'gap', kind: 'cancel' })
    // The hand is lost while it waits: no cancel goes to a pointer that had no down.
    const gone = after()
    gone.start('right', 1901, [0, 1.3, 0], 'chat-card')
    expect(gone.lost('right')).toBeNull()
    // Its frame finds the ray on nothing: not handed to the dot, which was not lit when the pinch began.
    const empty = after()
    empty.start('right', 1901, [0, 1.3, 0], 'chat-card')
    framed(empty, 'right', 1930, null)
    expect(empty.taken('right', 'nothing')).toBe(false)
    expect(empty.owner).toBeNull()
    expect(empty.probe.last).toMatchObject({ down: 'gap', kind: 'cancel' })
    expect(empty.light(1931)).toBe('dot')
    // Lowered in the gap, its ray still on a thing.
    const low = after()
    low.start('right', 1901, [0, 1.3, 0], 'chat-card')
    framed(low, 'right', 1930, 'room')
    low.hands.right.raised = false
    expect(low.taken('right', 'room')).toBe(false)
    // On a point that the eye does not see.
    const veiled = after()
    veiled.veil = () => true
    veiled.start('right', 1901, [0, 1.3, 0], 'chat-card')
    framed(veiled, 'right', 1930, 'room')
    expect(veiled.taken('right', 'room')).toBe(false)
  })

  it('beside: the quiet time of a close is not begun anew by a pinch in the gap; the other hand\'s pinch; both hands raised', () => {
    const closed = after(true)
    expect(closed.start('right', 1901, [0, 1.3, 0], 'chat-card')).toBeNull()
    expect(closed.quietUntil).toBe(1900)
    expect(closed.owner).toMatchObject({ to: 'right', held: true })
    // The left hand pinches while the right is raised on a key: the right hand's ray takes it at the right hand's frame.
    const other = after()
    expect(other.start('left', 1901, [0, 1, 0], 'chat-card')).toBeNull()
    expect(other.owner).toMatchObject({ hand: 'left', to: 'right', held: true })
    framed(other, 'right', 1930, 'room')
    expect(other.taken('left', 'room')).toBe(false)
    expect(other.taken('right', 'room')).toBe(true)
    expect(other.end('left', 2000, [0, 1, 0], 'room')).toEqual({ to: 'right', kind: 'click' })
    // Both raised: the light waits for both; the right one's is first, as before.
    const both = after()
    Object.assign(both.hands.left, { raised: true, onPanel: false })
    framed(both, 'right', 1930, 'room')
    expect([both.awaited(1931), both.light(1931)]).toEqual(['left', null])
    framed(both, 'left', 1931, 'room')
    expect(both.light(1932)).toBe('right')
  })

  it('with no quiet time behind it nothing waits', () => {
    const r = new PinchRoute()
    Object.assign(r.hands.right, { raised: true, onPanel: false })
    expect([r.awaited(5), r.light(5)]).toEqual([null, 'dot'])
  })
})

describe('the mark of his own let-go (D192, 15:50)', () => {
  it('is set while a release is handed on, and cleared after it', () => {
    const r = new PinchRoute()
    let seen: string | null = null
    r.release('long', () => { seen = r.ending })
    expect(seen).toBe('long')
    expect(r.ending).toBeNull()
  })
  it('is cleared when a handler throws, so a later cancel of a lost hand does not read a stale let-go', () => {
    const r = new PinchRoute()
    expect(() => r.release('long', () => { throw new Error('a handler failed') })).toThrow('a handler failed')
    expect(r.ending).toBeNull()
  })
})
