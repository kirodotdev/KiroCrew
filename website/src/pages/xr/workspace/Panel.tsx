/**
 * One page on a panel in the room: a flat rectangle that a hand moves by its edge and resizes by its corner.
 *
 * The picture comes from a source (`source.ts`). The panel knows nothing of what makes it. It draws the page at
 * the size the source says, in metres (`M_PER_PX`), so text keeps its size whatever the panel's size is: a bigger
 * panel is more page, never a stretched one.
 *
 * Hands, as Android XR's own windows work:
 *   - the EDGE lights when a ray is on it; pinch, hold and drag moves the panel. A hand pushed forward sends the
 *     panel farther and a hand pulled back brings it nearer, and the panel keeps facing the person.
 *   - a CORNER lights the same way; pinch, hold and drag resizes the page. The opposite corner stays put.
 *   - inside the page the ray is the page's own pointer: a pinch is a click, a pinch that is dragged scrolls.
 *   - the right end of the top edge carries a small close mark while the edge is lit; a pinch on it closes the page.
 *
 * `@react-three/handle` holds each drag: it takes the pointer, keeps the held point on the ray, and says when the
 * drag begins and ends. Two things on top are the page's own, in `move` below: how a hand's reach changes the
 * panel's distance, and the turn to face the person.
 */
import { Handle, type HandleState } from '@react-three/handle'
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { createRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { Box3, BufferGeometry, CanvasTexture, Float32BufferAttribute, Frustum, LinearFilter, LinearMipmapLinearFilter, Matrix4, Quaternion, SRGBColorSpace, Vector3, type Group, type Mesh, type MeshBasicMaterial, type Object3D, type Texture } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { CLOSE_MARK, closeSize, pressCloses, shownAfterPress } from './closeMark'
import { homePlace, lookAt, underStrip } from './homePlace'
import { dotHold, dragFor, inHisFrame, sizeFactor } from './carry'
import { M_PER_PX, PAGES, depthScale, dragCorner, eyePxPerCssPx, facing, sizeFrom, type Rect } from './layout'
import { PagePress, type PageEvent } from './pagePress'
import { sharper, sharperKey } from './pageShader'
import type { LayerShare } from './Layers'
import type { LayerPanel } from './pageLayer'
import { MOST_LIVE_PAGES, type PageFocus, type PageNote, type PageSource } from './source'
import { offLook } from './streamWatch'
import { useKeptPicture } from './keptPicture'
import { pinchRoute } from './pinchRoute'
import { PRESS_RING, placePressRing } from './pressRing'
import { roomSees } from './roomSees'
import { reachInRoom } from './walls'

/** How the page's picture is filtered. Both on is the sharpest at a slant; the test hook turns each off to measure it. */
export interface Quality { mipmaps: boolean; anisotropy: boolean }

/** How light a page that takes no input yet is drawn (D49, D50). */
const FRESH_DIM = 0.6
/** Scratch for near and far by the dot (a frame allocates nothing for it). */
const DEPTH_AT = { line: new Vector3() }
/** Scratch for where a corner held by the dot stands. */
const CORNER_AT = new Vector3()
/** One vector for the frame's close-area corners (a frame allocates nothing for them). */
const CLOSE_AT = new Vector3()

/** What a test reads of a panel: where it is, how big, what is lit and held, and where to aim a hand. */
export interface PanelProbe {
  id: string
  position: [number, number, number]
  /** How far the panel's middle is from the head, in metres. */
  distance: number
  /** The page's size on the panel, in metres and in its own CSS pixels. */
  size: [number, number]
  css: [number, number]
  /** The rectangle a hand asks for and the one the page is drawn at (m): they differ while a corner is held or after it. */
  rect: [number, number]
  shown: [number, number]
  /** Whether the picture drawn is the kept one, so a press on the page is dropped (M52). */
  kept: boolean
  /** What `kept` was made of at the last render: a kept picture, a held corner, a stretch, and how long ago the corner was taken. */
  keptBy: { picture: boolean; sizing: boolean; stretch: boolean; heldMs: number }
  lit: string
  /** Whether the frame last drawn showed the page dropping a press (dimmed, or the dot unlit): a down is judged by it (D50). */
  drops: boolean
  holding: string
  /** Whether the page draws its own mark of a pointer's place now (never for the head's dot, which is its own mark). */
  mark: boolean
  /** The point of the panel that a hand holds, in the panel's own plane, in metres from its middle. */
  held: [number, number] | null
  /** Which way the page's own right, up and front point in the room, and how far its front is turned from the head, in degrees. */
  right: [number, number, number]
  up: [number, number, number]
  turnedDeg: number
  /** Places to aim at, in the room: the page's middle, the middle of the top edge, the corner at the top right, and the close mark. */
  aim: { page: [number, number, number]; edge: [number, number, number]; corner: [number, number, number]; close: [number, number, number] }
  /** The close mark: the lower left and the upper right corner of the square that takes its press, in the room, and how big that square and the mark are, in metres. */
  closeMark: { box: [[number, number, number], [number, number, number]]; press: number; mark: number; atRest: boolean; restMs: number | null }
  /** Its frame is lit by a press on its Bot's seat. */
  flash?: boolean
}

/** What the panel says in one line under the page. */
export function noteText(note: PageNote): string {
  switch (note) {
    case 'guarded': return i18nT('xr.page_guarded')
    case 'refused': return i18nT('xr.page_refused')
    case 'dialog': return i18nT('xr.page_dialog')
    case 'picker': return i18nT('xr.page_picker')
    case 'stale': return i18nT('xr.page_press_stale')
    case 'host': return i18nT('xr.page_host', { scope: 'kernel.yama.ptrace_scope', setting: 'browser.xr_pages_accept_same_user_attach' })
    case 'elsewhere': return i18nT('xr.page_elsewhere')
    case 'signed-out': return i18nT('xr.page_signed_out')
    case 'failed': return i18nT('xr.page_failed')
    case 'waiting': return i18nT('xr.page_waiting')
    case 'no_picture': return i18nT('xr.page_no_picture')
    case 'off': return i18nT('xr.page_off')
    case 'full': return i18nT('xr.page_full', { most: MOST_LIVE_PAGES })
    default: return ''
  }
}

/**
 * The line's own picture: 1280 by 64 canvas pixels on a line 24 mm tall (`NOTE_LINE`), so 2.67 pixels a millimetre.
 * The largest letters that fit it are 16 mm (D91 5): a 42 px face, 15.75 mm, the page's own 16 px at 1.75 m being 24.
 */
const NOTE = { width: 1280, height: 64, cssPerPx: 0.5, font: 42 } as const
/**
 * The lowest a page's line may end under the eye with the head at rest (degrees): the lens ends at 20.35, and this
 * keeps 0.35 of it. A default page's line, 24 mm tall and 6 mm under its edge, ends 19.65 down (M41).
 */
const NOTE_LOWEST_DEG = 20
/** The line's size in the room (m): its 16 px letters need 24 mm, 6 mm under the page's edge (M41, D86 4). */
const NOTE_LINE = { heightM: 0.024, gapM: 0.006 } as const
/** How far past its flip a line over the top edge must come before it goes back under the page (degrees, M46). */
const NOTE_FLIP_BACK_DEG = 1
const NOTE_AT = new Vector3()
/**
 * How long a picture at a new size stands before it replaces the stretched one (ms, D90 3). The first pictures at a
 * new size are the page before its own layout follows (its old layout, small, in the upper part); in `hand6`'s film
 * at 10 frames a second they stood about 0.8 s after they came, so 600 ms showed two of them.
 */
const STRETCH_SETTLE_MS = 1000
/** The longest a held corner draws the kept picture instead of the live one (ms, D96 5). */
const KEPT_HELD_MOST_MS = 3000

/** A flat frame round a rectangle `w` by `h`, `t` thick: one mesh that covers no pixel of the page. */
function frameGeometry(w: number, h: number, t: number): BufferGeometry {
  const x = w / 2
  const y = h / 2
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute([-x - t, -y - t, 0, x + t, -y - t, 0, x + t, y + t, 0, -x - t, y + t, 0, -x, -y, 0, x, -y, 0, x, y, 0, -x, y, 0], 3))
  g.setIndex([0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7])
  return g
}

// Scratch for the view check: made once, used by every panel in turn.
const VIEW = { frustum: new Frustum(), matrix: new Matrix4(), box: new Box3(), corner: new Vector3(), ahead: new Vector3(), at: new Vector3() }
const SIGNS: readonly [number, number][] = [[-1, -1], [1, -1], [1, 1], [-1, 1]]

type Corner = { key: string; sx: 1 | -1; sy: 1 | -1 }
const CORNERS: readonly Corner[] = [{ key: 'ne', sx: 1, sy: 1 }, { key: 'nw', sx: -1, sy: 1 }, { key: 'se', sx: 1, sy: -1 }, { key: 'sw', sx: -1, sy: -1 }]
const MIN: readonly [number, number] = [PAGES.minCssWidth * M_PER_PX, PAGES.minCssHeight * M_PER_PX]
const MAX: readonly [number, number] = [PAGES.maxCssWidth * M_PER_PX, PAGES.maxCssHeight * M_PER_PX]
const tuple = (v: Vector3): [number, number, number] => [v.x, v.y, v.z]
/** A press on the edge moves the panel once the held point has left its place by this much: under it, it was a press. */
const EDGE_DRAG_M = 0.004
/** A move whose page's middle ends this close to where it stood at the down is a tremble: it places nothing (D78 2). */
const TREMBLE_M = 0.01

/** What a pointer event of the framework carries beyond the browser's own. */
type Pointed = ThreeEvent<PointerEvent> & { pointerId: number; pointOnFace?: Vector3 }

interface Props {
  id: string
  source: PageSource
  /** Where the panel opens. A hand moves it from there. */
  position: Vector3
  quality: Quality
  /** Where the person's head is, kept up to date by the workspace. */
  head: Vector3
  /** The sources that some panel draws now. The workspace frees a source only when it is not in here. */
  using: Set<PageSource>
  /** Hands the test hook a way to read this panel, and takes it back when the panel goes. */
  report: (id: string, read: (() => PanelProbe) | null) => void
  /** A hand has moved this panel or changed its size: from now on it is where the person wants it. */
  onPlaced?: (id: string) => void
  /** What the panels share with the layer path: a page that a layer shows draws its mesh for depth only. */
  layers?: LayerShare
  /** The panel's one line has changed: what it says now, or '' when it says nothing. */
  onNote?: (id: string, note: PageNote) => void
  /** A press ended on the page as a press, not as a scroll. `touch`: it was a finger on the flat preview's screen. */
  onPress?: (id: string, touch: boolean) => void
  /** Whether a press on this page would be dropped now by a hold that is not the old picture (an ask's, the quiet time after a close): the dot's light goes off, the page stays at full light (D53 2). */
  pressHeld?: (id: string) => boolean
  /**
   * Asked when a press ends on the page as a press: whether it goes to the page. With `false` it is called off:
   * the page hears nothing of it, and the keys stay where they were (`asks.ts`, `pressFate`).
   */
  mayPress?: (id: string) => boolean
  /** Asked for every notch of the wheel on the page: `false` and the page does not hear it. */
  mayWheel?: (id: string) => boolean
  /** The wheel turned on the page. */
  onWheelOn?: (id: string) => void
  /**
   * Whether a notch at this place (client px) still goes to the eye's nearness. With `deltaY` the notch is then taken
   * there, and the page does not hear it; a page drawn by the tab does not take the mouse itself meanwhile, so that
   * the browser hands the notch to the room and not to the page.
   */
  wheelLatched?: (x: number, y: number, deltaY?: number) => boolean
  /**
   * Which way the panel is turned where it opens. Left out, it faces the person. An ask's panel of the flat
   * preview stands square to the lens (`useAskPanels.ts`). A hand that moves the panel turns it to the person.
   */
  turned?: Quaternion
  /** The gateway said where the page's focus is, and it is not where it was. */
  onFocus?: (id: string, focus: PageFocus | null) => void
  /** A pinch or a tap on the close mark: the page is to go. */
  onClose?: (id: string) => void
  /**
   * The flat preview's quiet time (`previewLook.ts`). `quiet`: until when the pages are not where a touch was
   * aimed. `hushed`: that time runs now. The panel then takes no pointer at all, also not one that rests on it
   * while the pages move under it, and what was lit goes dark. In a session the panel takes every pointer,
   * whatever this says.
   */
  quiet?: MutableRefObject<number>
  hushed?: boolean
  /** The drawn room is on: a hand's push stops in front of its wall (`walls.ts`). */
  walled?: boolean
  /**
   * This is the page in front: the one nearest the picture's middle, in a session the one that the one light lies
   * on. Its close mark shows at rest, dim (`closeMark.ts`, `closesAtRest`).
   */
  markAtRest?: boolean
  /** A press on the seat of the Bot whose page this is: the frame lights for a moment, and nothing opens. */
  flash?: boolean
  /** A new `position` is not jumped to but glided to over this long (ms): a page that stays when he goes to another room. */
  glideMs?: number
  /**
   * On its way it keeps at least this far from the head (m): it passes behind the page he looks at, which stays on
   * top until the others have landed. 0: it glides straight.
   */
  glideBehindM?: number
  /**
   * The side of the page that is the seam of a pair standing as the tile put it (D69): that edge band is not there, so
   * the dot at rest on the seam takes no edge and lights nothing. The other three edges still light and carry.
   */
  seam?: 'left' | 'right' | null
}

export function Panel({ id, source, position, quality, head: headAt, using, report, onPlaced, layers, onNote, onPress, mayPress, pressHeld, mayWheel, onWheelOn, wheelLatched, turned: turnedTo, onFocus, onClose, quiet, hushed, walled, markAtRest = false, flash = false, glideMs = 0, glideBehindM = 0, seam = null }: Props) {
  const gl = useThree(s => s.gl)
  const three = useThree(s => s.get)
  // A page drawn by the tab asks the room whether a control it would snap to is uncovered (`roomSees.ts`).
  useEffect(() => {
    if (!source.seenBy) return
    source.seenBy((x, y) => { const m = page.current; const { scene, camera } = three(); const f = source.frame(); return !!m && roomSees(scene, camera, m, x, y, [f.cssWidth, f.cssHeight]) })
    return () => source.seenBy?.(null)
  }, [source, three])
  const group = useRef<Group>(null)
  const page = useRef<Mesh>(null)
  const mark = useRef<Mesh>(null)
  /** What a pinch would take on the page, lit before the pinch. */
  const aim = useRef<Mesh>(null)
  const pressRing = useRef<Mesh>(null)
  const aimPaint = useRef<MeshBasicMaterial>(null)
  /** The one line the panel says under the page. */
  const line = useRef<Mesh>(null)
  /** The line said now under the page, and its code. */
  const said = useRef<{ words: string; code: PageNote }>({ words: '', code: '' })
  /** Whether the line stands over the page's top edge now (M46): it keeps its place until it is 1 degree past. */
  const lineUp = useRef(false)
  const focusSaid = useRef<PageFocus | null>(null)
  const note = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = NOTE.width
    canvas.height = NOTE.height
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    return { canvas, texture }
  }, [])
  useEffect(() => () => note.texture.dispose(), [note])
  /** When the panel was last in view, and what the source was last told of it. */
  const view = useRef({ seenAt: 0, told: true, tick: 0 })
  // One knob for each corner: the thing the framework drags when a hand takes that corner.
  const knobs = useMemo(() => CORNERS.map(() => createRef<Group>()), [])
  const press = useMemo(() => new PagePress(), [])
  // The source says when its picture or its size is new.
  const [, setSeen] = useState(0)
  useEffect(() => source.subscribe(() => setSeen(n => n + 1)), [source])
  useLayoutEffect(() => { using.add(source); return () => { using.delete(source) } }, [using, source])
  // From its first frame: the dot's frame runs before the panels' (`Workspace.tsx`).
  useLayoutEffect(() => { if (source.notYet?.()) pinchRoute.notYet.add(id); return () => { pinchRoute.notYet.delete(id) } }, [id, source])
  useEffect(() => () => { homePlace.closeMarks.delete(id) }, [id])
  const frame = source.frame()
  const pageW = frame.cssWidth * M_PER_PX
  const pageH = frame.cssHeight * M_PER_PX

  /** The rectangle a hand asks for. Outside a resize it is the page's own. */
  const [rect, setRect] = useState<Rect>({ cx: 0, cy: 0, w: pageW, h: pageH })
  /** The corner a hand drags, while it does. */
  const [sizing, setSizing] = useState<Corner | null>(null)
  const [lit, setLit] = useState('')
  // The mouse's pointer goes when a session starts, and a hand's when it ends, and neither says that it left:
  // what it lit must not stay lit.
  const session = useXR(s => s.session)
  useEffect(() => setLit(''), [session])
  /** How thick the edge and how big the corner are in metres, for the angle they must hold at the eye. */
  /** And how big the close mark's press area and the mark itself are (`closeMark.ts`). */
  const [zone, setZone] = useState({ edge: 0.05, corner: 0.12, press: 0.1, mark: 0.025 })
  const holding = useRef('')
  const held = useRef<[number, number] | null>(null)
  const state = useRef({ rect, lit })
  state.current = { rect, lit }
  // The close mark is there to be seen while the edge is lit, or the mark itself. When it came to be seen is kept:
  // a press on it counts only after that (`closeMark.ts`).
  const showsClose = lit === 'edge' || lit === 'close'
  const closeSince = useRef<number | null>(null)
  useEffect(() => { closeSince.current = showsClose ? performance.now() : null }, [showsClose])
  /** When this page became the page in front, whose mark shows at rest; null while it is not. */
  const restSince = useRef<number | null>(null)
  /** The close mark's two bars: their colour is set in the frame itself, so it is white in exactly the frames in which a press closes (F2). */
  const markPaint = useRef<(MeshBasicMaterial | null)[]>([])
  /** A pointer is on the mark itself now (the shared `lit` can say "edge" while it is: both take the enter). */
  const onMark = useRef(false)
  /** The mark was drawn white in the last frame: only then does a press on it close (what is white takes the press). */
  const markWhite = useRef(false)
  useEffect(() => { restSince.current = markAtRest ? performance.now() : null }, [markAtRest])
  const markShows = showsClose || markAtRest
  /** Whether the frame last drawn showed this page dropping a press (dimmed, or the dot unlit): a down is judged by it. */
  const shownDrops = useRef(false)
  const flashNow = useRef(flash)
  flashNow.current = flash
  /** The pinch that began on the close mark, when the mark was there to be seen. */
  const closing = useRef<number | null>(null)
  /** What a finger lit goes out a little after the finger lifts. */
  const linger = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (linger.current) clearTimeout(linger.current) }, [])
  // The pages moved, or one closed: what was lit was lit for where the pages stood. (An edge in a hand stays lit.)
  useEffect(() => {
    if (!hushed || holding.current) return
    if (linger.current) { clearTimeout(linger.current); linger.current = null }
    setLit('')
  }, [hushed])

  /** The corner that was dragged last: when the page's size changes, the corner opposite stays where it is. */
  const anchor = useRef<Corner | null>(null)
  /** The last size drag was the head's dot: once it is let go the page turns to face him about its middle (D67). */
  const sizedByDot = useRef(false)
  /** The size (m) that drag asked for at its release: the turn waits for the picture at that size (Q1). */
  const askedAt = useRef<{ w: number; h: number } | null>(null)
  const placed = (r: Rect, a: Corner | null): Rect => (a ? sizeFrom(r, a.sx, a.sy, pageW, pageH) : { cx: r.cx, cy: r.cy, w: pageW, h: pageH })
  /**
   * A size drag never shows him the old page (D84 1): while a corner is held, and after the release until the picture
   * at the size he asked for comes, the picture he has is stretched to the rim. This is the picture's size (m) at
   * that release; null while the page is drawn at its picture's own size.
   */
  const [stretch, setStretch] = useState<{ w: number; h: number } | null>(null)
  /** When the picture at the asked size first came while the page was stretched; and a tick to look again. */
  const cameAt = useRef<number | null>(null)
  const [settled, setSettled] = useState(0)
  /**
   * The picture the page had when the corner was taken: it is the one drawn, stretched, while the corner is held and
   * until a picture at the new size has stood `STRETCH_SETTLE_MS` (D84 1, D90 3). The pictures at a size on the way
   * and the first ones at the new size (the page before its own layout follows) are never drawn.
   */
  const { kept: heldPicture, keep: keepPicture, drop: dropKept } = useKeptPicture(source)
  // Where the page is drawn: at the size the source gives, which may be another than the hand asks for.
  const shown = sizing || stretch ? rect : placed(rect, anchor.current)
  /** Whether the picture drawn now is the kept one, not the live one (read each frame by the press path). */
  const keptNow = useRef(false)
  /** When the corner was taken (the clock of `performance.now`). */
  const heldSince = useRef(0)
  // While the corner is held, the kept picture for at most `KEPT_HELD_MOST_MS`: past that the live one is drawn,
  // stretched, so that an approval that comes up on the page is not hidden for as long as he holds (D96 5).
  keptNow.current = heldPicture.current !== null && (sizing !== null ? performance.now() - heldSince.current < KEPT_HELD_MOST_MS : stretch !== null)
  // What the probe says `kept` was made of, as of this render.
  const sizingNow = useRef(false)
  sizingNow.current = sizing !== null
  const stretchNow = useRef(false)
  stretchNow.current = stretch !== null
  const shownAt = useRef(shown)
  shownAt.current = shown
  // No corner is held: the rectangle is the page's own, and the panel's middle is the middle of the page.
  useLayoutEffect(() => {
    const g = group.current
    if (sizing || !g) return
    // It turns for that drag only: until its picture comes at the size the drag asked for (it comes after the release),
    // then never again, so a page that the gateway lays out anew later is not turned by it (Q1).
    const turn = sizedByDot.current
    const asked = askedAt.current
    if (stretch) {
      const came = asked ? Math.abs(pageW - asked.w) < 0.003 && Math.abs(pageH - asked.h) < 0.003 : Math.abs(pageW - stretch.w) + Math.abs(pageH - stretch.h) > 1e-6
      if (!came) {
        cameAt.current = null
        // Its picture at the new size has not come: it stands stretched to the size he asked, about its own middle.
        if (Math.abs(rect.cx) + Math.abs(rect.cy) < 1e-6) return
        g.position.add(new Vector3(rect.cx, rect.cy, 0).applyQuaternion(g.quaternion))
        if (turn) facing(g.position, head(), g.quaternion)
        setRect({ cx: 0, cy: 0, w: rect.w, h: rect.h })
        return
      }
      // The first picture at the new size is the page caught while it lays itself out anew (its old content in the
      // upper part of the larger window, D90 3): the stretched one stays until a picture at that size has stood
      // `STRETCH_SETTLE_MS`, so he sees one picture, then the sharp one.
      // shortcut: a time, not the page's own word that its layout is done. The gateway now holds a new size's
      // pictures back for two of the page's frames (`panel_stream.py` `_let_page_lay_out`, D96 7); drop this wait
      // once the `letgo` row is green on a gateway that runs it.
      const now = performance.now()
      cameAt.current ??= now
      const left = STRETCH_SETTLE_MS - (now - cameAt.current)
      if (left > 0) { const t = setTimeout(() => setSettled(n => n + 1), left); return () => clearTimeout(t) }
      cameAt.current = null
      dropKept()
      setStretch(null)
    }
    if (turn && (!asked || (Math.abs(pageW - asked.w) < 0.003 && Math.abs(pageH - asked.h) < 0.003))) { sizedByDot.current = false; askedAt.current = null }
    const end = placed(rect, anchor.current)
    if (Math.abs(end.cx) + Math.abs(end.cy) + Math.abs(end.w - rect.w) + Math.abs(end.h - rect.h) < 1e-6) return
    g.position.add(new Vector3(end.cx, end.cy, 0).applyQuaternion(g.quaternion))
    // The opposite corner stayed while he sized it, so the page's middle went off the line that the panel faced. A
    // size made by the dot turns it to face him about that middle, at the distance it has (D67); a raised hand's ray
    // or a mouse keeps the turn it had.
    if (turn) facing(g.position, head(), g.quaternion)
    setRect({ cx: 0, cy: 0, w: end.w, h: end.h })
    // `placed` reads the page's size of this render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizing, rect, pageW, pageH, stretch, settled])

  const head = useCallback(() => headAt, [headAt])

  // It opens where it is told, facing the person. After that only a hand moves it, or a glide to a room's place.
  const glide = useRef<{ from: Vector3; to: Vector3; t0: number; ms: number; behindM: number } | null>(null)
  const stood = useRef(false)
  const glideNow = useRef(glideMs)
  glideNow.current = glideMs
  const behindNow = useRef(glideBehindM)
  behindNow.current = glideBehindM
  useLayoutEffect(() => {
    const g = group.current
    if (!g) return
    // A place given from outside (an open, a room, the tile) is the page's middle: a corner that a hand sized by
    // before no longer stays still when the page's size changes next (D70 3, M21).
    anchor.current = null
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
    if (stood.current && glideNow.current > 0 && !still && !g.position.equals(position)) { glide.current = { from: g.position.clone(), to: position.clone(), t0: performance.now(), ms: glideNow.current, behindM: behindNow.current }; return }
    stood.current = true
    glide.current = null
    g.position.copy(position)
    if (turnedTo) g.quaternion.copy(turnedTo)
    else facing(g.position, head(), g.quaternion)
  }, [position, head, turnedTo])

  // The picture is the source's; how it is filtered is the panel's.
  useLayoutEffect(() => {
    const t: Texture = frame.texture
    const anisotropy = quality.anisotropy ? gl.capabilities.getMaxAnisotropy() : 1
    if (source.beforeDraw) {
      // A source that writes its own pictures chooses when the small copies are worth making (`beforeDraw`).
      if (t.anisotropy !== anisotropy) { t.anisotropy = anisotropy; t.needsUpdate = true }
      return
    }
    const min = quality.mipmaps ? LinearMipmapLinearFilter : LinearFilter
    if (t.generateMipmaps === quality.mipmaps && t.minFilter === min && t.anisotropy === anisotropy) return
    t.generateMipmaps = quality.mipmaps
    t.minFilter = min
    t.magFilter = LinearFilter
    t.anisotropy = anisotropy
    t.needsUpdate = true
  }, [frame.texture, quality, gl, source])

  useFrame(({ camera, clock }) => {
    const g = group.current
    if (!g) return
    // A corner sized by the dot: the dot rides that corner as this frame draws it (the rect of the last render, not
    // the one the hand has just asked for), and is aimed again now, after the dot's own frame ran (X6, Q4).
    const sz = anchor.current
    if (holding.current === 'resize' && sizedByDot.current && sz) {
      const r = state.current.rect
      const w = g.localToWorld(CORNER_AT.set(r.cx + (sz.sx * r.w) / 2, r.cy + (sz.sy * r.h) / 2, 0))
      dotHold.corner = [w.x, w.y, w.z]
      dotHold.ride()
    }
    // Near and far by the dot (D67): held by its edge, the hand's travel toward him or away brings the page along the
    // held ray; the dot's ray does not turn meanwhile (`HeadAim.tsx`), so the framework sends no move for it.
    const dh = depthHold.current
    const holder = dh && holding.current === 'move' ? pinchRoute.owner : null
    const hand = holder ? pinchRoute.place() : null
    if (dh && holder && hand) {
      const d = dragFor(holder, inHisFrame([hand[0] - holder.from[0], hand[1] - holder.from[1], hand[2] - holder.from[2]], dotHold.yaw))
      if (d.kind === 'depth') {
        // The page's middle comes along the line from his eye to it, so it stays where he sees it, and faces him.
        const eye = head()
        const line = DEPTH_AT.line.copy(dh.middle).sub(eye)
        const length0 = line.length()
        line.normalize()
        const far = walledNow.current ? reachInRoom([eye.x, eye.y, eye.z], [line.x, line.y, line.z], PAGES.farM) : PAGES.farM
        const length = Math.max(PAGES.nearM, Math.min(far, length0 * d.factor))
        g.position.copy(eye).addScaledVector(line, length)
        facing(g.position, eye, g.quaternion)
        dh.moved = true
      }
    }
    const going = glide.current
    if (going) {
      const t = Math.min(1, (performance.now() - going.t0) / going.ms)
      g.position.lerpVectors(going.from, going.to, t * t * (3 - 2 * t))
      // Behind the page he looks at: out to that distance soon after it starts, back in as it lands.
      const far = g.position.distanceTo(headAt)
      if (going.behindM > far && far > 1e-6) g.position.sub(headAt).multiplyScalar((far + (going.behindM - far) * Math.min(1, Math.sin(Math.PI * t) * 2.5)) / far).add(headAt)
      if (!turnedTo) facing(g.position, headAt, g.quaternion)
      if (t >= 1) glide.current = null
    }
    const at = shownAt.current
    // A page outside the view, or behind the person, is told so: it need not make pictures nobody sees. This is
    // looked at before the picture is copied, so that a page that comes back into view is copied for this frame.
    // In a session each eye has its own camera, set from the headset's own pose; the page is in view when one eye sees it.
    if (source.visible || source.detail) {
      const eyes = gl.xr.isPresenting ? gl.xr.getCamera().cameras : [camera]
      VIEW.box.makeEmpty()
      for (const [sx, sy] of SIGNS) VIEW.box.expandByPoint(g.localToWorld(VIEW.corner.set(at.cx + (sx * at.w) / 2, at.cy + (sy * at.h) / 2, 0)))
      const ms = clock.elapsedTime * 1000
      const v = view.current
      for (const eye of eyes.length ? eyes : [camera]) {
        VIEW.frustum.setFromProjectionMatrix(VIEW.matrix.multiplyMatrices(eye.projectionMatrix, eye.matrixWorldInverse))
        if (VIEW.frustum.intersectsBox(VIEW.box)) { v.seenAt = ms; break }
      }
      const inView = ms - v.seenAt < PAGES.hiddenAfterMs
      if (inView !== v.told) { v.told = inView; source.visible?.(inView, true) }
      // How far it is from the middle of his look: of the pages that wait for a copy, the one he looks at goes first.
      if (inView && source.gaze) {
        // Measured in the page's own frame, to the point of the page nearest his look: a wide page counts where he
        // reads it, not only near its middle.
        const eye = eyes[0] ?? camera
        eye.getWorldPosition(VIEW.at)
        VIEW.corner.copy(VIEW.at).add(eye.getWorldDirection(VIEW.ahead))
        g.worldToLocal(VIEW.at)
        g.worldToLocal(VIEW.corner).sub(VIEW.at)
        const off = offLook(VIEW.at, VIEW.corner, at)
        source.gaze(off.deg, off.far)
      }
    }
    // A new picture goes into the texture now, before this frame's drawing starts.
    source.beforeDraw?.(gl, quality.mipmaps)
    // A page that the XR tab draws itself is told where it is drawn: the browser's own hit test then finds it.
    if (page.current) source.place?.(page.current, camera, gl)
    // A page that a layer shows is drawn by the headset's compositor, under the scene. Its mesh then writes depth
    // only: it cuts away what is behind the page, and a hand in front of the page still shows over it.
    const paint = page.current?.material as MeshBasicMaterial | undefined
    // A picture can come in a new texture: the first one takes the material in this frame, so the frame that has the
    // picture shows it (the `map` prop alone would bring it only at the next render). A picture of a new size waits for
    // that render, which brings its texture and the mesh's new size together; taken now, it would be drawn on the old
    // rectangle for a frame.
    const latest = source.frame()
    // While the kept picture is drawn, it stays the material's map: no newer picture takes it (D96 5).
    const show = keptNow.current && heldPicture.current ? heldPicture.current : latest.cssWidth === frame.cssWidth && latest.cssHeight === frame.cssHeight ? latest.texture : null
    if (paint && show && paint.map !== show) { paint.map = show; paint.needsUpdate = true }
    // A page that takes no input yet is drawn dimmed, at full light from the frame it takes input: a warm page taken
    // back until its fresh picture is in (D49), a new page until its first picture with content (D50).
    const notYet = source.notYet?.() ?? false
    // The head's dot reads it: it is drawn faint, not lit, over a page that takes nothing yet.
    // The dot is not lit over a page that would drop a press now: an old picture (dimmed too), or any other hold (D53 2).
    // While the kept picture is drawn the page he sees is not the page the gateway has: a press would land on a
    // layout he cannot see, so it is dropped as for a page not yet drawn (M52, D96 2).
    const dropsPress = notYet || keptNow.current || (pressHeld?.(id) ?? false)
    shownDrops.current = dropsPress
    if (dropsPress) pinchRoute.notYet.add(id); else pinchRoute.notYet.delete(id)
    const light = notYet ? FRESH_DIM : 1
    if (paint && paint.color.r !== light) paint.color.setScalar(light)
    const layered = layers?.onLayer.has(id) ?? false
    if (paint && paint.colorWrite === layered) paint.colorWrite = !layered
    // The edge and the corner hold the same angle at the eye at any distance.
    const d = g.position.distanceTo(head())
    const edge = Math.round(d * Math.tan((PAGES.edgeDeg * Math.PI) / 180) * 200) / 200
    const corner = Math.round(d * Math.tan((PAGES.cornerDeg * Math.PI) / 180) * 200) / 200
    // The close mark: as big as the edge makes it, and on a small screen as big as a finger needs.
    const lens = gl.xr.isPresenting ? gl.xr.getCamera().cameras[0] : null
    const tall = lens ? lens.viewport.w : gl.domElement.clientHeight
    const marks = closeSize(edge, (2 * d) / Math.max(1, tall * (lens ?? camera).projectionMatrix.elements[5]))
    // Rounded up, so that neither is ever a little under what a finger needs.
    const press = Math.ceil(marks.press * 200) / 200
    const mark = Math.ceil(marks.draw * 400) / 400
    if (edge !== zone.edge || corner !== zone.corner || press !== zone.press || mark !== zone.mark) setZone({ edge, corner, press, mark })
    // Where the close mark's press area is in the room, for Home's strip, which gives way to it (D54, `homePlace`).
    // The top right corner's handle joins it (D56 4): its upper arm begins where the mark's area ends, its arms reach
    // three quarters of a corner out and up, and a quarter down.
    const lo = g.localToWorld(CLOSE_AT.set(rect.cx + rect.w / 2 - corner / 4 - press, rect.cy + rect.h / 2 - corner / 4, 0))
    const loT = [lo.x, lo.y, lo.z] as const
    const hi = g.localToWorld(CLOSE_AT.set(rect.cx + rect.w / 2 + (3 * corner) / 4, rect.cy + rect.h / 2 + Math.max(press, (3 * corner) / 4), 0))
    homePlace.closeMarks.set(id, [loT, [hi.x, hi.y, hi.z]])

    // What a pinch would take lights before the pinch. A thing that grants an agent permission is greyed: a pinch does not take it.
    const lit = aim.current
    const target = source.target?.() ?? null
    if (lit) {
      // Nothing lights on a page that takes no input yet.
      lit.visible = target !== null && !dropsPress
      if (target) {
        const [left, top, w, h] = target.rect
        const f = source.frame()
        lit.position.set(at.cx + ((left + w / 2) / f.cssWidth - 0.5) * at.w, at.cy + (0.5 - (top + h / 2) / f.cssHeight) * at.h, 0.0015)
        lit.scale.set((w / f.cssWidth) * at.w, (h / f.cssHeight) * at.h, 1)
        const paint = aimPaint.current
        if (paint) { paint.color.setHex(target.guarded ? XR_GL.faint : XR_GL.white); paint.opacity = target.guarded ? 0.5 : 0.16 }
      }
    }
    // The mark at a press: a ring where the press was sent, until the page's picture answers it.
    placePressRing(pressRing.current, source.pressMark?.() ?? null, () => source.frame(), at)
    // The lit close mark turns white in the frame a press on it would close (F2).
    const litNow = state.current.lit
    const takes = onMark.current && pressCloses(restSince.current, closeSince.current, performance.now(), quiet?.current ?? 0)
    const markColour = takes ? XR_GL.white : onMark.current ? XR_GL.faint : litNow === 'edge' || litNow === 'close' ? XR_GL.ink : XR_GL.faint
    markWhite.current = takes
    // Ink on a lit edge is drawn a little see-through; white and faint are opaque.
    const markOpacity = markColour === XR_GL.ink ? 0.75 : 1
    for (const m of markPaint.current) { if (!m) continue; if (m.color.getHex() !== markColour) m.color.setHex(markColour); if (m.opacity !== markOpacity) m.opacity = markOpacity }
    // The one line under the page.
    const now = source.note?.() ?? ''
    const words = now ? source.noteWords?.() || noteText(now) : ''
    const text = line.current
    if (text && words !== said.current.words) {
      if (now !== said.current.code) onNote?.(id, now)
      said.current = { words, code: now }
      text.visible = now !== ''
      const c = now ? note.canvas.getContext('2d') : null
      if (c) {
        c.fillStyle = css(XR_GL.raised)
        c.fillRect(0, 0, NOTE.width, NOTE.height)
        c.fillStyle = css(XR_GL.ink)
        c.textBaseline = 'middle'
        c.textAlign = 'center'
        const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
        let size: number = NOTE.font
        do { c.font = `600 ${size}px ${family}`; size -= 2 } while (size > 18 && c.measureText(words).width > NOTE.width - 32)
        c.fillText(words, NOTE.width / 2, NOTE.height / 2 + 1)
        note.texture.needsUpdate = true
      }
    }
    if (text) {
      // Never over the page's picture (M41, D86 4): under the page's lower edge, at most as wide as the page; where it
      // would end lower than `NOTE_LOWEST_DEG` under the eye, over the page's upper edge instead, so it is whole in
      // the lens with the head at rest. It takes no pointer.
      const k = Math.min(1, at.w / (NOTE_LINE.heightM * (NOTE.width / NOTE.height)))
      const tallM = NOTE_LINE.heightM * k
      text.scale.set(tallM * (NOTE.width / NOTE.height), tallM, 1)
      const under = at.cy - at.h / 2 - NOTE_LINE.gapM - tallM / 2
      const over = at.cy + at.h / 2 + NOTE_LINE.gapM + tallM / 2
      const eye = head()
      const low = g.localToWorld(NOTE_AT.set(at.cx, under - tallM / 2, 0)).sub(eye)
      const lowDeg = (Math.atan2(low.y, Math.hypot(low.x, low.z)) * 180) / Math.PI
      // The place over the top edge is taken only when it is free (M46, D91 2): not on Home's strip (`underStrip`
      // reads `homePlace.strip`) and under the lens's top. And it flips back only 1 degree past where it flipped, so
      // a head that bobs does not move it.
      const wide = tallM * (NOTE.width / NOTE.height) / 2
      const seen = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => lookAt(eye, g.localToWorld(NOTE_AT.set(at.cx + sx * wide, over + (sy * tallM) / 2, 0))))
      const box = { yaw0: Math.min(...seen.map(s => s.yaw)), yaw1: Math.max(...seen.map(s => s.yaw)), low: Math.min(...seen.map(s => s.pitch)), top: Math.max(...seen.map(s => s.pitch)) }
      const overFree = underStrip(eye, box) === 0 && box.top < NOTE_LOWEST_DEG
      lineUp.current = overFree && (lineUp.current ? lowDeg < -NOTE_LOWEST_DEG + NOTE_FLIP_BACK_DEG : lowDeg < -NOTE_LOWEST_DEG)
      text.position.set(at.cx, lineUp.current ? over : under, 0.001)
    }
    // Where the page's focus is. The source keeps the same answer while nothing changed.
    const focus = source.focus?.() ?? null
    if (focus !== focusSaid.current) { focusSaid.current = focus; onFocus?.(id, focus) }

    if (!source.detail) return
    // As many picture pixels as the page covers on the display, and a little more. Not while a hand moves the panel.
    const v = view.current
    v.tick += 1
    if (v.told && !holding.current && v.tick % 15 === 0) {
      const eye = gl.xr.isPresenting ? gl.xr.getCamera().cameras[0] : null
      const focal = (eye ?? camera).projectionMatrix.elements[5]
      const height = eye ? eye.viewport.w : gl.domElement.height
      source.detail(eyePxPerCssPx(d, height, focal) * PAGES.texelsPerEyePx)
    }
  })

  /* ---------------- move: the edge ---------------- */

  const walledNow = useRef(walled === true)
  walledNow.current = walled === true
  /**
   * A move by the head's dot, as it was at its first frame: where the page's middle stood. Near and far (D67) bring
   * the middle along the line from his eye to it.
   */
  const depthHold = useRef<{ middle: Vector3; moved: boolean } | null>(null)
  const move = useMemo(() => {
    const grabLocal = new Vector3()
    const grab = new Vector3()
    const dir = new Vector3()
    const turned = new Vector3()
    const q = new Quaternion()
    const start = new Vector3()
    // Where the page's middle stood at the down, and how it was turned (D78 2).
    const middle0 = new Vector3()
    const turn0 = new Quaternion()
    let reach0 = 0
    let length0 = 0
    let moved = false
    return (s: HandleState<unknown>, target: Object3D): unknown => {
      const e = s.event
      if (s.last || !e) {
        // The head's dot (D67): let go after a move across, the page's middle is as far from him as it was, and it
        // faces him from where it stands (two turns about the held point leave it a degree or two off, and 3 cm out).
        const dh = depthHold.current
        if (holding.current === 'move' && dh && moved && !dh.moved) {
          const eye = head()
          const away = dh.middle.distanceTo(eye)
          turned.copy(target.position).sub(eye).normalize()
          target.position.copy(eye).addScaledVector(turned, away)
          facing(target.position, eye, target.quaternion)
        }
        // A tremble places nothing (D78 2): a page whose middle ends 1 cm or less from where it stood at the down goes
        // back there, as it was turned, and is not marked placed by hand. Past that, or brought nearer or farther, it is.
        if (holding.current === 'move' && (moved || depthHold.current?.moved)) {
          if (!depthHold.current?.moved && target.position.distanceTo(middle0) <= TREMBLE_M) { target.position.copy(middle0); target.quaternion.copy(turn0) } else onPlaced?.(id)
        }
        holding.current = ''; held.current = null; depthHold.current = null
        return undefined
      }
      const eye = head()
      if (s.first) {
        holding.current = 'move'
        glide.current = null
        // What the hand holds: this point of the panel, this far along its ray, with the hand this far from the head.
        grabLocal.copy(target.worldToLocal(grab.copy(e.point)))
        length0 = e.point.distanceTo(e.pointerPosition)
        reach0 = e.pointerPosition.distanceTo(eye)
        moved = false
        start.copy(e.point)
        middle0.copy(target.position)
        turn0.copy(target.quaternion)
        depthHold.current = e.pointerType === 'head' ? { middle: target.position.clone(), moved: false } : null
        // The press itself moves nothing: a panel that stands square to the lens would turn to the eye under a finger that only pressed.
        return undefined
      }
      // A drag of the dot that has become near and far is the frame loop's: it moves nothing across.
      const dh = depthHold.current
      const own = dh ? pinchRoute.owner : null
      const now = own ? pinchRoute.place() : null
      if (dh && own && now && dragFor(own, inHisFrame([now[0] - own.from[0], now[1] - own.from[1], now[2] - own.from[2]], dotHold.yaw)).kind === 'depth') return undefined
      // Nor does a hand that only trembles: the held point has to leave its place by a few millimetres first.
      if (!moved && e.point.distanceTo(start) < EDGE_DRAG_M) return undefined
      moved = true
      // The framework keeps the held point on the ray, as far along it as it was: `e.point`.
      // Ours, 1: a hand that reaches out sends the panel farther, one that is pulled back brings it nearer.
      dir.copy(e.point).sub(e.pointerPosition).normalize()
      const from = e.pointerPosition
      const far = walledNow.current ? reachInRoom([from.x, from.y, from.z], [dir.x, dir.y, dir.z], PAGES.farM) : PAGES.farM
      const length = Math.max(PAGES.nearM, Math.min(far, length0 * depthScale(from.distanceTo(eye) - reach0)))
      grab.copy(e.pointerPosition).addScaledVector(dir, length)
      // Ours, 2: the panel turns to face the person, about the point that is held, so that point stays on the ray.
      target.position.copy(grab)
      for (let i = 0; i < 2; i += 1) {
        facing(target.position, eye, q)
        target.position.copy(grab).sub(turned.copy(grabLocal).applyQuaternion(q))
      }
      target.quaternion.copy(q)
      held.current = [grabLocal.x, grabLocal.y]
      return undefined
    }
  }, [head, id, onPlaced])

  /* ---------------- resize: a corner ---------------- */

  const resize = useMemo(() => CORNERS.map(corner => {
    let from: Rect = { cx: 0, cy: 0, w: 1, h: 1 }
    let anchor0: Corner | null = null
    /** How far the hand (the dot's pinch) or the point on the page (another pointer) is from where the press began (m). */
    let start: [number, number, number] | null = null
    let endM = 0
    return (s: HandleState<unknown>): unknown => {
      if (s.first) {
        from = state.current.rect; anchor0 = anchor.current; start = null; endM = 0
        holding.current = 'resize'; anchor.current = corner; sizedByDot.current = s.event?.pointerType === 'head'
        // Kept as it is: a stream writes its next picture of the same size into the texture it draws now.
        keepPicture()
        // A held corner hides the live picture for at most `KEPT_HELD_MOST_MS` (D96 5): then a look again.
        heldSince.current = performance.now()
        setTimeout(() => setSettled(n => n + 1), KEPT_HELD_MOST_MS + 20)
        setStretch(null); setSizing(corner)
      }
      if (s.last || !s.event) {
        const r = state.current.rect
        let sized = Math.abs(r.w - from.w) > 1e-6 || Math.abs(r.h - from.h) > 1e-6
        // A corner pinch whose hand ended under 1 cm from its start sizes nothing and places nothing (M40): the size
        // goes back to where it was, as a tremble of a move does (`TREMBLE_M`).
        if (sized && endM < TREMBLE_M) {
          sized = false
          anchor.current = anchor0
          setRect(from)
          source.resize(from.w / M_PER_PX, from.h / M_PER_PX)
        }
        // A press on a corner that sized nothing (a tap, a tremble under the still band) placed nothing (X1).
        if (holding.current === 'resize' && sized) onPlaced?.(id)
        // A size that changed nothing turns nothing; one that did waits for its picture at this size (Q1), drawn
        // stretched to it until then (D84 1). So does one put back: the picture the drag asked for on its way may come
        // after the release, and the page stands at the size it ends at, not at that picture's (D90 2).
        const target = sized ? r : from
        const f = source.frame()
        const has = { w: f.cssWidth * M_PER_PX, h: f.cssHeight * M_PER_PX }
        // A corner held past `KEPT_HELD_MOST_MS` showed the live picture already: it does not go back to the kept one,
        // and keeps the one drawn now instead, as at the down, so the first pictures at the new size are not shown
        // (M61).
        if (performance.now() - heldSince.current >= KEPT_HELD_MOST_MS) keepPicture()
        const newPicture = heldPicture.current !== null && heldPicture.current !== f.texture
        const off = Math.abs(has.w - target.w) >= 0.003 || Math.abs(has.h - target.h) >= 0.003 || newPicture
        if (!off) dropKept()
        if (!sized) sizedByDot.current = false
        if (off) { setStretch(has); askedAt.current = { w: target.w, h: target.h } } else if (!sized) askedAt.current = null
        if (sized || off) {
          // No picture at that size within 2 s (the gateway gave another): the turn is let go, so a later layout does
          // not turn the page (C4). A page whose picture never came stays at the size he asked, stretched; one whose
          // picture came at another size stands at that size.
          const asked = askedAt.current
          setTimeout(() => {
            if (askedAt.current !== asked) return
            sizedByDot.current = false; askedAt.current = null
            // The kept picture goes too (J2): from here the live one is drawn, stretched while no picture at the asked
            // size has come.
            dropKept(); setSettled(n => n + 1)
            const g = source.frame()
            if (Math.abs(g.cssWidth * M_PER_PX - has.w) + Math.abs(g.cssHeight * M_PER_PX - has.h) > 1e-6) setStretch(null)
          }, 2000)
        }
        holding.current = ''
        held.current = null
        dotHold.corner = null
        setSizing(null)
        return undefined
      }
      // The head's dot (a resting hand's pinch): the hand's travel along the corner's diagonal sizes the page, its
      // shape kept, the opposite corner where it is (`sizeFactor`, D65): 15 cm out doubles it, 15 cm in halves it.
      // Another pointer (a raised hand's ray, a mouse): the framework keeps the corner under it, on the panel's plane.
      const p = s.current.position
      const own = s.event.pointerType === 'head' ? pinchRoute.owner : null
      const now = own ? pinchRoute.place() : null
      let next: Rect
      if (own && now) {
        // The hand's travel in his frame at the down (D68): right of where he looks, and the room's up.
        const mine = inHisFrame([now[0] - own.from[0], now[1] - own.from[1], now[2] - own.from[2]], dotHold.yaw)
        const outM = (mine[0] * corner.sx + mine[1] * corner.sy) / Math.SQRT2
        const most = Math.min(MAX[0] / from.w, MAX[1] / from.h)
        const least = Math.max(MIN[0] / from.w, MIN[1] / from.h)
        const k = Math.min(most, Math.max(least, sizeFactor(outM, Math.hypot(from.w, from.h))))
        next = sizeFrom(from, corner.sx, corner.sy, from.w * k, from.h * k)
        endM = Math.hypot(now[0] - own.from[0], now[1] - own.from[1], now[2] - own.from[2])
      } else {
        next = dragCorner(from, corner.sx, corner.sy, p.x, p.y, MIN, MAX)
        start ??= [p.x, p.y, p.z]
        endM = Math.hypot(p.x - start[0], p.y - start[1], p.z - start[2])
      }
      held.current = [p.x, p.y]
      if (own && now) askedAt.current = { w: next.w, h: next.h }
      setRect(next)
      // The page gets more room, or less: the source lays it out for the new size, at the same size of text.
      source.resize(next.w / M_PER_PX, next.h / M_PER_PX)
      return undefined
    }
  }), [source, id, onPlaced, dropKept, keepPicture, heldPicture])

  /* ---------------- inside the page ---------------- */

  const tell = (events: PageEvent[]) => {
    for (const ev of events) { if ('scroll' in ev) source.scroll(ev.scroll); else source.pointer(ev.pointer) }
  }
  /**
   * Where on the page a pointer is: across and up from the page's middle, as parts of its width and height. A pointer
   * that holds a press is read where its ray meets the page's plane, also past the page's edge.
   */
  const spot = useMemo(() => {
    const local = new Vector3()
    return (e: Pointed): [number, number] => {
      const m = page.current
      if (!m) return [0, 0]
      m.worldToLocal(local.copy(e.pointOnFace ?? e.point))
      return [local.x, local.y]
    }
  }, [])
  /** The same in the page's CSS pixels, from its top left corner. */
  const at = (e: Pointed): [number, number] => { const [x, y] = spot(e); return [(x + 0.5) * frame.cssWidth, (0.5 - y) * frame.cssHeight] }
  const point = (e: Pointed, on: boolean) => {
    const m = mark.current
    if (!m) return
    // The dot is the head's own mark: a page draws no second one for it (one light at a time).
    m.visible = on && e.pointerType !== 'head'
    if (!m.visible) return
    const [x, y] = spot(e)
    m.position.set(shown.cx + x * shown.w, shown.cy + y * shown.h, 0.002)
  }

  /* ---------------- the layer path ---------------- */

  useEffect(() => {
    if (!layers) return
    const at = new Vector3()
    const entry: LayerPanel = {
      id,
      source: source.toLayer && source.pictureSize ? { toLayer: l => source.toLayer?.(l), pictureSize: () => source.pictureSize?.() ?? null } : null,
      position: () => tuple(page.current ? page.current.getWorldPosition(at) : at.set(0, 0, 0)),
      inView: () => view.current.told,
      get anchor() { return page.current },
    }
    layers.panels.set(id, entry)
    return () => { if (layers.panels.get(id) === entry) layers.panels.delete(id) }
  }, [id, source, layers])

  /* ---------------- the test probe ---------------- */

  useEffect(() => {
    report(id, () => {
      const g = group.current
      const r = state.current.rect
      const w = (x: number, y: number) => tuple(g ? g.localToWorld(new Vector3(x, y, 0)) : new Vector3())
      const f = source.frame()
      const axis = (x: number, y: number, z: number) => new Vector3(x, y, z).applyQuaternion(g?.quaternion ?? new Quaternion())
      const toHead = head().clone().sub(g?.position ?? new Vector3())
      return {
        id, position: tuple(g?.position ?? new Vector3()), distance: toHead.length(),
        right: tuple(axis(1, 0, 0)), up: tuple(axis(0, 1, 0)), turnedDeg: (axis(0, 0, 1).angleTo(toHead) * 180) / Math.PI,
        size: [f.cssWidth * M_PER_PX, f.cssHeight * M_PER_PX], css: [f.cssWidth, f.cssHeight], lit: state.current.lit, drops: shownDrops.current, holding: holding.current, mark: mark.current?.visible ?? false, held: held.current,
        aim: { page: w(r.cx, r.cy), edge: w(r.cx, r.cy + r.h / 2 + zone.edge / 2), corner: w(r.cx + r.w / 2 + zone.corner / 4, r.cy + r.h / 2 + zone.corner / 4), close: w(r.cx + r.w / 2 - zone.corner / 4 - zone.press / 2, r.cy + r.h / 2 + Math.max(zone.edge / 2, zone.mark / 2 + zone.edge / 8)) },
        closeMark: { box: [w(r.cx + r.w / 2 - zone.corner / 4 - zone.press, r.cy + r.h / 2), w(r.cx + r.w / 2 - zone.corner / 4, r.cy + r.h / 2 + zone.press)], press: zone.press, mark: zone.mark, atRest: restSince.current !== null, restMs: restSince.current === null ? null : Math.round(performance.now() - restSince.current) },
        flash: flashNow.current,
        // The rectangle the hand asks for and the one drawn (m), which differ while a corner is held or stretched.
        rect: [r.w, r.h], shown: [shownAt.current.w, shownAt.current.h], kept: keptNow.current,
        keptBy: { picture: heldPicture.current !== null, sizing: sizingNow.current, stretch: stretchNow.current, heldMs: Math.round(performance.now() - heldSince.current) },
      }
    })
    return () => report(id, null)
  }, [id, report, source, head, zone, heldPicture])

  const { edge: t, corner: c } = zone
  const rim = Math.max(0.003, t / 8)
  // The mark is part of the edge: the edge stays lit while a ray is on the mark.
  const litEdge = showsClose || flash
  const over = (what: string) => ({
    onPointerEnter: () => {
      if (linger.current) { clearTimeout(linger.current); linger.current = null }
      setLit(what)
    },
    onPointerLeave: (e: Pointed) => {
      if (holding.current) return
      const out = () => setLit(now => (now === what ? '' : now))
      // A finger cannot rest on the edge to see the close mark: the edge it tapped stays lit a little, mark and all.
      if (e.pointerType === 'screen-touch' && (what === 'edge' || what === 'close')) linger.current = setTimeout(out, CLOSE_MARK.lingerMs)
      else out()
    },
  })
  // The close mark: at the right end of the top edge, clear of the corner that a hand resizes by. The area that
  // takes its press grows outward from the page's top line, never over the page; the mark sits on the edge, and
  // just above the page where the edge is thinner than the mark.
  const pressM = zone.press
  const markM = zone.mark
  const closeX = rect.cx + rect.w / 2 - c / 4 - pressM / 2
  const closeTop = rect.cy + rect.h / 2
  const closeY = closeTop + Math.max(t / 2, markM / 2 + t / 8)
  // The four sides of the edge: where each one's middle is, and its size, round the rectangle a hand asks for.
  // Top, bottom, right, left; the side at a pair's seam is left out (D69).
  const sides = ([
    [rect.cx, rect.cy + rect.h / 2 + t / 2, rect.w, t, null], [rect.cx, rect.cy - rect.h / 2 - t / 2, rect.w, t, null],
    [rect.cx + rect.w / 2 + t / 2, rect.cy, t, rect.h, 'right'], [rect.cx - rect.w / 2 - t / 2, rect.cy, t, rect.h, 'left'],
  ] as const).filter(s => s[4] === null || s[4] !== seam)
  // The rim is one mesh, and it covers no pixel of the page.
  const rimShape = useMemo(() => frameGeometry(rect.w, rect.h, rim), [rect.w, rect.h, rim])
  useEffect(() => () => rimShape.dispose(), [rimShape])

  return (
    // Whose panel this is, for what the one light lies on when it is on the frame, a handle or the close mark (`whatIs`).
    <group ref={group} userData={{ xrPanel: id }} pointerEvents={hushed && !session ? 'none' : undefined}>
      {/* The page. A press on it is the page's: it takes the pointer until the press ends. */}
      <mesh
        ref={page}
        // Whose page this is and how big, for what the probe says the dot lies on (`pinchRoute.whatIs`).
        userData={{ xrPage: id, css: [frame.cssWidth, frame.cssHeight] }}
        position={[shown.cx, shown.cy, 0]}
        scale={[shown.w, shown.h, 1]}
        onPointerMove={(e: Pointed) => { e.stopPropagation(); pinchRoute.movedOn(id, e.pointerId, e.pointerType, e.point ? [e.point.x, e.point.y, e.point.z] : null); point(e, true); source.hover?.(e.pointerType === 'screen-mouse' && !wheelLatched?.(e.nativeEvent.clientX, e.nativeEvent.clientY)); tell(press.move(e.pointerId, ...at(e))) }}
        onPointerDown={(e: Pointed) => {
          e.stopPropagation()
          ;(e.object as Object3D).setPointerCapture(e.pointerId)
          // The down carries what the last frame showed: the page taking a press or not (D50).
          const seen = !shownDrops.current
          tell(press.down(e.pointerId, ...at(e)).map(ev => ('pointer' in ev && ev.pointer.type === 'down' ? { pointer: { ...ev.pointer, seen } } : ev)))
        }}
        onPointerUp={(e: Pointed) => {
          e.stopPropagation()
          const out = press.up(e.pointerId, ...at(e))
          // A press that ends on the page as a press: not a drag that scrolled it, and not a finger of two.
          // One that is held back is called off: the page hears a cancel, and the keys are not this page's for it.
          if (out.length && mayPress && !mayPress(id)) { const [x, y] = at(e); source.pointer({ type: 'cancel', pointerId: e.pointerId, x, y }); return }
          // The keys follow a press that sends its click, not one whose down the page dropped (REVIEW-17 small 11).
          if (out.length && (source.takesPress?.() ?? true)) onPress?.(id, e.pointerType === 'screen-touch')
          tell(out)
        }}
        onPointerCancel={(e: Pointed) => { tell(press.cancel(e.pointerId, ...at(e))) }}
        onPointerLeave={(e: Pointed) => { point(e, false); source.hover?.(false); tell(press.leave(e.pointerId, ...at(e))) }}
        onWheel={(e: ThreeEvent<WheelEvent>) => { e.stopPropagation(); if (wheelLatched?.(e.nativeEvent.clientX, e.nativeEvent.clientY, e.nativeEvent.deltaY)) return; if (mayWheel && !mayWheel(id)) return; const [x, y] = at(e as unknown as Pointed); source.scroll({ x, y, dx: e.deltaX, dy: e.deltaY }); onWheelOn?.(id) }}
      >
        <planeGeometry />
        <meshBasicMaterial map={keptNow.current && heldPicture.current ? heldPicture.current : frame.texture} toneMapped={false} onBeforeCompile={sharper} customProgramCacheKey={sharperKey} />
      </mesh>
      {/* What a pinch would take, lit before the pinch. */}
      <mesh ref={aim} visible={false} renderOrder={4} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial ref={aimPaint} color={XR_GL.white} transparent opacity={0.16} depthTest={false} depthWrite={false} toneMapped={false} />
      </mesh>
      {/* The one line the panel says, under the page. */}
      <mesh ref={line} visible={false} scale={[NOTE_LINE.heightM * (NOTE.width / NOTE.height), NOTE_LINE.heightM, 1]} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial map={note.texture} toneMapped={false} />
      </mesh>
      {/* The page's own pointer: where the ray is on the page. */}
      <mesh ref={mark} visible={false} renderOrder={5} pointerEvents="none">
        <ringGeometry args={[rim * 1.6, rim * 2.4, 24]} />
        <meshBasicMaterial color={XR_GL.white} transparent opacity={0.9} depthTest={false} />
      </mesh>
      {/* The mark at a press: a ring round the place, so the word under it stays readable. */}
      <mesh ref={pressRing} visible={false} renderOrder={5} pointerEvents="none">
        <ringGeometry args={[rim * PRESS_RING.inner, rim * PRESS_RING.outer, 32]} />
        <meshBasicMaterial color={XR_GL.white} transparent opacity={0.9} depthTest={false} />
      </mesh>
      {/* The rim: a thin line round the page, so a dark page has an outline on glasses that add light. It lights with the edge. */}
      <mesh geometry={rimShape} position={[rect.cx, rect.cy, 0]} pointerEvents="none">
        <meshBasicMaterial color={litEdge ? XR_GL.white : XR_GL.faint} toneMapped={false} />
      </mesh>
      {/* The edge: aim at it until it lights, pinch, hold and drag to move the panel. */}
      <Handle targetRef={group} rotate={false} scale={false} multitouch={false} apply={move}>
        {/* `xrFrame`: the dot takes this band before a Bot or its board that stands nearer (D78 1, `HeadAim.tsx`). */}
        <group {...over('edge')} userData={{ xrFrame: id }}>
          {sides.map(([x, y, w, h, side]) => (
            <mesh key={side ?? (y > rect.cy ? 'top' : 'bottom')} position={[x, y, 0]} scale={[w, h, 1]}>
              <planeGeometry />
              {/* Unlit, the edge is still there for a ray to find, and it is not drawn. */}
              <meshBasicMaterial color={XR_GL.white} visible={litEdge} transparent opacity={0.28} depthWrite={false} toneMapped={false} />
            </mesh>
          ))}
        </group>
      </Handle>
      {/* The close mark. It shows with the edge; a pinch or a tap that begins and ends on it closes the page. */}
      <mesh
        position={[closeX, closeTop + pressM / 2, 0.0025]}
        scale={[pressM, pressM, 1]}
        userData={{ xrPress: 'close', xrFrame: id }}
        {...(() => { const o = over('close'); return { onPointerEnter: () => { onMark.current = true; o.onPointerEnter() }, onPointerLeave: (e: Pointed) => { onMark.current = false; o.onPointerLeave(e) } } })()}
        onPointerDown={(e: Pointed) => {
          e.stopPropagation()
          const now = performance.now()
          const still = quiet?.current ?? 0
          // A mark seen at rest closes on the first tap, once a second has passed since it came by itself; in that
          // second no tap counts, also one on a mark that a resting mouse lit as it came. The mark is white only then.
          // A ray or a mouse closes only a mark it saw white; a finger has no pointer before its tap, so it goes by the rule.
          closing.current = (e.pointerType === 'screen-touch' || markWhite.current) && pressCloses(restSince.current, closeSince.current, now, still) ? e.pointerId : null
          closeSince.current = shownAfterPress(closeSince.current, now, still)
        }}
        onPointerUp={(e: Pointed) => {
          e.stopPropagation()
          const began = closing.current
          closing.current = null
          if (began === e.pointerId) onClose?.(id)
        }}
        onPointerCancel={() => { closing.current = null }}
      >
        <planeGeometry />
        <meshBasicMaterial visible={false} />
      </mesh>
      {/* At rest, with the edge unlit, the mark stands over no drawn edge: in the rim's own faint colour, opaque, so the bars' crossing is no brighter (glasses that add light show it dim). */}
      <group visible={markShows} position={[closeX, closeY, 0.003]} pointerEvents="none">
        {[1, -1].map((turn, i) => (
          <mesh key={turn} rotation={[0, 0, (turn * Math.PI) / 4]} scale={[markM * Math.SQRT2, Math.max(rim, markM / 7), 1]}>
            <planeGeometry />
            <meshBasicMaterial ref={m => { markPaint.current[i] = m }} color={XR_GL.faint} transparent opacity={1} depthWrite={false} toneMapped={false} />
          </mesh>
        ))}
      </group>
      {/* The corners: the same, to resize. Each has a knob the framework drags on the panel's plane. */}
      {CORNERS.map((corner, i) => {
        const x = rect.cx + (corner.sx * rect.w) / 2
        const y = rect.cy + (corner.sy * rect.h) / 2
        const on = lit === corner.key
        return (
          <group key={corner.key}>
            <group ref={knobs[i]} position={[x, y, 0]} />
            <Handle targetRef={knobs[i]} translate={{ z: false }} rotate={false} scale={false} multitouch={false} apply={resize[i]}>
              {/* The square that a hand takes the corner by, less its quarter over the page: an L round the corner, as
                  big as that square was outside the page, so that a press inside the page's corner is the page's. */}
              <group {...over(corner.key)} userData={{ xrFrame: id }}>
                <mesh position={[x + (corner.sx * c) / 4, y + (corner.sy * 3 * c) / 8, 0.001]} scale={[c, (3 * c) / 4, 1]}>
                  <planeGeometry />
                  <meshBasicMaterial visible={false} />
                </mesh>
                <mesh position={[x + (corner.sx * 3 * c) / 8, y - (corner.sy * c) / 8, 0.001]} scale={[(3 * c) / 4, c / 4, 1]}>
                  <planeGeometry />
                  <meshBasicMaterial visible={false} />
                </mesh>
              </group>
            </Handle>
            {/* Lit, a corner is an angle of two short bars. */}
            <group visible={on} pointerEvents="none">
              <mesh position={[x - (corner.sx * (c - t)) / 4, y + (corner.sy * t) / 2, 0.002]} scale={[(c + t) / 2, t, 1]}>
                <planeGeometry />
                <meshBasicMaterial color={XR_GL.white} toneMapped={false} />
              </mesh>
              <mesh position={[x + (corner.sx * t) / 2, y - (corner.sy * (c - t)) / 4, 0.002]} scale={[t, (c + t) / 2, 1]}>
                <planeGeometry />
                <meshBasicMaterial color={XR_GL.white} toneMapped={false} />
              </mesh>
            </group>
          </group>
        )
      })}
    </group>
  )
}
