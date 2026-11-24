/**
 * The 3D dashboards in the room of pages (`dash3d.ts`, `useDash3d.ts`, `dashKit.tsx`): a Bot whose own Dashboard
 * holds a layout has a small solid thing of its own, made of the kit's parts, whose numbers move by themselves.
 *
 * It is an object, not a screen: solid parts in a row on one tray, with a sloped lip at the tray's front. It has no
 * backing panel, so it is seen from every side and shows its depth when the head moves. One thing, two sizes.
 *
 * AT REST its Bot holds it: the tray made small, with no lip and no words, its parts standing close, in front of the
 * Bot's own body and a little over its feet, so the two read as one. It is as wide as the Bot's body at most, so it
 * is in front of no neighbour; it ends under the Bot's eyes; and it stands high enough that, from where the person
 * sees the row, the ring at the Bot's feet, which says its state, is seen whole under it (`restBox`). It goes where
 * the Bot's feet go.
 *
 * TAKEN (a press on it) it stands 0.8 m ahead of where the person looked, low in the lens as his head is at the take,
 * as a thing set on a table in front of him; when his head rests elsewhere and it is cut, it glides whole again
 * (`takenComes`). Its words are on the lip, never in the air: under each part its number and its
 * name, and at the lip's lower edge whose it is. A press sends it back. One is taken at a time, and none while
 * a board is in his hand or the new chat card's keyboard shows (the keyboard stands 0.6 degrees from the taken form):
 * then a tray does not light, and a press on it says in one line what to do first. A board that a Bot holds up does
 * not stop a take; when one comes up it sends a taken tray back, since that board would stand in front of it.
 *
 * What never moves of it (the tray, the feet, posts and rails of its parts) is one merged shape while it rests, so a
 * resting dashboard is a handful of draws (the rim that a pointer lights is one of them, drawn alone); taken, or on its way, its pieces are drawn one by one, since they slide.
 *
 * A press that moves is no press. In the flat preview it becomes the drag that turns the picture, as on a Bot.
 *
 * A layout that is no layout is an empty grey tray; taken, it says why in one short line, so he can tell the Bot.
 * A part that is wrong is an empty socket, with its line on the lip. Nothing on a dashboard is a control: the whole
 * thing is one press, which takes it or sends it back.
 *
 * A Bot at a seat of the desk has no dashboard drawn yet.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXR, useXRStore } from '@react-three/xr'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Box3, Euler, Frustum, Matrix4, Mesh as MeshClass, Quaternion, Raycaster, Vector3, type Camera, type Group, type Mesh, type Object3D } from 'three'
import { fmtCompact } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { XR_GL } from '../palette'
import { crewText } from '../text'
import type { CrewProbe } from './Crew'
import { RoomMark } from './RoomMark'

/** One look of the taken form's fit, kept for the rows: when (ms after its placement), the drawn box it read, the step. */
type FitStep = { ms: number; box: string; k: number; dy: number }
import { HELD_COME } from './Board'
import { CAME_UP_MS, FORM, boardsCameUp, drawnUnder, raysOnDrawn, type RayPiece, fitAgain, fitTaken, gapTakes, glideGoes, glideShare, hiddenBy, hiddenStep, HIDDEN_SLACK, sameLook, restGaps, dataHues, picturesOverlap, isPart, partText, partValue, restBox, restRow, takenComes, takenRow, takenTrayTopY, TAKEN_DISTANCE_M, TAKEN_LOOK_DEG, trayPress, trayWideM, type DashLayout, type DashWhy, type DataHues, type TakenHold, type TrayBlock } from './dash3d'
import { DashLabel, DashLights, DashPartBody, PART_TOP, Pieces, mergePieces, partPieces, trayPieces } from './dashKit'
import { pinchRoute } from './pinchRoute'
import { downLetGo, trayHeld, trayHolds, upTakes } from './trayHold'
import { visibilityTakes } from './botPress'
import { TURN_DRAG_PX } from './previewLook'
import { boxCorners, drawnBox, drawnLensNdc, inFront, lensNdc, ndcOf, setTakenPicture } from './takenPicture'
import type { EmptyHand } from './RoomWalls'
import type { DashView } from './useDash3d'

/** The taken form: how far ahead it stands, and how far under the eye line its highest point is. */
export const TAKEN = { distanceM: TAKEN_DISTANCE_M } as const
/** A tray at rest that a nearer Bot hides stands this far over its own Bot's top. */
const REST_CLEAR_M = 0.03

/** The words of the taken form, all on the lip: under each part its number and its name, and at the lip's lower edge whose it is. */
const WORDS = {
  numberH: 0.034, numberSize: 0.03,
  nameH: 0.026, nameSize: 0.015,
  headerH: 0.016, headerSize: 0.012,
  /** A tray that only says why it is empty is at least this wide, so the line fits. */
  whyWideM: 0.3,
  /**
   * Why a whole layout is not drawn: on the lip, as large as a part's name (`nameSize`) and never smaller, in at most
   * `whyLines` lines across the tray, cut by whole signs past them (D107 3, D108 2).
   */
  whyLines: 2,
} as const
/** The height of the layout's reason on the lip. */
const WHY_H = WORDS.whyLines * WORDS.nameSize * 1.15 + 0.004
/** The highest point of the taken form over the tray's top: its tallest part. */
const TOP_Y = Math.max(...Object.values(PART_TOP))
const RAD = Math.PI / 180
/** Scratch for the gaps' reckoning: where the dashboard stands. */
const GAP_AT = new Vector3()
const LEAN = FORM.lipLeanDeg * RAD
/** Scratch for where his look points this frame. */
const LOOK_DIR = new Vector3()
/** A taken form at rest is judged whole or cut this often (ms). shortcut: a fixed beat; tie it to the board's `near` beat if both ever share one loop. */
const CHECK_MS = 100

/** The one short line that says why a layout, or a part of it, is not drawn. */
const WHY: Record<DashWhy, () => string> = {
  not_layout: () => i18nT('xr.dash3d_why_not_layout'),
  version: () => i18nT('xr.dash3d_why_version'),
  no_parts: () => i18nT('xr.dash3d_why_no_parts'),
  too_many: () => i18nT('xr.dash3d_why_too_many'),
  part: () => i18nT('xr.dash3d_why_part'),
  kind: () => i18nT('xr.dash3d_why_kind'),
  name: () => i18nT('xr.dash3d_why_name'),
  field: () => i18nT('xr.dash3d_why_field'),
  path: () => i18nT('xr.dash3d_why_path'),
  max: () => i18nT('xr.dash3d_why_max'),
  key: () => i18nT('xr.dash3d_why_key'),
}

/** What the probe reads of one dashboard. */
export interface DashMarks {
  where: 'rest' | 'taken'
  /** Whether a pointer lights it now. */
  lit: boolean
  /** Why the whole layout is not drawn ('' when it is). */
  why: string
  /** The middle of its tray's top in the room; its drawn parts' box in the room (least and most corner); its box in the picture. */
  at: [number, number, number]
  box: [[number, number, number], [number, number, number]]
  ndc: [number, number, number, number]
  /** What the lens shows of it (`drawnLensNdc`), cut to the lens; null when nothing of it is in the lens (D137). */
  lens: readonly [number, number, number, number] | null
  /** The box of its press in the picture. */
  press: [number, number, number, number]
  /** At rest: the two ends of its far edge, the one toward its Bot, in the room. */
  far: [[number, number, number], [number, number, number]] | null
  /** Taken: how many times it was placed again since the take, and whether it glides now (D103 2). */
  /** At rest: whether it stands lifted because a nearer Bot hides it, and how many times that changed (M72). */
  hidden: { hidden: boolean; changes: number } | null
  /** Taken: the fit's looks so far (4: done), how much farther and higher it stands, and how far it has come (`mix`, 1: there). */
  /** `log`: each look (rows only); `seen`: the drawn box as read in the last frame, beside the one the fit read. */
  fit: { n: number; k: number; dy: number; mix: number; log: FitStep[]; seen: string } | null
  moves: number
  gliding: boolean
  /** Taken: what holds it this frame (a pinch, a pointer on it, his head moving). */
  hold: TakenHold | null
  /** Taken: its last judgement, whether it is whole in the lens and what held it, and how long his look has rested. */
  come: ({ whole: boolean; restedMs: number } & TakenHold) | null
  /** How large it is drawn against the taken form; and how high in the room its highest word stands (taken only). */
  scale: number
  wordsTopY: number | null
  /** Each place: its kind, or `socket`; what it shows. */
  parts: { kind: string; found: boolean; share: number | null; number: number | null; lamps: readonly string[]; broken?: DashWhy }[]
  /** Each part's drawn box in the picture (null: none drawn). */
  partsNdc: ([number, number, number, number] | null)[]
  /** Each part's drawn middle in the room (null: none drawn), so a row can set the eye on a line through it. */
  partsAt: ([number, number, number] | null)[]
  /** Each gap press mesh at rest: whether it is on, and its world matrix (its geometry is the unit box). */
  gapsAt: { on: boolean; m: number[] }[]
  /** The words written on it (taken only). */
  /** `whyNdc`, `headerNdc`: the boxes in the picture of the reason's lines and of the tray's label, as drawn. */
  words: { header: string; numbers: string[]; names: string[]; why: string[]; whyNdc: [number, number, number, number] | null; headerNdc: [number, number, number, number] | null }
  /** The colours of its parts. */
  hues: DataHues
}

interface Props {
  views: readonly DashView[]
  /** Where each Bot's body stands now (`Crew`'s report). */
  crew: () => readonly CrewProbe[]
  /** What stands where the taken form would (`trayBlock`): a board in his hand, or the card's keyboard. No dashboard is taken then. */
  block: TrayBlock
  /** The asks whose boards the Bots hold up now: one that comes up sends a taken dashboard back. */
  boardsUp: ReadonlySet<string>
  /** One short line to him. */
  say: (words: string) => void
  /** In the flat preview: the hand that turns the picture by a drag of empty space. A press that moves is handed to it. */
  drag?: EmptyHand
  still: boolean
  /** The colours the room draws a Bot's state in, by state: no part takes one of them. */
  stateColors: Readonly<Record<string, number>>
  /** Told whether a dashboard is taken now. */
  onTaken?: (taken: boolean) => void
  /** `view=dash`: the first dashboard stands taken from the start, as for an eye here looking straight ahead (-z). */
  takenFrom?: readonly [number, number, number]
  report?: (read: (() => Record<string, DashMarks>) | null) => void
}

/** What the trays' own work costs each frame, in ms, summed: counted only while a probe reads the room. */
export const dashWork = { on: false, ms: 0, frames: 0, sentBack: [] as { board: string; tray: number[]; seen: number[] }[], presses: [] as DashPressLog[] }

/**
 * For the probe only (D188 2): each pointer event on a tray's group and each answer of its press, in order, the last
 * 40. `piece` is what the pointer's hit was (a gap's press mesh, the taken outline, else the mesh's name or `mesh`);
 * `drawn` is whether that piece takes a press (`drawn`); `held` whether a down of this pointer was kept when the event
 * came; `ending` the pinch's kind at an up; `does` what `trayPress` answered, with the block and the taken tray.
 */
export interface DashPressLog { ms: number; slug: string; ev: string; pointer: string; id: number; piece?: string; drawn?: boolean; held?: boolean; ending?: string | null; does?: string; block?: string | null; taken?: string | null }
const logPress = (entry: Omit<DashPressLog, 'ms'>) => {
  if (!dashWork.on) return
  dashWork.presses.push({ ms: Math.round(performance.now()), ...entry })
  if (dashWork.presses.length > 40) dashWork.presses.shift()
}
const pieceOf = (e: ThreeEvent<PointerEvent>): string => {
  const o = e.object as Object3D
  return o.userData?.dashGap ? 'gap' : o.userData?.dashOutline ? 'outline' : o.name || 'mesh'
}

interface TakenAt { x: number; z: number; yaw: number; eyeX: number; eyeY: number; eyeZ: number; pitchDeg: number; halfFovDeg: number; halfWideDeg: number }

/**
 * Where a dashboard taken by an eye at `eye`, looking along the level `flat` and `pitchDeg` up, stands:
 * `TAKEN.distanceM` ahead, whole in the lens as his head is then, and still whole while it turns `TAKEN_LOOK_DEG` up or
 * down from there (the lens it is placed in is that much shorter at each end). A take is made looking down at a tray
 * (17 to 21 degrees), so the form is whole at the take (D103 2); a head that rests elsewhere later brings it whole
 * again (`OneDash`, `takenComes`).
 */
function takenAt(eye: readonly [number, number, number], flat: Vector3, lens: { halfFovDeg: number; halfWideDeg: number }, pitchDeg = 0): TakenAt {
  return { x: eye[0] + flat.x * TAKEN.distanceM, z: eye[2] + flat.z * TAKEN.distanceM, yaw: Math.atan2(-flat.x, -flat.z), eyeX: eye[0], eyeY: eye[1], eyeZ: eye[2], pitchDeg, halfFovDeg: lens.halfFovDeg - TAKEN_LOOK_DEG, halfWideDeg: lens.halfWideDeg }
}

/** Where an eye looks now: its place, its level heading and how far up (degrees). */
function lookOf(camera: Camera): { eye: [number, number, number]; flat: Vector3; pitchDeg: number } {
  const dir = camera.getWorldDirection(new Vector3())
  const flat = new Vector3(dir.x, 0, dir.z)
  if (flat.lengthSq() < 1e-6) flat.set(0, 0, -1)
  flat.normalize()
  const eye = camera.getWorldPosition(new Vector3())
  return { eye: [eye.x, eye.y, eye.z], flat, pitchDeg: Math.asin(Math.max(-1, Math.min(1, dir.y))) / RAD }
}

const LENS = new Frustum()
const LENS_M = new Matrix4()
const CORNER = new Vector3()
/**
 * Whether every drawn piece of the form stands in the lens: each visible mesh's own box corners, in the room, inside
 * the camera's frustum (as a held board's paper is judged, `Board.tsx`). A mesh's own box, not the form's box in the
 * room's axes: a form turned aside would be judged by corners that are not there.
 */
function wholeInLens(body: Object3D, camera: Camera): boolean {
  LENS_M.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
  LENS.setFromProjectionMatrix(LENS_M)
  let whole = true
  body.traverseVisible(o => {
    const m = o as Mesh
    if (!whole || !m.isMesh || !m.geometry) return
    if (!m.geometry.boundingBox) m.geometry.computeBoundingBox()
    const b = m.geometry.boundingBox
    if (!b) return
    for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) {
      if (!LENS.containsPoint(CORNER.set(x, y, z).applyMatrix4(m.matrixWorld))) { whole = false; return }
    }
  })
  return whole
}

/** The box of an object in the picture: least and most x and y, as the camera draws its corners. */
function ndcBox(o: Object3D, camera: Camera): [number, number, number, number] {
  return ndcOf(new Box3().setFromObject(o), camera)
}


/**
 * The eight corners, in the room, of what of `o` is drawn, boxed in the frame of `frame` (the form's own group, turned
 * by its yaw) rather than along the room's axes (M71). Each visible mesh's own box corners are carried into that
 * frame, boxed there, and the box's corners carried back.
 */
function ownCorners(o: Object3D, frame: Object3D): [number, number, number][] & { key?: string } {
  const box = ownBox(o, frame, new Box3())
  if (box.isEmpty()) return []
  const out: [number, number, number][] & { key?: string } = []
  // The box in the form's own frame, to 2 mm: it changes only when what is drawn of it changes (the words on its lip
  // are laid out a few frames after it stands, so a fit made before them left the lip's last line out, w1114).
  out.key = [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z].map(v => Math.round(v * 500)).join(',')
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    CORNER.set(x, y, z).applyMatrix4(frame.matrixWorld)
    out.push([CORNER.x, CORNER.y, CORNER.z])
  }
  return out
}
/** The box, in the frame of `frame`, of each visible mesh of `o` (its own geometry bounds carried there). */
function ownBox(o: Object3D, frame: Object3D, box: Box3): Box3 {
  frame.updateMatrixWorld(true)
  OWN_INV.copy(frame.matrixWorld).invert()
  box.makeEmpty()
  o.traverseVisible(x => {
    const m = x as Mesh
    if (!m.isMesh || !m.geometry) return
    if (!m.geometry.boundingBox) m.geometry.computeBoundingBox()
    const b = m.geometry.boundingBox
    if (!b) return
    OWN_M.multiplyMatrices(OWN_INV, m.matrixWorld)
    box.union(OWN_BOX.copy(b).applyMatrix4(OWN_M))
  })
  return box
}
const OWN_INV = new Matrix4()
const OWN_M = new Matrix4()
const OWN_BOX = new Box3()
const OUTLINE_Q = new Quaternion()
const OUTLINE_Q2 = new Quaternion()
const NO_RAY = (): void => {}
const OUTLINE_AT = new Vector3()

/**
 * What is drawn of `o` on the picture: each visible mesh's own geometry bounds (what the lens test reads,
 * `wholeInLens`), its corners projected. Not `Box3.setFromObject(mesh, true)`: a text mesh's position attribute is one
 * unit quad whose glyphs are placed by other attributes, so its precise box is not where its letters are (w1115: the
 * probe put a whole form's foot at -1.58).
 */
function drawnNdc(o: Object3D, camera: Camera): [number, number, number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  o.traverseVisible(x => {
    const m = x as Mesh
    if (!m.isMesh || !m.geometry) return
    if (!m.geometry.boundingBox) m.geometry.computeBoundingBox()
    const b = m.geometry.boundingBox
    if (!b) return
    for (const cx of [b.min.x, b.max.x]) for (const cy of [b.min.y, b.max.y]) for (const cz of [b.min.z, b.max.z]) {
      CORNER.set(cx, cy, cz).applyMatrix4(m.matrixWorld).project(camera)
      x0 = Math.min(x0, CORNER.x); x1 = Math.max(x1, CORNER.x); y0 = Math.min(y0, CORNER.y); y1 = Math.max(y1, CORNER.y)
    }
  })
  return [x0, y0, x1, y1]
}

/** As `drawnNdc`, for the visible meshes of `o` that carry `name` only; null when none is drawn. */
function namedNdc(o: Object3D, name: string, camera: Camera): [number, number, number, number] | null {
  const got: [number, number, number, number][] = []
  o.traverseVisible(x => { if (x.name === name) got.push(drawnNdc(x, camera)) })
  if (!got.length) return null
  return [Math.min(...got.map(b => b[0])), Math.min(...got.map(b => b[1])), Math.max(...got.map(b => b[2])), Math.max(...got.map(b => b[3]))]
}


/** The kinds of a layout's places, as the rest row stands them: a wrong part is a socket. */
const kindsOf = (layout: DashLayout) => layout.parts.map(p => (isPart(p) ? p.kind : 'socket' as const))

/** `refused`: a press would take nothing now (`trayBlock`), so it does not light. */
function OneDash({ view, taken, refused, crew, hues, still, drag, lens, onPress }: { view: DashView; taken: TakenAt | null; refused: boolean; crew: () => readonly CrewProbe[]; hues: DataHues; still: boolean; drag?: EmptyHand; lens: () => { halfFovDeg: number; halfWideDeg: number }; onPress: () => void }) {
  const group = useRef<Group>(null)
  const slots = useRef<(Group | null)[]>([])
  const tray = useRef<Group>(null)
  const lip = useRef<Group>(null)
  const [hover, setLit] = useState(false)
  const lit = hover && !refused
  /** The pointers on a drawn piece of it now, by id (M246): it is under a pointer while any of them is. */
  const pointers = useRef(new Set<number>())
  const pointedNow = useRef(false)
  pointedNow.current = hover
  /**
   * Taken and cut in the lens, with his head at rest elsewhere: where it is placed again (`base` is the take it
   * replaces), and the glide there from where it stood (D103 2). Where his look rests and since when; when it was taken.
   */
  const again = useRef<{ base: TakenAt; at: TakenAt; moves: number } | null>(null)
  /** At rest: whether a nearer Bot hides it (so it stands over its Bot), since when the other answer stands, how often it changed. */
  const hideRef = useRef<{ hidden: boolean; since: number | null; changes: number }>({ hidden: false, since: null, changes: 0 })
  const glide = useRef<{ from: Vector3; fromQ: Quaternion; done: number } | null>(null)
  const look = useRef({ dir: new Vector3(0, 0, -1), since: 0, lastTaken: null as TakenAt | null, takenAt: 0, checkedAt: 0 })
  /** The press that is on it now: which pointer, where on the screen it began, and whether it has become a drag. */
  const down = useRef<{ id: number; at: [number, number]; dragging: boolean; pinch: number | null } | null>(null)
  /** The session's visibility, for the kept down (D192 2); nothing out of a session. */
  const seenNow = useRef<string | undefined>(undefined)
  seenNow.current = useXR(s => s.visibilityState)
  useEffect(() => () => { trayHeld.delete(view.slug) }, [view.slug])
  // A session that stops being visible, ends or begins lets the kept down go and forgets the pointers on it (M245,
  // M246): heard from the store as the board, the stamp and the Crew hear it, not only in a frame. The preview's
  // pointers are dropped with no leave when a session begins, so none of them keeps the Bot still.
  const xrStore = useXRStore()
  useEffect(() => xrStore.subscribe((state, prev) => {
    if (state.visibilityState === prev.visibilityState || !visibilityTakes(prev.visibilityState, state.visibilityState)) return
    down.current = null
    pointers.current.clear()
    setLit(false)
  }), [xrStore])
  /**
   * Whether a pointer lies on a piece of it that is drawn: a mesh shown, with its material shown, and no group above it
   * hidden. Only such a piece takes the press, so a press where the Bot behind it shows is the Bot's (D55 5). A ray
   * also finds hidden meshes (the folded lip, the tray drawn by the merged shape at rest).
   */
  // A gap between two parts at rest is the dashboard's while it is narrower than half a degree at his eye (D56 3): a
  // press mesh lies in it, never drawn, and is on only then (set each frame).
  // Taken, its whole drawn outline is its press shape (M93, D114 2): a ring's hole, the air under a bench and the
  // space between parts are the form's, not what stands behind it. Never drawn; on only while it is taken.
  const drawn = (e: ThreeEvent<PointerEvent>): boolean => {
    if ((e.object as Object3D).userData?.dashGap || (e.object as Object3D).userData?.dashOutline) return group.current?.visible === true && e.object.visible
    return drawnUnder(e.object, group.current)
  }
  const gaps = useRef<(Mesh | null)[]>([])
  const outline = useRef<Mesh>(null)
  // A form that goes (its Bot left the room, the page closed) leaves no picture behind for the room to read.
  useEffect(() => () => setTakenPicture(view.slug, null), [view.slug])
  /** It rests, and has come all the way back: what never moves of it is then one shape. */
  const [atRest, setAtRest] = useState(true)
  const atRestNow = useRef(true)
  const places = view.layout.parts
  const n = Math.max(1, places.length)
  const kinds = kindsOf(view.layout).join()
  // At rest the parts stand close; taken, a pitch apart.
  const row = useMemo(() => restRow(kinds ? (kinds.split(',') as ReturnType<typeof kindsOf>) : []), [kinds])
  const rest = useMemo(() => restBox(row.wideM), [row])
  const restGapList = useMemo(() => restGaps(kinds ? (kinds.split(',') as ReturnType<typeof kindsOf>) : [], PART_TOP), [kinds])
  const words = taken !== null
  const broken = view.layout.why !== ''
  const wide = broken ? Math.max(trayWideM(n), WORDS.whyWideM) : trayWideM(n)
  const xs = takenRow(n)
  const camera = useThree(s => s.camera)
  const fmt = (v: number) => fmtCompact(v)
  /** Where it is now, between its two sizes (`mix`: 0 at rest, 1 taken). Made once. */
  const poseRef = useRef<{ mix: number; restAt: Vector3; restSet: boolean; restQ: Quaternion; takenQ: Quaternion; euler: Euler; to: Vector3; at: Vector3; head: Vector3 } | null>(null)
  poseRef.current ??= { mix: 0, restAt: new Vector3(), restSet: false, restQ: new Quaternion(), takenQ: new Quaternion(), euler: new Euler(0, 0, 0, 'YXZ'), to: new Vector3(), at: new Vector3(), head: new Vector3() }
  /** How the taken form was moved to stand whole: so much higher (m), so many times farther; `n` frames looked at. */
  const fit = useRef({ key: '', dy: 0, k: 1, n: 0, box: '', at: 0, again: 0, log: [] as FitStep[], seen: '' })
  useFrame((_s, dt) => {
    const g = group.current
    const pose = poseRef.current
    if (!g || !pose) return
    const began = dashWork.on ? performance.now() : 0
    const head = camera.getWorldPosition(pose.head)
    const k = still ? 1 : Math.min(1, dt * 10)
    // A new take (or none) drops a place made again for the old one. His look's rest, as a held board reads it.
    const now = performance.now()
    const lk = look.current
    if (lk.lastTaken !== taken) { lk.lastTaken = taken; lk.takenAt = now; again.current = null; glide.current = null }
    const dir = camera.getWorldDirection(LOOK_DIR)
    if (lk.dir.dot(dir) < Math.cos(HELD_COME.restDeg * RAD)) { lk.dir.copy(dir); lk.since = now }
    const placed = again.current?.at ?? taken
    // At rest its Bot holds it in front of its own body, a little over its feet, facing him.
    const bot = crew().find(c => c.slug === view.slug)
    const seen = !!bot && !bot.seat
    if (bot && seen) {
      const [x, y, z] = bot.feet
      const yaw = Math.atan2(head.x - x, head.z - z)
      pose.to.set(x + Math.sin(yaw) * rest.centreM, y + rest.liftM, z + Math.cos(yaw) * rest.centreM)
      // A nearer Bot's body between his eye and the tray: the tray stands over its own Bot's head instead, where it is
      // seen and can be pressed (D99 2; a press is what is drawn, D55 5).
      const others = crew().filter(c => c.slug !== view.slug && !c.seat).map(c => ({ feet: c.feet, top: c.top, radiusM: c.widthM / 2 }))
      // With slack and a hold, and never while a pointer is down on it or lies on it, so it does not bob (M72).
      const eyeAt: [number, number, number] = [head.x, head.y, head.z]
      const mid: [number, number, number] = [pose.to.x, pose.to.y + rest.tallM / 2, pose.to.z]
      const h = hideRef.current
      const step = hiddenStep(h.hidden, hiddenBy(eyeAt, mid, others), hiddenBy(eyeAt, mid, others, HIDDEN_SLACK.slackM), h.since, now, pointedNow.current || down.current !== null)
      if (step.hidden !== h.hidden) h.changes += 1
      h.hidden = step.hidden; h.since = step.since
      if (h.hidden) pose.to.y = bot.top[1] + REST_CLEAR_M
      g.userData.hidden = { hidden: h.hidden, changes: h.changes }
      // D192 1: under a pointer, or with a down kept on it, the resting tray stands where it is (its Bot's drift stands
      // too, `Crew.tsx`); let go, it follows its Bot again by the same easing.
      const holds = trayHolds({ pointed: pointedNow.current, down: down.current !== null, resting: !taken && pose.mix === 0, carried: !!bot.carried, walking: !!bot.walking })
      if (holds) trayHeld.add(view.slug); else trayHeld.delete(view.slug)
      if (!pose.restSet) { pose.restAt.copy(pose.to); pose.restSet = true } else if (!holds) pose.restAt.lerp(pose.to, k)
      pose.restQ.setFromEuler(pose.euler.set(0, yaw, 0))
    }
    if (!(bot && seen)) trayHeld.delete(view.slug)
    // D192 2: in a session a kept down is let go with nothing taken once its pinch is over (its up came to something
    // else), at the quiet time, or when the session is not visible. In the preview the box holds the pointer.
    if (!drag && down.current && downLetGo({ pinching: pinchRoute.owner !== null, quiet: pinchRoute.quiet(now), visible: seenNow.current === 'visible', stale: down.current.pinch !== pinchRoute.downs })) down.current = null
    const goal = taken ? 1 : 0
    pose.mix += (goal - pose.mix) * k
    if (Math.abs(goal - pose.mix) < 0.002) pose.mix = goal
    const resting = !taken && pose.mix === 0
    if (resting !== atRestNow.current) { atRestNow.current = resting; setAtRest(resting) }
    // Drawn while it is taken or on its way back; at rest only by a Bot that stands in the row.
    g.visible = taken !== null || pose.mix > 0 || (seen && pose.restSet)
    const f = fit.current
    const fitKey = placed ? `${placed.x},${placed.z},${placed.eyeY},${placed.pitchDeg},${placed.halfFovDeg},${placed.halfWideDeg}` : ''
    if (f.key !== fitKey) { f.key = fitKey; f.dy = 0; f.k = 1; f.n = 0; f.box = ''; f.at = now; f.again = 0; f.log = [] }
    if (placed) {
      // `takenTrayTopY` is the first guess; the form's own drawn box then moves it until it stands whole (`fitTaken`).
      pose.at.set(placed.eyeX + (placed.x - placed.eyeX) * f.k, takenTrayTopY(placed.eyeY, TAKEN.distanceM * f.k, placed.pitchDeg, placed.halfFovDeg, TOP_Y) + f.dy, placed.eyeZ + (placed.z - placed.eyeZ) * f.k)
      pose.takenQ.setFromEuler(pose.euler.set(0, placed.yaw, 0))
    }
    const e = pose.mix
    if (e <= 0 || !pose.restSet) {
      if (e > 0) { g.position.copy(pose.at); g.quaternion.copy(pose.takenQ) } else { g.position.copy(pose.restAt); g.quaternion.copy(pose.restQ) }
    } else {
      g.position.lerpVectors(pose.restAt, pose.at, e)
      g.quaternion.slerpQuaternions(pose.restQ, pose.takenQ, e)
    }
    g.scale.setScalar(rest.scale + (1 - rest.scale) * e)
    // The parts slide apart as it is taken, the tray grows as wide as their row, and the lip folds out.
    slots.current.forEach((s, i) => { if (s) s.position.x = (row.xs[i] ?? 0) + ((xs[i] ?? 0) - (row.xs[i] ?? 0)) * e })
    if (tray.current) tray.current.scale.x = (row.wideM + (wide - row.wideM) * e) / wide
    if (lip.current) { lip.current.visible = e > 0.02; lip.current.scale.set((row.wideM + (wide - row.wideM) * e) / wide, Math.max(0.001, e), 1) }
    // Taken and standing: its drawn box, as angles at the eye, is held inside the lens it was placed for; a form too
    // tall for it stands farther. A few frames at most, then it stays (D99 2).
    if (placed && e === 1) {
      const body = g.getObjectByName('dash-body')
      if (body) {
        g.updateMatrixWorld(true)
        const corners = ownCorners(body, g)
        f.seen = corners.key ?? ''
        // What is drawn of it changed since its fit (the lip's words come a few frames late): the fit's four looks begin
        // again, once, and never while a pinch is down (`fitAgain`, M82). Later a change moves nothing (D103 2).
        if (fitAgain(f, corners.key ?? '', now, pinchRoute.owner !== null)) { f.n = 0; f.again += 1 }
        if (f.n < 4) {
          f.box = corners.key ?? ''
          const step = fitTaken(corners, [placed.eyeX, placed.eyeY, placed.eyeZ], placed.pitchDeg, placed.halfFovDeg, { yawRad: placed.yaw, halfWideDeg: placed.halfWideDeg })
          f.n += 1
          if (dashWork.on && f.log.length < 16) f.log.push({ ms: Math.round(now - f.at), box: f.box, k: +step.k.toFixed(4), dy: +step.dy.toFixed(4) })
          if (step.k !== 1) f.k *= step.k; else if (Math.abs(step.dy) > 0.001) f.dy += step.dy; else f.n = 4
        }
      }
    }
    // Taken and standing: cut in the lens with his head at rest, and nothing holding it, it is placed again where he
    // looks now and glides there in the board's time (D103 2, `HELD_COME`). It never starts to move while a pinch is
    // down, while a pointer lies on it, or while his head moves; a glide under way stops only for a pinch (D108 3).
    if (taken && e === 1) {
      const hold: TakenHold = { pinching: pinchRoute.owner !== null, pointed: pointedNow.current, moving: now - lk.since < HELD_COME.restMs }
      g.userData.hold = hold
      const gd = glide.current
      if (gd) {
        if (glideGoes(hold)) gd.done = Math.min(1, gd.done + dt / (HELD_COME.glideMs / 1000))
        const s = glideShare(gd.done)
        g.position.lerpVectors(gd.from, pose.at, s)
        g.quaternion.slerpQuaternions(gd.fromQ, pose.takenQ, s)
        if (gd.done >= 1) glide.current = null
      } else if (f.n >= 4 && now - lk.checkedAt >= CHECK_MS) {
        lk.checkedAt = now
        const body = g.getObjectByName('dash-body')
        g.updateMatrixWorld(true)
        const whole = body ? wholeInLens(body, camera) : true
        g.userData.come = { whole, ...hold, restedMs: Math.round(now - Math.max(lk.since, lk.takenAt)) }
        // Placed again only for a look that is not the one it was last placed for: a form that the fit cannot bring
        // whole is not placed again and again while he looks the same way.
        const l = lookOf(camera)
        const lastLook = again.current?.at ?? taken
        // A step or a lean of `SAME_PLACE_M` is a new look too (R9), though he faces the same way.
        const same = sameLook({ eye: l.eye, yawRad: Math.atan2(-l.flat.x, -l.flat.z), pitchDeg: l.pitchDeg }, { eye: [lastLook.eyeX, lastLook.eyeY, lastLook.eyeZ], yawRad: lastLook.yaw, pitchDeg: lastLook.pitchDeg }, HELD_COME.restDeg)
        if (body && !same && takenComes(whole, hold, now, lk.since, lk.takenAt, HELD_COME.restMs)) {
          again.current = { base: taken, at: takenAt(l.eye, l.flat, lens(), l.pitchDeg), moves: (again.current?.moves ?? 0) + 1 }
          glide.current = { from: g.position.clone(), fromQ: g.quaternion.clone(), done: 0 }
        }
      }
    }
    g.userData.moves = again.current?.moves ?? 0
    g.userData.fit = taken ? { n: f.n, k: +f.k.toFixed(3), dy: +f.dy.toFixed(3), mix: +pose.mix.toFixed(3), log: f.log, seen: f.seen } : null
    g.userData.gliding = glide.current !== null
    // The gaps at rest: on while narrower than half a degree at his eye; off as soon as the parts move apart.
    const far = g.visible ? head.distanceTo(g.getWorldPosition(GAP_AT)) : 0
    gaps.current.forEach((m, i) => { const gap = restGapList[i]; if (m && gap) m.visible = e === 0 && gapTakes((gap.x1 - gap.x0) * g.scale.x, far, m.visible) })
    // Taken, from the press of the take on (M95): its outline takes every press inside it, and the room reads its
    // picture (`takenPicture`: a desk line under it gives up its front). At rest in a Bot's hands it does neither.
    const dashBody = taken && g.visible ? g.getObjectByName('dash-body') : undefined
    const ol = outline.current
    // Only what is drawn inside the lens (D137): a corner behind the eye is cut, the box is cut to the lens, and a
    // form with nothing in the lens has no box, so no outline and no picture.
    const nd = dashBody ? drawnLensNdc(dashBody, camera) : null
    if (ol) {
      ol.visible = !!nd
      // A ray finds a hidden mesh too: off, it is never hit, so it stands in front of nothing (a Bot's lift, the dot).
      ol.raycast = ol.visible ? MeshClass.prototype.raycast : NO_RAY
      // Its outline is exactly its drawn box in the picture: a sheet that faces the eye, at the form's middle depth,
      // over that box. So a press just outside what is drawn of it is not the form's (D114 2: "2 cm outside").
      if (nd && ol.visible) {
        // shortcut: with the form's middle behind the eye (a turn past 90 degrees), the sheet stands 0.1 m ahead; give it
        // the seen part's own depth if a form so far turned is ever pressed.
        const mid = g.getWorldPosition(OUTLINE_AT)
        const depth = (mid.clone().applyMatrix4(camera.matrixWorldInverse).z < -0.1 ? mid : camera.localToWorld(new Vector3(0, 0, -0.1))).project(camera).z
        const at = (x: number, y: number) => new Vector3(x, y, depth).unproject(camera)
        const a = at(nd[0], nd[1]), b = at(nd[2], nd[1]), c = at(nd[0], nd[3])
        ol.position.copy(g.worldToLocal(at((nd[0] + nd[2]) / 2, (nd[1] + nd[3]) / 2)))
        ol.quaternion.copy(g.getWorldQuaternion(OUTLINE_Q).invert().multiply(camera.getWorldQuaternion(OUTLINE_Q2)))
        const k = g.getWorldScale(OUTLINE_AT).x || 1
        ol.scale.set(Math.max(1e-4, a.distanceTo(b) / k), Math.max(1e-4, a.distanceTo(c) / k), 1)
      }
    }
    setTakenPicture(view.slug, dashBody && nd ? { slug: view.slug, ndc: nd, distM: head.distanceTo(g.getWorldPosition(OUTLINE_AT)) } : null)
    // A piece that is not drawn is not hit (D140 D: a hidden one took the down and the press was dropped).
    raysOnDrawn(g as unknown as RayPiece, NO_RAY)
    if (dashWork.on) dashWork.ms += performance.now() - began
  })

  // The press is what is drawn of it, and nothing round it (`drawn`, D55 5): a press where the Bot behind it shows is
  // the Bot's.
  const grey = broken || view.stale
  // What never moves: each place's pieces, and the tray's body. At rest they are one shape, so one draw. The rim
  // that a pointer lights is not in it: it is drawn alone, so a pointer that comes and goes makes no shape again.
  const merged = atRest && !words
  const solid = places.length === 0
    ? partPieces('socket', null, hues, view.stale).map(piece => ({ piece, dx: 0 }))
    : places.flatMap((p, i) => (isPart(p) ? partPieces(p.kind, partValue(p, view.fields), hues, view.stale) : partPieces('socket', null, hues, view.stale)).map(piece => ({ piece, dx: row.xs[i] ?? 0 })))
  const [restBody, restRim] = trayPieces(row.wideM, grey, lit)
  const restPieces = [{ piece: restBody, dx: 0 }, ...solid]
  const restKey = merged ? restPieces.map(({ piece: q, dx }) => `${q.geo.uuid}|${q.at}|${q.rot ?? ''}|${q.scale ?? ''}|${q.color}|${dx}`).join(';') : ''
  // `restKey` stands for every piece: the shape is made again only when one of them changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const restShape = useMemo(() => (restKey ? mergePieces(restPieces) : null), [restKey])
  useEffect(() => () => restShape?.dispose(), [restShape])
  return (
    <group
      ref={group}
      visible={false}
      userData={{ dash3d: view.slug, lit, xrPress: true }}
      onPointerEnter={(e: ThreeEvent<PointerEvent>) => { logPress({ slug: view.slug, ev: 'enter', pointer: e.pointerType, id: e.pointerId, piece: pieceOf(e), drawn: drawn(e) }); if (drawn(e) && e.pointerType !== 'screen-touch') { pointers.current.add(e.pointerId); setLit(true) } }}
      // What is behind a drawn piece of it gets nothing of a pointer that lies on it: no press, and no move either.
      onPointerMove={(e: ThreeEvent<PointerEvent>) => {
          const p = down.current
          if (!drawn(e) && p?.id !== e.pointerId) return
          e.stopPropagation()
          if (!drag || p?.id !== e.pointerId) return
          if (!p.dragging) {
            if (!(Math.hypot(e.nativeEvent.clientX - p.at[0], e.nativeEvent.clientY - p.at[1]) > TURN_DRAG_PX)) return
            // A press that moves is no press: it is the drag that empty space would have had, from where it came down.
            p.dragging = true
            setLit(false)
            drag.onPointerDown({ pointerId: e.pointerId, object: e.object, nativeEvent: { clientX: p.at[0], clientY: p.at[1] } } as unknown as ThreeEvent<PointerEvent>)
          }
          drag.onPointerMove(e)
        }}
        onPointerOver={(e: ThreeEvent<PointerEvent>) => { if (drawn(e)) e.stopPropagation() }}
        // In a session a press that leaves it is no press. (In the preview the press is held until it ends.)
        // A leave of the pointer that holds a down keeps that down (D192 2): the up decides (`upTakes`).
        onPointerLeave={(e: ThreeEvent<PointerEvent>) => { logPress({ slug: view.slug, ev: 'leave', pointer: e.pointerType, id: e.pointerId, piece: pieceOf(e), held: down.current?.id === e.pointerId }); pointers.current.delete(e.pointerId); setLit(pointers.current.size > 0) }}
        onPointerDown={(e: ThreeEvent<PointerEvent>) => {
          logPress({ slug: view.slug, ev: 'down', pointer: e.pointerType, id: e.pointerId, piece: pieceOf(e), drawn: drawn(e) })
          if (!drawn(e)) return
          e.stopPropagation()
          down.current = { id: e.pointerId, at: [e.nativeEvent.clientX, e.nativeEvent.clientY], dragging: false, pinch: drag ? null : pinchRoute.downs }
          if (drag) (e.object as Object3D).setPointerCapture(e.pointerId)
        }}
        onPointerCancel={(e: ThreeEvent<PointerEvent>) => {
          logPress({ slug: view.slug, ev: 'cancel', pointer: e.pointerType, id: e.pointerId, held: down.current?.id === e.pointerId })
          const began = down.current
          down.current = null
          if (began?.id === e.pointerId && began.dragging) drag?.onPointerCancel(e)
        }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => {
          // Only the up of a down that this saw itself, by the same pointer, that did not leave it between.
          logPress({ slug: view.slug, ev: 'up', pointer: e.pointerType, id: e.pointerId, piece: pieceOf(e), drawn: drawn(e), held: down.current?.id === e.pointerId, ending: pinchRoute.ending })
          const began = down.current
          down.current = null
          if (began?.id !== e.pointerId) return
          e.stopPropagation()
          if (began.dragging) { drag?.onPointerUp(e); return }
          // A pinch that moved ends with no press, as on a page and at the desk; an up off its drawn pieces takes nothing.
          if (!upTakes(began, { pointerId: e.pointerId, drawn: drawn(e), ending: pinchRoute.ending, pinch: drag ? null : pinchRoute.downs })) return
          onPress()
        }}
    >
      <mesh ref={outline} name="dash-outline" visible={false} raycast={NO_RAY} userData={{ dashOutline: true }}>
        <planeGeometry />
        <meshBasicMaterial visible={false} />
      </mesh>
      {restGapList.map((gap, i) => (
        <mesh key={`gap-${i}`} ref={m => { gaps.current[i] = m }} name="dash-gap" visible={false} userData={{ dashGap: true }} position={[(gap.x0 + gap.x1) / 2, gap.top / 2, 0]} scale={[Math.max(0.0001, gap.x1 - gap.x0), gap.top, FORM.part[2]]}>
          <boxGeometry />
          <meshBasicMaterial visible={false} />
        </mesh>
      ))}
      <group name="dash-body">
        {restShape && (
          <>
            <mesh name="dash-rest" geometry={restShape}>
              <meshLambertMaterial vertexColors toneMapped={false} />
            </mesh>
            <Pieces list={[restRim]} />
          </>
        )}
        {/* Taken, or on its way: the tray, and the rim that a pointer on it lights, as wide as its row is now. */}
        <group ref={tray} visible={!restShape}>
          <Pieces list={trayPieces(wide, grey, lit)} />
        </group>
        {/* The lip at its front, sloped to him, folded away at rest: every word is written on it. */}
        <group position={[0, 0, FORM.trayDeepM / 2]} rotation={[-LEAN, 0, 0]}>
          <group ref={lip} visible={false}>
            <mesh position={[0, -FORM.lipM / 2, -0.003]}>
              <boxGeometry args={[wide, FORM.lipM, 0.006]} />
              <meshLambertMaterial color={grey ? XR_GL.edge : XR_GL.raised} toneMapped={false} />
            </mesh>
          </group>
          {words && places.map((p, i) => {
            const top = -0.004
            const both = WORDS.numberH + WORDS.nameH
            if (!isPart(p)) return <DashLabel key={i} name="dash-why" text={WHY[p.broken]()} widthM={FORM.pitchM - 0.006} heightM={both} sizeM={WORDS.nameSize} lines={3} color={XR_GL.dim} position={[xs[i], top - both / 2, 0.0008]} />
            // One rule for every part: its number, and under it its name.
            return (
              <group key={i}>
                <DashLabel name="dash-number" text={partText(p, partValue(p, view.fields), fmt)} widthM={FORM.pitchM - 0.006} heightM={WORDS.numberH} sizeM={WORDS.numberSize} color={view.stale ? XR_GL.faint : XR_GL.ink} weight={700} position={[xs[i], top - WORDS.numberH / 2, 0.0008]} />
                <DashLabel name="dash-name" text={p.name} widthM={FORM.pitchM - 0.006} heightM={WORDS.nameH} sizeM={WORDS.nameSize} color={XR_GL.dim} position={[xs[i], top - WORDS.numberH - WORDS.nameH / 2, 0.0008]} />
              </group>
            )
          })}
          {words && broken && <DashLabel name="dash-why" text={WHY[view.layout.why as DashWhy]()} widthM={wide - 0.01} heightM={WHY_H} sizeM={WORDS.nameSize} lines={WORDS.whyLines} shrink={false} color={XR_GL.ink} position={[0, -0.004 - WHY_H / 2, 0.0008]} />}
          {words && <DashLabel name="dash-header" text={i18nT('xr.dash3d_header', { name: crewText(view.label) })} widthM={wide - 0.01} heightM={WORDS.headerH} sizeM={WORDS.headerSize} color={XR_GL.dim} position={[0, -FORM.lipM + 0.002 + WORDS.headerH / 2, 0.0008]} />}
        </group>
        {places.map((p, i) => {
          const value = isPart(p) ? partValue(p, view.fields) : null
          return (
            <group key={i} name={`dash-part-${i}`} ref={g => { slots.current[i] = g }}>
              {isPart(p) && value ? <DashPartBody kind={p.kind} solid={!restShape} value={value} hues={hues} dim={view.stale} still={still} /> : !restShape && <Pieces list={partPieces('socket', null, hues, view.stale)} />}
            </group>
          )
        })}
        {places.length === 0 && !restShape && <Pieces list={partPieces('socket', null, hues, view.stale)} />}

      </group>
    </group>
  )
}

export function Dash3d({ views, crew, block, boardsUp, say, drag, still, stateColors, report, onTaken, takenFrom }: Props) {
  const camera = useThree(s => s.camera)
  const gl = useThree(s => s.gl)
  /** How tall the lens is over its middle: the headset's own eye in a session, else the page's camera. */
  const halfFovNow = (): { halfFovDeg: number; halfWideDeg: number } => {
    const lens = gl.xr.isPresenting ? gl.xr.getCamera().cameras[0] : null
    const m = (lens ?? camera).projectionMatrix.elements
    return { halfFovDeg: m[5] > 0 ? Math.atan(1 / m[5]) / RAD : 20, halfWideDeg: m[0] > 0 ? Math.atan(1 / m[0]) / RAD : 30 }
  }
  const root = useRef<Group>(null)
  /** The one that is taken, and where it stands. */
  const [taken, setTaken] = useState<{ slug: string; at: TakenAt } | null>(null)
  // Whether a dashboard is taken now: the crew with no row keeps the low middle free for it (`places.ts`).
  const takenSlug = taken?.slug ?? null
  useEffect(() => { onTaken?.(takenSlug !== null) }, [onTaken, takenSlug])
  const hues = useMemo(() => dataHues(Object.values(stateColors)), [stateColors])
  // A board in his hand or the card's keyboard sends the dashboard back; so does a dashboard that is no longer there.
  const there = taken !== null && views.some(v => v.slug === taken.slug)
  useEffect(() => { if (taken && (block || !there)) setTaken(null) }, [taken, block, there])
  // A board that a Bot holds up anew sends it back too, when the board's picture overlaps the taken tray's (the tray
  // would stand in front of that placard); one that was up already does not, nor one at a Bot far to the side. The
  // board is drawn and settles at its Bot in its first frames, so it is watched for `CAME_UP_MS`.
  const upBefore = useRef(boardsUp)
  const cameUp = useRef(new Map<string, number>())
  useEffect(() => {
    const until = performance.now() + CAME_UP_MS
    for (const id of boardsCameUp(upBefore.current, boardsUp)) cameUp.current.set(id, until)
    upBefore.current = boardsUp
  }, [boardsUp])
  const scene = useThree(s => s.scene)
  useFrame(() => {
    const watched = cameUp.current
    if (!watched.size) return
    const now = performance.now()
    for (const [id, until] of watched) if (now > until) watched.delete(id)
    const slug = state.current.taken?.slug
    const body = slug ? root.current?.children.find(g => g.userData.dash3d === slug)?.getObjectByName('dash-body') : undefined
    const trayBox = body ? new Box3().setFromObject(body) : null
    if (!trayBox || !watched.size || !inFront(trayBox, camera)) { if (!slug) watched.clear(); return }
    const tray = lensNdc(boxCorners(trayBox), camera)
    if (!tray) return
    let over = false
    scene.traverse(o => {
      const id = o.userData.board as string | undefined
      if (over || !id || !watched.has(id)) return
      const board = drawnBox(o)
      const seen = inFront(board, camera) ? lensNdc(boxCorners(board), camera) : null
      if (seen && picturesOverlap(seen, tray)) {
        over = true
        if (dashWork.on) dashWork.sentBack.push({ board: id, tray: [...tray], seen: [...seen] })
      }
    })
    if (over) { watched.clear(); setTaken(null) }
  })
  const press = (slug: string) => {
    const does = trayPress(taken?.slug === slug, block)
    logPress({ slug, ev: 'press', pointer: '', id: -1, does, block, taken: taken?.slug ?? null })
    if (does === 'back') { setTaken(null); return }
    if (does === 'refused') { say(i18nT(block === 'board' ? 'xr.dash3d_refused_board' : 'xr.dash3d_refused_keys')); return }
    // In front of where he looks, facing him, whole in the lens as his head is at the take (D103 2).
    const l = lookOf(camera)
    setTaken({ slug, at: takenAt(l.eye, l.flat, halfFovNow(), l.pitchDeg) })
  }
  const firstSlug = views[0]?.slug ?? null
  const fromKey = takenFrom ? takenFrom.join(',') : ''
  useEffect(() => {
    if (!takenFrom || !firstSlug) return
    setTaken({ slug: firstSlug, at: takenAt(takenFrom, new Vector3(0, 0, -1), halfFovNow()) })
    // `fromKey` stands for `takenFrom`; the lens is read once, at the take.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromKey, firstSlug])
  const state = useRef({ views, taken, hues })
  state.current = { views, taken, hues }
  useFrame(() => { if (dashWork.on) dashWork.frames += 1 })
  useEffect(() => {
    if (!report) return
    dashWork.on = true
    ;(window as unknown as { __xrDashWork?: typeof dashWork }).__xrDashWork = dashWork
    // The probe's hits on the dot's ray (DASH-16 B): every mesh the ray meets, nearest first, by its path of names,
    // its distance, the press order the library sorts by (the nearest `pointerEventsOrder` above it) and whether a
    // board above it is drawn in front of the desk (`front`), so a row can say why one hit won over a nearer one.
    ;(window as unknown as { __xrHeadHits?: () => unknown[] }).__xrHeadHits = () => {
      const h = pinchRoute.probe.head
      if (!h) return []
      const ray = new Raycaster(new Vector3(...h.origin), new Vector3(...h.direction).normalize())
      return ray.intersectObject(scene, true).slice(0, 12).map(x => {
        const path: string[] = []
        let order: number | undefined
        let front: boolean | undefined
        let shown = true
        for (let o: Object3D | null = x.object; o; o = o.parent) {
          if (path.length < 4) path.push(o.name || o.type)
          const po = (o as Object3D & { pointerEventsOrder?: number }).pointerEventsOrder
          if (order === undefined && po !== undefined) order = po
          if (front === undefined && typeof o.userData.front === 'boolean') front = o.userData.front
          if (!o.visible) shown = false
        }
        return { path: path.join(' < '), distanceM: +x.distance.toFixed(4), order: order ?? null, front: front ?? null, shown }
      })
    }
    report(() => {
      const out: Record<string, DashMarks> = {}
      const now = state.current
      for (const g of root.current?.children ?? []) {
        const slug = g.userData.dash3d as string | undefined
        const view = now.views.find(v => v.slug === slug)
        if (!slug || !view || !g.visible) continue
        const body = g.getObjectByName('dash-body')
        // Its press is what it draws (D55 5): the box of its drawn pieces.
        if (!body) continue
        const b = new Box3().setFromObject(body)
        const at = g.getWorldPosition(new Vector3())
        const isTaken = now.taken?.slug === slug
        const half = restRow(kindsOf(view.layout)).wideM / 2
        const end = (x: number): [number, number, number] => { const v = g.localToWorld(new Vector3(x, 0, -FORM.trayDeepM / 2)); return [v.x, v.y, v.z] }
        const texts = (name: string): string[] => { const list: string[] = []; g.traverse(o => { if (o.name === name) list.push(String(o.userData.text ?? '')) }); return list }
        out[slug] = {
          where: isTaken ? 'taken' : 'rest', lit: g.userData.lit === true, why: view.layout.why,
          // `ndc`: the box of what is DRAWN of it (its visible meshes' own bounds, `drawnNdc`), so a row judges the form
          // a person sees; the hidden pieces that wait in it (the gaps, the merged rest shape) stood out past it.
          at: [at.x, at.y, at.z], box: [[b.min.x, b.min.y, b.min.z], [b.max.x, b.max.y, b.max.z]], ndc: drawnNdc(body, camera), lens: drawnLensNdc(body, camera), press: ndcBox(body, camera),
          far: isTaken ? null : [end(-half), end(half)],
          hidden: (g.userData.hidden as DashMarks['hidden']) ?? null,
          fit: (g.userData.fit as DashMarks['fit']) ?? null,
          moves: Number(g.userData.moves ?? 0), gliding: g.userData.gliding === true, hold: isTaken ? ((g.userData.hold as TakenHold | undefined) ?? null) : null, come: (g.userData.come as DashMarks['come']) ?? null,
          scale: g.scale.x,
          wordsTopY: (() => { let top: number | null = null; g.traverse(o => { if (/^dash-(number|name|why|header)$/.test(o.name)) top = Math.max(top ?? -Infinity, new Box3().setFromObject(o).max.y) }); return top })(),
          parts: view.layout.parts.map(p => {
            if (!isPart(p)) return { kind: 'socket', broken: p.broken, found: false, share: null, number: null, lamps: [] }
            const v = partValue(p, view.fields)
            return { kind: p.kind, found: v.found, share: v.share, number: v.number, lamps: v.lamps }
          }),
          // Each part's drawn box in the picture, so a row can aim at a part's drawn middle (D107 5).
          partsNdc: view.layout.parts.map((_, i) => { const s = g.getObjectByName(`dash-part-${i}`); return s ? drawnNdc(s, camera) : null }),
          partsAt: view.layout.parts.map((_, i) => { const s = g.getObjectByName(`dash-part-${i}`); const b = s ? drawnBox(s) : null; if (!b || b.isEmpty()) return null; const c = b.getCenter(new Vector3()); return [c.x, c.y, c.z] as [number, number, number] }),
          // Each gap's press box as the room has it (its unit box by this matrix), so a row can test a ray against it.
          gapsAt: (() => { const list: { on: boolean; m: number[] }[] = []; g.traverse(o => { if (o.name === 'dash-gap') list.push({ on: o.visible, m: o.matrixWorld.toArray() }) }); return list })(),
          words: { header: texts('dash-header')[0] ?? '', numbers: texts('dash-number'), names: texts('dash-name'), why: texts('dash-why'), whyNdc: namedNdc(g, 'dash-why', camera), headerNdc: namedNdc(g, 'dash-header', camera) },
          hues: now.hues,
        }
      }
      return out
    })
    return () => { report(null); dashWork.on = false }
  }, [report, camera])
  return (
    <group ref={root}>
      <DashLights />
      {report ? <RoomMark /> : null}
      {views.map(v => <OneDash key={v.slug} view={v} taken={taken?.slug === v.slug ? taken.at : null} refused={block !== null && taken?.slug !== v.slug} crew={crew} hues={hues} still={still} drag={drag} lens={halfFovNow} onPress={() => press(v.slug)} />)}
    </group>
  )
}
