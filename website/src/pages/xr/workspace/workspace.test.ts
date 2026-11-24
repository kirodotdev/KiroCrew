import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { Vector3 } from 'three'
import { describe, expect, it } from 'vitest'
import { DETAIL_STEPS, M_PER_PX, PAGES, RINGS, crossing, depthScale, detailStep, dragCorner, eyePxPerCssPx, facing, inZone, minTextPx, placeAt, placeNew, ring, sizeFrom, slotOf, type Standing } from './layout'
import { PagePress } from './pagePress'
import { PinchGate } from './pinchGate'
import { LIVE_SHAPES, SHUT_SHAPES, listedPath, livePath } from './streamPath'

const DEG = Math.PI / 180

describe('how big a page is in the room', () => {
  it('makes one CSS pixel one dp where a panel opens, so 14 px text is the 14 dp that Android XR asks for', () => {
    // Android XR: 1024 dp is seen as 1556.24 mm from 1.75 m.
    expect(Math.abs(1024 * M_PER_PX * 1000 - 1556.24)).toBeLessThan(1)
    expect(minTextPx(PAGES.distanceM)).toBeCloseTo(14, 6)
  })

  it('needs bigger text the farther a panel is pushed, and smaller the nearer it is pulled', () => {
    expect(minTextPx(1)).toBeCloseTo(8, 6)
    expect(minTextPx(3.5)).toBeCloseTo(28, 6)
  })

  it('opens a page at the size of an Android XR window, inside the middle 50 degrees of what the Aura shows (61.4 by 40.7)', () => {
    expect([PAGES.cssWidth, PAGES.cssHeight]).toEqual([1024, 720])
    const w = 2 * Math.atan((PAGES.cssWidth * M_PER_PX) / 2 / PAGES.distanceM) / DEG
    const h = 2 * Math.atan((PAGES.cssHeight * M_PER_PX) / 2 / PAGES.distanceM) / DEG
    expect(w).toBeGreaterThan(45)
    expect(w).toBeLessThan(50)
    expect(h).toBeLessThan(40.7)
  })
})

describe('how many pixels a live page asks for', () => {
  // The Aura's one eye: 1200 pixels for 40.7 degrees up and down.
  const focal = 1 / Math.tan((40.7 / 2) * DEG)

  it('counts 1.4 pixels of the display for a CSS pixel where a panel opens, and half of that twice as far', () => {
    expect(eyePxPerCssPx(PAGES.distanceM, 1200, focal)).toBeCloseTo(1.4, 1)
    expect(eyePxPerCssPx(PAGES.distanceM * 2, 1200, focal)).toBeCloseTo(0.7, 1)
  })

  it('asks for the smallest step that is enough: 2 where a panel opens, 1 at 4 m, and never more than 2', () => {
    const want = (distanceM: number) => eyePxPerCssPx(distanceM, 1200, focal) * PAGES.texelsPerEyePx
    expect(detailStep(want(PAGES.distanceM), 2)).toBe(2)
    expect(detailStep(want(4), 2)).toBe(1)
    expect(detailStep(want(6), 2)).toBe(0.75)
    expect(detailStep(want(0.5), 1)).toBe(2)
    for (const step of DETAIL_STEPS) expect(detailStep(step, 0.5)).toBe(step)
  })

  it('keeps the step it has for a panel that sits right at a step\'s edge, so a small move makes no new picture', () => {
    expect(detailStep(1.38, 2)).toBe(2)
    expect(detailStep(1.25, 2)).toBe(1.4)
    expect(detailStep(1.41, 1.4)).toBe(2)
    expect(detailStep(0.98, 2)).toBe(1.4)
    expect(detailStep(0.98, 1.4)).toBe(1.4)
    expect(detailStep(Number.NaN, 1.4)).toBe(1.4)
    expect(detailStep(0, 1)).toBe(1)
  })

  it('names the pages that a panel may show with the gateway\'s guard on', () => {
    for (const path of ['/chat', '/chat/dashboard-ab12', '/members', '/notifications', '/sessions', '/session-dashboards', '/overview', '/crew-board', '/schedule', '/projects', '/projects/p1/tasks/t-2', '/tasks', '/artifacts', '/artifacts/report-1', '/logs', '/settings', '/settings/browser']) expect(listedPath(path), path).toBe(true)
    for (const path of ['/', '/xr', '/chat?x=1', '//evil.example/chat', 'https://evil.example/chat', '/chat/../members', '/deploy', '/artifacts/deploy', '/artifacts/remote/github/42', '/apps', '/apps/notes', '/channels', '/connections', '/instances', '/mcp', '/hooks', '/webhooks', '/agents', '/mc-agents', '/capabilities', '/developer', '/knowledge', '/settings/people', '/settings/connections', '/settings/instances', '/popout/chat', '/embed/chat', '/aws-control', '']) expect(listedPath(path), path).toBe(false)
  })

  it('asks the gateway for any page but the few that open in neither of its ways', () => {
    for (const path of ['/chat', '/settings/connections', '/connections', '/capabilities', '/apps/notes', '/knowledge', '/xrays', '/artifacts/remote-notes', '/settings/people-search']) expect(livePath(path), path).toBe(path)
    for (const path of ['/xr', '/XR', '/xr/', '//xr', '/%78r', '/xr/x', '/deploy', '/Deploy/', '/artifacts/deploy', '/artifacts/remote/github/42', '/settings/people', '/settings/People/invite', '/settings%2Fpeople', '/settings/instances', '/instances', '/instances/', '//evil.example/chat', '/\\evil.example/chat']) expect(livePath(path), path).toBeNull()
    // Nor what is not a plain path: a query or a fragment, another origin, a dot segment, a space, a path that cannot be decoded.
    for (const path of ['', 'chat', '/chat?x=1', '/chat?autoSend=1', '/chat#x', 'https://evil.example/chat', '/chat/../members', '/a/%2e%2e/xr', '/chat x', '/ch\u00e4t', '/%zz', `/${'a'.repeat(300)}`, null, undefined]) expect(livePath(path), String(path)).toBeNull()
  })

  it('holds what the gateway keeps shut in both ways, one for one', () => {
    const page = readFileSync(resolve(__dirname, '../../../../../src/kiro_crew/browser_harness/panel_page.py'), 'utf8')
    const list = /SHUT_SHAPES: tuple\[re\.Pattern\[str\], \.\.\.\] = \(([\s\S]*?)\n\)\n/.exec(page)?.[1] ?? ''
    const shapes = [...list.matchAll(/re\.compile\(\s*r"([^"]+)"\s*\)/g)].map(m => m[1])
    expect(shapes.length).toBeGreaterThan(3)
    expect([...SHUT_SHAPES]).toEqual(shapes)
  })

  it('holds the gateway\'s own list of shapes, one for one', () => {
    const page = readFileSync(resolve(__dirname, '../../../../../src/kiro_crew/browser_harness/panel_page.py'), 'utf8')
    const list = /PATH_SHAPES: tuple\[re\.Pattern\[str\], \.\.\.\] = \(([\s\S]*?)\n\)\n/.exec(page)?.[1] ?? ''
    const shapes = [...list.matchAll(/re\.compile\(\s*r"([^"]+)"\s*\)/g)].map(m => m[1])
    expect(shapes.length).toBeGreaterThan(3)
    expect([...LIVE_SHAPES]).toEqual(shapes)
  })
})

describe('where panels stand', () => {
  const head = new Vector3(0, 1.6, 0)

  it('puts one panel straight ahead, 1.4 degrees under the eyes (D74), at the distance it opens at', () => {
    const [one] = ring(1)
    const at = placeAt(head, one.yawDeg, one.pitchDeg, PAGES.distanceM)
    expect(at.distanceTo(head)).toBeCloseTo(PAGES.distanceM, 6)
    expect(at.x).toBeCloseTo(0, 6)
    expect(at.z).toBeLessThan(0)
    expect(at.y).toBeCloseTo(head.y + PAGES.distanceM * Math.sin((PAGES.lowerDeg * Math.PI) / 180), 6)
  })

  it('stands three side by side, six all the way round, and twelve in two rings, the second over the first', () => {
    expect(ring(3).map(p => p.yawDeg)).toEqual([-60, 0, 60])
    expect(new Set(ring(6).map(p => p.yawDeg)).size).toBe(6)
    expect(new Set(ring(6).map(p => p.pitchDeg))).toEqual(new Set([PAGES.lowerDeg]))
    const twelve = ring(12)
    expect(new Set(twelve.map(p => `${p.yawDeg}:${p.pitchDeg}`)).size).toBe(12)
    // No ring stands under the level one: that place is the crew's.
    expect(new Set(twelve.map(p => p.pitchDeg))).toEqual(new Set([RINGS.level.pitchDeg, RINGS.upper.pitchDeg]))
    // The level ring stands 1.4 degrees under level (D74), and every reader of it takes the same signed pitch.
    expect(RINGS.level.pitchDeg).toBe(PAGES.lowerDeg)
    expect(PAGES.lowerDeg).toBe(-1.4)
    expect(RINGS.upper.pitchDeg).toBeGreaterThan(0)
    // A ring of six pages of full size fits round a person only from a little farther off: their 14 px text is then just under 14 dp.
    expect(minTextPx(twelve[0].distanceM)).toBeGreaterThan(14)
  })

  it('lets no two panels cross: each is flat, faces the head, and ends before the plane of its neighbour', () => {
    const w = PAGES.cssWidth * M_PER_PX
    const h = PAGES.cssHeight * M_PER_PX
    for (const n of [1, 2, 3, 6, 12]) {
      const panels = ring(n).map(p => {
        const at = placeAt(head, p.yawDeg, p.pitchDeg, p.distanceM)
        const q = facing(at, head)
        const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => new Vector3((x * w) / 2, (y * h) / 2, 0).applyQuaternion(q).add(at))
        return { at, front: new Vector3(0, 0, 1).applyQuaternion(q), corners }
      })
      // Every corner of every panel is on the head's side of every other panel's plane: then no two can cross.
      // The second ring leans back over the first, so that holds within a ring; between the two rings, no corner
      // of an upper panel is as low as the top of a level one.
      const upper = (i: number) => i >= 6
      panels.forEach((a, i) => panels.forEach((b, j) => {
        if (i === j) return
        if (upper(i) === upper(j)) for (const c of a.corners) expect(c.clone().sub(b.at).dot(b.front)).toBeGreaterThan(0.01)
        else if (upper(i)) expect(Math.min(...a.corners.map(c => c.y))).toBeGreaterThan(Math.max(...b.corners.map(c => c.y)) + 0.05)
      }))
    }
  })

  it('turns a panel to face the head, level, wherever it stands', () => {
    for (const [yaw, pitch] of [[0, 0], [70, 0], [-140, 20], [180, -10]]) {
      const at = placeAt(head, yaw, pitch, 2)
      const q = facing(at, head)
      const front = new Vector3(0, 0, 1).applyQuaternion(q)
      expect(front.angleTo(head.clone().sub(at))).toBeLessThan(1e-6)
      // No roll: its own right stays level.
      expect(new Vector3(1, 0, 0).applyQuaternion(q).y).toBeCloseTo(0, 6)
    }
  })
})

describe('a held panel comes nearer and goes farther', () => {
  it('doubles its distance for a hand pushed out by one step, halves it for a hand pulled back, and stays for a hand that stays', () => {
    expect(depthScale(0)).toBe(1)
    expect(depthScale(PAGES.depthDoubleM)).toBeCloseTo(2, 9)
    expect(depthScale(-PAGES.depthDoubleM)).toBeCloseTo(0.5, 9)
    // The same push from any distance is the same ratio: far panels are not slower to bring back.
    expect(depthScale(0.1) * depthScale(0.1)).toBeCloseTo(depthScale(0.2), 9)
  })
})

describe('a corner that is dragged', () => {
  const rect = { cx: 0, cy: 0, w: 2, h: 1 }
  const min: [number, number] = [0.5, 0.5]
  const max: [number, number] = [4, 3]

  it('follows the hand and keeps the corner opposite where it is', () => {
    const r = dragCorner(rect, 1, 1, 2, 1.5, min, max)
    expect([r.w, r.h]).toEqual([3, 2])
    // The bottom left corner did not move.
    expect([r.cx - r.w / 2, r.cy - r.h / 2]).toEqual([-1, -0.5])
    const l = dragCorner(rect, -1, -1, -2, -1, min, max)
    expect([l.w, l.h]).toEqual([3, 1.5])
    expect([l.cx + l.w / 2, l.cy + l.h / 2]).toEqual([1, 0.5])
  })

  it('stops at the smallest and at the biggest size, and never turns inside out', () => {
    expect(dragCorner(rect, 1, 1, -5, -5, min, max)).toMatchObject({ w: 0.5, h: 0.5 })
    expect(dragCorner(rect, 1, 1, 50, 50, min, max)).toMatchObject({ w: 4, h: 3 })
  })

  it('takes the size the source gives, about the same fixed corner', () => {
    const asked = dragCorner(rect, 1, 1, 2, 1.5, min, max)
    const given = sizeFrom(asked, 1, 1, 2.5, 1.5625)
    expect([given.cx - given.w / 2, given.cy - given.h / 2]).toEqual([-1, -0.5])
  })
})

describe('a pinch that the tracker drops for a frame', () => {
  it('presses at once, and lets go only after the pinch has stayed open for the whole wait', () => {
    const gate = new PinchGate(70)
    expect(gate.start()).toBe('down')
    expect(gate.end(1000)).toBeNull()
    expect(gate.tick(1069)).toBeNull()
    expect(gate.held).toBe(true)
    expect(gate.tick(1070)).toBe('up')
    expect(gate.held).toBe(false)
    expect(gate.tick(1100)).toBeNull()
  })

  it('goes on as one press when the pinch is back before the wait is over: nothing is let go, nothing is pressed again', () => {
    const gate = new PinchGate(70)
    gate.start()
    gate.end(1000)
    expect(gate.start()).toBeNull()
    expect(gate.tick(2000)).toBeNull()
    expect(gate.held).toBe(true)
    expect(gate.end(3000)).toBeNull()
    expect(gate.tick(3070)).toBe('up')
  })

  it('with no wait takes the session at its word: each end is a release and each start a new press', () => {
    const raw = new PinchGate(0)
    expect(raw.start()).toBe('down')
    expect(raw.end(1000)).toBe('up')
    expect(raw.start()).toBe('down')
  })

  it('cancels a held press when the hand is gone, and has nothing to cancel otherwise', () => {
    const gate = new PinchGate(70)
    expect(gate.drop()).toBeNull()
    gate.start()
    expect(gate.drop()).toBe('cancel')
    expect(gate.start()).toBe('down')
  })
})

describe('a pointer on a page', () => {
  it('moves the page pointer, and a press that stays put is a click', () => {
    const press = new PagePress()
    expect(press.move(1, 10, 20)).toEqual([{ pointer: { type: 'move', pointerId: 1, x: 10, y: 20 } }])
    expect(press.down(1, 10, 20)).toEqual([{ pointer: { type: 'down', pointerId: 1, x: 10, y: 20 } }])
    // A hand shakes a little while it pinches: that is still a click.
    expect(press.move(1, 14, 26)).toEqual([{ pointer: { type: 'move', pointerId: 1, x: 14, y: 26 } }])
    expect(press.up(1, 14, 26)).toEqual([{ pointer: { type: 'up', pointerId: 1, x: 14, y: 26 } }])
    expect(press.held(1)).toBe(false)
  })

  it('turns a press that is dragged into a scroll: the click is cancelled once, the page follows the hand, and no release is told', () => {
    const press = new PagePress()
    press.down(1, 100, 300)
    const first = press.move(1, 100, 300 - PAGES.scrollStartPx - 6)
    expect(first[0]).toEqual({ pointer: { type: 'cancel', pointerId: 1, x: 100, y: 270 } })
    // Dragged up by 30, the page scrolls down by 30. The scroll stays on what lay under the press when it began, as a
    // browser keeps a touch's scroll on the element it started on (the eighth pass): the place is the down's.
    expect(first[1]).toEqual({ scroll: { x: 100, y: 300, dx: 0, dy: 30 } })
    expect(press.move(1, 100, 250)).toEqual([{ scroll: { x: 100, y: 300, dx: 0, dy: 20 } }])
    // A drag that goes on past the list's edge (onto the field under it) still scrolls the list.
    expect(press.move(1, 100, 120)).toEqual([{ scroll: { x: 100, y: 300, dx: 0, dy: 130 } }])
    expect(press.up(1, 100, 120)).toEqual([])
  })

  it('keeps each hand its own press', () => {
    const press = new PagePress()
    press.down(1, 0, 0)
    expect(press.move(2, 500, 500)).toEqual([{ pointer: { type: 'move', pointerId: 2, x: 500, y: 500 } }])
    expect(press.held(1)).toBe(true)
    expect(press.held(2)).toBe(false)
  })

  it('tells the page a pointer left only when it holds no press there', () => {
    const press = new PagePress()
    expect(press.leave(1, 0, 0)).toEqual([{ pointer: { type: 'leave', pointerId: 1, x: 0, y: 0 } }])
    press.down(1, 5, 5)
    expect(press.leave(1, 0, 0)).toEqual([])
    expect(press.cancel(1, 0, 0)).toEqual([{ pointer: { type: 'cancel', pointerId: 1, x: 0, y: 0 } }])
  })
})

describe('where a new page stands', () => {
  const W = PAGES.cssWidth * M_PER_PX, H = PAGES.cssHeight * M_PER_PX
  const page = (id: string, yawDeg: number, pitchDeg = 0, distanceM = 1.8, user = false, widthM = W, heightM = H): Standing => ({ id, at: { yawDeg, pitchDeg, distanceM }, widthM, heightM, user })
  /** Open `n` pages one after the other, each where `placeNew` says, and move the ones it moves. */
  function openAll(n: number, start: Standing[] = []): { pages: Standing[]; crossed: number } {
    const pages = start.map(p => ({ ...p }))
    let crossed = 0
    for (let i = 0; i < n; i += 1) {
      const got = placeNew(pages, W, H)
      if (got.crosses) crossed += 1
      for (const m of got.moved) { const p = pages.find(x => x.id === m.id)!; expect(p.user).toBe(false); p.at = m.at }
      pages.push(page(`n${i}`, got.at.yawDeg, got.at.pitchDeg, got.at.distanceM))
    }
    return { pages, crossed }
  }
  const clearOfEachOther = (pages: Standing[]) => pages.every((a, i) => pages.every((b, j) => j <= i || crossing(a, b) === 0))

  it('reads a place in the room as the head sees it, and back', () => {
    const head = new Vector3(0.2, 1.6, -0.1)
    const at = slotOf(head, placeAt(head, 60, 17, 2.1))
    expect(at.yawDeg).toBeCloseTo(60, 6)
    expect(at.pitchDeg).toBeCloseTo(17, 6)
    expect(at.distanceM).toBeCloseTo(2.1, 6)
  })

  it('says that two pages cross when one is in front of the other, and not when they stand side by side', () => {
    expect(crossing(page('a', 0), page('b', 60))).toBe(0)
    expect(crossing(page('a', 0), page('b', 30))).toBeGreaterThan(0)
    // The same direction, farther away: it would stand behind the first.
    expect(crossing(page('a', 0, 0, 1.75), page('b', 0, 0, 3))).toBeGreaterThan(0)
    // Round the back: 170 and -170 are 20 degrees apart, not 340.
    expect(crossing(page('a', 170), page('b', -170))).toBeGreaterThan(0)
    // A page that leans up to the head is a wider angle at its top edge.
    expect(crossing(page('a', 0, 51, 2.1), page('b', 60, 51, 2.1))).toBeGreaterThan(0)
    expect(crossing(page('a', 0, 51, 2.1), page('b', 120, 51, 2.1))).toBe(0)
  })

  it('puts the first page straight ahead where a window opens, and the next two beside it', () => {
    const { pages } = openAll(3)
    expect(pages.map(p => [p.at.yawDeg, p.at.pitchDeg, p.at.distanceM])).toEqual([[0, PAGES.lowerDeg, 1.75], [60, PAGES.lowerDeg, 1.75], [-60, PAGES.lowerDeg, 1.75]])
  })

  it('fills one level ring with six pages, and moves none of them to do it', () => {
    const pages: Standing[] = []
    for (let i = 0; i < 6; i += 1) {
      const got = placeNew(pages, W, H)
      expect(got).toMatchObject({ moved: [], crosses: false })
      pages.push(page(`n${i}`, got.at.yawDeg, got.at.pitchDeg, got.at.distanceM))
    }
    expect(new Set(pages.map(p => p.at.yawDeg))).toEqual(new Set([0, 60, -60, 120, -120, 180]))
    expect(pages.every(p => p.at.pitchDeg === PAGES.lowerDeg)).toBe(true)
    expect(clearOfEachOther(pages)).toBe(true)
  })

  it('stands the seventh page over the first and moves none of the six, and holds twelve with none crossing another', () => {
    const six = openAll(6).pages
    const seventh = placeNew(six, W, H)
    expect(seventh).toMatchObject({ at: { yawDeg: 0, ...RINGS.upper }, moved: [], crosses: false })
    const twelve = openAll(12)
    expect(twelve.crossed).toBe(0)
    expect(clearOfEachOther(twelve.pages)).toBe(true)
    expect(twelve.pages.filter(p => p.at.pitchDeg === RINGS.level.pitchDeg)).toHaveLength(6)
    expect(twelve.pages.filter(p => p.at.pitchDeg === RINGS.upper.pitchDeg)).toHaveLength(6)
    // The first six stand where they stood when they were six.
    expect(twelve.pages.slice(0, 6).map(p => p.at)).toEqual(six.map(p => p.at))
  })

  it('finds a place for a thirteenth page too, low and behind and clear of the twelve, and moves none of them', () => {
    const { pages } = openAll(12)
    const got = placeNew(pages, W, H)
    expect(got).toMatchObject({ moved: [], crosses: false })
    expect(got.at.pitchDeg).toBeLessThan(-30)
    expect(Math.abs(got.at.yawDeg)).toBeGreaterThanOrEqual(90)
    expect(clearOfEachOther([...pages, page('n12', got.at.yawDeg, got.at.pitchDeg, got.at.distanceM)])).toBe(true)
  })

  it('stands no page in a part of the view that is kept out, and says when a page reaches into one', () => {
    const zone = { yawMinDeg: -50, yawMaxDeg: 50, pitchMinDeg: -40, pitchMaxDeg: -19 }
    // A page that is level with the eyes ends over the zone; pulled down, or low and straight ahead, it is in it.
    expect(inZone(page('level', 0, 0, 1.75), zone)).toBe(false)
    expect(inZone(page('pulled', 0, -4, 1.75), zone)).toBe(true)
    expect(inZone(page('low', 0, -17, 2.1), zone)).toBe(true)
    expect(inZone(page('low at the side', 60, -17, 2.1), zone)).toBe(true)
    // Round the back, or over the zone, it is not.
    expect(inZone(page('behind', 180, -17, 2.1), zone)).toBe(false)
    expect(inZone(page('at the side', 120, -17, 2.1), zone)).toBe(false)
    expect(inZone(page('over', 0, 34, 2.2), zone)).toBe(false)
    // Thirteen newcomers, and a page that a hand stood in the way of the first place: none is stood in the zone.
    const pages: Standing[] = [page('mine', 20, 6, 1.75, true)]
    for (let i = 0; i < 13; i += 1) {
      const got = placeNew(pages, W, H, 0, [zone])
      for (const m of got.moved) pages.find(x => x.id === m.id)!.at = m.at
      pages.push(page(`n${i}`, got.at.yawDeg, got.at.pitchDeg, got.at.distanceM))
    }
    expect(pages.filter(p => inZone(p, zone))).toEqual([])
  })

  // Nine and no more: with these two in the way, and no place under the level ring in front, the tenth finds none that is clear.
  it.each([1, 3, 6, 7, 9])('never moves a page that the person placed, with %i newcomers', n => {
    // The person pulled one page low and near, and pushed one far off to the left, bigger than it opened.
    const mine = [page('near', 25, -8, 1.1, true), page('far', -95, 12, 4, true, W * 1.6, H * 1.4)]
    const before = JSON.stringify(mine)
    const { pages, crossed } = openAll(n, mine)
    expect(JSON.stringify(pages.slice(0, 2))).toBe(before)
    expect(crossed).toBe(0)
    expect(clearOfEachOther(pages)).toBe(true)
  })

  it('keeps a newcomer off a page that the person stood between two places of the ring', () => {
    const got = placeNew([page('mine', 30, 0, 1.75, true)], W, H)
    expect(got.crosses).toBe(false)
    expect(crossing({ at: got.at, widthM: W, heightM: H }, page('mine', 30, 0, 1.75, true))).toBe(0)
    // Neither straight ahead nor the first place to the right is clear of it.
    expect(got.at.yawDeg).toBe(-60)
  })

  it('says so when every place crosses a page that the person placed, moves nothing, and crosses the least it can', () => {
    // Pages that the person made very large, all round and up and down: no place is left.
    const walls = [0, 90, 180, -90].flatMap((yaw, i) => [page(`w${i}`, yaw, 30, 1.2, true, 6, 3), page(`v${i}`, yaw, -30, 1.2, true, 6, 3)])
    const before = JSON.stringify(walls)
    const got = placeNew(walls, W, H)
    expect(got.crosses).toBe(true)
    expect(got.moved).toEqual([])
    expect(JSON.stringify(walls)).toBe(before)
  })

  it('stands a new page in front of where the person looks, in the free place nearest to that', () => {
    const first = [page('a', 0, 0, 1.75)]
    // Looking straight ahead, the list's own order: the first place to the right.
    expect(placeNew(first, W, H).at.yawDeg).toBe(60)
    expect(placeNew(first, W, H, 0).at.yawDeg).toBe(60)
    // Looking to the left, to the right and behind: the place of the ring that is nearest.
    expect(placeNew(first, W, H, -70).at.yawDeg).toBe(-60)
    expect(placeNew(first, W, H, 100).at.yawDeg).toBe(120)
    expect(placeNew(first, W, H, 175).at.yawDeg).toBe(180)
    expect(placeNew(first, W, H, -175).at.yawDeg).toBe(180)
    // Looking at a page that is there: the nearest place beside it, and the page does not move.
    const beside = placeNew(first, W, H, 5)
    expect(beside.at.yawDeg).toBe(60)
    expect(beside.moved).toEqual([])
    // With nothing in the room it opens where they look, at the distance a first page opens at.
    expect(placeNew([], W, H, -50).at).toEqual({ yawDeg: -60, pitchDeg: PAGES.lowerDeg, distanceM: PAGES.distanceM })
    // A page that a hand placed never moves for it, wherever the person looks.
    const mine = [page('mine', 100, 0, 1.75, true)]
    const got = placeNew(mine, W, H, 100)
    expect(got.moved).toEqual([])
    expect(got.crosses).toBe(false)
    expect(crossing({ at: got.at, widthM: W, heightM: H }, mine[0])).toBe(0)
  })
})
