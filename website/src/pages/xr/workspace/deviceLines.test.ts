import { describe, expect, it } from 'vitest'
import { DeviceBook, HandWatch, STILL_WINDOW_MS, StillMeter, TRACKED_WINDOW_MS, handsLine, tipLine, deviceLine, deviceLines, grantedLine, layersLine, pinchLine, stillLine, viewDeg, type DeviceFacts } from './deviceLines'
import { PAGES } from './layout'
import { PinchRoute, raised } from './pinchRoute'
import { PLATE_PX, PLATE_ROWS, plateRows, plateWidthM } from './readoutLines'

/** A projection matrix, column by column, for a view that spans `left`..`right` and `down`..`up` degrees of straight ahead. */
function projection(left: number, right: number, down: number, up: number): number[] {
  const t = (deg: number) => Math.tan((deg * Math.PI) / 180)
  const [l, r, b, u] = [t(left), t(right), t(down), t(up)]
  const m = new Array<number>(16).fill(0)
  m[0] = 2 / (r - l)
  m[5] = 2 / (u - b)
  m[8] = (r + l) / (r - l)
  m[9] = (u + b) / (u - b)
  m[10] = -1
  m[11] = -1
  return m
}

const facts = (more: Partial<DeviceFacts> = {}): DeviceFacts => ({
  mode: 'immersive-ar', blend: 'additive', features: ['local-floor', 'hand-tracking', 'layers'], frameRate: 90, view: [61.4, 40.7], picture: { size: [3840, 1200], from: 'base', eyes: 2 },
  layers: { on: 2, most: 6, granted: true, off: false }, pinch: null, still: null, hands: { left: new HandWatch().facts(), right: new HandWatch().facts() }, tip: null, ...more,
})

describe('what the readout says of the device', () => {
  it('reads one eye\'s view from its projection matrix, the same to both sides or not', () => {
    const [w, h] = viewDeg(projection(-30.7, 30.7, -20.35, 20.35))
    expect(w).toBeCloseTo(61.4, 5)
    expect(h).toBeCloseTo(40.7, 5)
    // An eye that sees farther to one side than to the other: the whole span counts.
    const [w2, h2] = viewDeg(projection(-40, 25, -30, 15))
    expect(w2).toBeCloseTo(65, 5)
    expect(h2).toBeCloseTo(45, 5)
  })

  it('says the session\'s kind, its blend, its rate, the view and the picture\'s size on one line', () => {
    expect(deviceLine(facts())).toBe('device · immersive-ar · blend additive · 90 Hz · view 61x41° an eye · picture 3840x1200 whole base layer, eyes 2')
    // The picture's size says what it is the size of: which of the session's pictures was read, and for how many eyes.
    expect(deviceLine(facts({ picture: { size: [1920, 1200], from: 'layer', eyes: 2 } }))).toMatch(/picture 1920x1200 first projection layer texture, eyes 2$/)
  })

  it('puts a dash where the browser says nothing', () => {
    expect(deviceLine(facts({ mode: null, blend: null, frameRate: null, view: null, picture: null }))).toBe('device · - · blend - · - Hz · view -x-° an eye · picture -x-')
    expect(grantedLine(facts({ features: null }))).toBe('granted · -')
  })

  it('names the granted features in a fixed order, and says when there are none', () => {
    expect(grantedLine(facts())).toBe('granted · hand-tracking, layers, local-floor')
    expect(grantedLine(facts({ features: [] }))).toBe('granted · nothing')
  })

  it('counts the pages on layers, and says why none may stand on one', () => {
    expect(layersLine({ on: 2, most: 6, granted: true, off: false })).toBe('layers 2 of 6')
    expect(layersLine({ on: 0, most: 0, granted: false, off: false })).toBe('layers 0 of 0 · not granted')
    // The address word wins over the grant: that is the case a person set up to compare.
    expect(layersLine({ on: 0, most: 0, granted: true, off: true })).toBe('layers 0 of 0 · off by layers=0 in the address')
  })

  it('keeps the last session\'s lines after it, under a line that says so', () => {
    const book = new DeviceBook()
    expect(book.lines()).toEqual([])
    book.facts = facts()
    book.live = true
    expect(book.lines()).toEqual(deviceLines(facts()))
    book.live = false
    expect(book.lines()).toEqual(['Numbers from the last time in XR', ...deviceLines(facts())])
  })
})

describe('what the readout says of a pinch', () => {
  it('says whose the last pinch was, where it came from, what it was and how long it lasted', () => {
    expect(pinchLine(null)).toBe('pinch · none yet')
    expect(pinchLine({ hand: 'right', select: true, held: false, kind: 'click', ms: 180, on: 'page:chat' })).toBe('pinch · right · select · click · 180 ms · on page:chat')
    // A device that sends no select of its own: the finger tips are read.
    expect(pinchLine({ hand: 'left', select: false, held: false, kind: 'cancel', ms: 0, on: 'quiet' })).toBe('pinch · left · tips · cancel · 0 ms · on quiet')
    // While it lasts it is held, whatever the book's kind says meanwhile.
    expect(pinchLine({ hand: 'left', select: true, held: true, kind: 'cancel', ms: 640, on: '' })).toBe('pinch · left · select · held · 640 ms · on -')
  })

  it('measures the largest travel of a held hand in any half second, and from the start', () => {
    const meter = new StillMeter()
    expect(stillLine(meter.last)).toBe('still · no held pinch yet')
    // A hand that drifts 2 mm every 100 ms along one line for a second: 10 mm in any half second, 20 mm in all.
    for (let i = 0; i <= 10; i += 1) meter.read(1000 + i * 100, { hand: 'right', since: 1000, at: [i * 0.002, 1, -0.3] })
    expect(meter.last?.windowMm).toBeCloseTo((STILL_WINDOW_MS / 100) * 2, 5)
    expect(meter.last?.fromStartMm).toBeCloseTo(20, 5)
    expect(meter.last?.ms).toBe(1000)
    expect(stillLine(meter.last)).toBe('still 10.0 mm in any half second · 20.0 mm from the start · right held 1,000 ms')
  })

  it('counts a hand that shakes in place by its shake, not by its path', () => {
    const meter = new StillMeter()
    // 3 mm to one side and back, every frame, for two seconds: it never is farther than 3 mm from anywhere it was.
    for (let i = 0; i <= 120; i += 1) meter.read(i * 16, { hand: 'left', since: 0, at: [i % 2 ? 0.003 : 0, 1, -0.3] })
    expect(meter.last?.windowMm).toBeCloseTo(3, 5)
    expect(meter.last?.fromStartMm).toBeCloseTo(3, 5)
  })

  it('keeps the last pinch\'s numbers after it ends, and starts anew at the next', () => {
    const meter = new StillMeter()
    meter.read(0, { hand: 'left', since: 0, at: [0, 1, 0] })
    meter.read(100, { hand: 'left', since: 0, at: [0.004, 1, 0] })
    meter.read(200, null)
    expect(meter.last).toMatchObject({ hand: 'left', ms: 100 })
    expect(meter.last?.windowMm).toBeCloseTo(4, 5)
    meter.read(900, { hand: 'right', since: 900, at: [0.5, 1, 0] })
    expect(meter.last).toEqual({ hand: 'right', windowMm: 0, fromStartMm: 0, ms: 0 })
  })
})

describe('a held pinch that the page lets go a little after the fingers open', () => {
  it('does not count the hand\'s way after the fingers opened: it reads the place that the page\'s rule reads', () => {
    const meter = new StillMeter()
    const route = new PinchRoute()
    // Held still for 400 ms; then the fingers open and the hand leaves at a metre a second, 70 ms before the page lets go.
    route.start('right', 0, [0, 1, 0], 'page:a')
    for (let at = 0; at <= 400; at += 10) { route.move('right', at, [0, 1, 0]); meter.readRoute(at, route) }
    route.opened('right', 400, [0, 1, 0])
    for (let at = 410; at <= 470; at += 10) { route.move('right', at, [(at - 400) / 1000, 1, 0]); meter.readRoute(at, route) }
    expect(meter.last).toEqual({ hand: 'right', windowMm: 0, fromStartMm: 0, ms: 400 })
    // The line and the rule agree on the same pinch: still, and a click.
    expect(route.end('right', 470, [0.07, 1, 0], 'page:a')?.kind).toBe('click')
    meter.readRoute(480, route)
    expect(meter.last).toEqual({ hand: 'right', windowMm: 0, fromStartMm: 0, ms: 400 })
  })

  it('counts on for a pinch that closes again before the page lets go', () => {
    const meter = new StillMeter()
    const route = new PinchRoute()
    route.move('left', 0, [0, 1, 0])
    route.start('left', 0, [0, 1, 0], 'page:a')
    meter.readRoute(0, route)
    route.opened('left', 100, [0, 1, 0])
    route.reclosed('left')
    route.move('left', 150, [0.02, 1, 0])
    meter.readRoute(150, route)
    expect(meter.last?.fromStartMm).toBeCloseTo(20, 5)
    expect(meter.last?.ms).toBe(150)
  })

  it('keeps every frame of a pinch read from the finger tips, which ends at once', () => {
    const meter = new StillMeter()
    for (let at = 0; at <= 100; at += 10) meter.read(at, { hand: 'left', since: 0, at: [at / 10000, 1, 0] })
    meter.read(110, null)
    expect(meter.last?.fromStartMm).toBeCloseTo(10, 5)
    expect(meter.last?.ms).toBe(100)
  })
})

describe('what the readout says of the hands', () => {
  it('says a hand is seen, how far under the eyes its ray begins, and the share of two seconds it was tracked', () => {
    const left = new HandWatch()
    const right = new HandWatch()
    expect(handsLine({ left: left.facts(), right: right.facts() })).toBe('hands · left not seen yet · right not seen yet')
    // Sixty frames a second for a second: the left hand at the chest in every frame, the right one in every second frame.
    for (let i = 0; i < 60; i += 1) {
      left.read(i * 16, 1.28, 1.6, 1.25)
      right.read(i * 16, i % 2 ? 1.15 : null, 1.6, 1.1)
    }
    expect(handsLine({ left: left.facts(), right: right.facts() })).toBe('hands · left seen 32 cm under the eyes, 100% of 2 s, wrist 35 cm · right seen 45 cm under the eyes, 50% of 2 s, wrist 50 cm')
  })

  it('gives the height of the ray\'s start, which the raised-hand rule compares, and the wrist only beside it', () => {
    // A tilted hand: its ray begins 33 cm under the eyes (raised, by the page's rule), its wrist 41 cm under them (not).
    const hand = new HandWatch()
    hand.read(0, 1.6 - 0.33, 1.6, 1.6 - 0.41)
    const facts = hand.facts()
    expect(facts.underCm).toBeCloseTo(33, 5)
    expect(facts.wristCm).toBeCloseTo(41, 5)
    expect(raised(1.6, 1.6 - facts.underCm! / 100)).toBe(true)
    expect(raised(1.6, 1.6 - facts.wristCm! / 100)).toBe(false)
    expect(handsLine({ left: hand.facts(), right: new HandWatch().facts() })).toBe('hands · left seen 33 cm under the eyes, 100% of 2 s, wrist 41 cm · right not seen yet')
  })

  it('says where a hand was lost, and keeps saying it', () => {
    const hand = new HandWatch()
    for (let i = 0; i < 30; i += 1) hand.read(i * 16, 1.6 - 0.3 - i * 0.005, 1.6, 1.6 - 0.34 - i * 0.005)
    hand.read(480, null, 1.6)
    expect(hand.facts()).toMatchObject({ state: 'lost', joints: true })
    // The last place it was seen: 30 cm and 29 steps of half a centimetre under the eyes.
    expect(hand.facts().underCm).toBeCloseTo(44.5, 5)
    for (let i = 0; i < 200; i += 1) hand.read(500 + i * 16, null, 1.6)
    expect(hand.facts().share).toBe(0)
    expect(handsLine({ left: hand.facts(), right: new HandWatch().facts() })).toBe('hands · left lost at 45 cm under the eyes, wrist 49 cm · right not seen yet')
  })

  it('forgets frames older than its window, and says when a device gives a ray and no joints', () => {
    const hand = new HandWatch()
    hand.read(0, null, 1.6)
    hand.read(TRACKED_WINDOW_MS + 1, 1.2, 1.6)
    expect(hand.facts()).toMatchObject({ state: 'seen', joints: false, share: 1 })
    expect(handsLine({ left: hand.facts(), right: hand.facts() })).toContain('left seen, no joints, 40 cm under the eyes, 100% of 2 s · right')
  })

  it('says each index tip\'s height over the desk in millimetres, and a dash for a hand that is not there', () => {
    expect(tipLine({ left: 0.0034, right: null })).toBe('tip · left 3 mm · right - mm over the desk')
    expect(tipLine({ left: -0.004, right: 0.12 })).toBe('tip · left -4 mm · right 120 mm over the desk')
    // Only with a desk is there such a line.
    expect(deviceLines(facts())).toHaveLength(6)
    expect(deviceLines(facts({ tip: { left: 0, right: 0 } }))).toHaveLength(7)
  })
})

describe('the plate of a session', () => {
  it('is as high as its lines, between its least and its most', () => {
    expect(plateRows(0)).toBe(PLATE_ROWS.least)
    expect(plateRows(5)).toBe(PLATE_ROWS.least)
    expect(plateRows(14)).toBe(14)
    expect(plateRows(40)).toBe(PLATE_ROWS.most)
  })

  it('is so wide that its text is as big as the smallest text a page may show at that distance', () => {
    for (const d of [1, PAGES.distanceM, 3]) {
      const textM = (plateWidthM(d) * PLATE_PX.text) / PLATE_PX.width
      // 14 dp, at 0.868 mm a dp for each metre of distance.
      expect(textM / d).toBeCloseTo((PAGES.minTextDp * PAGES.mmPerDpAt1M) / 1000, 9)
    }
    expect(plateWidthM(PAGES.distanceM)).toBeCloseTo(0.99, 2)
  })
})
