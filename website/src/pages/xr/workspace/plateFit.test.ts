import { describe, expect, it } from 'vitest'
import { BoxGeometry, Group, Matrix4, Mesh, Object3D, PlaneGeometry, Quaternion, Scene, Vector3 } from 'three'
import { behindOf, marginOf, roomSeen, weigh, type Shape, type Sight } from './ReadoutPlate'
import { GIVE, NO_PLACE, coverOf, coversClearlyLess, coversLess, drawnIn, grown, hides, inside, nearestBoxM, nearestFlatM, overlaps, seen, seenBox, settle, sharedView, viewOf, within, type Cover, type P2, type P3 } from './plateFit'

/** A flat square of side `side`, facing the eye, with its middle at `[x, y]`, `away` metres ahead. */
const square = (x: number, y: number, side: number, away = 1): P3[] => [[x - side / 2, y - side / 2, -away], [x + side / 2, y - side / 2, -away], [x + side / 2, y + side / 2, -away], [x - side / 2, y + side / 2, -away]]

describe('where the readout\'s plate may stand', () => {
  it('sees a flat thing ahead as it is, a far one smaller, and nothing of one behind the eye', () => {
    expect(seen(square(0, 0, 1))).toEqual([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]])
    expect(seen(square(0, 0, 1, 2))[0]).toEqual([-0.25, -0.25])
    expect(seen(square(0, 0, 1, -1))).toEqual([])
  })

  it('cuts a page that passes the eye at the eye\'s plane, and keeps what is ahead', () => {
    // A page that stands along the line of sight, from 1 m ahead to 1 m behind, half a metre to the right.
    const shape = seen([[0.5, -0.5, -1], [0.5, -0.5, 1], [0.5, 0.5, 1], [0.5, 0.5, -1]])
    expect(shape).toHaveLength(4)
    expect(Math.min(...shape.map(p => p[0]))).toBeCloseTo(0.5, 9)
    expect(Math.max(...shape.map(p => p[0]))).toBeCloseTo(10, 9)
  })

  it('takes a box as the upright rectangle around its corners, never smaller than it', () => {
    const box: P3[] = [[0.1, 0, -1], [0.3, 0, -1], [0.1, 0.4, -1], [0.3, 0.4, -1], [0.1, 0, -2], [0.3, 0, -2], [0.1, 0.4, -2], [0.3, 0.4, -2]]
    expect(seenBox(box)).toEqual([[0.05, 0], [0.3, 0], [0.3, 0.4], [0.05, 0.4]])
    expect(seenBox(box.map(([x, y, z]) => [x, y, -z] as P3))).toEqual([])
  })

  it('says two shapes lie over each other only when they do: side by side they do not, turned they may', () => {
    const a = seen(square(0, 0, 1))
    expect(overlaps(a, seen(square(0.9, 0, 1)))).toBe(true)
    expect(overlaps(a, seen(square(1, 0, 1)))).toBe(false)
    expect(overlaps(a, seen(square(1.2, 0, 1)))).toBe(false)
    // A diamond whose corner reaches into the square, and one whose upright rectangle does but which itself does not.
    const diamond = (x: number, y: number, r: number) => [[x - r, y], [x, y - r], [x + r, y], [x, y + r]] as const
    expect(overlaps(a, diamond(0.9, 0, 0.5))).toBe(true)
    expect(overlaps(a, diamond(0.85, 0.85, 0.5))).toBe(false)
    // One inside the other.
    expect(overlaps(a, seen(square(0, 0, 0.1)))).toBe(true)
    expect(overlaps(a, [])).toBe(false)
  })

  it('reads one eye\'s picture from its projection matrix, and what two eyes both show', () => {
    // A view of 30 degrees to each side and 20 up and down: the picture reaches as far as the tangents.
    const m = new Array<number>(16).fill(0)
    m[0] = 1 / Math.tan((30 * Math.PI) / 180)
    m[5] = 1 / Math.tan((20 * Math.PI) / 180)
    const view = viewOf(m)
    expect(view.x1).toBeCloseTo(Math.tan((30 * Math.PI) / 180), 9)
    expect(view.y0).toBeCloseTo(-Math.tan((20 * Math.PI) / 180), 9)
    const both = sharedView([{ x0: -0.7, x1: 0.5, y0: -0.4, y1: 0.4 }, { x0: -0.5, x1: 0.7, y0: -0.4, y1: 0.4 }])
    expect(both).toEqual({ x0: -0.5, x1: 0.5, y0: -0.4, y1: 0.4 })
    const drawnIn = sharedView([{ x0: -1, x1: 1, y0: -0.5, y1: 0.5 }], 0.1)
    expect([drawnIn.x0, drawnIn.x1, drawnIn.y0, drawnIn.y1].map(v => Math.round(v * 1000) / 1000)).toEqual([-0.8, 0.8, -0.4, 0.4])
  })

  it('says a plate is whole in the picture only when every corner is ahead and inside it', () => {
    const view = { x0: -0.59, x1: 0.59, y0: -0.37, y1: 0.37 }
    expect(within(square(-0.3, -0.2, 0.3), view)).toBe(true)
    // Cut by the picture's right edge, as the plate was beside a page.
    expect(within(square(0.5, 0, 0.3), view)).toBe(false)
    expect(within(square(0, 0, 0.3, -1), view)).toBe(false)
  })

  it('has give: a new place needs a degree to spare, and the place it stands at is left only after two choices running', () => {
    expect(GIVE.margin).toBeCloseTo(Math.tan(Math.PI / 180), 12)
    // A thing half a degree from the plate's edge: the plate that stands there is free, one that would come is not.
    const plate = seen(square(0, 0, 0.4))
    const thing = seen(square(0.2 + GIVE.margin / 2 + 0.1, 0, 0.2))
    expect(overlaps(plate, thing)).toBe(false)
    expect(overlaps(grown(plate, GIVE.margin), thing)).toBe(true)
    expect(overlaps(grown(plate, GIVE.margin), seen(square(0.2 + GIVE.margin * 2 + 0.1, 0, 0.2)))).toBe(false)
    // Whole with room to spare: a plate that touches the picture's edge is whole, and not with a degree to spare.
    const view = { x0: -0.5, x1: 0.5, y0: -0.3, y1: 0.3 }
    expect(within(square(0.3, 0, 0.4), view)).toBe(true)
    expect(within(square(0.3, 0, 0.4), drawnIn(view, GIVE.margin))).toBe(false)
    // A head at rest on a boundary: the choices say `under`, `view`, `under`, `view`. The plate stays under.
    let at = settle<string>(null, 'under', 0)
    expect(at).toEqual({ stands: 'under', fails: 0 })
    for (const says of ['view', 'under', 'view', 'under']) { at = settle(at.stands, says, at.fails); expect(at.stands).toBe('under') }
    // Two choices running that say otherwise: it leaves, and the count starts again.
    at = settle(at.stands, 'view', at.fails)
    expect(at).toEqual({ stands: 'under', fails: 1 })
    at = settle(at.stands, 'view', at.fails)
    expect(at).toEqual({ stands: 'view', fails: 0 })
    // The same while it is forced: one choice that finds a free place does not move it.
    at = settle('view:true', 'viewRight:true', 0)
    expect(at).toEqual({ stands: 'view:true', fails: 1 })
    expect(settle(at.stands, 'view:true', at.fails)).toEqual({ stands: 'view:true', fails: 0 })
  })
})

describe('what the plate hides from the eye', () => {
  // The plate in the upper left of the view, 1 m ahead: 0.56 m wide, 0.2 m high.
  const plate: P3[] = [[-0.58, 0.16, -1], [-0.02, 0.16, -1], [-0.02, 0.36, -1], [-0.58, 0.36, -1]]

  it('says whether a point of the picture is in a shape, its edge counted, in either order of its corners', () => {
    const shape = seen(plate)
    expect(inside([-0.3, 0.26], shape)).toBe(true)
    expect(inside([-0.02, 0.26], shape)).toBe(true)
    expect(inside([-0.01, 0.26], shape)).toBe(false)
    expect(inside([-0.3, 0.37], shape)).toBe(false)
    expect(inside([-0.3, 0.26], [...shape].reverse())).toBe(true)
    expect(inside([-0.3, 0.26], shape.slice(0, 2))).toBe(false)
  })

  it('hides a point in its shape at any distance (nearer than it, behind it), and none beside it or behind the eye', () => {
    // Along one line of sight through the plate: the palette's 0.5 m, a card's 0.65 m, a page's 1.75 m, a Bot's 3 m.
    for (const away of [0.5, 0.65, 0.7, 0.75, 1.75, 3]) expect(hides(plate, [-0.3 * away, 0.26 * away, -away])).toBe(true)
    // Beside the plate, at the same distances.
    for (const away of [0.5, 1.75]) expect(hides(plate, [0.1 * away, 0.26 * away, -away])).toBe(false)
    for (const away of [0.5, 1.75]) expect(hides(plate, [-0.3 * away, 0.1 * away, -away])).toBe(false)
    // The same direction, behind the eye.
    expect(hides(plate, [0.3, -0.26, 1])).toBe(false)
  })
})

describe('which corner the plate takes when none is free of marked things', () => {
  /** The plate in a corner of a view that reaches 0.59 across and 0.37 up, as the eye's picture has it. */
  const corner = (right: boolean, up: boolean): P2[] => {
    const x0 = right ? 0.02 : -0.58
    const y0 = up ? 0.16 : -0.36
    return [[x0, y0], [x0 + 0.56, y0], [x0 + 0.56, y0 + 0.2], [x0, y0 + 0.2]]
  }
  const CORNERS = { view: corner(false, false), viewRight: corner(true, false), viewUp: corner(false, true), viewUpRight: corner(true, true) }
  // The eye at the room's origin, looking along minus z.
  const sight: Sight = { inv: new Matrix4(), at: new Vector3(), turn: new Quaternion(), view: { x0: -0.59, x1: 0.59, y0: -0.37, y1: 0.37 } }
  /** A marked group of `parts` meshes, `away` metres ahead, where the eye sees it at `[x, y]` of its picture. */
  const thing = (mark: string, x: number, y: number, away: number, parts = 1) => {
    const group = new Group()
    group.userData.xrPress = mark
    group.position.set(x * away, y * away, -away)
    for (let i = 0; i < parts; i += 1) {
      const mesh = new Mesh(new BoxGeometry(0.04 * away, 0.04 * away, 0.01))
      mesh.position.set(((i % 3) - 1) * 0.05 * away, (Math.floor(i / 3) - 1) * 0.05 * away, 0)
      group.add(mesh)
    }
    return group
  }
  /** A page with no mark, `away` metres ahead, facing the eye: the eye sees it from `x0` to `x1` across and `y0` to `y1` up. */
  const pageAt = (id: string, x0: number, x1: number, y0: number, y1: number, away: number) => {
    const mesh = new Mesh(new PlaneGeometry())
    mesh.userData.xrPage = id
    mesh.position.set(((x0 + x1) / 2) * away, ((y0 + y1) / 2) * away, -away)
    mesh.scale.set((x1 - x0) * away, (y1 - y0) * away, 1)
    return mesh
  }
  const room = (...things: Object3D[]): Shape[] => {
    const scene = new Scene()
    scene.add(...things)
    scene.updateMatrixWorld(true)
    return roomSeen(scene, null, sight)
  }
  /** The corner that the plate's choice takes: the corners in their order, each one's cover, the least of them, the earlier of two alike. */
  const takes = (shapes: Shape[]): { where: string; cover: Cover } => {
    let least = { where: 'view', cover: NO_PLACE }
    for (const [where, plate] of Object.entries(CORNERS)) {
      const cover = coverOf(behindOf(shapes, plate, true), 1)
      if (coversLess(cover, least.cover)) least = { where, cover }
    }
    return least
  }

  it('counts a marked group as one thing, whatever it is built of', () => {
    const shapes = room(thing('palette', -0.3, -0.26, 3, 9), thing('bot', 0.2, -0.26, 3, 3), thing('bot', 0.4, -0.26, 3, 3))
    expect(shapes).toHaveLength(15)
    expect(coverOf(shapes.filter(s => overlaps(CORNERS.view, s.shape)), 1)).toEqual({ near: false, far: 1 })
    expect(coverOf(shapes.filter(s => overlaps(CORNERS.viewRight, s.shape)), 1)).toEqual({ near: false, far: 2 })
    // A mark on a group and on a mesh under it is still one thing: the outermost.
    const bot = thing('bot', 0.2, -0.26, 3, 3)
    bot.children[0].userData.xrPress = 'sign'
    expect(coverOf(room(bot), 1)).toEqual({ near: false, far: 1 })
  })

  it('stands over two Bots\' rims before a chat card of one mesh that is nearer than the plate', () => {
    // Lower left: the chat card, 0.65 m. Lower right: two Bots, 3 m. Upper left: three close marks. Upper right: three Bots.
    const shapes = room(
      thing('card', -0.3, -0.26, 0.65),
      thing('bot', 0.2, -0.26, 3, 3), thing('bot', 0.4, -0.26, 3, 3),
      thing('close', -0.5, 0.26, 1.75), thing('close', -0.3, 0.26, 1.75), thing('close', -0.1, 0.26, 1.75),
      thing('bot', 0.1, 0.26, 3, 3), thing('bot', 0.3, 0.26, 3, 3), thing('bot', 0.5, 0.26, 3, 3),
    )
    // By meshes the card's corner would win (one against six, three and nine).
    expect(shapes.filter(s => overlaps(CORNERS.view, s.shape))).toHaveLength(1)
    expect(shapes.filter(s => overlaps(CORNERS.viewRight, s.shape))).toHaveLength(6)
    expect(takes(shapes)).toEqual({ where: 'viewRight', cover: { near: false, far: 2 } })
  })

  it('never covers a near thing while a corner has none: the palette, the card, a taken tray, a held board', () => {
    for (const [mark, away, parts] of [['palette', 0.5, 9], ['card', 0.65, 1], ['tray', 0.7, 2], ['board', 0.75, 4]] as const) {
      // The near thing alone in the lower left; every other corner has three far things.
      const shapes = room(
        thing(mark, -0.3, -0.26, away, parts),
        thing('bot', 0.1, -0.26, 3), thing('bot', 0.3, -0.26, 3), thing('bot', 0.5, -0.26, 3),
        thing('bot', -0.5, 0.26, 3), thing('bot', -0.3, 0.26, 3), thing('bot', -0.1, 0.26, 3),
        thing('bot', 0.1, 0.26, 3), thing('bot', 0.3, 0.26, 3), thing('bot', 0.5, 0.26, 3),
      )
      expect(coverOf(shapes.filter(s => overlaps(CORNERS.view, s.shape)), 1)).toEqual({ near: true, far: 0 })
      expect(takes(shapes)).toEqual({ where: 'viewRight', cover: { near: false, far: 3 } })
    }
    // A thing behind the plate is a far thing.
    expect(coverOf(room(thing('close', -0.3, -0.26, 1.75)), 1)).toEqual({ near: false, far: 1 })
  })

  it('with a near thing in every corner takes the one with the fewest far things, the earlier of two alike', () => {
    const shapes = room(
      thing('palette', -0.3, -0.26, 0.5, 9), thing('bot', -0.5, -0.26, 3), thing('bot', -0.1, -0.26, 3),
      thing('card', 0.3, -0.26, 0.65), thing('bot', 0.5, -0.26, 3),
      thing('tray', -0.3, 0.26, 0.7, 2), thing('bot', -0.5, 0.26, 3),
      thing('board', 0.3, 0.26, 0.75, 4), thing('bot', 0.1, 0.26, 3), thing('bot', 0.5, 0.26, 3),
    )
    // Lower right and upper left have one far thing each: the lower right is the earlier.
    expect(takes(shapes)).toEqual({ where: 'viewRight', cover: { near: true, far: 1 } })
    // A corner that is no place (not whole) is never taken over one that is.
    expect(coversLess(NO_PLACE, { near: true, far: 9 })).toBe(false)
    expect(coversLess({ near: true, far: 9 }, NO_PLACE)).toBe(true)
    expect(coversLess({ near: false, far: 9 }, { near: true, far: 0 })).toBe(true)
    expect(coversLess({ near: false, far: 2 }, { near: false, far: 2 })).toBe(false)
  })

  it('leaves a corner where it takes presses only for one that covers clearly less (D60)', () => {
    // The palette put away: the lower left covers two far things, the upper corner it stands in four. It stays.
    expect(coversClearlyLess({ near: false, far: 2 }, { near: false, far: 4 })).toBe(false)
    // A page came to stand under it, with a corner of clearly less cover there: it goes.
    expect(coversClearlyLess({ near: false, far: 2 }, { near: false, far: 5 })).toBe(true)
    // A near thing under it, none in the other corner: it goes, whatever the count.
    expect(coversClearlyLess({ near: false, far: 6 }, { near: true, far: 0 })).toBe(true)
    expect(coversClearlyLess({ near: true, far: 0 }, { near: false, far: 9 })).toBe(false)
  })

  it('makes one choice as the plate does, place by place: the held corner, a bare corner, a near thing, the margin (V2)', () => {
    type T = { where: 'view' | 'viewRight' | 'viewUp' | 'viewUpRight' | 'over'; takes: boolean }
    const far = (n: number) => Array.from({ length: n }, () => ({ thing: {}, awayM: 3 }))
    const near = [{ thing: {}, awayM: 0.5 }]
    // The corners in the order the plate tries them, the held one first among its kind, each with what lies behind it.
    const choose = (held: T | null, steps: [T, { thing: unknown; awayM: number }[] | null][]): string => {
      let least = { place: { where: 'view', takes: true } as T, cover: NO_PLACE }
      for (const [next, behind] of steps) {
        const w = weigh(held, least, next, behind)
        if (w.says) return `${next.where}:${next.takes}`
        least = w.least as typeof least
      }
      return `${least.place.where}:${least.place.takes}`
    }
    const upHeld: T = { where: 'viewUp', takes: true }
    const rest = (bare: number[]): [T, ReturnType<typeof far> | null][] =>
      (['view', 'viewRight', 'viewUpRight'] as const).map((where, i) => [{ where, takes: true }, bare.includes(i) ? [] : far(3)])
    // The palette put away: the held upper corner covers two far things, the lower left none. It stays (D60, V1).
    expect(choose(upHeld, [[upHeld, far(2)], ...rest([0, 1])])).toBe('viewUp:true')
    // A near thing under it: a bare corner is clearly less, and it goes there.
    expect(choose(upHeld, [[upHeld, near], ...rest([0])])).toBe('view:true')
    // Four far things under it: a bare corner is three fewer, and it goes.
    expect(choose(upHeld, [[upHeld, far(4)], ...rest([1])])).toBe('viewRight:true')
    // Nothing under the held corner: it stays, said at once.
    expect(choose(upHeld, [[upHeld, []], ...rest([0])])).toBe('viewUp:true')
    // Standing nowhere, or at a free place: the first bare corner is said at once.
    expect(choose(null, rest([1, 2]))).toBe('viewRight:true')
    expect(choose({ where: 'over', takes: false }, [[{ where: 'over', takes: false }, far(1)], ...rest([2])])).toBe('viewUpRight:true')
    // A place not whole is passed over.
    expect(choose(upHeld, [[upHeld, null], ...rest([0])])).toBe('view:true')
    // The margin: the place it stands at passes as it is; another must pass with a degree to spare.
    expect(marginOf(upHeld, { where: 'viewUp', takes: true })).toBe(0)
    expect(marginOf(upHeld, { where: 'view', takes: true })).toBe(GIVE.margin)
    expect(marginOf(null, upHeld)).toBe(GIVE.margin)
  })

  it('measures how near a thing is from the eye, not along its line of sight: a page to the side at 1.75 m is far', () => {
    // A page that faces the eye is nearest at its middle, not at a corner.
    expect(nearestFlatM([[-1, -0.6, -0.9], [1, -0.6, -0.9], [1, 0.6, -0.9], [-1, 0.6, -0.9]])).toBeCloseTo(0.9, 9)
    // The eye beside the page: the nearest point is on its edge.
    expect(nearestFlatM([[1, -0.6, -2], [3, -0.6, -2], [3, 0.6, -2], [1, 0.6, -2]])).toBeCloseTo(Math.hypot(1, 2), 9)
    // A page 1.75 m away, 60 degrees to the side: 0.87 m ahead along the line of sight, and still 1.75 m away.
    const side = pageAt('side', -0.1, 0.1, -0.1, 0.1, 1)
    side.position.set(-1.75 * Math.sin(Math.PI / 3), 0, -1.75 * Math.cos(Math.PI / 3))
    side.rotation.y = Math.PI / 3
    side.scale.set(0.2, 0.2, 1)
    expect(room(side)[0].awayM).toBeGreaterThan(1.7)
    // A marked thing's box: nearest on its face that faces the eye, not at a corner.
    const box = (x0: number, x1: number, y0: number, y1: number, z0: number, z1: number): P3[] => {
      const out: P3[] = []
      for (const x of [x0, x1]) for (const y of [y0, y1]) for (const z of [z0, z1]) out.push([x, y, z])
      return out
    }
    expect(nearestBoxM(box(-0.2, 0.2, -0.1, 0.1, -0.8, -0.7))).toBeCloseTo(0.7, 9)
    // Beside the eye: the nearest point is on an edge.
    expect(nearestBoxM(box(0.3, 0.5, -0.1, 0.1, -0.5, -0.4))).toBeCloseTo(0.5, 9)
  })

  it('never covers a page that was brought nearer than the plate while a corner has none; a far page it may cover', () => {
    // The pages at 1.75 m fill the view. A page at 0.6 m stands over the left half of it. Each lower corner has two
    // Bots, each upper corner one close mark.
    const far = pageAt('page', -0.7, 0.7, -0.45, 0.45, 1.75)
    const near = pageAt('near', -0.7, 0, -0.45, 0.45, 0.6)
    const marks = [
      thing('bot', -0.5, -0.26, 3), thing('bot', -0.1, -0.26, 3), thing('bot', 0.1, -0.26, 3), thing('bot', 0.5, -0.26, 3),
      thing('close', -0.3, 0.26, 1.75), thing('close', 0.3, 0.26, 1.75),
    ]
    // With no near page the two upper corners are alike, and the upper left is the earlier.
    expect(takes(room(far, ...marks))).toEqual({ where: 'viewUp', cover: { near: false, far: 1 } })
    const shapes = room(far, near, ...marks)
    expect(shapes.find(s => s.name === 'near')?.awayM).toBeCloseTo(0.6, 9)
    // The upper left would cover the near page: it counts as a near thing there, the far page as nothing.
    expect(coverOf(behindOf(shapes, CORNERS.viewUp, true), 1)).toEqual({ near: true, far: 1 })
    expect(behindOf(shapes, CORNERS.viewUpRight, true).map(s => s.name)).toEqual(['press:close'])
    expect(takes(shapes)).toEqual({ where: 'viewUpRight', cover: { near: false, far: 1 } })
    // A place that takes no presses is free of no page, near or far.
    expect(behindOf(shapes, CORNERS.viewUpRight, false).map(s => s.name)).toEqual(['page', 'press:close'])
    // The near page over the whole view: every corner has a near thing, and the fewest far things decide.
    const whole = room(far, pageAt('near', -0.7, 0.7, -0.45, 0.45, 0.6), ...marks)
    expect(takes(whole)).toEqual({ where: 'viewUp', cover: { near: true, far: 1 } })
    // A page just behind the plate's distance is a far page.
    expect(takes(room(far, pageAt('behind', -0.7, 0, -0.45, 0.45, 1.05), ...marks)).where).toBe('viewUp')
  })
})
