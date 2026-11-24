/**
 * The crew in the room of pages: the real Bots, each with the body that the land draws (`Bot.tsx`), 0.30 m tall, in
 * a row under the first page (`crewRow.ts`).
 *
 * A Bot shows its state by its body, as on the land (it rests, works, asks, slumps), and by a ring at its feet that
 * is sized for this row: none when it rests, moving dashes when it works, a whole ring over a glowing disc when it
 * asks for the person, a broken ring when a cut of it is not seen yet. A cut that he has seen is a short still arc at
 * the ring's front, in the cut's red, faint, beside any state but that one (`rims.ts`). The one that asks also hops
 * now and then.
 *
 * A pointer on a Bot lights it and shows its name on a small sign at the head's upper side: a finger that goes down
 * on it, a mouse that rests on it, a hand's ray in a session. A Bot that asks carries that sign without a pointer,
 * with its name and how many asks of it wait (`plates`). The sign is no wider than the body and stands in front of
 * no page; a press on it is a press on the Bot. A press that begins and ends on the same Bot is a tap: the Bot hops and turns to the
 * person (with reduced motion it only lights), and `onTap` is told. A press that moves is no tap: in the flat preview
 * it becomes the drag that turns the picture, as on empty space; in a session a ray that leaves the Bot ends it.
 * Nothing here sends, approves, answers or starts anything.
 */
import { useFrame, type ThreeEvent } from '@react-three/fiber'
import { useXRStore } from '@react-three/xr'
import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, DoubleSide, LinearFilter, SRGBColorSpace, Vector3, type Group, type MeshBasicMaterial, type Object3D } from 'three'
import { BotNote } from './BotNote'
import type { BotState } from '../body'
import { Ghost, Plain, type BotProps, type BotStage } from '../Bot'
import { BOT_H, BotDrive } from '../botDrive'
import { seedOf } from '../botMotion'
import { GLOW, tintOf } from '../character'
import { faceArtFor } from '../faces'
import { ModelGuard } from '../ModelGuard'
import type { XrBot } from '../model'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { BOT_SPOT, idleLife, idleStep } from '../plots'
import { loadFace } from '../sceneParts'
import type { BotLook, WorldColors } from '../world'
import { CREW, CREW_BAND, STROLL, crewHomes, crewPoint, crewStepDeg, freeYaw, strollGoal, yawOnArc, type KeptCrew, type StrollAsk, type StrollGoal } from './crewRow'
import { DESK, deskPoint, seatPlaces, type DeskPlace } from './desk'
import { touchTargets } from './deskTouch'
import { PlaceDesks } from './PlaceDesk'
import { SincePool } from './SinceLight'
import type { KeptFront } from './room'
import { LIFE, idleAct, crossesPage, inQuiet, quietFor, readsPage, visitHolds, visitSpot, walkAt, walkMs, walkWay, wayPoint, type HoldAsk, type IdleAct, type VisitAsk, type Way } from './placeLife'
import { HOME_BOX, HOME_STRIP, MID_M, PLACES, asCrewPlace, pageSignCount, forwardCovers, driftAt, driftNow, frontNow, keptPoint, leansNow, onBox, onSphere, placeHomes, reachOf, stage as placeStage, staysHome, heldHome, type SentHome, type Front, type KeptPlaces, type Look, type LowBlock, type Pt, type SeenBox, type Staged } from './places'
import { PINCH, pinchRoute } from './pinchRoute'
import { leaveEndsPress, visibilityTakes } from './botPress'
import { driftStep, trayHeld } from './trayHold'
import { TURN_DRAG_PX } from './previewLook'
import type { EmptyHand } from './RoomWalls'

/** A Bot that asks for the person hops this often, so that it is found. */
const HOP_EVERY_MS = 2600
/** After a tap the Bot stays lit, and turned to the person, this long. */
const ANSWER_MS = 900
/** The answer (the hop, or with reduced motion the light alone) is seen before the page opens and the picture turns away. */
const OPEN_AFTER_MS = 320
/**
 * The sign at a Bot: its name while a pointer lights it, and its name with how many asks wait while it asks. Its
 * widest picture, the air at each side of the words, and how wide the widest sign is in the room: no wider than
 * the body, so that it is a part of the Bot, stands in front of no page, and no two signs cover each other.
 */
const SIGN = { width: 384, height: 80, font: 40, pad: 16, widthM: CREW.heightM * CREW.widthShare } as const
/** A plate's line that he must read (D83 5, `BotNote.tsx`): its letters half as large again as a name's (0.65 degrees at 3 m). */
const NOTE_SIGN = { scale: 1.5 } as const
/**
 * The sign's middle is this far over the top of the head. It stands in front of the body, and the person looks down
 * at the crew: so it is seen on the head's upper side, clear of the eyes, and a press on it meets the Bot's own plate.
 */
const SIGN_OVER_TOP_M = 0.01
/** A press held still this long on a Bot lifts it, the time after which a pinch is no click (`PINCH.clickMs`). */
const LIFT_MS = PINCH.clickMs
/** A lifted Bot is carried this much over its row or the desk. */
const LIFT_M = 0.03
/** At the desk a Bot put down this near a seat's middle sits on that seat; farther, it goes back to its own. */
const SEAT_DROP_M = DESK.padM * 1.5
/** A test's switch (`__xrPages.stroll`): every Bot at rest strolls, a new way every `everyMs`, or none does; `null` leaves it to `idleLife`. */
export const crewStroll: { all: boolean | null; everyMs: number } = { all: null, everyMs: 3000 }

/** What a test reads of one Bot. */
export interface CrewProbe {
  slug: string
  /** The name that is shown, and `key`, the name every route of the Bot is keyed on. */
  name: string
  key: string
  state: BotState
  /** Whether a pointer lights it now. */
  lit: boolean
  row: 0 | 1
  /** Its feet, and the top of its head, as points of the room. */
  feet: [number, number, number]
  top: [number, number, number]
  /** How wide its body is, in metres. */
  widthM: number
  /** How many times it was tapped, and how many times it hopped. */
  taps: number
  hops: number
  /** Whether its body is drawn: its art is loaded. */
  drawn: boolean
  /** Whether its state is shown: false while the states cannot be known. */
  shown: boolean
  /** It has something unread, as the dashboard folds it; and it stands at a seat of the desk. */
  unread?: boolean
  /** Its ring shows the dim mark of a cut that he has seen. */
  dim?: boolean
  seat?: boolean
  /** Its sign (the name) is drawn now. */
  sign?: boolean
  /** A press lifted it, and carries it now. */
  carried?: boolean
  /** A press under way on it (`press.current`): whether it became a drag, lifted it, or holds it. */
  pressing?: { dragging: boolean; lifted: boolean; holding: boolean; type: string; at: [number, number]; held: boolean } | null
  /** While it is lit: the type of the pointer that lit it. */
  litBy?: string | null
  /** The last eight pointer events on its box (no moves): which, by which pointer, at the page's ms (DASH-15 C). */
  boxEvents?: { type: string; id: number; pointer: string; ms: number }[]
  /** Each change of the session's `visibilityState` this Crew saw, with the page's ms; `none` out of a session. */
  visibility?: { state: string; ms: number }[]
  /** Its art faces the other way, and its body with it (`BotLook.flip`). */
  flip?: boolean
  /** The line its plate says now (`notes`). */
  note?: string
  /** Where the middle of that line's last letter is drawn, as a point of the room, and how many rows the line has. */
  noteLast?: { at: [number, number, number]; rows: number }
  /** What it walks to of its own now: `home`, a stroll (`look:…`), or `needs`, the step toward him. */
  goal?: string
  /** Where its ring, sign and press box stand now, as a point of the room. */
  parts?: [number, number, number]
  /** With no row (`places.ts`): what it does where it is, whether it came forward, and its desk. */
  does?: Staged['does']
  forward?: boolean
  desk?: { at: [number, number, number]; lit: boolean; hidden: boolean }
  /** How much of its size its body has now: under 1 while it shrinks away from a spot or grows at its new one. */
  grow?: number
  /** It gave way to a page, to Home's large surface or to the one that came forward. */
  moved?: boolean
  /** With no row: it walks now (a walk of its own, or home). */
  walking?: boolean
  /** Its ask's sign at the edge of the page he reads, where it could not come forward. */
  pageSign?: [number, number, number]
}

/** The room with no row (`places.ts`; the room as it opens, `?places=0` gives the row back): what `Crew` reads, and what it tells. */
export interface PlacesRoom {
  /** The places that a drag gave Bots, as points; and the pages that show, as the eye sees them. */
  kept: KeptPlaces
  /** The Bots that stand where he put them, with the state each was in then: the mark holds while the state does. */
  put: Readonly<Record<string, string>>
  pages: readonly SeenBox[]
  /** What holds the low middle now. */
  block: LowBlock
  /** The card's keyboard as the eye sees it, while it is out. */
  keys?: SeenBox
  /** Home's large surface stands in the middle. */
  homeLarge: boolean
  /** Where he looks now, to the right of straight ahead, in degrees. */
  look: () => number
  /** A Bot was put down: its place is this point. */
  onPut: (slug: string, at: Pt, state: string) => void
  /** Whether a Bot that needs him stands forward now. */
  onForward: (on: boolean) => void
  /** He holds a board; and where he looks now, up and down too. While he holds one, or his look's middle is on a page, nothing new moves in that middle. */
  boardHeld: boolean
  gaze: () => Look
  /** The asks whose board he handed back ("not now", D27): each keeps its Bot home and leaves the front free. */
  sentHome: SentHome
  /** Those asks as the room kept them, and what to tell when they change. */
  home: SentHome
  onHome: (home: SentHome) => void
  /** The Bot that stood in front when the room was kept, and what to tell when another stands there or none. */
  front: KeptFront | null
  onFront: (front: KeptFront | null) => void
}

interface Member {
  drive: BotDrive; look: BotLook | null; dim: BotLook | null; artKey: string; hopAt: number; taps: number; hops: number; until: number
  /** Its idle drift's own clock (`driftStep`): it stands while the Bot or its resting tray is under a pointer. */
  drift?: { t: number; rate: number }
  /** What rides with the body: the ring, the pad, the sign and the press box, placed where the body is at each frame. */
  parts: Group | null
  /** The body stands away from its place: a walk of it was stopped there. It walks back once nothing holds it. */
  away: boolean
  /** Where a press that lifted it carries it now, or `null`; and where it walks once it was put down. */
  carry: Vector3 | null
  landing: [number, number, number] | null
  /** What it walks to of its own now (`goalOf`), and a stroll that a pointer or a press ended. */
  goal: string
  ended: string
  /** The goal worked out last, and what it was worked out from: it is worked out again only when that changes. */
  target: StrollGoal | null
  why: { beat: number; state: string; rest: boolean; x: number; z: number; ended: string }
  /** When its goal began (a turn in place runs from here), and the goal whose hop it hopped. */
  lifeAt: number
  hopFor: string
  /** With no row: the spot its body is shown at (before its drift), the spot it shrinks away to and when that began, and how far a drag holds it. */
  shown: Pt | null
  swapTo: Pt | null
  swapAt: number
  reach: number
  /** With no row: its desk is drawn (it stood at it, drawn and grown, and has not given way since); and how much of its size it has now. */
  homed: boolean
  grow: number
  /** With no row: the walk it is on; the beat of its idle life it picked a visit in, and where that visit stands; what it does idle. */
  walk: { way: Way; to: Pt; t0: number; ms: number; pages: string } | null
  life: { beat: number; spot: Pt | null }
  act: IdleAct
  /** Since when it waits for the others to stand aside before it walks to the front; 0 while it does not. */
  waitFrom: number
  /** The goal, pages and quiet it last searched a way for and found none but past the quiet middle; '' when none. */
  tried: string
  /** Its walk stopped short of a page: it goes on at once, whatever the cap of two. */
  resume: boolean
}

/** Whether two spots are the same spot. */
const nearPt = (a: readonly number[], b: readonly number[]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]) < 0.02

/** The look of a Bot whose state cannot be known: its own shape and what it wears, in the dim grey of a label that is out of date. Made once for each look. */
function dimLook(m: Member): BotLook {
  if (!m.look) throw new Error('a Bot is dimmed before it has a look')
  if (!m.dim || m.dim.wear !== m.look.wear || m.dim.flip !== m.look.flip) m.dim = { ...m.look, tint: XR_GL.faint }
  return m.dim
}

interface Props {
  bots: readonly XrBot[]
  /** The head, as the bridge keeps it. */
  head: Vector3
  colors: WorldColors
  /** Reduced motion: no Bot moves by itself, and a tap is answered by light. */
  still: boolean
  /** The quiet time of the flat preview: a touch on a Bot does nothing and lights nothing. */
  hushed: boolean
  /**
   * Whether the states can be known now: the dashboard's socket is up and its lists were read. While they cannot,
   * the row shows none: no ring is drawn and the bodies are a dim grey, since an old state may be a wrong one. A tap still
   * opens the Bot's page.
   */
  known: boolean
  onTap: (slug: string) => void
  /**
   * What a pointer does on empty space, in the flat preview (`Workspace.tsx`). A press on a Bot that moves farther
   * than a tap may is handed to it: the row stands where a thumb begins its drag, and a drag there turns the picture.
   * Left out in a session (`EmptySpace.tsx` hands its children `undefined`), where a hand's ray that leaves the Bot
   * ends the press (`leaveEndsPress`).
   */
  drag?: EmptyHand
  report?: (read: (() => CrewProbe[]) | null) => void
  /** The Bots that carry their sign now, each with how many asks of it wait (`useAskPanels.ts`). */
  plates?: ReadonlyMap<string, number>
  /**
   * The desk (`desk.ts`): each Bot stands at its own seat on it, 8 cm tall on a round pad, instead of in the row
   * under the first page. The pad says the state: a ring that turns while it works, the pad lit in the ask's colour
   * and a small blank board held up while it asks, a red rim for a cut of it that he has not seen (the dim mark
   * for one he has), a mark when it
   * has something unread, and dim while it rests or while the states cannot be known.
   */
  desk?: DeskPlace | null
  /** At the desk: the seat of each Bot that has one (`seatOrder`), and how many seats stand (`seatPlaces`). */
  seatOf?: ReadonlyMap<string, number>
  spots?: number
  /** The places in the row that a drag kept, by slug (`crewRow.crewHomes`). */
  kept?: KeptCrew
  /** A Bot was put down in the row: the places of its row now, by slug, its own new one with them. */
  onPlace?: (row: KeptCrew) => void
  /** At the desk: a Bot was put down on this seat (`useDesk.moveSeat`). */
  onSeat?: (slug: string, seat: number) => void
  /** The Bots that hold up a board (`Board.tsx`): at a seat the board stands in place of the pad's small blank one. */
  boarded?: ReadonlySet<string>
  /** The room with no row (`placesWanted`): read only with no desk. */
  room?: PlacesRoom | null
  /**
   * A press on a Bot held still for `LIFT_MS` is a hold, his words to it (P2, `speak.ts`): `start` then; a move past
   * the drag's own measure ends it as `drag` and lifts the Bot as before (D10); his release ends it as `let`; any other
   * end is `cancel`. Without it, the press held still lifts the Bot at once. Its answer to `let`: whether his words went
   * (the Bot nods then).
   */
  onHold?: (slug: string, phase: 'start' | 'drag' | 'let' | 'cancel') => boolean | void
  /** The Bots that changed since he left (D36, `since.ts`): each is lit where it stands, and a tap on one is told. */
  since?: { lit: ReadonlySet<string>; onTap: (slug: string) => void }
  /** One line that a Bot's own plate says for a little while (D82 3): why no page came, or that its sheet is in his hand. */
  notes?: ReadonlyMap<string, string>
}

/** A Bot's size, and the ring at its feet, in the row and at a seat. */
interface Size { heightM: number; ringM: readonly [number, number] }
const ROW_SIZE: Size = { heightM: CREW.heightM, ringM: CREW.ringM }
const SEAT_SIZE: Size = { heightM: DESK.botM, ringM: [DESK.padM / 2 - 0.008, DESK.padM / 2] }
/** A seat's sign is drawn at this share of the row's: about as wide as its pad. */
const SEAT_SIGN = 0.5

/** The pad of a seat: a dim disc on the desk, the mark of something unread, and the ask's small blank board. */
function Pad({ state, color, unread, asks, yaw }: { state: BotState; color: number; unread: boolean; asks: boolean; yaw: number }) {
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position-y={0.001}>
        <circleGeometry args={[DESK.padM / 2, 40]} />
        <meshBasicMaterial color={XR_GL.faint} transparent opacity={state === 'idle' ? 0.25 : 0.4} side={DoubleSide} depthWrite={false} toneMapped={false} />
      </mesh>
      {unread && (
        <mesh rotation-x={-Math.PI / 2} position={[Math.sin(yaw + Math.PI / 2) * DESK.padM * 0.32, 0.004, Math.cos(yaw + Math.PI / 2) * DESK.padM * 0.32]}>
          <circleGeometry args={[0.008, 20]} />
          <meshBasicMaterial color={XR_GL.white} side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      {asks && (
        <mesh position={[Math.sin(yaw - Math.PI / 2) * DESK.padM * 0.42, DESK.botM * 0.95, Math.cos(yaw - Math.PI / 2) * DESK.padM * 0.42]} rotation-y={yaw} pointerEvents="none">
          <planeGeometry args={[0.045, 0.03]} />
          <meshBasicMaterial color={color} side={DoubleSide} toneMapped={false} />
        </mesh>
      )}
    </group>
  )
}

/** The dim mark: an arc of this share of the ring, centred on the side that faces the person, at this opacity. */
const DIM_MARK = { share: 1 / 6, opacity: 0.4 } as const

/**
 * The ring at a Bot's feet. Its shape says the state; the lit ring is white and whole. `dim` adds the dim mark, in
 * `cut`'s colour, at the side that faces the person (`yaw`). It never turns.
 */
function Ring({ state, color, lit, still, size = ROW_SIZE, dim = false, cut = color, yaw = 0 }: { state: BotState; color: number; lit: boolean; still: boolean; size?: Size; dim?: boolean; cut?: number; yaw?: number }) {
  const turn = useRef<Group>(null)
  const glow = useRef<MeshBasicMaterial>(null)
  useFrame((_s, dt) => {
    if (turn.current && state === 'working' && !still) turn.current.rotation.y += dt * 0.6
    if (glow.current) glow.current.opacity = still ? 0.4 : 0.3 + 0.2 * Math.sin(performance.now() / 240)
  })
  const [inner, outer] = size.ringM
  // At a seat the interrupted state is a whole red rim; in the row, a broken ring.
  const seat = size !== ROW_SIZE
  const arcs: [number, number][] = state === 'working' ? [0, 1, 2, 3, 4, 5].map(i => [(i * Math.PI) / 3, Math.PI / 5])
    : state === 'needs' ? [[0, Math.PI * 2]] : state === 'failed' ? (seat ? [[0, Math.PI * 2]] : [[0.5, Math.PI * 0.8], [0.5 + Math.PI, Math.PI * 0.8]]) : []
  return (
    <group position-y={0.003}>
      <group ref={turn}>
        {arcs.map(([start, length]) => (
          <mesh key={start} rotation-x={-Math.PI / 2}>
            <ringGeometry args={[inner, outer, 48, 1, start, length]} />
            <meshBasicMaterial color={color} transparent opacity={Math.max(0.85, GLOW[state].ring)} side={DoubleSide} depthWrite={false} toneMapped={false} />
          </mesh>
        ))}
      </group>
      {state === 'needs' && (
        <mesh rotation-x={-Math.PI / 2} position-y={-0.001}>
          <circleGeometry args={[inner, 40]} />
          <meshBasicMaterial ref={glow} color={color} transparent opacity={0.4} side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      {/* The ring lies flat: its angle a is the room's (cos a, 0, -sin a), so the side that faces the person is yaw - 90 degrees. */}
      {dim && state !== 'failed' && (
        <mesh rotation-x={-Math.PI / 2} position-y={0.001}>
          <ringGeometry args={[inner, outer, 16, 1, yaw - Math.PI / 2 - Math.PI * DIM_MARK.share, Math.PI * 2 * DIM_MARK.share]} />
          <meshBasicMaterial color={cut} transparent opacity={DIM_MARK.opacity} side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      {lit && (
        <mesh rotation-x={-Math.PI / 2} position-y={0.002}>
          <ringGeometry args={[outer, outer + 0.004, 48]} />
          <meshBasicMaterial color={XR_GL.white} side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
    </group>
  )
}

/** The sign at a Bot: pale words on a dark plate at the head's upper side, turned to the person. It is as wide as its words and their padding, and no wider than the body. */
/** For the probe: where a plate's line (`BotNote`) draws its last letter, from the Bot's riding parts. */
function noteLastOf(parts: Object3D | null | undefined): CrewProbe['noteLast'] {
  const n = parts?.getObjectByName('bot-note')
  const note = n?.userData.note as { rows: number; last: [number, number] } | undefined
  if (!n || !note) return undefined
  n.updateWorldMatrix(true, false)
  const at = n.localToWorld(new Vector3(note.last[0], note.last[1], 0))
  return { at: [at.x, at.y, at.z], rows: note.rows }
}

function Sign({ text, yaw, scale = 1, topM = CREW.heightM * (1 + CREW.hoverShare), overM }: { text: string; yaw: number; scale?: number; topM?: number; overM?: number }) {
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
    const c = canvas.getContext('2d')
    // The words at the sign's own size of letter, or smaller when they are longer than the widest sign.
    let size: number = SIGN.font
    let wide: number = SIGN.width
    if (c) do { c.font = `600 ${size}px ${family}`; wide = c.measureText(text).width; size -= 2 } while (size > 22 && wide > SIGN.width - 2 * SIGN.pad)
    const width = Math.min(SIGN.width, Math.ceil(wide) + 2 * SIGN.pad)
    // A canvas forgets its font when its size is set: the size first, then the words.
    canvas.width = width
    canvas.height = SIGN.height
    if (c) {
      c.fillStyle = css(XR_GL.room)
      c.globalAlpha = 0.9
      c.beginPath()
      c.roundRect(2, 2, width - 4, SIGN.height - 4, 20)
      c.fill()
      c.globalAlpha = 1
      c.fillStyle = css(XR_GL.ink)
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.font = `600 ${size + 2}px ${family}`
      c.fillText(text, width / 2, SIGN.height / 2 + 2, width - 12)
    }
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { texture, widthM: (SIGN.widthM * width) / SIGN.width }
  }, [text])
  useEffect(() => () => plate.texture.dispose(), [plate])
  // In front of the body, on the side that faces the person: the body is round, and half as deep as it is wide.
  // With `overM` it stands over the head instead, in the body's own plane and that far over its top: a Bot that
  // stands level with his eyes, or rises as it asks, would have a sign in front of it across its face.
  const out = overM === undefined ? (SIGN.widthM / 2) * scale + 0.012 : 0
  return (
    <mesh
      position={[Math.sin(yaw) * out, topM + (overM ?? SIGN_OVER_TOP_M), Math.cos(yaw) * out]}
      rotation-y={yaw}
      scale={[plate.widthM * scale, ((SIGN.widthM * SIGN.height) / SIGN.width) * scale, 1]}
      pointerEvents="none"
      userData={{ xrPress: 'sign' }}
    >
      <planeGeometry />
      <meshBasicMaterial map={plate.texture} transparent depthWrite={false} toneMapped={false} />
    </mesh>
  )
}

/** One body, as the land draws it, or a plain one when the model cannot be loaded. */
function Body(props: BotProps) {
  return (
    <ModelGuard fallback={<Plain {...props} />}>
      <Suspense fallback={null}><Ghost {...props} /></Suspense>
    </ModelGuard>
  )
}

export function Crew({ bots, head, colors, still, hushed, known, onTap, drag, report, plates, desk = null, seatOf, spots, kept, onPlace, onSeat, boarded, room = null, onHold, since, notes }: Props) {
  const sinceNow = useRef(since)
  sinceNow.current = since
  const notesNow = useRef(notes)
  notesNow.current = notes
  const size = desk ? SEAT_SIZE : ROW_SIZE
  const members = useRef(new Map<string, Member>())
  const [, redraw] = useState(0)
  const [lit, setLit] = useState('')
  const litNow = useRef('')
  litNow.current = lit
  const knownNow = useRef(known)
  knownNow.current = known
  /** The press that is on a Bot now: which pointer, on which Bot it began and where on the screen, and whether it has become a drag. */
  const press = useRef<{ pointerId: number; slug: string; at: [number, number]; dragging: boolean; lifted: boolean; moved: boolean; obj: Object3D; holding?: boolean; type?: string } | null>(null)
  /** For the probe: the type of the pointer that lit a Bot last (M168). */
  const litBy = useRef('')
  /** For the probe: each Bot's last box events, and the session's visibility changes (DASH-15 C). No behaviour reads them. */
  const boxLog = useRef(new Map<string, NonNullable<CrewProbe['boxEvents']>>())
  const seenLog = useRef<NonNullable<CrewProbe['visibility']>>([])
  const logBox = (slug: string, e: ThreeEvent<PointerEvent>) => {
    const list = boxLog.current.get(slug) ?? []
    list.push({ type: e.type, id: e.pointerId, pointer: e.pointerType, ms: Math.round(performance.now()) })
    boxLog.current.set(slug, list.slice(-8))
  }
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>())
  const alive = useRef(true)
  useEffect(() => { alive.current = true; const all = timers.current; return () => { alive.current = false; for (const t of all) clearTimeout(t) } }, [])
  const stage = useMemo<BotStage>(() => ({ colors, still }), [colors, still])
  // With no row each Bot has a place of its own (`places.ts`); where it is now says what it does.
  const roomOn = !desk && !!room
  const homes = useMemo(() => (roomOn ? placeHomes(bots.map(b => b.slug), room?.kept, kept) : null), [roomOn, bots, room?.kept, kept])
  const places = useMemo(() => (homes ? homes.map(asCrewPlace) : crewHomes(bots.map(b => b.slug), kept)), [homes, bots, kept])
  // The one that needs him comes to where he looked when it began to ask, and stays there while he looks about.
  // A Bot that he put down stands where he let it go until its state changes.
  const putMarks = room?.put
  const putNow = putMarks ? bots.filter(b => putMarks[b.slug] === b.state).map(b => b.slug).sort().join(' ') : ''
  const putSet = useMemo(() => new Set(putNow ? putNow.split(' ') : []), [putNow])
  // The one in front stays until its own ask ends, and stands where he looked when it came (`frontOf`). While the
  // states cannot be known nothing is chosen anew: when they come back it has its place and its look still.
  const askLook = useRef<Front>({ slug: '', yaw: 0 })
  // After a reload the one that stood in front stands at the look it came to, while it still holds that ask.
  const frontRead = useRef(false)
  if (roomOn && room && known && !frontRead.current) {
    frontRead.current = true
    const was = room.front
    if (was && bots.find(b => b.slug === was.slug)?.needs.some(n => n.id === was.ask)) askLook.current = { slug: was.slug, yaw: was.yaw }
  }
  // A Bot that he put down stays where it is, and a Bot whose board he handed back ("not now") goes home to its desk
  // with its board up: neither stands in front, so the next one that needs him can come.
  // It stays home when he takes that board again: nothing else moves under his hand. Only that ask holds it: with
  // a question besides it (a question comes past a handed-back approval) it comes forward as any asker.
  const atDesk = useRef<SentHome>(room?.home ?? {})
  if (roomOn && known) atDesk.current = staysHome(atDesk.current, room?.sentHome, bots)
  const deskNow = atDesk.current
  const onHome = room?.onHome
  useEffect(() => { if (known) onHome?.(deskNow) }, [onHome, known, deskNow])
  const notFront = useMemo(() => new Set([...putSet, ...heldHome(deskNow, bots)]), [putSet, deskNow, bots])
  if (roomOn && room) askLook.current = frontNow(askLook.current, bots, known, notFront, room.look)
  const { slug: frontSlug, yaw: lookYaw } = askLook.current
  // The ask it came for: its oldest. The room keeps the three together.
  const frontAsk = frontSlug ? [...(bots.find(b => b.slug === frontSlug)?.needs ?? [])].sort((a, b) => a.ts - b.ts)[0]?.id ?? '' : ''
  const onFront = room?.onFront
  useEffect(() => { if (known) onFront?.(frontSlug && frontAsk ? { slug: frontSlug, ask: frontAsk, yaw: lookYaw } : null) }, [onFront, known, frontSlug, frontAsk, lookYaw])
  const staged = useMemo(() => (homes && room ? placeStage({ bots, homes, pages: room.pages, block: room.block, keys: room.keys, known, homeLarge: room.homeLarge, lookYaw, front: frontSlug, put: putSet }) : null), [homes, room, bots, known, lookYaw, frontSlug, putSet])
  const forwardNow = !!staged?.some(s => s.forward)
  /** Whether the desk of the Bot at `i` is drawn now: never before its Bot stood at it (`Member.homed`). */
  const deskDrawn = (i: number): boolean => !!staged?.[i] && !staged[i].desk.hidden && !!members.current.get(bots[i].slug)?.homed
  const onForward = room?.onForward
  useEffect(() => { onForward?.(forwardNow) }, [onForward, forwardNow])
  const seats = useMemo(() => seatPlaces(spots ?? bots.length), [spots, bots.length])
  /** The seat of the Bot at `i`: its own (`seatOf`), else its place in the crew's order. */
  const seatAt = (i: number) => seats[seatOf?.get(bots[i]?.slug ?? '') ?? i]
  /** Where a Bot's feet are: at its place in the row, or on its seat's pad. */
  const feetOf = (i: number): [number, number, number] => {
    const seat = seatAt(i)
    if (desk && seat) return deskPoint(desk, seat.u, seat.v)
    if (staged?.[i]) return staged[i].at
    const place = places[i]
    return place ? crewPoint(place) : [0, 0, 0]
  }

  // Each Bot of the crew has a body; one that left the crew has none. The look is the dashboard's own art for it.
  useEffect(() => {
    const seen = new Set<string>()
    for (const bot of bots) {
      seen.add(bot.slug)
      let m = members.current.get(bot.slug)
      if (!m) { m = { drive: new BotDrive(seedOf(bot.slug)), look: null, dim: null, artKey: '', hopAt: 0, taps: 0, hops: 0, until: 0, parts: null, away: false, carry: null, landing: null, goal: 'home', ended: '', target: null, why: { beat: 0, state: '', rest: true, x: 0, z: 0, ended: '' }, lifeAt: 0, hopFor: '', shown: null, swapTo: null, homed: false, grow: 1, walk: null, life: { beat: -1, spot: null }, act: 'rest', waitFrom: 0, tried: '', resume: false, swapAt: 0, reach: 0 }; members.current.set(bot.slug, m) }
      // A body shows its state by its pose too: while the state cannot be known, it rests.
      m.drive.setState(known ? bot.state : 'idle')
      const artKey = JSON.stringify([bot.name, bot.avatar ?? null])
      if (artKey === m.artKey) continue
      m.artKey = artKey
      const member = m
      const art = faceArtFor(bot.avatar, bot.name)
      void Promise.all([loadFace(art), tintOf(art.layers.find(l => l.id === 'tile')?.uri)]).then(([tx, tint]) => {
        // The crew is told anew many times while one look loads: the look is for this body as long as its art is the same.
        if (!alive.current || member.artKey !== artKey || members.current.get(bot.slug) !== member) return
        member.look = { tint, wear: tx.layers.find(l => l.id === 'extra')?.tex ?? null, flip: art.flip }
        member.dim = null
        redraw(n => n + 1)
      }).catch(() => undefined)
    }
    for (const slug of [...members.current.keys()]) if (!seen.has(slug)) members.current.delete(slug)
    redraw(n => n + 1)
  }, [bots, known])

  /**
   * The press on a Bot is taken from him: what was lit goes dark, and the press is no tap. A press that had become a
   * drag lets the room go, as a lifted finger does; a lifted Bot goes back to its place, nothing kept; a hold is cut
   * off (`cancel`: nothing is sent, its sheet goes down). A Bot that answers a tap stays lit.
   */
  const pressTaken = () => {
    const held = press.current
    if (held?.dragging) drag?.onPointerUp({ pointerId: held.pointerId } as unknown as ThreeEvent<PointerEvent>)
    if (held?.lifted) putDown(held.slug, false)
    if (held?.holding) onHold?.(held.slug, 'cancel')
    press.current = null
    setLit(now => ((members.current.get(now)?.until ?? 0) > performance.now() ? now : ''))
  }
  // The quiet time takes it. A Bot that answers a tap stays lit: its tap is what began the quiet time.
  useEffect(() => {
    if (hushed) pressTaken()
    // `drag` is made anew at each render of the workspace: the quiet time's start is what this waits for.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hushed])
  // So does a session that stops being visible (a system layer over the room, D185), or ends: the store then reads
  // `visible-blurred`, `hidden`, or nothing (`@pmndrs/xr` `store.js`, its `end` handler). `@react-three/xr` turns every
  // session pointer off on each of them (`RootCombinedPointer`: `pointer.setEnabled(visible)`): the box gets a leave,
  // which a hold and a carry outlive, and never the release. And so does a session that begins (M223): the preview's
  // pointers are dropped then with no cancel, so a Bot carried there would stay in the air.
  // The Crew hears the store as the library does (`@pmndrs/xr` `setupSyncIsVisible`, M221): each change of the
  // store, not each render, so a change that goes and comes back inside one task still takes the press. At the mount
  // it reads the store once, before any press.
  const xrStore = useXRStore()
  const pressTakenNow = useRef(pressTaken)
  pressTakenNow.current = pressTaken
  useEffect(() => {
    type Seen = { visibilityState?: string }
    const update = (state: Seen, prev?: Seen) => {
      if (prev && state.visibilityState === prev.visibilityState) return
      seenLog.current = [...seenLog.current, { state: state.visibilityState ?? 'none', ms: Math.round(performance.now()) }].slice(-8)
      if (visibilityTakes(prev === undefined ? null : prev.visibilityState, state.visibilityState)) pressTakenNow.current()
    }
    update(xrStore.getState())
    return xrStore.subscribe(update)
  }, [xrStore])

  // A hold whose release the Bot never saw (it was let go off the canvas, or the press was taken from it) ends at the
  // window's own release as cut off: nothing is sent, and its sheet goes down.
  const onHoldNow = useRef(onHold)
  onHoldNow.current = onHold
  useEffect(() => {
    const end = (e: PointerEvent) => {
      const p = press.current
      if (!p?.holding || p.pointerId !== e.pointerId) return
      press.current = null
      onHoldNow.current?.(p.slug, 'cancel')
    }
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
    return () => { window.removeEventListener('pointerup', end); window.removeEventListener('pointercancel', end) }
  }, [])

  const at = useMemo(() => new Map(bots.map((b, i) => [b.slug, i] as const)), [bots])
  const home = useMemo(() => new Vector3(), [])
  const ray = useMemo(() => new Vector3(), [])
  const hushedNow = useRef(hushed)
  hushedNow.current = hushed
  /**
   * Where a Bot walks of its own now (`crewRow.strollGoal`). At rest the land's `idleLife` picks every 9 to 15 s: a
   * look around is a stroll a little way, back at the next pick; a nap is a nap at its place. It is worked out again
   * only when what it reads changes: the pick, the state, whether moves are off, its place, or a stroll that ended.
   */
  const goalOf = (bot: XrBot, m: Member, i: number, now: number): StrollGoal => {
    const [x, y, z] = feetOf(i)
    const rest = !!desk || still || !known || hushedNow.current
    const forced = crewStroll.all
    const beat = forced === null ? idleStep(bot.slug, now / 1000) : forced ? Math.floor(now / crewStroll.everyMs) : -1
    const w = m.why
    if (m.target && w.beat === beat && w.state === bot.state && w.rest === rest && w.x === x && w.z === z && w.ended === m.ended) return m.target
    m.why = { beat, state: bot.state, rest, x, z, ended: m.ended }
    let pick: StrollAsk['pick'] = { stroll: null, nap: false }
    if (forced) pick = { stroll: { key: `look:${beat}`, angle: (seedOf(`${bot.slug}:${beat}`) % 360) * (Math.PI / 180) }, nap: false }
    else if (forced === null) {
      const life = idleLife(bot.slug, [], now / 1000)
      pick = life.act === 'look' ? { stroll: { key: `look:${beat}`, angle: Math.atan2(life.z - BOT_SPOT[1], life.x - BOT_SPOT[0]) }, nap: false } : { stroll: null, nap: life.act === 'nap' }
    }
    const row = places[i]?.row
    const others = bots.flatMap((o, j) => {
      const n = j !== i && places[j]?.row === row ? members.current.get(o.slug) : undefined
      // Where it walks to, or, with no goal (a halt, a landing), where its body stands now: never its place alone.
      return n ? [n.target?.at ?? bodyAt(n, j)] : []
    })
    m.target = strollGoal({ home: [x, y, z], head: [head.x, head.y, head.z], state: bot.state, rest, pick, ended: m.ended, others, widthM: CREW.heightM * CREW.widthShare })
    return m.target
  }
  /** Where a Bot's body is now: at its place, or where it walks or was stopped. */
  const bodyAt = (m: Member, i: number): [number, number, number] => {
    // Carried, the body is where the press holds it (lifted): what rides with it, and its board, go along.
    if (m.carry) return [m.carry.x, m.carry.y, m.carry.z]
    // With no row the body is where its spot and its drift put it in the last frame.
    if (staged) { if (!m.shown) return feetOf(i); const p = m.drive.root.position; return [p.x, p.y, p.z] }
    if (!m.drive.walking && !m.away) return feetOf(i)
    const p = m.drive.root.position
    return [p.x, p.y, p.z]
  }
  /** Which way a Bot at this place faces to look at the person. */
  const yawOf = (i: number) => { const [x, , z] = feetOf(i); return Math.atan2(head.x - x, head.z - z) }

  // A desk is drawn only once its Bot stood at it: this counts the desks that came or went.
  const [, setStood] = useState(0)
  const lastFrame = useRef(performance.now())
  useFrame(() => {
    const now = performance.now()
    let stood = false
    // With no row: how many Bots walk now, where every body is, and what no way and no visit may cross.
    let moving = 0
    for (const m of members.current.values()) if (m.walk) moving += 1
    const bodies: Pt[] = staged ? bots.map(b => { const p = members.current.get(b.slug)?.drive.root.position; return p ? [p.x, p.y, p.z] : [0, -99, 0] }) : []
    // He reads a page when the middle of his look is ON it, not when a page is open somewhere.
    const gaze = room?.gaze()
    const quiet: Look | null = room && gaze && (room.boardHeld || readsPage(gaze, room.pages)) ? gaze : null
    // What covers things in the room NOW: the pages, Home, and the one in front with its board. A spot or a way that
    // was free when it was picked is checked against this, not against the room as it was then.
    const front = staged?.find(x => x.forward)
    const frontBox = front ? forwardCovers(front.at, { yaw: lookYaw, pitch: 0 }) : null
    const coversNow: SeenBox[] = staged ? [...(room?.pages ?? []), HOME_STRIP, ...(room?.homeLarge ? [HOME_BOX] : []), ...(frontBox ? [frontBox] : [])] : []
    const holdNow: HoldAsk = { covers: coversNow, faces: front ? [front.at] : [] }
    /** Where each Bot is going: the end of its walk and points along it, or the spot the rule gives it. */
    const going: Pt[][] = staged ? bots.map((b, j) => {
      const o = members.current.get(b.slug)
      if (o?.walk) return [o.walk.to, wayPoint(o.walk.way, 0.25), wayPoint(o.walk.way, 0.5), wayPoint(o.walk.way, 0.75)]
      return o?.life.spot ? [o.life.spot] : [staged[j].at]
    }) : []
    // Where each page is and how large: a page that is moved or resized where it stands is a change of the pages.
    const pagesKey = (room?.pages ?? []).map(p => `${p.yaw.toFixed(1)},${p.pitch.toFixed(1)},${p.x1.toFixed(3)},${p.y1.toFixed(3)},${p.distanceM.toFixed(2)}`).join(';')
    const quietKey = quiet ? `${quiet.yaw.toFixed(0)},${quiet.pitch.toFixed(0)}` : '-'
    const triedKey = (goal: Pt) => `${goal.map(v => v.toFixed(2)).join(',')}|${pagesKey}|${quietKey}`
    let visits: VisitAsk | null = null
    const visitAsk = (): VisitAsk => {
      if (visits || !staged) return visits ?? { at: [], open: [], desks: [], boxes: [] }
      visits = {
        at: bots.map((b, j) => members.current.get(b.slug)?.shown ?? staged[j].at),
        open: bots.map((b, j) => !staged[j].forward && !members.current.get(b.slug)?.walk),
        desks: staged.filter(x => !x.desk.hidden).map(x => x.desk.at),
        boxes: coversNow,
      }
      return visits
    }
    /** Whether a Bot that gives way to the one in front (its place is under that one's board) has not yet reached the spot it gives way to. A Bot that gives way to a page or to Home is not waited for. */
    const yielding = () => !!staged && !!homes && !!frontBox && bots.some((b, j) => { const o = members.current.get(b.slug); return staged[j].moved && onBox(homes[j], frontBox as SeenBox) && !!o?.shown && !nearPt(o.shown, staged[j].at) })
    for (const bot of bots) {
      const m = members.current.get(bot.slug)
      const i = at.get(bot.slug)
      if (!m || i === undefined) continue
      const [x, y, z] = feetOf(i)
      const body = m.drive.root.position
      // A Bot that a pointer lights, or that a press is on, stands still at once, so that a press hits what it shows.
      const held = litNow.current === bot.slug || press.current?.slug === bot.slug
      // Lit, pressed or lifted, it stands where it is, and a stroll that it was on is over.
      if (m.drive.walking && (held || m.carry)) { m.drive.halt(); m.away = true; m.ended = m.goal; m.goal = ''; m.target = null }
      // In a session a press is still only while its hand is (`PINCH.stillM`): a hand that moved lifts nothing.
      const p = press.current
      if (p && !drag && !p.lifted && p.slug === bot.slug && pinchRoute.travel() >= PINCH.stillM) p.moved = true
      // A look around in place turns the body this far from facing him (none while lit, pressed or lifted).
      let aside = 0
      let leaning = false
      let stretching = false
      let walkYaw: number | null = null
      let grow = 1
      const s = staged?.[i]
      if (s) {
        // With no row a Bot walks from spot to spot (`placeLife.ts`); where no way is free, and with reduced motion,
        // it changes spot as before: at once, or shrinking away and growing there.
        const quietHere = !!quiet && !!m.shown && inQuiet(m.shown, quiet)
        const lifeOn = !still && known && !hushedNow.current && crewStroll.all !== false
        let act: IdleAct = 'rest'
        let ranMs = 0
        let goal: Pt = s.at
        // Its wait for the others is over when it no longer needs him: its next ask waits afresh.
        if (!s.forward) m.waitFrom = 0
        if (lifeOn && s.does === 'idle' && !s.moved && !held && !quietHere && !m.carry) {
          // A test's switch makes every idle Bot visit and come back by turns; else each has beats of its own.
          const beat = crewStroll.all ? Math.floor(now / crewStroll.everyMs) : -1
          const pick = crewStroll.all ? { act: (beat % 2 === 0 ? 'visit' : 'rest') as IdleAct, beat, ranMs: now - beat * crewStroll.everyMs } : idleAct(bot.slug, now)
          act = pick.act
          ranMs = pick.ranMs
          if (act === 'visit') {
            // Where it visits is picked once, as the beat begins, and only while fewer than two Bots walk.
            if (m.life.beat !== pick.beat) m.life = { beat: pick.beat, spot: moving < LIFE.mostMoving ? visitSpot(i, visitAsk()) : null }
            // The room changed under it (an ask came, a page opened, Home grew): the visit ends at once, for this beat.
            if (m.life.spot && !visitHolds(m.life.spot, holdNow)) m.life.spot = null
            if (m.life.spot) goal = m.life.spot
            else act = 'rest'
          }
        }
        if (act !== 'visit') m.life.spot = null
        m.act = act
        let heading: number | null = null
        if (m.carry) { body.copy(m.carry); if (m.walk) { m.walk = null; moving -= 1 } }
        else {
          if (!m.shown) m.shown = goal
          // A page that opened while it walks: the rest of its way is checked against it. Where the way crosses it,
          // the Bot goes round from where it is, or stops short and changes spot with the fade; it never walks through.
          if (m.walk && m.walk.pages !== pagesKey) {
            m.walk.pages = pagesKey
            const t = (now - m.walk.t0) / m.walk.ms
            if (crossesPage(m.walk.way, t, room?.pages ?? [])) {
              moving -= 1
              m.walk = null
              m.shown = [body.x, body.y, body.z]
              // A stopped walk is no new walk for the cap of two: it goes on at once, so it never stands between spots.
              m.resume = true
            }
          }
          const dest = m.walk ? m.walk.to : m.swapTo ?? m.shown
          if (!nearPt(dest, goal)) {
            if (still) { m.walk = null; m.swapTo = null; m.shown = goal }
            else if (m.swapTo) m.swapTo = goal
            // The Bots that give way to the one that needs him go first: it sets out once they stand aside (or
            // after `LIFE.yieldMs`), so that it walks through nobody and no face is under its board when it arrives.
            // The others wait until fewer than two walk.
            else if (s.forward && !m.walk && yielding() && now - (m.waitFrom ||= now) < LIFE.yieldMs) { /* it waits */ }
            // The one in front goes at once, and so does a Bot that gives way or that stands under what covers things
            // now: past the cap of two, so that nobody stands under the board while two others walk.
            // A Bot that waits for the quiet middle searches its way again only when its goal, the pages or the quiet change.
            else if (!m.walk && m.tried === triedKey(goal)) { /* it waits */ }
            else if (s.forward || m.walk || m.resume || s.moved || coversNow.some(c => onBox(m.shown as Pt, c)) || moving < LIFE.mostMoving) {
              m.waitFrom = 0
              m.resume = false
              m.tried = ''
              const from: Pt = m.walk ? [body.x, body.y, body.z] : m.shown
              // The quiet middle holds no walk of the one in front, which is meant to end there, and no walk that is
              // under way: it finishes, so that nobody is left between spots.
              // Past the bodies where they stand and where they are going, and over no face as he sees them.
              const ask = { bodies: bots.flatMap((_, j) => (j === i ? [] : [bodies[j], ...going[j]])), pages: room?.pages ?? [], quiet: quietFor(quiet, s.forward, !!m.walk), faces: true }
              const way = walkWay(from, goal, ask)
              if (way) { if (!m.walk) moving += 1; m.walk = { way, to: goal, t0: now, ms: walkMs(way), pages: pagesKey } }
              // Only the middle of his look is in the way, while he reads or holds a board: it waits where it is
              // until he is done, and walks then. A fade there would move in the middle too.
              else if (quiet && walkWay(from, goal, { ...ask, quiet: null })) { if (m.walk) { moving -= 1; m.walk = null; m.shown = from } m.tried = triedKey(goal) }
              else { if (m.walk) moving -= 1; m.walk = null; m.shown = from; m.swapTo = goal; m.swapAt = now }
            }
          }
          if (m.walk) {
            // A pointer on it holds it where it is: the walk goes on from there.
            if (held) m.walk.t0 += now - lastFrame.current
            const t = (now - m.walk.t0) / m.walk.ms
            const step = walkAt(m.walk.way, t)
            body.set(step.at[0], step.at[1], step.at[2])
            heading = step.yaw
            if (t >= 1) { m.shown = m.walk.to; m.walk = null; moving -= 1 }
          } else {
            if (m.swapTo) {
              const t = Math.min(1, (now - m.swapAt) / PLACES.swapMs)
              if (t >= 0.5) m.shown = m.swapTo
              grow = Math.abs(1 - 2 * t)
              if (t >= 1) m.swapTo = null
            }
            // D192 1: a Bot under a pointer, or whose resting tray is (`trayHeld`), holds its drift where it is, so the
            // tray it carries stands under his dot; let go, the drift goes on from there with no jump (`driftStep`).
            m.drift = driftStep(m.drift ?? { t: now, rate: 1 }, held || trayHeld.has(bot.slug), now - lastFrame.current)
            const spot = s.does === 'idle' && !still && !quietHere ? driftAt(m.shown, driftNow(bot.slug, m.drift.t)) : m.shown
            body.set(spot[0], spot[1], spot[2])
          }
        }
        walkYaw = heading
        // A look around turns the body to one side and back; a stretch stands it tall for a moment.
        if (!m.walk && act === 'look' && ranMs < LIFE.turnMs) aside = (seedOf(`${bot.slug}:side`) % 2 ? 1 : -1) * LIFE.turnRad * Math.sin((Math.PI * ranMs) / LIFE.turnMs)
        stretching = !m.walk && act === 'stretch' && ranMs < LIFE.stretchMs
        // Its desk is drawn from when it first stands at it, and stays while it is out (in front, on a visit, on a
        // walk). It goes when the Bot gives way or is lifted, and comes back only when the Bot stands at it again.
        const atHome = m.drive.drawn && !!m.shown && !m.swapTo && !m.carry && !m.walk && !!homes && nearPt(m.shown, homes[i])
        const homed = atHome ? !s.desk.hidden : s.desk.hidden || !!m.carry || !m.drive.drawn ? false : m.homed
        if (homed !== m.homed) { m.homed = homed; stood = true }
        // Asleep: by its own beat while it lives; with reduced motion, or for a still picture, as the rule of places says.
        m.drive.sleep((lifeOn ? act === 'nap' : s.nap) && !held && !m.walk)
        leaning = s.does === 'work' && !still && !held && !quietHere && !m.walk && leansNow(bot.slug, now)
      }
      else if (m.carry) body.copy(m.carry)
      // Put down: from where it was let go, it walks to the place it got.
      else if (m.landing) { m.drive.walkTo(home.set(...m.landing), undefined, STROLL.homeMps); m.landing = null; m.away = true; m.goal = 'home'; m.target = null; body.y = y }
      else if (held) { if (m.away) body.y = y; else body.set(x, y, z) }
      else {
        // A Bot at its place stands there before any walk of its own begins: a walk starts from its place.
        if (!m.away) body.set(x, y, z)
        const g = goalOf(bot, m, i, now)
        if (g.key !== m.goal) { m.goal = g.key; m.lifeAt = now; m.away = true; m.drive.walkTo(home.set(...g.at), undefined, g.key === 'home' ? STROLL.homeMps : STROLL.speedMps) }
        if (!m.drive.walking && m.goal === 'home') { body.set(x, y, z); m.away = false }
        else body.y = y
        m.drive.sleep(g.nap && !m.drive.walking)
        // In a full row the look around shows in place: out to one side and back, now and then with a lean or a hop.
        const p = (now - m.lifeAt) / STROLL.turnMs
        if (g.turn && p < 1) {
          aside = g.turn * Math.sin(Math.PI * p)
          const kind = seedOf(`${bot.slug}:${g.key}`) % 3
          leaning = kind === 1
          if (kind === 2 && p >= 0.5 && m.hopFor !== g.key) { m.hopFor = g.key; m.hops += 1; m.drive.hop() }
        }
      }
      m.drive.setPose(leaning ? 'think' : stretching ? 'stand' : null)
      // Everything that rides with the body stands where the body is, in this frame. The walk's own step moves it
      // too (`BotDrive.riders`), so it never trails a body that walks, whichever frame callback runs first.
      m.parts?.position.copy(body)
      m.grow = grow
      m.drive.root.scale.setScalar((size.heightM / BOT_H) * Math.max(0.001, grow))
      m.drive.setRest((walkYaw !== null ? walkYaw : s && !held && s.face !== null ? s.face : yawOf(i)) + aside)
      m.drive.update(0, now, still)
      // The room's own walk and his hand (in the room and in a full row) put the body each frame: the Bot moves as one
      // that walks (M90). Its sway in place (`driftAt`) and a room laid out again are no move; a full row's own walk
      // is `walkTo`'s.
      m.drive.setMoved(!!m.walk || !!m.carry)
      // It turns to the person while it answers a tap.
      // And while he holds it to speak (P2) it leans in, eyes on him; with reduced motion it only faces him, as at rest.
      m.drive.listen(now < m.until || (!still && p?.holding && p.slug === bot.slug) ? head : null)
      // No hop in the quiet middle while he reads a page or holds a board.
      if (known && bot.state === 'needs' && !still && !(quiet && inQuiet([body.x, body.y, body.z], quiet)) && now - m.hopAt > HOP_EVERY_MS) { m.hopAt = now; m.hops += 1; m.drive.hop() }
    }
    lastFrame.current = now
    if (stood) setStood(n => n + 1)
  })

  useEffect(() => {
    if (!report) return
    report(() => bots.flatMap((bot, i) => {
      const place = places[i]
      const m = members.current.get(bot.slug)
      if (!place || !m) return []
      const feet = bodyAt(m, i)
      const row = desk ? seatAt(i)?.row ?? 0 : place.row
      return [{ slug: bot.slug, name: bot.label, key: bot.name, state: bot.state, lit: litNow.current === bot.slug, since: !!sinceNow.current?.lit.has(bot.slug), pool: !desk && !!sinceNow.current?.lit.has(bot.slug) && !staged?.[i]?.forward, row, feet, top: [feet[0], feet[1] + size.heightM * (1 + CREW.hoverShare), feet[2]] as [number, number, number], widthM: desk ? DESK.padM : size.heightM * CREW.widthShare, taps: m.taps, hops: m.hops, drawn: !!m.look, shown: knownNow.current, unread: bot.unread, dim: bot.dim, seat: !!desk, sign: litNow.current === bot.slug || (!desk && knownNow.current && !!plates?.has(bot.slug)), carried: !!m.carry, pressing: press.current?.slug === bot.slug ? { dragging: press.current.dragging, lifted: press.current.lifted, holding: !!press.current.holding, type: press.current.type ?? '', at: press.current.at, held: press.current.obj.hasPointerCapture(press.current.pointerId) } : null, litBy: litNow.current === bot.slug ? litBy.current : null, boxEvents: [...(boxLog.current.get(bot.slug) ?? [])], visibility: [...seenLog.current], flip: m.look?.flip, note: notesNow.current?.get(bot.slug), noteLast: noteLastOf(m.parts), goal: m.goal, parts: m.parts ? [m.parts.position.x, m.parts.position.y, m.parts.position.z] : undefined, ...(staged?.[i] ? { does: staged[i].does, forward: staged[i].forward, desk: { at: staged[i].desk.at, lit: staged[i].desk.lit, hidden: staged[i].desk.hidden, drawn: deskDrawn(i) }, grow: m.grow, walking: !!m.walk, act: m.act, moved: staged[i].moved, pageSign: staged[i].sign ?? undefined } : {}) }]
    }))
    return () => report(null)
    // `feetOf` reads the desk and the places that are named here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report, bots, places, seats, desk, seatOf, plates, staged])

  /** The press on `slug` was held still long enough: the Bot is lifted, and the press carries it from now on. */
  const lift = (slug: string, fromHold = false) => {
    const p = press.current
    const m = members.current.get(slug)
    const i = at.get(slug)
    // A Bot of the second row keeps its place. shortcut: carry it along its own arc when a crew of 13 is dragged.
    // A hold that moves is the drag: its move is what lifts it.
    if (!p || p.slug !== slug || p.dragging || (p.moved && !fromHold) || p.lifted || !m || i === undefined || hushedNow.current || (!desk && places[i]?.row !== 0)) return
    p.holding = false
    p.lifted = true
    m.drive.halt()
    m.away = false
    m.carry = m.drive.root.position.clone()
    m.reach = reachOf([m.carry.x, m.carry.y, m.carry.z])
    m.carry.y += LIFT_M
    // In a session a ray that leaves the Bot no longer ends the press: the press holds it until it is let go.
    if (!drag) p.obj.setPointerCapture(p.pointerId)
    setLit(slug)
  }
  /** Where the press's ray carries the lifted Bot: along its row at the row's height, or over the desk's top. */
  const carryTo = (e: ThreeEvent<PointerEvent>, slug: string) => {
    const m = members.current.get(slug)
    const i = at.get(slug)
    const ev = e as unknown as { point?: Vector3; pointerPosition?: Vector3 }
    if (!m?.carry || i === undefined || !ev.point || !ev.pointerPosition) return
    const o = ev.pointerPosition
    const d = ray.copy(ev.point).sub(o)
    if (desk) {
      const y = feetOf(i)[1]
      if (Math.abs(d.y) < 1e-6) return
      const t = (y - o.y) / d.y
      if (t > 0) m.carry.set(o.x + t * d.x, y + LIFT_M, o.z + t * d.z)
      return
    }
    if (staged) {
      // With no row a drag keeps the Bot's distance from him: it moves over the sphere its middle is on.
      const mid = onSphere([o.x, o.y, o.z], [d.x, d.y, d.z], m.reach)
      if (mid) m.carry.set(mid[0], mid[1] - MID_M + LIFT_M, mid[2])
      return
    }
    const place = places[i]
    const yaw = place && yawOnArc([o.x, o.y, o.z], [d.x, d.y, d.z], place.radiusM)
    if (!place || yaw === null || yaw === undefined) return
    const [x, y, z] = crewPoint({ ...place, yawDeg: Math.max(CREW_BAND[0], Math.min(CREW_BAND[1], yaw)) })
    m.carry.set(x, y + LIFT_M, z)
  }
  /**
   * The lifted Bot is let go. In the row it gets the place where it was let go, or the free place nearest it when
   * that is nearer than one step to another Bot; nobody else moves. At the desk it sits on the nearest seat, and
   * the Bot there takes its seat; let go off the desk, it goes back to its own. `keep` false: it goes back.
   */
  const putDown = (slug: string, keep = true) => {
    const m = members.current.get(slug)
    const i = at.get(slug)
    const where = m?.carry
    if (!m || !where || i === undefined) return
    m.carry = null
    m.drive.root.position.set(where.x, where.y - LIFT_M, where.z)
    if (!keep) { m.away = true; return }
    if (staged) {
      // With no row it gets the place where it was let go, and its desk comes with it; nobody else moves.
      const p = keptPoint([where.x, where.y - LIFT_M, where.z])
      room?.onPut(slug, p, bots[i].state)
      m.shown = p
      m.swapTo = null
      return
    }
    if (desk) {
      const pads = seatPlaces(spots ?? bots.length).map(seat => deskPoint(desk, seat.u, seat.v))
      let best = -1
      let near = SEAT_DROP_M
      pads.forEach(([x, , z], j) => { const dist = Math.hypot(x - where.x, z - where.z); if (dist <= near) { near = dist; best = j } })
      if (best >= 0 && best !== (seatOf?.get(slug) ?? i)) { onSeat?.(slug, best); m.landing = pads[best] }
      else m.away = true
      return
    }
    const place = places[i]
    if (!place) { m.away = true; return }
    const taken = places.filter((p, j) => j !== i && p.row === place.row).map(p => p.yawDeg)
    const yawDeg = freeYaw(Math.atan2(where.x, -where.z) / (Math.PI / 180), taken, crewStepDeg(place.radiusM))
    // No free place left in the row: it goes back to its own, and nothing is kept.
    if (yawDeg === null) { m.away = true; return }
    // Every other Bot of its row keeps the place it has now: a place it stood at by the row's order only would
    // move it into the gap that this one left.
    const row: Record<string, number> = {}
    places.forEach((p, j) => { if (j !== i && p.row === place.row && bots[j]) row[bots[j].slug] = p.yawDeg })
    row[slug] = yawDeg
    onPlace?.(row)
    m.landing = crewPoint({ ...place, yawDeg })
  }

  const tap = (slug: string) => {
    const m = members.current.get(slug)
    if (!m) return
    m.taps += 1
    m.until = performance.now() + ANSWER_MS
    if (!still) { m.hops += 1; m.drive.hop() }
    setLit(slug)
    const dark = setTimeout(() => { timers.current.delete(dark); setLit(now => (now === slug && press.current?.slug !== slug ? '' : now)) }, ANSWER_MS)
    timers.current.add(dark)
    const open = setTimeout(() => { timers.current.delete(open); onTap(slug); since?.onTap(slug) }, OPEN_AFTER_MS)
    timers.current.add(open)
  }

  // At the desk each seat's pad is a target of a finger's touch (`deskTouch.ts`), pressed as a tap on its Bot.
  useEffect(() => {
    if (!desk || hushed) return
    const ids = bots.map((bot, i) => {
      const id = `seat:${bot.slug}`
      touchTargets.set(id, { center: feetOf(i), radiusM: DESK.padM / 2, press: () => tap(bot.slug), box: { halfM: DESK.padM / 2, heightM: DESK.botM * 1.4, yawDeg: desk.yawDeg } })
      return id
    })
    return () => { for (const id of ids) touchTargets.delete(id) }
  })

  return (
    <group pointerEvents={hushed ? 'none' : undefined}>
      {/* With no row: every Bot's desk, where its place is, lit in its colour while it works there. */}
      {staged && <PlaceDesks desks={bots.map((bot, i) => ({ key: bot.slug, ...staged[i].desk, hidden: !deskDrawn(i), tint: members.current.get(bot.slug)?.look?.tint ?? XR_GL.faint }))} />}
      {bots.map((bot, i) => {
        const place = places[i]
        const m = members.current.get(bot.slug)
        if (!place || !m) return null
        const on = lit === bot.slug
        const shown: BotState = known ? bot.state : 'idle'
        return (
          <group key={bot.slug}>
            {/* With no row: where it could not come forward, its ask's sign at the edge of the page he reads. */}
            {staged?.[i]?.sign && known && (
              <group position={staged[i].sign ?? undefined}>
                <Sign text={i18nT('xr.ask_sign', { name: bot.label, count: pageSignCount(bot.needs) })} yaw={Math.atan2(-(staged[i].sign?.[0] ?? 0), -(staged[i].sign?.[2] ?? 0))} topM={0} />
              </group>
            )}
            {m.look && <Body world={stage} node={{ bot }} look={known ? m.look : dimLook(m)} drive={m.drive} bare />}
            <group ref={g => { if (m.parts) m.drive.riders.delete(m.parts); m.parts = g; if (g) m.drive.riders.add(g) }} position={bodyAt(m, i)}>
              {desk && <Pad state={shown} color={colors.state.needs} unread={known && bot.unread} asks={known && bot.state === 'needs' && !boarded?.has(bot.slug)} yaw={yawOf(i)} />}
              <Ring state={shown} color={colors.state[bot.state]} lit={on} still={still} size={size} dim={known && bot.dim} cut={colors.state.failed} yaw={yawOf(i)} />
              {/* A Bot that changed since he left stands in a warm pool of light: "look over here". Not the one in front: he looks at it already, and it has its ring and its board. */}
              {known && !desk && since?.lit.has(bot.slug) && !staged?.[i]?.forward && <SincePool yaw={yawOf(i)} still={still} />}
              {/* The sign: the name while a pointer lights the Bot; while it asks, the name and how many asks wait, with no pointer. */}
              {/* At a seat only the name, and only while the seat is lit: at rest the desk shows faces and states. */}
              {desk && notes?.has(bot.slug) && <BotNote text={notes.get(bot.slug) ?? ''} yaw={yawOf(i)} nameM={SIGN.widthM} scale={SEAT_SIGN * NOTE_SIGN.scale} topM={DESK.botM * 1.4} overM={SIGN_OVER_TOP_M} outM={(SIGN.widthM / 2) * SEAT_SIGN + 0.012} />}
              {desk && on && !notes?.has(bot.slug) && <Sign text={bot.label} yaw={yawOf(i)} scale={SEAT_SIGN} topM={DESK.botM * 1.4} />}
              {!desk && notes?.has(bot.slug) && <BotNote text={notes.get(bot.slug) ?? ''} yaw={yawOf(i)} nameM={SIGN.widthM} scale={NOTE_SIGN.scale} topM={CREW.heightM * (1 + CREW.hoverShare)} overM={staged ? PLACES.signOverM : SIGN_OVER_TOP_M} outM={staged ? 0 : SIGN.widthM / 2 + 0.012} />}
              {!desk && !notes?.has(bot.slug) && (known && plates?.has(bot.slug)
                ? <Sign text={i18nT('xr.ask_sign', { name: bot.label, count: plates.get(bot.slug) ?? 0 })} yaw={yawOf(i)} overM={staged ? PLACES.signOverM : undefined} />
                : on && <Sign text={bot.label} yaw={yawOf(i)} overM={staged ? PLACES.signOverM : undefined} />)}
              {/* What a pointer meets: a thin plate as wide and as tall as the body, turned to the person. It is never drawn. */}
              {/* At a seat the pad and the body take a pointer together, as one upright target (the dot's aim is coarse). */}
              <mesh
                position-y={desk ? (DESK.botM * 1.4) / 2 : CREW.heightM * (CREW.hoverShare + 0.5)}
                rotation-y={yawOf(i)}
                scale={desk ? [DESK.padM, DESK.botM * 1.4, DESK.padM] : [CREW.heightM * CREW.widthShare, CREW.heightM, 0.02]}
                userData={{ xrPress: 'bot', xrBot: bot.slug }}
                onPointerEnter={(e: ThreeEvent<PointerEvent>) => { logBox(bot.slug, e); if (e.pointerType !== 'screen-touch' && !press.current?.dragging) { litBy.current = e.pointerType; setLit(bot.slug) } }}
                onPointerDown={(e: ThreeEvent<PointerEvent>) => {
                  logBox(bot.slug, e)
                  e.stopPropagation()
                  // Another press (a second hand) ends a hold that is still on: it is cut off, and nothing is sent.
                  const was = press.current
                  if (was?.holding) { was.holding = false; onHold?.(was.slug, 'cancel') }
                  press.current = { pointerId: e.pointerId, slug: bot.slug, at: [e.nativeEvent.clientX, e.nativeEvent.clientY], dragging: false, lifted: false, moved: false, obj: e.object as Object3D, type: e.pointerType }
                  // Held still this long, the press lifts the Bot (a quick move before it turns the picture, below).
                  const began = press.current
                  // With `onHold` the press held still is a hold instead, and a move after it lifts the Bot (below).
                  const lifting = setTimeout(() => {
                    timers.current.delete(lifting)
                    if (press.current !== began) return
                    if (!onHold) { lift(bot.slug); return }
                    if (began.dragging || began.moved || hushedNow.current) return
                    began.holding = true
                    // The hold keeps its pointer in both modes, so its moves and its release come here wherever they land.
                    began.obj.setPointerCapture(began.pointerId)
                    onHold(bot.slug, 'start')
                  }, LIFT_MS)
                  timers.current.add(lifting)
                  // In the flat preview the press stays this Bot's until it ends: when it moves it becomes a drag (below).
                  if (drag) (e.object as Object3D).setPointerCapture(e.pointerId)
                  setLit(bot.slug)
                }}
                onPointerMove={(e: ThreeEvent<PointerEvent>) => {
                  const p = press.current
                  // A hold that moves past the drag's measure is the drag (D10): the hold ends, and the Bot goes with
                  // the hand from this very move.
                  if (p?.pointerId === e.pointerId && p.holding && (drag ? Math.hypot(e.nativeEvent.clientX - p.at[0], e.nativeEvent.clientY - p.at[1]) > TURN_DRAG_PX : p.moved)) {
                    onHold?.(p.slug, 'drag')
                    lift(p.slug, true)
                  }
                  if (p?.pointerId === e.pointerId && p.lifted) { e.stopPropagation(); carryTo(e, p.slug); return }
                  if (p?.pointerId === e.pointerId && p.holding) { e.stopPropagation(); return }
                  if (!drag || p?.pointerId !== e.pointerId) return
                  e.stopPropagation()
                  if (!p.dragging) {
                    if (Math.hypot(e.nativeEvent.clientX - p.at[0], e.nativeEvent.clientY - p.at[1]) <= TURN_DRAG_PX) return
                    // A press that moves is no tap: it is the drag that empty space would have had, from the place
                    // where the finger came down. The Bot's light goes out.
                    p.dragging = true
                    setLit(now => (now === bot.slug ? '' : now))
                    drag.onPointerDown({ pointerId: e.pointerId, object: e.object, nativeEvent: { clientX: p.at[0], clientY: p.at[1] } } as unknown as ThreeEvent<PointerEvent>)
                  }
                  drag.onPointerMove(e)
                }}
                onPointerUp={(e: ThreeEvent<PointerEvent>) => {
                  logBox(bot.slug, e)
                  e.stopPropagation()
                  const began = press.current
                  press.current = null
                  // His release of a hold: the sheet goes down, and when his words went it nods once (none with reduced motion).
                  if (began?.pointerId === e.pointerId && began.holding) { if (onHold?.(began.slug, 'let') === true && !still) members.current.get(began.slug)?.drive.nod(); return }
                  if (began?.pointerId === e.pointerId && began.lifted) { putDown(began.slug); return }
                  if (began?.pointerId === e.pointerId && began.dragging) { drag?.onPointerUp(e); return }
                  if (began?.pointerId === e.pointerId && began.slug === bot.slug) tap(bot.slug)
                  else setLit(now => (now === bot.slug && e.pointerType === 'screen-touch' ? '' : now))
                }}
                onPointerLeave={(e: ThreeEvent<PointerEvent>) => {
                  logBox(bot.slug, e)
                  // In a session a press that leaves the Bot is no tap (`leaveEndsPress`). (In the preview the press is held until it ends.)
                  if (leaveEndsPress(press.current, e.pointerId, { drag: !!drag })) press.current = null
                  const m2 = members.current.get(bot.slug)
                  if (press.current?.slug !== bot.slug && (!m2 || performance.now() >= m2.until)) setLit(now => (now === bot.slug ? '' : now))
                }}
                onPointerCancel={(e: ThreeEvent<PointerEvent>) => {
                  logBox(bot.slug, e)
                  const began = press.current
                  // A pinch held still ends as a cancel in a session (`hands.tsx`): for a lifted Bot it is the put-down.
                  // A hold ends as his release only when the cancel is his long pinch's own end; else nothing is sent.
                  if (began?.pointerId === e.pointerId) { press.current = null; if (began.holding) { if (onHold?.(began.slug, pinchRoute.ending === 'long' ? 'let' : 'cancel') === true && !still) members.current.get(began.slug)?.drive.nod() } else if (began.lifted) putDown(began.slug); else if (began.dragging) drag?.onPointerCancel(e) }
                  setLit(now => (now === bot.slug ? '' : now))
                }}
                // The wheel over a Bot does what it does over empty space, and goes no farther: not to a page that a hand stood behind the Bot.
                onWheel={(e: ThreeEvent<WheelEvent>) => { e.stopPropagation(); drag?.onWheel(e) }}
              >
                <boxGeometry />
                <meshBasicMaterial visible={false} />
              </mesh>
            </group>
          </group>
        )
      })}
    </group>
  )
}
