/**
 * The boards in the room of pages (`board.ts`, `useBoards.ts`). A Bot that waits on an approval holds up a signature
 * clipboard over its head (Ray's D18), its paper saying what it asks; at a desk seat a small one with its name. A
 * short press on it takes it: it stands in front of him where he looked, fixed in the room, the sentence and the whole
 * command on its paper, and a stamp beside it (`BoardStamp.tsx`, D17). He grabs the stamp and presses it on the
 * paper: that is the yes, and a green APPROVED print stays where it was pressed. A small line at the paper's foot
 * opens the Bot's page. A press on the board that moves it far and fast is a throw; any other move is a hand-back. A
 * thrown board with no print lies on the floor until its time ends; a press on it picks it up. A board that leaves
 * comes apart into pieces of its own colours (`BoardDust.tsx`, D4a and D21).
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXRStore } from '@react-three/xr'
import { SPEAK_EDGE, speakFit } from './speakFit'
import { useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { Box3, DoubleSide, Frustum, Matrix4, Plane, Raycaster, Vector3, type Group, type Mesh, type MeshBasicMaterial, type Object3D, type Ray } from 'three'

/** The paper of a board's group in the room: its face's four corners, and the stamp's place when it has one. */
function paperPoints(g: Object3D): Vector3[] {
  const face = g.children[0]
  const size = face ? face.getWorldScale(new Vector3()) : new Vector3()
  const sx = g.scale.x || 1
  const sy = g.scale.y || 1
  const out = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => g.localToWorld(new Vector3((x * size.x) / 2 / sx, (y * size.y) / 2 / sy, 0)))
  const stamp = g.getObjectByName('board-stamp')
  if (stamp) out.push(stamp.getWorldPosition(new Vector3()))
  return out
}
/** The paper's middle in the room. */
const paperMiddle = (g: Object3D) => g.localToWorld(new Vector3(0, 0, 0))
import type { CrewProbe } from './Crew'
import { BoardDust, useDustWarm, type DustFrom } from './BoardDust'
import { BoardLower } from './BoardLower'

/** For the probe: how many boards left in pieces (a throw) and how many were lowered (any other end). */
const boardLeaves = { dust: 0, lowered: 0 }
import { BoardStamp, pressRay, useStampWarm } from './BoardStamp'
import { DEPTH, DESK_FRONT, PRESS, layIn, ownOrder } from './drawOrder'
import { CLIPBOARD, PRINT_FACE, TAG_AT_BOT, boardPaints, printPlace, useBoardPicture, useTagPicture, type BoardKind, type Uv, type UvBox } from './boardPicture'
import { DESK } from './desk'
import { DESK_FIT, besideClear, besideOrUnder, deskPlace, deskWords, span, underAt, type FacingBox, type SeenBox } from './deskFit'
import { STRIP_LABEL_M } from './home'
import { pinchRoute, whatIs } from './pinchRoute'
import { ROOM_FRONT_YAW_DEG, homePlace } from './homePlace'
import { takenCovers, takenPicture } from './takenPicture'
import type { BoardView } from './useBoards'
import { askBoardShiftM, cancelMoved, sheetTopDeg, handSpeed, previewMoved, sessionMoved, STAY_MOVED, upTakesBoard, type BoardWhere, type Moved, type TakeLens } from './board'
import { visibilityTakes } from './botPress'
import { questionSheets } from './question'

/**
 * The clipboard a Bot of the row holds up: 32 cm wide, wider than the body (21.6 cm) so its sentence reads from the
 * seat, its lower edge `liftM` over the head (the eyes stay clear), leaning back a little as a thing held up does.
 * It stands `nearM` nearer than the body's middle and behind the body's front: the room's pages stand 30 cm behind
 * the row, so none hides it, while a page brought up for a Bot (1 cm before a Bot's front, `ASK_CLEAR_BOT_M`) stands
 * nearer and hides it, by depth, and its press is that page's. Over the heads it covers no Bot's body.
 */
const AT_BOT = { widthM: 0.32, liftM: 0.025, nearM: 0.02, leanRad: 0.12, pairM: 0.2, giveM: 0.04 } as const
/** The sheet a Bot holds up while he speaks to it (P2): at least this wide, and at most `mostM`, by its distance (below). */
const SPEAK_AT_BOT = { ...AT_BOT, widthM: 0.42, mostM: 1.2 } as const
/**
 * The sheet he speaks on is the thing he looks at: it is drawn in the speak band of `drawOrder.ts`, over all but the
 * dot, with no depth test, so Home's strip, another Bot's board or a nearer body never lies over it while he holds.
 * The others stay where they are.
 *
 * A desk line is drawn in front of the other Bots (D39, ruling 4): after the room's bodies and with no depth test, and
 * a press on it is its own before a Bot's (`DESK_FRONT`), so a press goes where the picture shows. Only while
 * no page, Home, card or other board stands nearer on the eye's line to it (`covered`): then it is drawn and pressed
 * as any board, by its distance.
 */
/**
 * A words board at a Bot this far from him or farther (at a desk of the room of places) holds ONE large line, its head
 * or its first line (the lead's 12:42, D35): a Bot far off cannot hold a readable page, and the whole sheet is what
 * he gets when he takes it. Past `refM` it grows with its distance, up to `mostScale`, so that line reads at the size
 * the title of the board of a Bot that came to him has.
 */
const AT_DESK = { fromM: 1.8, refM: 1.6, mostScale: 2.4 } as const
const deskScale = (d: number): number => Math.min(AT_DESK.mostScale, Math.max(1, d / AT_DESK.refM))
/** How wide a near desk's line is to his eye (D39, ruling 4): no desk line is wider (`deskPlace`). */
const NEAR_DESK_RAD = 2 * Math.atan2((AT_BOT.widthM * deskScale(AT_DESK.fromM)) / 2, AT_DESK.fromM)
/** How far the strip stands from him when no band is given (`places.ts` stands it about here). */
const FLOOR_STRIP_M = 1.71
/** A board at its Bot that is not at the front of a desk: in the room's band, by depth, over its Bot's body. */
const ROOM_SUB = 2
/**
 * At a seat: as wide as two seats of one row stand apart less a little (13.6 cm), with the Bot's name only, its lower
 * edge over the seat's press box (`Crew`: `DESK.botM * 1.4`), so a press on the seat stays the seat's.
 */
const AT_SEAT = { widthM: 0.13, overBoxM: 0.008, nearM: 0.01, leanRad: 0.12, stepM: 0.055, giveM: 0.02 } as const
const HELD = { widthM: 0.5, distanceM: 0.75, dropM: 0.12, floorM: 0.45 } as const
/**
 * In a session a board in his hand stands whole in the lens with his head at rest (D71): straight ahead in the room,
 * where the first page and Home stand, whichever way he looked to take it (up at a name, aside at a desk).
 * `dropM`: its middle so far under his eyes, so the tallest board (0.45 m) ends 19 degrees under level and under
 * Home's names. `asideM`: so far to the left, so the board and the stamp to its right are centred together.
 * A board taken while he holds one comes in front; each older one stands `fanM` to the left and `backM` behind the
 * one before it, so its sentence's start (who asks) is seen, and it has no stamp until it is in front again. Two
 * boards and the stamp, whose base stands out toward him, fill the lens's 0.89 m at that distance.
 * shortcut: a third held board's seen part is outside the lens (61 degrees wide); fan upward when three are held.
 */
const HELD_AHEAD = { dropM: 0.04, asideM: -0.05, fanM: 0.12, backM: 0.05 } as const
/**
 * A board taken where he faced aside (a desk) comes to the room's front when he faces the front again (D76 1): his
 * head within `frontDeg` of it, at rest for `restMs`, and the board not whole in the lens. It glides there in `glideMs`
 * and stays. It never moves while it is whole in the lens, while a pinch is down, or while he writes on it.
 * A page nearer than the board would cover it: the board stands `clearM` in front of that page, never nearer than
 * `nearestM` (D79 4). The palette on his hand is drawn and pressed over a board wherever each stands (`Palette.tsx`,
 * D81). A change of distance eases over `easeMs`.
 */
export const HELD_COME = { frontDeg: 15, restMs: 500, restDeg: 2, glideMs: 300, clearM: 0.1, nearestM: 0.45, easeMs: 150 } as const
/** Where a board in his hand stands, by the way he faced at the take (`yaw`), and what moves it from there. */
interface Ahead { /** Its place in the fan now, in places aside and behind, and when that was set. */ slot?: { side: number; back: number; t: number }; /** The frame of the fan it last stood behind in: the front board's yaw and eye. */ frame?: { yaw: number; eye: Vector3 }; eye: Vector3; n: number; takenAt: number; /** A sheet's top, in degrees over level (`sheetTopDeg`). */ topDeg: number; /** His lens at the take, for an ask's board (`askBoardShiftM`). */ lens: TakeLens; yaw: number; nearM: number; glide: { from: number; done: number } | null; ease: { from: number; to: number; done: number } | null }
/** The command's window: a resting hand's travel up the board scrolls this many times as far; a wheel notch, rows. */
const SCROLL = { restGain: 5, wheelRows: 3 } as const
/** The backing board's thickness, and the clip's, as shares of the board's width. */
const THICK = { boardM: 0.007, clipW: 0.29, clipH: 0.06, clipTop: 0.012 } as const

interface Props {
  boards: readonly BoardView[]
  /** Where each Bot's body stands now (`Crew`'s report). */
  crew: () => readonly CrewProbe[]
  head: Vector3
  inSession: boolean
  /** Reduced motion: a board that leaves fades where it is. */
  still: boolean
  onTake: (id: string) => boolean
  onStamp: (id: string, at: Uv) => void
  onRelease: (id: string, moved: Moved) => { particles: boolean }
  onPick: (id: string) => void
  /** The turned corner of a words board: its next sheet. */
  onCorner: (id: string) => void
  /** The mark on a words board's last sheet: the Bot's real page. */
  onPage: (id: string) => void
  /** The stamp pressed on a question sheet, on box `box` (`-1`: on no box). */
  onStampBox: (id: string, at: Uv, box: number) => void
  /** A pinch on the paper's foot: the line to write his note on opens, or closes. */
  onFoot: (id: string) => void
  /** The words of his note still heard, and whether the recognizer listens (`useComment`). */
  hearing?: { interim: string; listening: boolean }
  onAway: (id: string, inView: boolean) => void
  /** The boards at a Bot that a nearer page hides now (`useBoards`' `hide`). */
  onHidden?: (ids: ReadonlySet<string>) => void
  /** For the probe: where each board's body, stamp and page line stand in the room now. */
  report?: (read: (() => Record<string, BoardMarks>) | null) => void
  /** Home's band (the since line and the strip) as it stands now: no desk line reaches into it (D39, ruling 4). */
  band?: () => readonly FacingBox[]
}

/** Where a board's parts stand in the room, for the probe; `paper`: its paper's middle. */
export interface BoardMarks { body: [number, number, number]; stamp: [number, number, number] | null; page: [number, number, number] | null; mark: [number, number, number] | null; foot: [number, number, number] | null; paper: [number, number, number] | null; sizeM: [number, number]; scroll: { row: number; maxRow: number } | null; desk: { line: string; drawn: string; letterM: number; letter: number } | null; front: boolean; ndc: Record<'body' | 'stamp' | 'page' | 'mark' | 'foot' | 'paper', [number, number] | null>; corners: ([number, number] | null)[]; boxes: { at: [number, number, number]; ndc: [number, number] | null }[] }

/**
 * The page line at the paper's foot, as a press target that is never drawn. It acts only on the up of a down it saw
 * itself, by the same pointer, that did not leave it between: a drag that began elsewhere does nothing.
 */
function PageLine({ box, widthM, heightM, onPress, name = 'board-page' }: { box: UvBox; widthM: number; heightM: number; onPress: () => void; name?: string }) {
  const down = useRef<number | null>(null)
  const cx = ((box.u0 + box.u1) / 2 - 0.5) * widthM
  const cy = (0.5 - (box.v0 + box.v1) / 2) * heightM
  return (
    <mesh name={name} position={[cx, cy, 0.002]} scale={[(box.u1 - box.u0) * widthM, (box.v1 - box.v0) * heightM, 1]}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); down.current = e.pointerId }}
      onPointerLeave={(e: ThreeEvent<PointerEvent>) => { if (down.current === e.pointerId) down.current = null }}
      onPointerCancel={() => { down.current = null }}
      onPointerUp={(e: ThreeEvent<PointerEvent>) => {
        const mine = down.current === e.pointerId
        down.current = null
        if (mine) { e.stopPropagation(); onPress() }
      }}>
      <planeGeometry />
      <meshBasicMaterial visible={false} />
    </mesh>
  )
}

/**
 * Where a board stands in the room and which way it faces. At a Bot `at` is the middle of its lower edge and `seat`
 * says whether that Bot sits at the desk; held or on the floor `at` is its middle.
 */
interface Place { at: Vector3; yaw: number; seat?: boolean; scale?: number; /** A held sheet's top, in degrees over level. */ topDeg?: number; lens?: TakeLens; /** In his hand behind a board taken after it (`HELD_AHEAD`). */ behind?: boolean }

/**
 * In a session, how the press's hand was let go: as the pinch read it when its fingers opened (`pinchRoute.released`),
 * when that pinch is the press's own hand's; else from the press's own samples.
 */
function handMoved(p: { pinch: 'left' | 'right' | null; handM: number; hand: { at: [number, number, number]; t: number }[] }) {
  return sessionMoved({ hand: p.pinch, handM: p.handM, handMps: handSpeed(p.hand) }, pinchRoute.released)
}

/** In a session, where the pinching hand is now, kept for its speed at the release (its last 300 ms). */
function sampleHand(p: { hand: { at: [number, number, number]; t: number }[] }) {
  const own = pinchRoute.owner
  if (!own) return
  const t = performance.now()
  const [x, y, z] = pinchRoute.at[own.hand]
  p.hand.push({ at: [x, y, z], t })
  while (p.hand.length > 2 && t - p.hand[0].t > 300) p.hand.shift()
}

/** Whether a ray passes through a Bot's own body: within half its width of the line from its feet to its top. */
function rayOnBot(ray: Ray | undefined, bot: CrewProbe | undefined): boolean {
  if (!ray || !bot) return false
  const reach = bot.widthM / 2
  return ray.distanceSqToSegment(new Vector3(...bot.feet), new Vector3(...bot.top)) <= reach * reach
}

/**
 * Two neighbours' boards never cut each other's words. Along the row from left to right, two boards whose Bots stand
 * nearer than a board is wide are a pair: in the row both are drawn narrower (`AT_BOT.pairM`), so no board moves up
 * over a page; at the desk the second stands a step up (no page stands over the seats). A pair holds until its Bots
 * stand `giveM` farther apart than the width, so a stroll does not make a board jump. `was`: the pairs a frame ago.
 */
function boardPairs(boards: readonly BoardView[], crew: readonly CrewProbe[], head: Vector3, was: ReadonlySet<string>): { pairs: Set<string>; narrow: Set<string>; up: Set<string> } {
  const at = boards.flatMap(b => {
    const bot = b.where === 'bot' ? crew.find(c => c.slug === b.bot) : undefined
    return bot ? [{ id: b.needId, bot, side: Math.atan2(bot.feet[0] - head.x, head.z - bot.feet[2]) }] : []
  }).sort((p, q) => p.side - q.side)
  const pairs = new Set<string>()
  const narrow = new Set<string>()
  const up = new Set<string>()
  for (let i = 1; i < at.length; i += 1) {
    const [p, q] = [at[i - 1], at[i]]
    const size = q.bot.seat ? AT_SEAT : AT_BOT
    const key = `${p.id}|${q.id}`
    const apart = Math.hypot(q.bot.feet[0] - p.bot.feet[0], q.bot.feet[2] - p.bot.feet[2])
    if (apart >= size.widthM + (was.has(key) ? size.giveM : 0)) continue
    pairs.add(key)
    if (q.bot.seat) { if (!up.has(p.id)) up.add(q.id) } else { narrow.add(p.id); narrow.add(q.id) }
  }
  return { pairs, narrow, up }
}

/** What a board shows each frame to the leaving that may follow it. */
type Snap = { group: Group; canvas: HTMLCanvasElement; widthM: number; heightM: number }

/** One board, at its Bot, in his hand, or on the floor. */
function OneBoard({ b, props, kind, place, onThrown, register, covered, deskSeen }: { b: BoardView; props: Props; kind: BoardKind; place: (id: string, write?: boolean) => Place | null; onThrown: (dir: Vector3) => void; register: (s: Snap | null) => void; covered: MutableRefObject<ReadonlySet<string>>; deskSeen: MutableRefObject<Map<string, SeenBox>> }) {
  /** The first row the command's window shows, when the command is longer than the paper holds. */
  const [row, setRow] = useState(0)
  // In his hand a words board shows its sheet; at the Bot, the newest answer's first sentence.
  const wordsAt = b.kind === 'words' && kind === 'held' && b.sheets.length ? b.sheets[Math.min(b.sheet, b.sheets.length - 1)] : null
  // The choices a Bot's words offer him are boxes that the stamp marks; his print stays on the one he chose.
  const sheet = wordsAt ? { at: wordsAt, turns: b.sheets.length > 1, picked: b.picks[0] ?? [], prints: Object.entries(b.marks).map(([key, print]) => ({ label: key.slice(key.indexOf('|') + 1), print })) } : null
  const choosing = !!wordsAt?.choices?.length
  // In his hand a question board shows its sheet: the question and its boxes, with his prints on them.
  const qSheets = b.asked && kind === 'held' ? questionSheets(b.asked) : []
  const qAt = qSheets[Math.min(b.sheet, qSheets.length - 1)]
  const question = qAt ? { at: qAt, picked: b.picks[qAt.q] ?? [], prints: Object.entries(b.marks).flatMap(([key, print]) => { const label = key.slice(key.indexOf('|') + 1); return key.startsWith(`${qAt.q}|`) && qAt.options.includes(label) ? [{ label, print }] : [] }), turns: qSheets.length > 1 } : null
  // The sheet of a hold (P2) shows his words at the Bot, as he speaks.
  const speaking = b.kind === 'speak'
  const note = kind === 'held' || speaking ? { text: b.comment, interim: b.writing ? props.hearing?.interim ?? '' : '', writing: b.writing, listening: b.writing && !!props.hearing?.listening } : null
  // At its Bot a done sheet shows its head and its links too (P3); in his hand they are on its first sheet.
  const done = b.kind === 'words' && kind === 'bot' ? b.sheets[0]?.done ?? null : null
  // How far a words board at its Bot stands from him, by a tenth of a metre, once it is far enough to be at a desk.
  const [desk, setDesk] = useState<number | null>(null)
  const deskLine = desk === null ? null : { line: done ? done.short : b.text?.sentence ?? '', name: b.label, done: !!done, failed: !!done?.failed }
  const pic = useBoardPicture({ kind, sentence: b.text?.sentence ?? '', command: b.text?.command ?? '', name: b.label, print: question ? null : b.print, behind: b.behind, sheet, question, note, speak: speaking ? { listening: !b.writing || !!props.hearing?.listening } : null, done, desk: deskLine }, row)
  useFrame(() => {
    if (kind !== 'bot' || b.kind !== 'words') { if (desk !== null) setDesk(null); return }
    const d = place(b.needId)?.at.distanceTo(props.head) ?? 0
    const next = d >= AT_DESK.fromM ? Math.round(d * 10) / 10 : null
    if (next !== desk) setDesk(next)
  })
  const tag = useTagPicture(kind === 'held' ? 0 : b.behind ?? 0)
  const maxRow = pic.scroll?.maxRow ?? 0
  const group = useRef<Group>(null)
  /** The paper, the backing board and the clip: their draw order and depth test follow where the board stands. */
  const face = useRef<Mesh>(null)
  const back = useRef<Mesh>(null)
  const clip = useRef<Mesh>(null)
  const parts = [back, face, clip]
  /**
   * A drag on the words of a command's window: it scrolls, and is no throw. In the preview the press's own point on
   * the board moves the rows; with the dot, the resting hand's travel up the board does, `SCROLL.restGain` times.
   */
  const scrolling = useRef<{ id: number; fromV: number; fromRow: number; hand: Vector3 | null } | null>(null)
  const scrollTo = (r: number) => setRow(Math.max(0, Math.min(maxRow, Math.round(r))))
  /** Where on the picture (`v`, down) a press's ray falls on the board's plane. */
  const vOf = (e: ThreeEvent<PointerEvent>): number | null => {
    const ray = pressRay(e)
    const g = group.current
    if (!ray || !g) return null
    g.updateWorldMatrix(true, false)
    const n = new Vector3(0, 0, 1).transformDirection(g.matrixWorld)
    const hit = ray.intersectPlane(new Plane().setFromNormalAndCoplanarPoint(n, g.getWorldPosition(new Vector3())), new Vector3())
    return hit ? 0.5 - g.worldToLocal(hit).y / (height * (g.scale.y || 1)) : null
  }
  const handNow = () => { const own = pinchRoute.owner; return own ? new Vector3(...pinchRoute.at[own.hand]) : null }
  useFrame(() => {
    const s = scrolling.current
    const g = group.current
    if (!s?.hand || !g || !pic.scroll) return
    const now = handNow()
    if (!now) return
    const up = new Vector3(0, 1, 0).transformDirection(g.matrixWorld)
    // The hand going up moves the words up: the window shows later rows.
    const rows = (now.sub(s.hand).dot(up) * SCROLL.restGain) / (height * pic.scroll.rowV)
    scrollTo(s.fromRow + rows)
  })
  /** The press on a held board: its down, its moves (the preview's), and the pinching hand's places (a session's). */
  const press = useRef<{ id: number; x: number; y: number; t: number; from: Vector3; at: Vector3; handM: number; pinch: 'left' | 'right' | null; trail: { x: number; y: number; t: number }[]; hand: { at: [number, number, number]; t: number }[] } | null>(null)
  /** The downs on it while it stands at its Bot or on the floor, by pointer: where it stood at each (`upTakesBoard`). */
  const downs = useRef(new Map<number, BoardWhere>())
  /** Each down's place in the order the board heard them, so a let-go keeps a down heard after the release it follows. */
  const downOrder = useRef({ n: 0, of: new Map<number, number>() })
  const inSessionNow = useRef(props.inSession)
  inSessionNow.current = props.inSession
  // A down whose up came to something else is let go: in a session once no pinch owns the button (as a tray's,
  // `downLetGo`). In the flat preview, after the window's own release, but only once the board's own up has had its
  // turn (REVIEW-73 M242): the window hears the release first, and the room's pointer library hands it to the board
  // at the next frame (`forward.js` `update`, run by the room's loop before it draws, `@react-three/xr` `events.js`).
  // A callback asked for now runs after that loop in the next frame, since the loop asks for its next frame first
  // thing (`@react-three/fiber` `loop`: `frame = requestAnimationFrame(loop)`, then `flushGlobalEffects('before')`).
  // When the canvas does not batch, the library hands the up on at once, before the window hears it.
  useFrame(() => { if (props.inSession && downs.current.size > 0 && pinchRoute.owner === null) downs.current.clear() })
  useEffect(() => {
    const later = () => {
      if (inSessionNow.current) return
      const heard = downOrder.current.n
      requestAnimationFrame(() => {
        for (const [id, at] of downOrder.current.of) if (at <= heard) { downs.current.delete(id); downOrder.current.of.delete(id) }
      })
    }
    window.addEventListener('pointerup', later)
    window.addEventListener('pointercancel', later)
    return () => { window.removeEventListener('pointerup', later); window.removeEventListener('pointercancel', later) }
  }, [])
  // A session that stops being visible, ends or begins ends a press on it as a stay (D192 2): in his hand it stays, and
  // nothing is sent, thrown or handed back. The store is heard as the library hears it (`Crew.tsx`, M221).
  const xrStore = useXRStore()
  const releaseNow = useRef(props.onRelease)
  releaseNow.current = props.onRelease
  const needNow = useRef(b.needId)
  needNow.current = b.needId
  useEffect(() => xrStore.subscribe((state, prev) => {
    if (state.visibilityState === prev.visibilityState || !visibilityTakes(prev.visibilityState, state.visibilityState)) return
    const p = press.current
    press.current = null
    scrolling.current = null
    downs.current.clear()
    if (p) releaseNow.current(needNow.current, STAY_MOVED)
  }), [xrStore])
  const size = kind === 'held' ? HELD : kind === 'seat' ? AT_SEAT : speaking ? SPEAK_AT_BOT : AT_BOT
  // The sheet he speaks on reads as large as a sheet in his hand: as wide, for how far it stands, as the held board is
  // at its own distance, so its words are a sheet's body words to his eye.
  const far = speaking ? place(b.needId, false)?.at.distanceTo(props.head) ?? null : null
  // A words board at a desk far off holds one large line, wider by its distance (`AT_DESK`).
  const width = far !== null ? Math.min(SPEAK_AT_BOT.mostM, Math.max(SPEAK_AT_BOT.widthM, (HELD.widthM * far) / HELD.distanceM)) : desk !== null ? AT_BOT.widthM * deskScale(desk) : size.widthM
  const height = width * pic.aspect
  const fitAt = useMemo(() => new Vector3(), [])
  const fitDir = useMemo(() => new Vector3(), [])
  /** Where the sheet of this hold stands: its lower edge's middle in the room, its scale and its turn, set once. */
  const spoke = useRef<{ low: Vector3; scale: number; yaw: number } | null>(null)
  useEffect(() => {
    if (group.current) register({ group: group.current, canvas: pic.canvas, widthM: width, heightM: height })
  }, [register, pic, width, height])
  useEffect(() => () => { deskSeen.current.delete(b.needId) }, [deskSeen, b.needId])
  // Each frame: at its Bot it is held up over that Bot's head; held or on the floor, it stands where it was put.
  useFrame(state => {
    const g = group.current
    const p = place(b.needId)
    if (!g || !p) return
    if (press.current && b.where === 'held') return
    // A desk line keeps its lower edge where its Bot holds it and grows smaller to stay out of Home's band (D39).
    const atDesk = desk !== null && kind === 'bot' && !speaking
    const headAt: [number, number, number] = [props.head.x, props.head.y, props.head.z]
    const bands = atDesk ? props.band?.() ?? [] : []
    // The strip's small words, as high to his eye as they stand from him: the floor of a desk line (the lead's 17:46).
    const floorRad = STRIP_LABEL_M / (bands[0] ? Math.hypot(bands[0].center[0] - headAt[0], bands[0].center[2] - headAt[2]) : FLOOR_STRIP_M)
    const scale0 = p.scale ?? 1
    const fit = atDesk ? deskPlace(headAt, [p.at.x, p.at.y, p.at.z], { widthM: width * scale0, heightM: height * scale0, letterM: pic.deskLetter * width * scale0 }, NEAR_DESK_RAD, floorRad, bands) : { share: 1, side: 0 as const }
    const k = scale0 * fit.share
    g.position.copy(p.at)
    if (kind !== 'held') g.position.y += (height * k) / 2
    // A Bot's sheet in his hand hangs from its top line (`sheetTopDeg`): short, its lower edge is in the lens with
    // his head still up at the Bot or the name he pinched (D82 1), and its heading with his head down at a low Bot.
    // An ask's board is whole in the lens at the take, both edges, and under Home's names (D92 2).
    if (kind === 'held' && props.inSession && b.kind === 'ask' && p.lens) g.position.y += askBoardShiftM(height, HELD.distanceM, HELD_AHEAD.dropM, p.lens) * k
    if (kind === 'held' && props.inSession && b.kind === 'words' && p.topDeg !== undefined) g.position.y += (Math.tan((p.topDeg * Math.PI) / 180) * HELD.distanceM + HELD_AHEAD.dropM - height / 2) * k
    if (fit.side !== 0) {
      // Beside its Bot, at head height, on the side away from Home's band, and on no other desk line.
      const dx = p.at.x - headAt[0]
      const dz = p.at.z - headAt[2]
      const flat = Math.hypot(dx, dz) || 1
      const [rx, rz] = [-dz / flat, dx / flat]
      g.position.y = p.at.y - AT_BOT.liftM - DESK_FIT.headDropM
      const out = DESK_FIT.besideM + (width * k) / 2
      g.position.x += rx * out * fit.side
      g.position.z += rz * out * fit.side
      const mine = span(headAt, { center: [g.position.x, g.position.y, g.position.z], size: [width * k, height * k] })
      // On no other desk line, and not on Home's band either (a Bot dragged up behind the strip: REVIEW-15, small).
      const others = [...[...deskSeen.current].flatMap(([id, box]) => (id === b.needId ? [] : [box])), ...bands.map(x => span(headAt, x))]
      const more = Math.tan(besideClear(mine, fit.side, others)) * flat
      if (besideOrUnder(more) === 'beside') {
        g.position.x += rx * more * fit.side
        g.position.z += rz * more * fit.side
      } else {
        // Too far out to be told as its Bot's: under its Bot, at its desk's height, still drawn in front.
        const bot = props.crew().find(c => c.slug === b.bot)
        g.position.set(...underAt([p.at.x, p.at.y, p.at.z], bot && { feet: bot.feet, deskY: bot.desk?.at[1] }))
        // Under its Bot it lies on no other desk line either: the same clear step, toward the same side.
        const below = span(headAt, { center: [g.position.x, g.position.y, g.position.z], size: [width * k, height * k] })
        const step = Math.tan(besideClear(below, fit.side, others)) * flat
        g.position.x += rx * step * fit.side
        g.position.z += rz * step * fit.side
      }
    }
    if (atDesk) deskSeen.current.set(b.needId, span(headAt, { center: [g.position.x, g.position.y, g.position.z], size: [width * k, height * k] }))
    else deskSeen.current.delete(b.needId)
    const front = desk !== null && kind === 'bot' && !speaking && !covered.current.has(b.needId)
    g.userData.front = front
    // One order, drawn and pressed the same (M37): a sheet he speaks on, a held board with its stamp, else the room's.
    const layer = speaking ? 'speak' : kind === 'held' ? 'held' : 'bot'
    // A desk line in front is drawn over the other Bots: no depth test for it, else its layer's (`DEPTH`).
    const depth = !front && DEPTH[layer]
    parts.forEach((part, i) => {
      const m = part.current
      if (!m) return
      // The backing board, then the paper, then the clip: with no depth test the order alone keeps the paper in front.
      ownOrder(m, (front ? DESK_FRONT : speaking ? 0 : ROOM_SUB) + (speaking ? i : i / 100))
      const mat = m.material as MeshBasicMaterial
      if (mat.depthTest !== depth) { mat.depthTest = depth; mat.needsUpdate = true }
    })
    layIn(g, layer)
    ;(g as Group & { pointerEventsOrder?: number }).pointerEventsOrder = layer === 'bot' ? undefined : PRESS[layer]
    if (face.current) (face.current as Mesh & { pointerEventsOrder?: number }).pointerEventsOrder = front ? DESK_FRONT : undefined
    const lean = 'leanRad' in size ? -size.leanRad : 0
    g.rotation.set(b.where === 'floor' ? -Math.PI / 2 : lean, p.yaw, 0, 'YXZ')
    g.scale.setScalar(k)
    if (!speaking) return
    // The sheet he speaks on belongs to its Bot. It is placed once, when the hold begins: whole inside his view then
    // (`speakFit`, read in the frame of the eye that draws it), its lower edge where the Bot holds it up, facing him
    // from the Bot's place as its clipboard does. It stays there for the hold: when he turns his head it stays with
    // its Bot, and as his words make it taller it grows upward, never down over the Bot's face.
    let at = spoke.current
    if (!at) {
      const eye = state.gl.xr.isPresenting ? state.gl.xr.getCamera() : state.camera
      eye.updateMatrixWorld()
      const low = fitAt.copy(p.at).applyMatrix4(eye.matrixWorldInverse)
      const e = eye.projectionMatrix.elements
      const [tanX, d] = [1 / e[0], -low.z]
      const fit = speakFit({ low: [low.x, low.y], d, tanX, tanY: 1 / e[5], w: width * k, h: height * k, leastW: SPEAK_AT_BOT.widthM })
      at = { low: new Vector3(fit.x, low.y, low.z).applyMatrix4(eye.matrixWorld), scale: fit.scale, yaw: p.yaw }
      // It faces him from its Bot, so at a side of the view its near edge reaches farther than the fit's: it is
      // moved in by what a corner is past the edge, as the eye draws it.
      for (let pass = 0; pass < 3; pass += 1) {
        g.position.copy(at.low)
        g.position.y += (height * k * at.scale) / 2
        g.rotation.set(lean, at.yaw, 0, 'YXZ')
        g.scale.setScalar(k * at.scale)
        g.updateMatrixWorld()
        let [over, under] = [0, 0]
        for (const sx of [-1, 1]) for (const sy of [-1, 1]) {
          const c = fitDir.set((sx * width) / 2, (sy * height) / 2, 0).applyMatrix4(g.matrixWorld).applyMatrix4(eye.matrixWorldInverse)
          const t = c.x / -c.z
          over = Math.max(over, t - tanX * SPEAK_EDGE)
          under = Math.max(under, -tanX * SPEAK_EDGE - t)
        }
        if ((over <= 0 && under <= 0) || (over > 0 && under > 0)) break
        const side = fitDir.set(1, 0, 0).transformDirection(eye.matrixWorld)
        at.low.addScaledVector(side, (under - over) * d * 1.02)
      }
      spoke.current = at
    }
    g.position.copy(at.low)
    g.position.y += (height * k * at.scale) / 2
    g.rotation.set(lean, at.yaw, 0, 'YXZ')
    g.scale.setScalar(k * at.scale)
  })
  return (
    // `xrHeld`: a held board is known by being held, wherever it stands (M39, `frameFirst.ts`).
    <group ref={group} userData={{ board: b.needId, xrPress: 'board', xrHeld: kind === 'held', scroll: pic.scroll ? { row, maxRow } : null, boxes: pic.boxes, desk: deskLine ? { line: deskWords(deskLine.name, deskLine.line), drawn: pic.deskDrawn, letterM: pic.deskLetter * width, letter: pic.deskLetter } : null }}>
      <mesh
        ref={face}
        name="board-paper"
        scale={[width, height, 1]}
       
        onPointerDown={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          if (b.where !== 'held') { downs.current.set(e.pointerId, b.where); downOrder.current.of.set(e.pointerId, (downOrder.current.n += 1)); return }
          ;(e.object as Object3D).setPointerCapture(e.pointerId)
          const box = pic.scroll?.box
          const uv = e.uv
          if (box && uv && uv.x >= box.u0 && uv.x <= box.u1 && 1 - uv.y >= box.v0 && 1 - uv.y <= box.v1) {
            const dot = pinchRoute.owner?.to === 'dot'
            scrolling.current = { id: e.pointerId, fromV: 1 - uv.y, fromRow: row, hand: dot ? handNow() : null }
            return
          }
          const at = group.current?.position.clone() ?? new Vector3()
          press.current = { id: e.pointerId, x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, t: e.nativeEvent.timeStamp, from: e.point.clone(), at, handM: 0, pinch: pinchRoute.owner?.hand ?? null, trail: [], hand: [] }
          sampleHand(press.current)
        }}
        onPointerMove={(e: ThreeEvent<PointerEvent>) => {
          const s = scrolling.current
          if (s && s.id === e.pointerId) {
            e.stopPropagation()
            const v = s.hand ? null : vOf(e)
            // The words follow the press: dragged up, the window shows later rows.
            if (v !== null && pic.scroll) scrollTo(s.fromRow + (s.fromV - v) / pic.scroll.rowV)
            return
          }
          const p = press.current
          if (!p || p.id !== e.pointerId) return
          e.stopPropagation()
          // The board follows the press, so a throw is seen as it is made.
          group.current?.position.copy(p.at).add(e.point.clone().sub(p.from))
          p.handM = Math.max(p.handM, pinchRoute.travel())
          p.trail.push({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, t: e.nativeEvent.timeStamp })
          if (p.trail.length > 24) p.trail.shift()
          sampleHand(p)
        }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          if (scrolling.current?.id === e.pointerId) { scrolling.current = null; return }
          // Only the up of a down that began on it, where it stands (DASH-16 D): an up that came from a tray, the room or
          // another board takes nothing.
          if (b.where === 'bot' || b.where === 'floor') {
            const takes = upTakesBoard(downs.current, e.pointerId, b.where)
            downs.current.delete(e.pointerId); downOrder.current.of.delete(e.pointerId)
            if (takes) (b.where === 'bot' ? props.onTake : props.onPick)(b.needId)
            return
          }
          const p = press.current
          press.current = null
          if (!p || p.id !== e.pointerId) return
          sampleHand(p)
          // Each point's time is its event's own: a slow frame delays when the release is handled, and the time of
          // handling would read a throw as a stop.
          const moved = previewMoved({ x: p.x, y: p.y, t: p.t }, p.trail, { x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, t: e.nativeEvent.timeStamp })
          const onBot = rayOnBot(pressRay(e) ?? undefined, props.crew().find(c => c.slug === b.bot))
          // The throw's way: where the board went under the press.
          const dir = (group.current?.position.clone() ?? p.at).sub(p.at)
          const out = props.onRelease(b.needId, props.inSession ? { ...handMoved(p), onBot } : { ...moved, onBot })
          if (out.particles) onThrown(dir.lengthSq() > 1e-6 ? dir.normalize() : new Vector3(1, 0, 0))
        }}
        onPointerCancel={(e: ThreeEvent<PointerEvent>) => {
          if (scrolling.current?.id === e.pointerId) { scrolling.current = null; return }
          // A pinch held still ends as a cancel in a session: it moved too little, so the board stays in his hand. Only
          // his own let-go acts (`pinchRoute.ending` is set then); any other cancel ends as a stay and takes nothing.
          const letGo = pinchRoute.ending !== null
          const p = press.current
          press.current = null
          const mine = upTakesBoard(downs.current, e.pointerId, b.where)
          downs.current.delete(e.pointerId); downOrder.current.of.delete(e.pointerId)
          if (p && b.where === 'held') props.onRelease(b.needId, cancelMoved(letGo, props.inSession ? handMoved(p) : { px: 0 }))
          else if (letGo && mine && e.pointerType && b.where === 'bot') props.onTake(b.needId)
        }}
        onWheel={(e: ThreeEvent<WheelEvent>) => {
          // In the preview the wheel over a held board's window scrolls its words, a few rows a notch.
          if (b.where !== 'held' || !pic.scroll) return
          e.stopPropagation()
          scrollTo(row + Math.sign(e.nativeEvent.deltaY) * SCROLL.wheelRows)
        }}
      >
        <planeGeometry />
        {/* Opaque: the page behind never shows through its paper. The rounded corners are cut, not blended. */}
        <meshBasicMaterial map={pic.texture} alphaTest={0.5} side={DoubleSide} toneMapped={false} transparent={speaking} depthTest={!speaking} depthWrite={!speaking} />
      </mesh>
      {/* The backing board's thickness, a little inside its rounded corners, behind the face. */}
      <mesh ref={back} position={[0, 0, -THICK.boardM / 2 - 0.0005]} scale={[width * 0.965, height * 0.965, THICK.boardM]} pointerEvents="none">
        <boxGeometry />
        <meshBasicMaterial color={CLIPBOARD.board} toneMapped={false} transparent={speaking} depthTest={!speaking} depthWrite={!speaking} />
      </mesh>
      {/* The metal clip stands off the paper at the top middle. */}
      <mesh ref={clip} position={[0, height / 2 - (THICK.clipTop + THICK.clipH / 2) * width, 0.003]} scale={[THICK.clipW * width, THICK.clipH * width, 0.006]} pointerEvents="none">
        <boxGeometry />
        <meshBasicMaterial color={CLIPBOARD.clip} toneMapped={false} transparent={speaking} depthTest={!speaking} depthWrite={!speaking} />
      </mesh>
      {/* At a Bot, the tag of the asks behind it hangs beside the clip, in front of the paper and under the clip. */}
      {tag && (
        <mesh name="board-tag" position={[TAG_AT_BOT.x * width, height / 2 + TAG_AT_BOT.y * width, 0.0025]} rotation={[0, 0, TAG_AT_BOT.turn]}
          scale={[TAG_AT_BOT.w * width, TAG_AT_BOT.h * width, 1]} pointerEvents="none">
          <planeGeometry />
          <meshBasicMaterial map={tag} transparent toneMapped={false} />
        </mesh>
      )}
      {b.where === 'held' && !b.stamped && (b.kind === 'ask' || choosing) && !place(b.needId, false)?.behind && (
        <BoardStamp board={() => group.current} widthM={width} heightM={height} paperRightM={(pic.paper.u1 - 0.5) * width} faceM={[PRINT_FACE.u * width, PRINT_FACE.u * PRINT_FACE.ratio * width]}
          onPress={at => {
            const on = printPlace(at, pic.paper, pic.aspect)
            if (!on) return
            // On a question sheet the press is on one of its boxes, or on none.
            if (question || choosing) props.onStampBox(b.needId, on, pic.boxes.findIndex(x => at.u >= x.u0 && at.u <= x.u1 && at.v >= x.v0 && at.v <= x.v1))
            else props.onStamp(b.needId, on)
          }} />
      )}
      {b.where === 'held' && pic.noteLine && <PageLine name="board-foot" box={pic.noteLine} widthM={width} heightM={height} onPress={() => props.onFoot(b.needId)} />}
      {b.where === 'held' && pic.pageLine && <PageLine box={pic.pageLine} widthM={width} heightM={height} onPress={() => props.onCorner(b.needId)} />}
      {b.where === 'held' && pic.pageMark && <PageLine name="board-mark" box={pic.pageMark} widthM={width} heightM={height} onPress={() => props.onPage(b.needId)} />}
    </group>
  )
}

/**
 * Whether a hit on the eye's line to a desk line is something that stands over it in the picture: a page, Home or its
 * since line, the card, or another board. A Bot's body, its sign, a desk and the room do not: the line is in front of them.
 */
function coversDesk(o: Object3D, own: Object3D): boolean {
  for (let x: Object3D | null = o; x; x = x.parent) {
    if (x === own) return false
    const press = x.userData.xrPress
    if (typeof x.userData.xrPage === 'string' || typeof x.userData.xrPanel === 'string') return true
    if (press === 'bot' || press === 'sign' || press === 'desk') return false
    if (press !== undefined) return true
  }
  return false
}

export function Boards(props: Props) {
  const camera = useThree(s => s.camera)
  const scene = useThree(s => s.scene)
  useDustWarm(props.inSession)
  useStampWarm(props.inSession)
  /** Where a board went when it was taken, and where it lies on the floor: in the room, by board. */
  const heldAt = useRef(new Map<string, { at: Vector3; yaw: number; ahead?: Ahead }>())
  /** Counts the takes of a session, so the held boards are known newest first. */
  const takes = useRef(0)
  /** Where his head rests, and since when; the last frame's time; when the pages in front were last looked for. */
  const restAt = useRef({ dir: new Vector3(0, 0, -1), since: 0 })
  const lastFrame = useRef(0)
  const nearAt = useRef(0)
  const box = useMemo(() => new Box3(), [])
  const root = useRef<Group>(null)
  const report = props.report
  /** Each board's last frame, for the leaving that may follow it; and the throws' ways, by board. */
  const snaps = useRef(new Map<string, Snap & { matrix: Matrix4 }>())
  const thrown = useRef(new Map<string, Vector3>())
  const [leaving, setLeaving] = useState<{ key: string; from: DustFrom; dir: Vector3 | null }[]>([])
  const before = useRef<ReadonlySet<string>>(new Set())
  // A board that is gone from the list: a throw leaves in pieces (D4a); any other end is lowered (`BoardLower`).
  useEffect(() => {
    const ids = new Set(props.boards.map(b => b.needId))
    const out: { key: string; from: DustFrom; dir: Vector3 | null }[] = []
    for (const id of before.current) {
      if (ids.has(id)) continue
      const s = snaps.current.get(id)
      if (s) out.push({ key: `${id}:${performance.now()}`, from: { matrix: s.matrix.clone(), widthM: s.widthM, heightM: s.heightM, canvas: s.canvas }, dir: thrown.current.get(id) ?? null })
      snaps.current.delete(id)
      thrown.current.delete(id)
    }
    before.current = ids
    for (const l of out) boardLeaves[l.dir ? 'dust' : 'lowered'] += 1
    if (out.length) setLeaving(list => [...list, ...out])
  }, [props.boards])
  useEffect(() => {
    if (!report) return
    ;(window as unknown as { __xrBoardPaints?: typeof boardPaints }).__xrBoardPaints = boardPaints
    ;(window as unknown as { __xrBoardLeaves?: typeof boardLeaves }).__xrBoardLeaves = boardLeaves
    report(() => {
      const out: Record<string, BoardMarks> = {}
      const v = new Vector3()
      const at = (o: Object3D | undefined): [number, number, number] | null => (o ? (o.getWorldPosition(v), [v.x, v.y, v.z]) : null)
      for (const g of root.current?.children ?? []) {
        const id = g.userData.board as string | undefined
        if (!id) continue
        const body = at(g) as [number, number, number]
        const face = g.children[0]
        const size = face ? face.getWorldScale(new Vector3()) : new Vector3()
        const ndc = (p: [number, number, number] | null): [number, number] | null => (p ? (v.set(...p).project(camera), [v.x, v.y]) : null)
        const stamp = at(g.getObjectByName('board-stamp'))
        const page = at(g.getObjectByName('board-page'))
        const mark = at(g.getObjectByName('board-mark'))
        const foot = at(g.getObjectByName('board-foot'))
        // A question sheet's boxes: their middles, from the picture's own fractions.
        const boxes = ((g.userData.boxes as UvBox[] | undefined) ?? []).map(x => { const p = g.localToWorld(new Vector3(((x.u0 + x.u1) / 2 - 0.5) * size.x / (g.scale.x || 1), (0.5 - (x.v0 + x.v1) / 2) * size.y / (g.scale.y || 1), 0)); const a: [number, number, number] = [p.x, p.y, p.z]; return { at: a, ndc: ndc(a) } })
        // The paper's middle: a little under the board's (the clip takes its top).
        const paper = body ? (g.localToWorld(v.set(0, -size.y * 0.06 / (g.scale.y || 1), 0)), [v.x, v.y, v.z] as [number, number, number]) : null
        // Its four corners as the camera draws them: lower left, lower right, upper right, upper left.
        const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => { const c = g.localToWorld(new Vector3((sx * size.x) / 2 / (g.scale.x || 1), (sy * size.y) / 2 / (g.scale.y || 1), 0)); return ndc([c.x, c.y, c.z]) })
        out[id] = { body, stamp, page, mark, foot, paper, corners, sizeM: [size.x, size.y], scroll: (g.userData.scroll as BoardMarks['scroll']) ?? null, desk: (g.userData.desk as BoardMarks['desk']) ?? null, front: g.userData.front === true, ndc: { body: ndc(body), stamp: ndc(stamp), page: ndc(page), mark: ndc(mark), foot: ndc(foot), paper: ndc(paper) }, boxes }
      }
      return out
    })
    return () => report(null)
  }, [report, camera])
  const seen = useRef(new Map<string, number>())
  const fr = useMemo(() => ({ frustum: new Frustum(), m: new Matrix4(), dir: new Vector3() }), [])
  /** Which boards at a Bot stand as a pair now (`boardPairs`), kept from frame to frame for its give. */
  const pairs = useRef<ReturnType<typeof boardPairs>>({ pairs: new Set(), narrow: new Set(), up: new Set() })
  const caster = useMemo(() => new Raycaster(), [])
  const hiddenAt = useRef(0)
  /** The take (or hand back) of a dashboard that the boards' cover was last worked out for (`takenPicture.seq`). */
  const takenSeen = useRef(takenPicture.seq)
  /** The boards at a Bot that something other than a Bot covers, as last found: a desk line among them is drawn and pressed by its distance. */
  const coveredNow = useRef<ReadonlySet<string>>(new Set())
  /** Each desk line as he sees it, last frame: a line beside its Bot lies on none of them. */
  const deskSeen = useRef(new Map<string, SeenBox>())
  const place = (id: string, write = true): Place | null => {
    const b = props.boards.find(x => x.needId === id)
    if (!b) return null
    if (b.where === 'bot') {
      if (write) heldAt.current.delete(id)
      const bot = props.crew().find(c => c.slug === b.bot)
      if (!bot) return null
      const [x, y, z] = bot.feet
      const yaw = Math.atan2(props.head.x - x, props.head.z - z)
      const p = pairs.current
      // Held up: its lower edge over the head (in the row) or over the seat's press box (at the desk), a little nearer.
      const near = bot.seat ? AT_SEAT.nearM : AT_BOT.nearM
      // A step up is a board's height and a little: the one below stays whole under it.
      const up = p.up.has(id) ? AT_SEAT.stepM : 0
      const bottom = up + (bot.seat ? y + DESK.botM * 1.4 + AT_SEAT.overBoxM : bot.top[1] + AT_BOT.liftM)
      // A board never shows before its Bot: while the body shrinks away or grows at a new spot, the board has its share.
      return { at: new Vector3(x + Math.sin(yaw) * near, bottom, z + Math.cos(yaw) * near), yaw, seat: bot.seat, scale: (p.narrow.has(id) ? AT_BOT.pairM / AT_BOT.widthM : 1) * Math.max(0.001, bot.grow ?? 1) }
    }
    let h = heldAt.current.get(id)
    // The render only asks whether a board stands behind another: it takes no place and writes nothing (W4).
    if (!h && !write) return null
    if (!h) {
      if (props.inSession) {
        // Taken now, in a session: where he FACES (his head's yaw, level), whole in the lens there (`HELD_AHEAD`, D76).
        camera.getWorldDirection(fr.dir)
        takes.current += 1
        const eyeNow = camera.getWorldPosition(new Vector3())
        const pitchDeg = (Math.asin(Math.max(-1, Math.min(1, fr.dir.y))) * 180) / Math.PI
        const halfDeg = (Math.atan(1 / camera.projectionMatrix.elements[5]) * 180) / Math.PI
        // Home's line alone: the lens's top is no bound for it here.
        const lineDeg = sheetTopDeg(eyeNow, 1000, 0, homePlace.strip)
        h = { at: new Vector3(), yaw: 0, ahead: { eye: eyeNow, n: takes.current, takenAt: performance.now(), topDeg: sheetTopDeg(eyeNow, pitchDeg, halfDeg, homePlace.strip), lens: { pitchDeg, halfDeg, lineDeg }, yaw: Math.atan2(-fr.dir.x, -fr.dir.z), nearM: HELD.distanceM, glide: null, ease: null } }
      } else {
        // Taken now: in front of where he looks, a little lower, facing him, and it stays there in the room.
        camera.getWorldDirection(fr.dir)
        const yaw = Math.atan2(-fr.dir.x, -fr.dir.z)
        const at = camera.getWorldPosition(new Vector3()).addScaledVector(new Vector3(fr.dir.x, 0, fr.dir.z).normalize(), HELD.distanceM)
        at.y -= HELD.dropM
        h = { at, yaw }
      }
      heldAt.current.set(id, h)
    }
    if (b.where === 'floor') return { at: h.at.clone().setY(h.at.y - HELD.floorM), yaw: h.yaw }
    if (!h.ahead) return h
    // How many boards in his hand were taken after this one: it stands so many places behind. A board on the floor
    // is in no fan (W3).
    const own = h.ahead
    const out = props.boards.filter(x => x.where === 'held' && heldAt.current.get(x.needId)?.ahead)
    const rank = out.filter(x => (heldAt.current.get(x.needId)?.ahead?.n ?? 0) > own.n).length
    if (!write) return { at: h.at, yaw: h.yaw, behind: rank > 0, topDeg: own.topDeg, lens: own.lens }
    // The fan as a whole is centred: the front board stands half the fan's width to the right of its own place. All
    // of it is laid in the frame of the way he faced at the take, at the distance that keeps it in front of the pages
    // (`nearM`): nearer, it is smaller by the same share, so it is as big on the lens (D76 3).
    const k = own.nearM / HELD.distanceM
    // Its place in the fan, in places aside and behind. A board steps back at once; it comes forward, or to the
    // middle, in `glideMs`, so the board behind glides to the front when the front one goes (D98 8).
    const now = performance.now()
    const to = { side: (out.length - 1) / 2 - rank, back: rank }
    const was = own.slot
    if (!was || to.back > was.back) own.slot = { ...to, t: now }
    else {
      // One straight way: aside and forward by the same share, a whole place in `glideMs`. It waits while a pinch
      // is down, as the glide to the room's front does (N12): nothing moves under his fingers.
      const left = Math.max(Math.abs(to.side - was.side), Math.abs(to.back - was.back))
      const share = pinchRoute.owner !== null ? 0 : left > 0 ? Math.min(1, (now - was.t) / HELD_COME.glideMs / left) : 1
      own.slot = { side: was.side + (to.side - was.side) * share, back: was.back + (to.back - was.back) * share, t: now }
    }
    const aside = (HELD_AHEAD.asideM + own.slot.side * HELD_AHEAD.fanM) * k
    const ahead = (HELD.distanceM + own.slot.back * HELD_AHEAD.backM) * k
    // The fan is one thing: a board behind stands in the frame of the board in front (the newest), wherever each was
    // taken, so its left part, where its heading begins with its Bot's name, shows beside the front one (D92 3).
    // When the front one goes, the board behind stays in that frame and comes to the front in it: it does not hop to
    // where it was taken first (D98 8).
    // Once it rests in front it is its own again (M65): the frame it came forward in is now its own yaw and eye, so
    // the glide to the room's front (D76 1), which turns `own.yaw`, moves it.
    if (rank === 0 && own.frame && own.slot.side === to.side && own.slot.back === to.back) { own.yaw = own.frame.yaw; own.eye = own.frame.eye; own.frame = undefined }
    const lead = rank > 0 ? out.map(x => heldAt.current.get(x.needId)?.ahead).filter((a): a is Ahead => !!a).sort((a, b) => b.n - a.n)[0] ?? own : own.frame ?? own
    if (rank > 0) own.frame = { yaw: lead.yaw, eye: lead.eye }
    const [s, c] = [Math.sin(lead.yaw), Math.cos(lead.yaw)]
    h.at.set(lead.eye.x + aside * c - ahead * s, lead.eye.y - HELD_AHEAD.dropM * k, lead.eye.z - aside * s - ahead * c)
    h.yaw = lead.yaw
    return { at: h.at, yaw: h.yaw, behind: rank > 0, scale: k, topDeg: own.topDeg, lens: own.lens }
  }
  // A held board that stood out of his view for a minute goes back (`awayTick`): looked at twice a second.
  useFrame(() => {
    const t = performance.now()
    for (const [id, s] of snaps.current) { s.group.updateWorldMatrix(true, false); s.matrix.copy(s.group.matrixWorld); snaps.current.set(id, s) }
    pairs.current = boardPairs(props.boards, props.crew(), props.head, pairs.current.pairs)
    // Four times a second: which boards at a Bot a nearer page hides, along the eye's line to the board's middle.
    // And at once when a dashboard is taken or handed back: the form counts from the take (M95).
    if (props.onHidden && (t - hiddenAt.current >= 250 || takenSeen.current !== takenPicture.seq)) {
      hiddenAt.current = t
      takenSeen.current = takenPicture.seq
      const eye = camera.getWorldPosition(new Vector3())
      const hidden = new Set<string>()
      const covered = new Set<string>()
      for (const g of root.current?.children ?? []) {
        const id = g.userData.board as string | undefined
        const b = id ? props.boards.find(x => x.needId === id) : undefined
        if (!id || b?.where !== 'bot') continue
        const mid = g.getWorldPosition(new Vector3())
        const far = eye.distanceTo(mid)
        caster.set(eye, mid.clone().sub(eye).normalize())
        caster.far = far - 0.005
        const hits = caster.intersectObjects(scene.children, true)
        if (hits.some(h => /^(page|panel):/.test(whatIs(h)))) hidden.add(id)
        // shortcut: one line to the board's middle, so a corner under something nearer is not seen; a line per corner if that shows.
        // A taken dashboard over any part of it in the picture, nearer than it, covers it too (D114 2): the whole
        // board is then drawn and pressed by depth, so a press inside the form's outline is the form's.
        if (hits.some(h => coversDesk(h.object, g)) || takenCovers(g, camera)) covered.add(id)
      }
      coveredNow.current = covered
      props.onHidden(hidden)
    }
    fr.m.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
    fr.frustum.setFromProjectionMatrix(fr.m)
    // Where he faces now, and since when his head has rested there.
    camera.getWorldDirection(fr.dir)
    const facing = Math.atan2(-fr.dir.x, -fr.dir.z)
    // At rest: his look, up and down too, has turned less than `restDeg` since then.
    const rest = restAt.current
    if (rest.dir.dot(fr.dir) < Math.cos((HELD_COME.restDeg * Math.PI) / 180)) restAt.current = { dir: fr.dir.clone(), since: t }
    // The room's front is where its Home strip stands (`homePlace.ts`, `ROOM_FRONT_YAW_DEG`).
    // `placeAt` counts a yaw to the right; the way he faces is counted to the left (`atan2(-x, -z)`).
    const front = (-ROOM_FRONT_YAW_DEG * Math.PI) / 180
    const facesFront = Math.abs(facing - front) < (HELD_COME.frontDeg * Math.PI) / 180
    // A rest counts only after the take: a take by name happens with his head still on the names line.
    const restedSince = (own: Ahead) => t - Math.max(restAt.current.since, own.takenAt) >= HELD_COME.restMs
    const dt = Math.min(0.1, (t - (lastFrame.current || t)) / 1000)
    lastFrame.current = t
    const near = props.inSession && t - nearAt.current >= 250
    if (near) nearAt.current = t
    for (const g of root.current?.children ?? []) {
      const id = g.userData.board as string | undefined
      const b = id ? props.boards.find(x => x.needId === id) : undefined
      const h = id ? heldAt.current.get(id) : undefined
      if (!id || b?.where !== 'held' || !h?.ahead) continue
      const own = h.ahead
      box.setFromObject(g)
      // Whole in the lens is the PAPER's four corners and the stamp (D79 1): the backing, the press areas and a turned
      // board's depth do not count.
      const paper = paperPoints(g)
      const whole = paper.every(p => fr.frustum.containsPoint(p))
      // The glide to the front: it waits while a pinch is down, and ends at the front.
      const still = pinchRoute.owner !== null || b.writing
      if (own.glide) {
        if (!still) own.glide.done = Math.min(1, own.glide.done + dt / (HELD_COME.glideMs / 1000))
        const k = own.glide.done
        own.yaw = own.glide.from + (front - own.glide.from) * k * k * (3 - 2 * k)
        if (k >= 1) { own.yaw = front; own.glide = null }
      } else if (own.yaw !== front && facesFront && restedSince(own) && !whole && !still) own.glide = { from: own.yaw, done: 0 }
      if (own.ease) {
        own.ease.done = Math.min(1, own.ease.done + dt / (HELD_COME.easeMs / 1000))
        const k = own.ease.done
        own.nearM = own.ease.from + (own.ease.to - own.ease.from) * k * k * (3 - 2 * k)
        if (k >= 1) own.ease = null
      }
      // A page that stands between his eye and the board: the board comes in front of it.
      if (near && !own.glide && !still) {
        const eye = camera.getWorldPosition(new Vector3())
        const far = own.nearM
        let nearest: number = HELD.distanceM
        // The paper's corners, the stamp and the paper's middle (B4).
        for (const p of [...paper, paperMiddle(g)]) {
          const d = eye.distanceTo(p)
          caster.set(eye, p.clone().sub(eye).normalize())
          caster.far = (d * HELD.distanceM) / far + 0.05
          const hit = caster.intersectObjects(scene.children, true).find(x => /^(page|panel):/.test(whatIs(x)))
          if (hit) nearest = Math.min(nearest, (hit.distance * far) / d - HELD_COME.clearM)
        }
        const to = Math.max(HELD_COME.nearestM, Math.min(HELD.distanceM, nearest))
        if (Math.abs(to - (own.ease?.to ?? own.nearM)) > 0.005) own.ease = { from: own.nearM, to, done: 0 }
      }
      // The minute's clock runs only while no part of the board is in the lens.
      if (t - (seen.current.get(id) ?? 0) >= 500) { seen.current.set(id, t); props.onAway(id, fr.frustum.intersectsBox(box)) }
    }
    for (const b of props.boards) {
      if (b.where !== 'held' || heldAt.current.get(b.needId)?.ahead) continue
      if (t - (seen.current.get(b.needId) ?? 0) < 500) continue
      seen.current.set(b.needId, t)
      const h = heldAt.current.get(b.needId)
      if (h) props.onAway(b.needId, fr.frustum.containsPoint(h.at))
    }
  })
  // At a seat a Bot holds its board up in the air, where a finger's touch of the desk does not reach (`DeskTouch`
  // reads a tip that comes down to the desk's top): a touch on the pad stays the seat's, and a pinch takes the board.
  const seated = new Set(props.crew().filter(c => c.seat).map(c => c.slug))
  return (
    <>
      <group ref={root}>
        {props.boards.map(b => {
          const kind: BoardKind = b.where !== 'bot' ? 'held' : seated.has(b.bot) ? 'seat' : 'bot'
          return (
            <OneBoard key={`${b.needId}:${kind}`} b={b} props={props} kind={kind} place={place} covered={coveredNow} deskSeen={deskSeen}
              onThrown={dir => thrown.current.set(b.needId, dir)}
              register={s => { if (s) snaps.current.set(b.needId, { ...s, matrix: s.group.matrixWorld.clone() }) }} />
          )
        })}
      </group>
      {leaving.map(l => (l.dir
        ? <BoardDust key={l.key} from={l.from} dir={l.dir} still={props.still} onDone={() => setLeaving(list => list.filter(x => x.key !== l.key))} />
        : <BoardLower key={l.key} from={l.from} still={!!props.still} onDone={() => setLeaving(list => list.filter(x => x.key !== l.key))} />))}
    </>
  )
}
