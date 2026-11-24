import { describe, expect, it } from 'vitest'
import { M_PER_PX, PAGES } from './layout'
import { PagePress } from './pagePress'
import { LOOK, START_TURN, TAP_AGAIN_MS, TURN_DRAG_PX, TURN_MS, fovFor, hold, inPicture, keysStay, leastNear, lensFor, lineOverCanvas, mostNear, nearestAim, frontAim, notAPress, WHEEL_LATCH_MS, wheelLatched, pinchNear, quietAfter, shiftAfterTurn, shiftAt, shiftShare, shiftToShow, shorterWay, shownAt, turnAt, turnTo, under, wheelNear, type Direction, type Turn, type View } from './previewLook'

/** The canvas on a phone held upright and on its side: the screen less the bars above the canvas. */
const upright = (): View => ({ width: 390, height: 760, fovDeg: fovFor(390 / 760) })
const sideways = (): View => ({ width: 844, height: 318, fovDeg: fovFor(844 / 318) })
const desk: View = { width: 1280, height: 720, fovDeg: LOOK.fovDeg }
const level: Turn = { yawDeg: 0, pitchDeg: 0 }

const close = (a: Direction, b: Direction) => { for (let i = 0; i < 3; i += 1) expect(a[i]).toBeCloseTo(b[i], 9) }

/** Where a point of the room is on the canvas, in CSS pixels, with a level head. */
function onCanvas(x: number, y: number, z: number, view: View): [number, number] {
  const t = Math.tan((view.fovDeg * Math.PI) / 360)
  return [((x / -z / (t * (view.width / view.height)) + 1) * view.width) / 2, ((1 - y / -z / t) * view.height) / 2]
}

describe('which way a point of the canvas looks', () => {
  it('looks straight ahead from the middle with a level head, and to the right after a quarter turn to the right', () => {
    close(under(level, 640, 360, desk), [0, 0, -1])
    close(under({ yawDeg: 90, pitchDeg: 0 }, 640, 360, desk), [1, 0, 0])
  })

  it('looks up from the top of the canvas and right from its right side', () => {
    expect(under(level, 640, 0, desk)[1]).toBeCloseTo(Math.sin((LOOK.fovDeg * Math.PI) / 360), 9)
    expect(under(level, 1280, 360, desk)[0]).toBeGreaterThan(0)
  })
})

describe('a drag that turns the head', () => {
  it('leaves the head alone while the finger has not moved', () => {
    const from: Turn = { yawDeg: 25, pitchDeg: -10 }
    const got = hold(under(from, 100, 500, upright()), 100, 500, upright(), from)
    expect(got.yawDeg).toBeCloseTo(25, 9)
    expect(got.pitchDeg).toBeCloseTo(-10, 9)
  })

  it.each([
    ['upright', upright(), [60, 600], [300, 420]],
    ['on its side', sideways(), [700, 40], [200, 180]],
    ['a desk', desk, [200, 100], [900, 500]],
  ] as const)('keeps what the finger took hold of under the finger (%s)', (_name, view, [x0, y0], [x1, y1]) => {
    const from: Turn = { yawDeg: -40, pitchDeg: 5 }
    const grab = under(from, x0, y0, view)
    const to = hold(grab, x1, y1, view, from)
    expect(Math.abs(to.pitchDeg)).toBeLessThan(LOOK.maxPitchDeg)
    close(under(to, x1, y1, view), grab)
  })

  it('turns the head left for a drag to the right, and up for a drag down: the room follows the finger', () => {
    const grab = under(level, 195, 380, upright())
    expect(hold(grab, 255, 380, upright(), level).yawDeg).toBeLessThan(0)
    expect(hold(grab, 195, 440, upright(), level).pitchDeg).toBeGreaterThan(0)
  })

  it('stops the pitch at its bound, both ways, and still turns sideways there', () => {
    const view = sideways()
    const grab = under(level, 422, 20, view)
    expect(hold(grab, 422, 5000, view, level).pitchDeg).toBe(LOOK.maxPitchDeg)
    expect(hold(under(level, 422, 280, view), 422, -5000, view, level).pitchDeg).toBe(-LOOK.maxPitchDeg)
    const far = hold(grab, 600, 5000, view, level)
    expect(far.pitchDeg).toBe(LOOK.maxPitchDeg)
    expect(far.yawDeg).toBeLessThan(0)
  })

  it('answers with the yaw nearest to the one so far, so a head that has gone round does not spin back', () => {
    const from: Turn = { yawDeg: 710, pitchDeg: 0 }
    const to = hold(under(from, 640, 360, desk), 660, 360, desk, from)
    expect(Math.abs(to.yawDeg - 710)).toBeLessThan(5)
  })

  it('keeps the yaw when what the finger holds is straight overhead, where there is no heading to hold', () => {
    const from: Turn = { yawDeg: 33, pitchDeg: 40 }
    expect(hold([0, 1, 0], 640, 100, desk, from).yawDeg).toBe(33)
  })
})

describe('the lens of the preview', () => {
  it('is the lens of the glasses on a wide canvas', () => {
    expect(fovFor(1280 / 720)).toBe(LOOK.fovDeg)
    expect(fovFor(844 / 318)).toBe(LOOK.fovDeg)
    expect(fovFor(0)).toBe(LOOK.fovDeg)
  })

  it('widens on a narrow canvas, and no further than its limit', () => {
    expect(fovFor(390 / 760)).toBeGreaterThan(LOOK.fovDeg)
    expect(fovFor(0.1)).toBe(LOOK.maxFovDeg)
  })

  it.each([['upright', upright()], ['on its side', sideways()], ['a desk', desk]] as const)('holds the whole first page, with air round it, on a phone held %s', (_name, view) => {
    const halfW = (PAGES.cssWidth * M_PER_PX) / 2
    const halfH = (PAGES.cssHeight * M_PER_PX) / 2
    for (const [sx, sy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
      const [x, y] = onCanvas(sx * (halfW + LOOK.marginM), sy * (halfH + LOOK.marginM), -PAGES.distanceM, view)
      expect(x).toBeGreaterThanOrEqual(-1e-6)
      expect(x).toBeLessThanOrEqual(view.width + 1e-6)
      expect(y).toBeGreaterThanOrEqual(-1e-6)
      expect(y).toBeLessThanOrEqual(view.height + 1e-6)
    }
  })
})

describe('a finger on a page: a tap or a drag', () => {
  /** How many CSS pixels of the first page one pixel of the canvas covers. */
  const pagePx = (view: View) => PAGES.distanceM / (M_PER_PX * (view.height / 2 / Math.tan((view.fovDeg * Math.PI) / 360)))

  it.each([['upright', upright()], ['on its side', sideways()]] as const)('is a press when the finger wobbles by 5 pixels of the screen (%s)', (_name, view) => {
    const press = new PagePress()
    const k = pagePx(view)
    const events = [...press.down(1, 500, 300), ...press.move(1, 500 + 3 * k, 300 + 4 * k), ...press.up(1, 500 + 3 * k, 300 + 4 * k)]
    expect(events.map(e => ('pointer' in e ? e.pointer.type : 'scroll'))).toEqual(['down', 'move', 'up'])
  })

  it.each([['upright', upright()], ['on its side', sideways()]] as const)('is a scroll, and never a press, when the finger is dragged by 30 pixels of the screen (%s)', (_name, view) => {
    const press = new PagePress()
    const k = pagePx(view)
    const events = [...press.down(1, 500, 300), ...press.move(1, 500, 300 - 30 * k), ...press.up(1, 500, 300 - 30 * k)]
    expect(events.map(e => ('pointer' in e ? e.pointer.type : 'scroll'))).toEqual(['down', 'cancel', 'scroll'])
    const last = events[2]
    // The page follows the finger: dragged up, it scrolls down by as much.
    expect('scroll' in last && last.scroll.dy).toBeCloseTo(30 * k, 6)
  })
})

describe('coming nearer with two fingers', () => {
  /** How many CSS pixels of the screen one CSS pixel of the first page covers, through a lens this tall. */
  const screenPx = (height: number, fovDeg: number) => (height / 2 / Math.tan((fovDeg * Math.PI) / 360)) * (M_PER_PX / PAGES.distanceM)

  it.each([['upright', 390, 760], ['on its side', 844, 318]] as const)('comes near enough to read on a phone held %s: one pixel of the page is one pixel of the screen, and no nearer', (_name, width, height) => {
    const most = mostNear(width, height)
    expect(most).toBeGreaterThan(1.4)
    // Stepped back to the whole first page, a page's text is too small to read.
    expect(screenPx(height, lensFor(width, height, leastNear(width, height)))).toBeLessThan(0.5)
    expect(screenPx(height, lensFor(width, height, most))).toBeCloseTo(1, 9)
    // Asking for more than the nearest gives the nearest.
    expect(lensFor(width, height, most * 3)).toBeCloseTo(lensFor(width, height, most), 9)
  })

  it('starts with a lens 50 degrees tall on every screen, with a level head', () => {
    for (const [width, height] of [[390, 760], [844, 318], [1366, 816], [1920, 1080]]) expect(lensFor(width, height, 1)).toBeCloseTo(LOOK.startFovDeg, 9)
    expect(LOOK.startFovDeg).toBe(50)
    // The head is level: the frame is shifted down instead, so that a page's sides stay upright.
    expect(START_TURN).toEqual({ yawDeg: 0, pitchDeg: 0 })
    expect(LOOK.startShift).toBeGreaterThan(0.25)
    expect(LOOK.startShift).toBeLessThan(0.35)
    // On an upright phone one pixel of the first page covers at least 0.6 px of the screen, and 0.7 on a desk.
    expect(screenPx(760, lensFor(390, 760, 1))).toBeGreaterThanOrEqual(0.6)
    expect(screenPx(816, lensFor(1366, 816, 1))).toBeGreaterThanOrEqual(0.7)
  })

  it('shifts the picture\'s frame down and keeps upright edges upright', () => {
    const view: View = { width: 1366, height: 816, fovDeg: LOOK.startFovDeg, shift: LOOK.startShift }
    const level: Turn = { yawDeg: 0, pitchDeg: 0 }
    // The level line is above the picture's middle by the shift: straight ahead is there.
    const yLevel = (view.height / 2) * (1 - LOOK.startShift)
    close(under(level, view.width / 2, yLevel, view), [0, 0, -1])
    // With no shift it is the middle.
    close(under(level, view.width / 2, view.height / 2, { ...view, shift: 0 }), [0, 0, -1])
    // An upright line of the room is an upright line of the picture: every point of one column looks the same way on the floor.
    const headings = [40, 200, 400, 640, 800].map(y => { const d = under(level, 1100, y, view); return Math.atan2(d[0], -d[2]) })
    for (const h of headings) expect(h).toBeCloseTo(headings[0], 9)
    // A head that is tipped instead makes the same column lean.
    const tipped = [40, 800].map(y => { const d = under({ yawDeg: 0, pitchDeg: -7.3 }, 1100, y, { ...view, shift: 0 }); return Math.atan2(d[0], -d[2]) })
    expect(Math.abs(tipped[0] - tipped[1])).toBeGreaterThan(0.01)
    // What a finger took hold of stays under it, with the shift as without it.
    const from: Turn = { yawDeg: 12, pitchDeg: 4 }
    const grab = under(from, 300, 500, view)
    const to = hold(grab, 420, 380, view, from)
    close(under(to, 420, 380, view), grab)
    // The picture reaches farther down than up: a page level with the eyes whose middle is 20 degrees down is in it, 20 degrees up is not.
    expect(inPicture(level, view, { yawDeg: 0, pitchDeg: -20 })).toBe(true)
    expect(inPicture(level, view, { yawDeg: 0, pitchDeg: 20 })).toBe(false)
    expect(inPicture(level, { ...view, shift: 0 }, { yawDeg: 0, pitchDeg: 20 })).toBe(true)
  })

  it('steps back as far as the lens that shows the whole first page, and no farther', () => {
    // A phone held upright: the whole page needs a wider lens than the start's.
    const least = leastNear(390, 760)
    expect(least).toBeLessThan(1)
    expect(lensFor(390, 760, least)).toBeCloseTo(fovFor(390 / 760), 9)
    expect(lensFor(390, 760, least / 5)).toBeCloseTo(fovFor(390 / 760), 9)
    // A desk: the start picture shows the whole page already. There is no farther.
    expect(leastNear(1366, 816)).toBe(1)
    expect(lensFor(1366, 816, 0.2)).toBeCloseTo(LOOK.startFovDeg, 9)
    expect(leastNear(0, 0)).toBe(1)
    // Two fingers together, and the wheel toward the person, go back that far.
    expect(pinchNear(1, 200, 10, 3, least)).toBeCloseTo(least, 9)
    expect(wheelNear(1, 5000, 3, least)).toBeCloseTo(least, 9)
    expect(pinchNear(1, 200, 10, 3)).toBe(1)
    expect(wheelNear(1, 5000, 3)).toBe(1)
  })

  it('has no nearer on a canvas whose start view already shows the page at its own size', () => {
    expect(mostNear(2560, 1440)).toBe(1)
    expect(mostNear(0, 0)).toBe(1)
  })

  it('comes nearer as the fingers move apart and goes back as they move together, between the start view and the nearest', () => {
    expect(pinchNear(1, 100, 212, 3)).toBeCloseTo(2, 9)
    expect(pinchNear(2, 200, 94, 3)).toBeCloseTo(1, 9)
    expect(pinchNear(1, 100, 1000, 3)).toBe(3)
    expect(pinchNear(2, 200, 10, 3)).toBe(1)
  })

  it('stays as near as it was while the fingers keep about their distance: they are turning the head', () => {
    expect(pinchNear(1.7, 100, 105, 3)).toBe(1.7)
    expect(pinchNear(1.7, 100, 95, 3)).toBe(1.7)
  })

  it('keeps what is under the middle of the fingers there while the eye comes nearer and the fingers move', () => {
    const view = upright()
    const from: Turn = { yawDeg: 10, pitchDeg: -5 }
    const grab = under(from, 150, 300, view)
    const near = pinchNear(1, 80, 240, mostNear(view.width, view.height))
    const nearer: View = { ...view, fovDeg: lensFor(view.width, view.height, near) }
    const to = hold(grab, 210, 420, nearer, from)
    close(under(to, 210, 420, nearer), grab)
    expect(nearer.fovDeg).toBeLessThan(view.fovDeg)
  })

  it('comes nearer for a mouse wheel turned away from the person on empty space, and goes back for one turned toward them', () => {
    expect(wheelNear(1, -100, 3)).toBeCloseTo(LOOK.wheelNear, 9)
    expect(wheelNear(LOOK.wheelNear, 100, 3)).toBeCloseTo(1, 9)
    expect(wheelNear(1, 100, 3)).toBe(1)
    expect(wheelNear(2.9, -400, 3)).toBe(3)
  })
})

describe('the one line of a panel, said over the canvas as well', () => {
  it('is said there on a phone, where the page and its line are too small to read', () => {
    expect(shownAt(upright())).toBeLessThan(LOOK.readable)
    expect(shownAt(sideways())).toBeLessThan(LOOK.readable)
    expect(lineOverCanvas(upright(), 1)).toBe(true)
    expect(lineOverCanvas(sideways(), 1)).toBe(true)
  })

  it('is not said there on a desk, where the line under the page can be read', () => {
    expect(shownAt(desk)).toBeGreaterThan(LOOK.readable)
    expect(lineOverCanvas(desk, 1)).toBe(false)
  })

  it('is said there whenever the eye is near, since the line under the page may then be out of view', () => {
    expect(lineOverCanvas(desk, 1.04)).toBe(true)
    const view = upright()
    const near = mostNear(view.width, view.height)
    expect(shownAt({ ...view, fovDeg: lensFor(view.width, view.height, near) })).toBeCloseTo(1, 9)
    expect(lineOverCanvas({ ...view, fovDeg: lensFor(view.width, view.height, near) }, near)).toBe(true)
  })
})

describe('the picture turns to a page', () => {
  it('ends with the page\'s place in the middle: its yaw and its pitch', () => {
    // The first place a new page finds with the chat in front: 60 degrees to the right, a little below level.
    expect(turnTo(level, { yawDeg: 60, pitchDeg: -PAGES.lowerDeg })).toEqual({ yawDeg: 60, pitchDeg: -PAGES.lowerDeg })
    expect(turnTo(level, { yawDeg: -60, pitchDeg: 17 })).toEqual({ yawDeg: -60, pitchDeg: 17 })
    // A page high up: the head looks no further up than its bound.
    expect(turnTo(level, { yawDeg: 0, pitchDeg: 80 }).pitchDeg).toBe(LOOK.maxPitchDeg)
  })

  it('goes the shorter way round, at half a turn and past it', () => {
    expect(shorterWay(0, 170)).toBe(170)
    expect(shorterWay(0, 180)).toBe(180)
    expect(shorterWay(0, 190)).toBe(-170)
    expect(shorterWay(0, -180)).toBe(180)
    expect(shorterWay(150, -150)).toBe(60)
    expect(shorterWay(-150, 150)).toBe(-60)
    // A head that looks 60 to the right turns left to a page at -120, and right to one at -121: the yaw is counted on.
    expect(turnTo({ yawDeg: 60, pitchDeg: 0 }, { yawDeg: -120, pitchDeg: 0 }).yawDeg).toBe(240)
    expect(turnTo({ yawDeg: 60, pitchDeg: 0 }, { yawDeg: -119, pitchDeg: 0 }).yawDeg).toBe(-119)
    expect(turnTo({ yawDeg: 60, pitchDeg: 0 }, { yawDeg: -121, pitchDeg: 0 }).yawDeg).toBe(239)
    // A head that a finger has taken round more than once stays near where it is.
    expect(turnTo({ yawDeg: 725, pitchDeg: 0 }, { yawDeg: 60, pitchDeg: 0 }).yawDeg).toBe(780)
    expect(turnTo({ yawDeg: -350, pitchDeg: 0 }, { yawDeg: 0, pitchDeg: 0 }).yawDeg).toBe(-360)
  })

  it('is eased: quick at first, slow into its place, and there when the time is up', () => {
    const to: Turn = { yawDeg: 60, pitchDeg: -8 }
    expect(turnAt(level, to, 0)).toEqual(level)
    expect(turnAt(level, to, 1)).toEqual(to)
    expect(turnAt(level, to, 7)).toEqual(to)
    const half = turnAt(level, to, 0.5)
    expect(half.yawDeg).toBeGreaterThan(30)
    expect(half.yawDeg).toBeLessThan(60)
    expect(TURN_MS).toBe(400)
  })

  it('holds a touch back while it turns by itself, and for half a second from the moment the pages began to move', () => {
    expect(notAPress(true, 1000, 0)).toBe(true)
    // The pages began to move at 1000: a touch a moment later was aimed at where they stood before.
    expect(notAPress(false, 1300, 1000 + TAP_AGAIN_MS)).toBe(true)
    expect(notAPress(false, 1000 + TAP_AGAIN_MS, 1000 + TAP_AGAIN_MS)).toBe(false)
    // A jump (reduced motion) is no turn under way: the half second from the jump holds the next touch back.
    expect(notAPress(false, 2000 + TAP_AGAIN_MS - 1, 2000 + TAP_AGAIN_MS)).toBe(true)
    // With no turn, and none just ended, a touch is what it always is.
    expect(notAPress(false, 1000, 0)).toBe(false)
    expect(TAP_AGAIN_MS).toBe(500)
  })

  it('starts the half second anew for each touch that is held back after a close, and not after the plus', () => {
    // A page closed at 1000. Taps go on at the close mark's place, 417 ms apart: each one is held back, however long the run.
    let until = 1000 + TAP_AGAIN_MS
    for (const at of [1417, 1834, 2251, 2668]) {
      expect(notAPress(false, at, until)).toBe(true)
      until = quietAfter(until, at, true)
      expect(until).toBe(at + TAP_AGAIN_MS)
    }
    // Only a tap that comes after a pause of half a second is a touch again.
    expect(notAPress(false, 2668 + TAP_AGAIN_MS - 1, until)).toBe(true)
    expect(notAPress(false, 2668 + TAP_AGAIN_MS, until)).toBe(false)
    // A touch that is held back while the picture still turns after a close never shortens the time.
    expect(quietAfter(5000, 1200, true)).toBe(5000)
    // After the plus the time is the plain half second from the moment the picture began to move.
    expect(quietAfter(1000 + TAP_AGAIN_MS, 1300, false)).toBe(1000 + TAP_AGAIN_MS)
    expect(notAPress(false, 1000 + TAP_AGAIN_MS, quietAfter(1000 + TAP_AGAIN_MS, 1300, false))).toBe(false)
    expect(TURN_DRAG_PX).toBe(8)
  })

  it('lets a page keep the keys only while the person can see it', () => {
    // An upright phone at the start view: the picture is about one page wide. A page 1.75 m away, 1.0 m by 0.7 m.
    const view: View = { width: 390, height: 700, fovDeg: 60 }
    const size = [1.0, 0.7] as const
    const chat = { yawDeg: 0, pitchDeg: 0, distanceM: 1.75 }
    const level: Turn = { yawDeg: 0, pitchDeg: 0 }
    expect(keysStay(level, view, chat, size)).toBe(true)
    // The picture turned to a new page, a sixth of a turn away: the chat is out of it, and the keys go.
    expect(inPicture({ yawDeg: 60, pitchDeg: 0 }, view, chat)).toBe(false)
    expect(keysStay({ yawDeg: 60, pitchDeg: 0 }, view, chat, size)).toBe(false)
    expect(keysStay({ yawDeg: -60, pitchDeg: 0 }, view, chat, size)).toBe(false)
    // Turned a little, with the page's middle still in the picture: the keys stay.
    expect(keysStay({ yawDeg: 12, pitchDeg: 0 }, view, chat, size)).toBe(true)
    // A phone on its side with the eye near: the picture is a strip of the page's lower part. The page's middle is
    // above the picture's top, and the picture's middle is on the page: the person types into what they see.
    const near: View = { width: 844, height: 300, fovDeg: 6 }
    const low: Turn = { yawDeg: 2, pitchDeg: -9 }
    expect(inPicture(low, near, chat)).toBe(false)
    expect(keysStay(low, near, chat, size)).toBe(true)
    // The same strip turned off the page's side: the keys go.
    expect(keysStay({ yawDeg: 20, pitchDeg: -9 }, near, chat, size)).toBe(false)
    // And looked down under the page.
    expect(keysStay({ yawDeg: 0, pitchDeg: -14 }, near, chat, size)).toBe(false)
  })

  it('latches the wheel to the nearness at the place of its last notch on empty space, for a short while', () => {
    const last = { x: 400, y: 500, at: 1000 }
    expect(wheelLatched(null, 400, 500, 1000)).toBe(false)
    expect(wheelLatched(last, 400, 500, 1100)).toBe(true)
    expect(wheelLatched(last, 405, 506, 1100)).toBe(true)
    expect(wheelLatched(last, 420, 500, 1100)).toBe(false)
    expect(wheelLatched(last, 400, 500, 1000 + WHEEL_LATCH_MS)).toBe(false)
  })

  it('finds the page in front: of the pages in the picture the one nearest its middle, and none when none is in it', () => {
    const view = desk
    const chat = { id: 'page', yawDeg: 0, pitchDeg: -PAGES.lowerDeg }
    const right = { id: 'open-1', yawDeg: 20, pitchDeg: -PAGES.lowerDeg }
    const behind = { id: 'open-2', yawDeg: 180, pitchDeg: -PAGES.lowerDeg }
    expect(frontAim(level, view, [right, chat, behind])?.id).toBe('page')
    expect(frontAim({ yawDeg: 15, pitchDeg: 0 }, view, [right, chat, behind])?.id).toBe('open-1')
    expect(frontAim({ yawDeg: 90, pitchDeg: 0 }, view, [right, chat])).toBeNull()
  })

  it('finds the nearest page that is left when no page is in the picture, and none when one is', () => {
    const view = upright()
    const chat = { id: 'page', yawDeg: 0, pitchDeg: -PAGES.lowerDeg }
    const right = { id: 'open-1', yawDeg: 60, pitchDeg: -PAGES.lowerDeg }
    const left = { id: 'open-2', yawDeg: -60, pitchDeg: -PAGES.lowerDeg }
    // An upright phone shows about one page: the page 60 degrees to the side is not in the picture.
    expect(inPicture(level, view, chat)).toBe(true)
    expect(inPicture(level, view, right)).toBe(false)
    expect(inPicture({ yawDeg: 60, pitchDeg: 0 }, view, right)).toBe(true)
    // The page in front was closed: the picture goes to the nearer of the two that are left.
    expect(nearestAim({ yawDeg: 10, pitchDeg: 0 }, view, [right, left])?.id).toBe('open-1')
    expect(nearestAim({ yawDeg: -10, pitchDeg: 0 }, view, [right, left])?.id).toBe('open-2')
    // The nearest is counted the shorter way round.
    expect(nearestAim({ yawDeg: 170, pitchDeg: 0 }, view, [{ id: 'a', yawDeg: -150, pitchDeg: 0 }, { id: 'b', yawDeg: 100, pitchDeg: 0 }])?.id).toBe('a')
    // A page is still in the picture, or no page is left: nothing turns.
    expect(nearestAim(level, view, [chat, right])).toBeNull()
    expect(nearestAim(level, view, [])).toBeNull()
  })
})

describe('the frame\'s shift after the picture turned to a page', () => {
  const page = { distanceM: PAGES.distanceM, heightM: PAGES.cssHeight * M_PER_PX }
  const shapes: View[] = [
    { width: 1366, height: 816, fovDeg: LOOK.startFovDeg },
    { width: 390, height: 772, fovDeg: LOOK.startFovDeg },
    { width: 844, height: 318, fovDeg: LOOK.startFovDeg },
  ]
  /** How many metres of the page one pixel of the screen covers, and a close mark of 44 px. */
  const mark = (view: View) => 44 * ((2 * page.distanceM * Math.tan((view.fovDeg * Math.PI) / 360)) / view.height)
  /** How high over the page's middle the picture's row `y` looks, at the page's distance, with a level head. */
  const heightAt = (view: View, y: number) => { const d = under({ yawDeg: 0, pitchDeg: 0 }, view.width / 2, y, view); return (d[1] / -d[2]) * page.distanceM }

  it('is all in force up to the start\'s lens, and none from 1.3 times nearer on', () => {
    expect(shiftShare(0.6)).toBe(1)
    expect(shiftShare(1)).toBe(1)
    expect(shiftShare(1.15)).toBeCloseTo(0.5, 9)
    expect(shiftShare(LOOK.noShiftNear)).toBe(0)
    expect(shiftShare(3)).toBe(0)
  })

  it('puts the close mark\'s upper edge four pixels under the picture\'s upper edge, on every shape', () => {
    for (const view of shapes) {
      const shift = shiftToShow(view, page, mark(view))
      expect(shift).toBeGreaterThan(0)
      expect(shift).toBeLessThan(LOOK.startShift)
      // The row four pixels under the picture's upper edge looks at the upper edge of the mark's square.
      expect(heightAt({ ...view, shift }, LOOK.markAirPx)).toBeCloseTo(page.heightM / 2 + mark(view), 6)
      // With the start's shift that square is over the picture's upper edge.
      expect(heightAt({ ...view, shift: LOOK.startShift }, 0)).toBeLessThan(page.heightM / 2 + mark(view))
    }
  })

  it('is nothing for a page that is taller than the picture', () => {
    expect(shiftToShow(shapes[0], { distanceM: 1, heightM: 3 }, 0.1)).toBe(0)
    expect(shiftToShow({ width: 0, height: 0, fovDeg: 50 }, page, 0.1)).toBe(0)
  })

  it('never grows, and stays as it is where no shift is in force', () => {
    const view = shapes[0]
    const most = shiftToShow(view, page, mark(view))
    expect(shiftAfterTurn(LOOK.startShift, 1, view, page, mark(view))).toBeCloseTo(most, 12)
    // A shift that is smaller already stays: the picture of a person who has it so, and of a test with none.
    expect(shiftAfterTurn(0, 1, view, page, mark(view))).toBe(0)
    expect(shiftAfterTurn(0.1, 1, view, page, mark(view))).toBe(0.1)
    // Nearer, the share in force is what must fit.
    const near = 1.15
    const lens: View = { ...view, fovDeg: lensFor(view.width, view.height, near) }
    const base = shiftAfterTurn(LOOK.startShift, near, lens, page, mark(lens))
    expect(base).toBeLessThanOrEqual(LOOK.startShift)
    expect(base * shiftShare(near)).toBeLessThanOrEqual(shiftToShow(lens, page, mark(lens)) + 1e-12)
    // Y23: so near that no shift is in force, the turn still leaves the mark its room at the start's lens, where the
    // whole shift is in force again once the eye steps back.
    const nearest: View = { ...view, fovDeg: lensFor(view.width, view.height, LOOK.noShiftNear) }
    expect(shiftAfterTurn(LOOK.startShift, LOOK.noShiftNear, nearest, page, mark(nearest), mark(view))).toBeCloseTo(most, 12)
  })

  it('goes with the turn: at its start where it was, at its end where it must be, quick at first', () => {
    expect(shiftAt(0.317, 0.2, 0)).toBeCloseTo(0.317, 12)
    expect(shiftAt(0.317, 0.2, 1)).toBeCloseTo(0.2, 12)
    expect(shiftAt(0.317, 0.2, 5)).toBeCloseTo(0.2, 12)
    expect(shiftAt(0.317, 0.2, 0.5)).toBeLessThan((0.317 + 0.2) / 2)
  })
})
