/**
 * The readout (`?fps=1`) in a session: the page's block of lines (`Readout.tsx`) is not seen through the glasses, so
 * the same lines are drawn on a plate. A person who asked for the readout always has it, whole in the eye's picture,
 * and a press never goes through it to a thing that it hides:
 *
 * - It stands where nothing that can be pressed is behind it as the eye sees it (no page, and no thing of the room
 *   that carries the mark `userData.xrPress`, `PRESS_MARK`) and where it lies whole in the eye's picture: over the
 *   page that the dot lies on (or lay on last), under it, to its right, to its left, or in a corner of the view
 *   (lower left, lower right, upper left, upper right). There it takes no pointer: neither the dot nor a hand's ray
 *   lights it.
 * - Where no such place is, it stands in the first corner of the view that has no marked thing behind it (a page
 *   may be), so that what the left palm holds up, low on the left, can still be seen and pressed. With no such
 *   corner it stands in the one with the fewest marked things behind it, the earlier of two alike. Things are
 *   counted, not meshes (a Bot, a card, the palette are one each, by the group that carries the mark), and a marked
 *   thing nearer to the eye than the plate is never covered while a corner without one is there, and neither is a
 *   page that was brought nearer than the plate; with none, the
 *   corner with the fewest farther things. In all of these it takes the presses that fall on it: they do nothing
 *   and reach nothing behind it. Its first line says so.
 * - That holds for every pointer. The dot's ray starts at the eye and meets the plate. A hand's ray starts at the
 *   hand and reaches what the plate hides without crossing it, so while the plate takes presses it tells the route
 *   which points it hides (`pinchRoute.veil`), and a pinch whose ray lies on one presses nothing.
 *
 * The places are tried one a frame, twice a second, each by a few sums on shapes in the eye's picture
 * (`plateFit.ts`): no ray is cast. The choice has give (`GIVE`): a place it does not stand at must be free with a
 * degree to spare, and it leaves where it stands only when two choices running say so. Through the first of the two
 * it takes the presses that fall on it, and is drawn as where it takes them. A corner where it takes presses is left
 * for another such corner only when that one covers clearly less (`coversClearlyLess`, D60), even when nothing
 * marked is behind that one.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, LinearFilter, Matrix4, Object3D, Quaternion, SRGBColorSpace, Vector3, type Camera, type Mesh } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { DEPTH, DRAW, PRESS } from './drawOrder'
import { pinchRoute, type V3 } from './pinchRoute'
import { GIVE, NO_PLACE, coverOf, coversClearlyLess, coversLess, drawnIn, grown, hides, nearestBoxM, nearestFlatM, overlaps, seen, seenBox, settle, sharedView, viewOf, within, type Behind, type Cover, type P2, type P3, type View } from './plateFit'
import { useReadoutLines, type ReadoutProps } from './Readout'
import { PLATE_PX, plateRows, plateWidthM } from './readoutLines'

/**
 * The plate: its canvas in pixels (it is as high as its lines, `plateRows`); how far from the eye it stands at its
 * place in the view, in metres; how far from a page's edge it stands (clear of the page's line and its edge). It is
 * as wide as its text needs to be read from where it stands (`plateWidthM`).
 */
const PLATE = { width: PLATE_PX.width, line: PLATE_PX.line, pad: 18, viewM: 1, underM: 0.09, besideM: 0.06 }

/** The font of the pace lines: the block on the page draws every line in it (`font-mono`). */
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

/**
 * The eye's picture is taken a little smaller than it is, at each side, when a place is tried (`fit`), and smaller
 * again where the plate is stood in the view's corner (`stand`), so that the plate there is whole by the first.
 */
const INSET = { fit: 0.02, stand: 0.04 }

/**
 * The mark of a thing of the room that can be pressed, or that says what a press does: `userData={{ xrPress: 'name' }}`
 * on its mesh, or on a group for every mesh under it. The plate stands over none of them while they are shown.
 * (A page needs no mark: its mesh carries `xrPage`.)
 */
export const PRESS_MARK = 'xrPress'

/**
 * Where the plate can stand, in the order it is tried: over the page that the dot lies on, under it, beside it, and
 * the view's corners: lower left (`view`), lower right, upper left, upper right.
 */
export type PlateWhere = 'over' | 'under' | 'right' | 'left' | 'view' | 'viewRight' | 'viewUp' | 'viewUpRight'
const BY_PAGE: readonly PlateWhere[] = ['over', 'under', 'right', 'left']
const CORNERS_OF_VIEW: readonly PlateWhere[] = ['view', 'viewRight', 'viewUp', 'viewUpRight']
const byPage = (where: PlateWhere) => BY_PAGE.includes(where)

/** The corners of a flat thing's unit square, in order around it. */
const CORNERS = [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]] as const

/** What the test hook (`?probe=1`) reads of the plate. */
export interface PlateProbe {
  /** What lies behind the plate as the eye sees it: pages by their id, marked things as `press:name`. Nothing, unless it takes presses. */
  covers: string[]
  /** The page that it stands by, and the place it took. */
  under: string | null
  where: PlateWhere
  /** Its four corners in the room, and whether it lies whole in the eye's picture. */
  corners: [number, number, number][]
  whole: boolean
  /** It found no free place, stands in a corner of the view, and takes the presses that fall on it. */
  takes: boolean
  /** It takes the presses that fall on it: as `takes`, or through the one choice in which the place it stands at first failed. */
  guards: boolean
  /** What the last choice cost: the longest of its frames and all of them together, in ms, and how many shapes it looked at. */
  cost: { worstMs: number; sumMs: number; shapes: number }
  /** What it covers where it stands, as the choice counts it: a near thing or not, and how many far things. */
  cover?: Cover
  /** The last choice: the place it held, what each place it tried gave, what it said, and where the plate stands after. */
  choice?: { held: string | null; said: string; stands: string; fails: number; tried: ChoiceStep[] }
}

/** How the eye sees in one frame: into its own space, where it is and how it is turned, and its picture. */
export interface Sight { inv: Matrix4; at: Vector3; turn: Quaternion; view: View }

/**
 * One thing that the plate must not stand over, as the eye sees it; `page`: it is a page, not a marked thing.
 * `thing`: the marked thing that it is a part of (the outermost group or mesh that carries the mark), or the page.
 * `awayM`: how far from the eye its nearest point is (of a marked mesh: its box's nearest corner).
 */
export interface Shape { name: string; shape: P2[]; page: boolean; thing: Object3D; awayM: number }

const V = new Vector3()
const ROOM = new Matrix4()

/** A point of the room, in the eye's space. */
const toEye = (sight: Sight, x: number, y: number, z: number, from: Matrix4): P3 => { V.set(x, y, z).applyMatrix4(from).applyMatrix4(sight.inv); return [V.x, V.y, V.z] }

/** The corners of a flat thing (a page, the plate), in the eye's space. */
const flat = (sight: Sight, o: Object3D): P3[] => CORNERS.map(([u, v]) => toEye(sight, u, v, 0, o.matrixWorld))

/** The name of the marked thing that a mesh belongs to, as the plate's probe says it. */
function pressName(o: Object3D): string {
  for (let at: Object3D | null = o; at; at = at.parent) {
    const mark: unknown = at.userData[PRESS_MARK]
    if (mark) return `press:${typeof mark === 'string' ? mark : at.name || 'thing'}`
  }
  return 'press:thing'
}

/** What the plate must not stand over, as the eye sees it now: the pages, and the marked things that are shown, one shape each. */
export function roomSeen(scene: Object3D, plate: Object3D | null, sight: Sight): Shape[] {
  const out: Shape[] = []
  const walk = (o: Object3D, marked: Object3D | null, shown: boolean) => {
    if (o === plate) return
    const mine = marked ?? (o.userData[PRESS_MARK] ? o : null)
    // A marked thing that is not shown (or stands under one that is not) is not there to be covered.
    const is = shown && o.visible
    if (typeof o.userData.xrPage === 'string') {
      const corners = flat(sight, o)
      const shape = seen(corners)
      if (shape.length >= 3) out.push({ name: o.userData.xrPage, shape, page: true, thing: o, awayM: nearestFlatM(corners) })
    } else if (mine && is && (o as Mesh).isMesh) {
      const geometry = (o as Mesh).geometry
      if (!geometry.boundingBox) geometry.computeBoundingBox()
      const b = geometry.boundingBox
      if (b && !b.isEmpty()) {
        const corners: P3[] = []
        for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) corners.push(toEye(sight, x, y, z, o.matrixWorld))
        const shape = seenBox(corners)
        if (shape.length) out.push({ name: pressName(o), shape, page: false, thing: mine, awayM: nearestBoxM(corners) })
      }
    }
    for (const child of o.children) walk(child, mine, is)
  }
  walk(scene, null, true)
  return out
}

/**
 * What counts as behind a place with this shape. A place that takes no presses must be free of everything. One
 * that takes them may stand over a page, but not over one that was brought nearer than the plate: that page is
 * the one he works on, and it counts as a near thing (`coverOf`).
 */
export function behindOf(room: readonly Shape[], shape: readonly P2[], takes: boolean): Shape[] {
  return room.filter(thing => !(takes && thing.page && thing.awayM >= PLATE.viewM) && overlaps(shape, thing.shape))
}

/** The page mesh of a panel, by its id (`userData.xrPage`, set by `Panel.tsx`), or null. */
function pageOf(scene: Object3D, id: string): Object3D | null {
  let found: Object3D | null = null
  scene.traverse(o => { if (!found && o.userData.xrPage === id) found = o })
  return found
}

/** Stand `o` at one of the plate's places, for a plate `heightPx` high: by the page, or in a corner of the view. */
function stand(o: Object3D, where: PlateWhere, page: Object3D | null, sight: Sight, heightPx: number): void {
  if (!byPage(where) || !page) {
    const widthM = plateWidthM(PLATE.viewM)
    const heightM = (widthM * heightPx) / PLATE.width
    const view = sharedView([sight.view], INSET.stand - INSET.fit)
    o.scale.set(widthM, heightM, 1)
    o.quaternion.copy(sight.turn)
    const x = where === 'viewRight' || where === 'viewUpRight' ? view.x1 * PLATE.viewM - widthM / 2 : view.x0 * PLATE.viewM + widthM / 2
    const y = where === 'viewUp' || where === 'viewUpRight' ? view.y1 * PLATE.viewM - heightM / 2 : view.y0 * PLATE.viewM + heightM / 2
    o.position.set(x, y, -PLATE.viewM).applyQuaternion(sight.turn).add(sight.at)
  } else {
    // As wide as its text needs from where the page stands, and turned as the page is; the page's mesh is its unit square, scaled to its size.
    const widthM = plateWidthM(page.getWorldPosition(V).distanceTo(sight.at))
    const heightM = (widthM * heightPx) / PLATE.width
    o.scale.set(widthM, heightM, 1)
    page.getWorldQuaternion(o.quaternion)
    const [cornerX, cornerY, dx, dy] = where === 'under' ? [-0.5, -0.5, widthM / 2, -(PLATE.underM + heightM / 2)]
      : where === 'over' ? [-0.5, 0.5, widthM / 2, PLATE.underM + heightM / 2]
        : where === 'right' ? [0.5, -0.5, PLATE.besideM + widthM / 2, heightM / 2] : [-0.5, -0.5, -(PLATE.besideM + widthM / 2), heightM / 2]
    page.localToWorld(o.position.set(cornerX, cornerY, 0))
    o.position.add(V.set(dx, dy, 0.001).applyQuaternion(o.quaternion))
  }
  // The plate hangs in the room itself: its own place is its place in the room.
  o.updateMatrix()
  o.matrixWorld.copy(o.matrix)
}

/** A place to try. `takes` false: it must be free of pages and of marked things. True: of marked things only, and there it takes its presses. */
interface Try { where: PlateWhere; takes: boolean }
const key = (t: Try) => `${t.where}:${t.takes}`
const same = (a: Try | null, b: Try) => !!a && key(a) === key(b)

/** The places to try, in order: the free ones, then the corners where it takes its presses; the one it stands at is first among its kind. */
function tries(page: boolean, stands: Try | null): Try[] {
  const free: Try[] = [...(page ? BY_PAGE : []), ...CORNERS_OF_VIEW].map(where => ({ where, takes: false }))
  const forced: Try[] = CORNERS_OF_VIEW.map(where => ({ where, takes: true }))
  const first = (list: Try[]) => [...list.filter(t => same(stands, t)), ...list.filter(t => !same(stands, t))]
  return [...first(free), ...first(forced)]
}

/** A choice of the plate's place that is under way: what the eye saw when it began, and the places still to try. */
interface Choice { sight: Sight; room: Shape[]; held: Try | null; left: Try[]; least: { place: Try; cover: Cover }; worstMs: number; sumMs: number; tried: ChoiceStep[] }

/** The room to spare a place must pass with: none for the place it stands at, `GIVE.margin` for another. */
export const marginOf = (held: Try | null, next: Try): number => (same(held, next) ? 0 : GIVE.margin)

/** The place a choice holds as the least covered so far, and what it covers; `NO_PLACE` before any. */
export interface Least { place: Try; cover: Cover }

/**
 * One place of a choice, weighed: what lies behind it (`null`: it is not whole there). It is said at once when it is
 * free; otherwise a corner where it takes presses may lead the corners tried so far (`least`). Standing in a corner
 * where it takes presses, the plate leaves it for another such corner only through D60's compare, even for one with
 * no marked thing behind it.
 */
export function weigh(held: Try | null, least: Least, next: Try, behind: readonly Behind[] | null): { says: boolean; least: Least; cover: Cover | null; leads: boolean } {
  const atOnce = !next.takes || !held?.takes || same(held, next)
  if (behind?.length === 0 && atOnce) return { says: true, least, cover: null, leads: true }
  if (!behind || !next.takes) return { says: false, least, cover: null, leads: behind?.length === 0 }
  // No corner may be free of marked things: then the one that covers least (`coversLess`), the earlier of two alike.
  const cover = coverOf(behind, PLATE.viewM)
  // The corner it stands at is tried first among these; another takes its place only by covering clearly less (D60).
  const beats = same(held, least.place) ? coversClearlyLess : coversLess
  const leads = beats(cover, least.cover)
  return { says: false, least: leads ? { place: next, cover } : least, cover, leads }
}

/** For the probe: one place a choice tried, whether it was whole, what it covers (free or not), and whether it then led. */
export interface ChoiceStep { place: string; whole: boolean; behind: number | null; cover: Cover | null; leads: boolean }

/**
 * `said`: hears the plate's lines each time they change. `probe`: hears where the plate stands after each choice,
 * for the test hook (`?probe=1`).
 */
export function ReadoutPlate({ said, probe, ...props }: ReadoutProps & { said?: (lines: readonly string[]) => void; probe?: (plate: PlateProbe) => void }) {
  const read = useReadoutLines(props)
  const [takes, setTakes] = useState(false)
  const [guards, setGuards] = useState(false)
  const lines = useMemo(() => (takes ? [i18nT('xr.readout_plate_takes'), ...read.lines] : read.lines), [takes, read])
  // The rows of the pace lines (`paceLines`), which keep their width only in a font whose digits are all as wide.
  const paceFrom = takes ? 1 : 0
  const paceTo = paceFrom + read.pace
  useEffect(() => { said?.(lines) }, [lines, said])
  const camera = useThree(s => s.camera)
  const gl = useThree(s => s.gl)
  const mesh = useRef<Mesh>(null)
  // The plate is as high as what it says: a new canvas when the number of its rows changes.
  const rows = plateRows(lines.length)
  const heightPx = rows * PLATE.line + PLATE.pad
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = PLATE.width
    canvas.height = heightPx
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [heightPx])
  useEffect(() => () => plate.texture.dispose(), [plate])
  useEffect(() => {
    const c = plate.canvas.getContext('2d')
    if (!c) return
    c.fillStyle = css(XR_GL.raised)
    c.fillRect(0, 0, PLATE.width, heightPx)
    c.fillStyle = css(XR_GL.ink)
    const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
    c.textBaseline = 'top'
    lines.slice(0, rows).forEach((line, i) => {
      c.font = `${PLATE_PX.text}px ${i >= paceFrom && i < paceTo ? MONO : family}`
      c.fillText(line, 12, 10 + i * PLATE.line, PLATE.width - 24)
    })
    plate.texture.needsUpdate = true
  }, [lines, plate, rows, heightPx, paceFrom, paceTo])
  // Where it stands, kept over a change of its lines: a plate that grows by a line stays where it is.
  const at = useMemo(() => ({
    sight: { inv: new Matrix4(), at: new Vector3(), turn: new Quaternion(), view: { x0: -1, x1: 1, y0: -1, y1: 1 } } as Sight,
    press: { inv: new Matrix4(), at: new Vector3(), turn: new Quaternion(), view: { x0: -1, x1: 1, y0: -1, y1: 1 } } as Sight,
    trial: new Object3D(), page: null as Object3D | null, last: null as string | null, checked: -1, stands: null as Try | null, fails: 0, anew: false, guards: false, choice: null as Choice | null,
  }), [])
  /** How the eye sees now: the picture is what both eyes show. */
  const see = (into: Sight): Sight => {
    camera.updateWorldMatrix(true, false)
    into.inv.copy(camera.matrixWorld).invert()
    camera.matrixWorld.decompose(into.at, into.turn, V)
    const eyes: Camera[] = gl.xr.isPresenting ? gl.xr.getCamera().cameras : []
    into.view = sharedView((eyes.length ? eyes : [camera]).map(eye => viewOf(eye.projectionMatrix.elements)), INSET.fit)
    return into
  }
  useFrame(({ scene, clock }) => {
    const m = mesh.current
    if (!m) return
    const id = pinchRoute.probe.head?.on?.panel ?? at.last
    // Another page: a place by the page is another place now, so the next choice is made with no give.
    if (id !== at.last || (at.page && !at.page.parent)) { at.page = id ? pageOf(scene, id) : null; at.last = at.page ? id : null; at.checked = -1; at.anew = true }
    const sight = see(at.sight)
    // Twice a second a choice begins: what the eye sees of the room is taken once, and then one place is tried a
    // frame, the place it stands at first. The first that passes is what the choice says; with none, the corner
    // of the view with the fewest marked things behind it, where it takes its presses.
    if (!at.choice && clock.elapsedTime - at.checked >= 0.5) {
      at.checked = clock.elapsedTime
      const t0 = probe ? performance.now() : 0
      const then: Sight = { inv: sight.inv.clone(), at: sight.at.clone(), turn: sight.turn.clone(), view: sight.view }
      // By another page now, the place it stands at is no place it holds: this choice is made as if it stood nowhere.
      const held = at.anew && at.stands && byPage(at.stands.where) ? null : at.stands
      if (at.anew) { at.anew = false; at.fails = 0 }
      at.choice = { sight: then, room: roomSeen(scene, m, then), held, left: tries(!!at.page, held), least: { place: { where: 'view', takes: true }, cover: NO_PLACE }, worstMs: 0, sumMs: 0, tried: [] }
      if (probe) at.choice.sumMs = at.choice.worstMs = performance.now() - t0
    } else if (at.choice) {
      const choice = at.choice
      const t0 = probe ? performance.now() : 0
      const next = choice.left.shift()
      let says: Try | null = null
      if (next) {
        // The place it stands at passes as it is; another must pass with room to spare (`GIVE`).
        const by = marginOf(choice.held, next)
        stand(at.trial, next.where, at.page, choice.sight, heightPx)
        const corners = flat(choice.sight, at.trial)
        const shape = grown(seen(corners), by)
        // A corner of the view is whole as it is stood; a place by the page must be whole with the same room to spare.
        const whole = within(corners, byPage(next.where) ? drawnIn(choice.sight.view, by) : choice.sight.view)
        const behind = whole ? behindOf(choice.room, shape, next.takes) : null
        const w = weigh(choice.held, choice.least, next, behind)
        if (w.says) says = next
        choice.least = w.least
        if (probe) choice.tried.push({ place: key(next), whole, behind: behind ? behind.length : null, cover: w.cover, leads: w.leads })
      } else says = choice.least.place
      if (probe) { const ms = performance.now() - t0; choice.sumMs += ms; choice.worstMs = Math.max(choice.worstMs, ms) }
      if (says) {
        at.choice = null
        const now = settle(choice.held ? key(choice.held) : null, key(says), at.fails)
        const stands = !choice.held || now.stands === key(says) ? says : choice.held
        at.stands = stands
        at.fails = now.fails
        setTakes(stands.takes)
        // Through a first choice that says otherwise it stays, over a thing perhaps: it takes its presses meanwhile.
        const guard = stands.takes || at.fails > 0
        if (guard !== at.guards) { at.guards = guard; setGuards(guard) }
        if (probe) {
          stand(at.trial, stands.where, at.page, choice.sight, heightPx)
          const corners = flat(choice.sight, at.trial)
          const shape = seen(corners)
          probe({
            covers: choice.room.filter(thing => overlaps(shape, thing.shape)).map(thing => thing.name), under: at.page ? at.last : null, where: stands.where,
            corners: CORNERS.map(([u, v]) => V.set(u, v, 0).applyMatrix4(at.trial.matrixWorld).toArray()), whole: within(corners, choice.sight.view), takes: stands.takes, guards: guard,
            cost: { worstMs: choice.worstMs, sumMs: choice.sumMs, shapes: choice.room.length },
            cover: coverOf(behindOf(choice.room, shape, stands.takes), PLATE.viewM),
            choice: { held: choice.held ? key(choice.held) : null, said: key(says), stands: key(stands), fails: at.fails, tried: choice.tried },
          })
        }
      }
    }
    m.visible = at.stands !== null
    if (at.stands) stand(m, at.stands.where, at.page, sight, heightPx)
  })
  // While it takes presses, the hands' rays are told what it hides: as it stands now, to the eye as it is now.
  useEffect(() => {
    const veil = (point: V3): boolean => {
      const m = mesh.current
      if (!m || !m.visible || !at.guards) return false
      const sight = see(at.press)
      return hides(flat(sight, m), toEye(sight, point[0], point[1], point[2], ROOM))
    }
    pinchRoute.veil = veil
    return () => { if (pinchRoute.veil === veil) pinchRoute.veil = null }
  })
  const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()
  // Where it takes presses, it is first for every pointer, whatever stands nearer: nothing is pressed through it.
  return (
    <mesh
      ref={mesh} renderOrder={DRAW.readout} name="xr-readout-plate" visible={false} matrixAutoUpdate={false}
      pointerEvents={guards ? 'auto' : 'none'} pointerEventsOrder={guards ? PRESS.readout : undefined}
      onPointerDown={stop} onPointerUp={stop} onPointerMove={stop} onPointerEnter={stop} onPointerLeave={stop} onPointerCancel={stop} onClick={stop}
    >
      <planeGeometry />
      <meshBasicMaterial map={plate.texture} depthTest={DEPTH.readout} depthWrite={false} transparent opacity={guards ? 1 : 0.92} toneMapped={false} />
    </mesh>
  )
}
