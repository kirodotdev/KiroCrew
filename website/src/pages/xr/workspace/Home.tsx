/**
 * The Home surface (`home.ts`, `useHome.ts`), drawn: one plate with the four numbers, large; under them one short
 * line for each thing that waits; and the switch that puts the real pages away. It takes a pointer on the switch
 * and on each line, as a tile of the desk does: the dot and a pinch of a hand that rests, a raised hand's ray, the
 * mouse in the flat preview. The rest of the plate takes none, so a drag on it turns the preview's head as on empty
 * space.
 *
 * `HomeMirror` says the same to a screen reader and to Tab: the canvas is a picture.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, LinearFilter, Quaternion, SRGBColorSpace, Vector3, type Group, type Mesh, type Object3D } from 'three'
import { Btn } from '../../../components/ui'
import { fmtNumber, fmtTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css, readPalette, type XrPalette } from '../palette'
import { usePress } from './Desk'
import { DEPTH, PRESS, layIn } from './drawOrder'
import { HOME, STRIP_LABEL_M, homeDoneRows, homeLayout, type DoneLine, type HomeLine, type HomeRect, type HomeStats, type LineDoes } from './home'
import { HOME_NAMES, cutNames, foldNext, placeNames, type HomeName } from './homeNames'
import { ROOM_FRONT_YAW_DEG, homePlace, markSides, stripCutsLeaning, stripFit } from './homePlace'
import { PAGES, facing, placeAt } from './layout'
import { pinchRoute } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import type { EmptyHand } from './RoomWalls'
import type { HomeState } from './useHome'
import type { NameDoes } from './useHomeNames'

type V3 = [number, number, number]

/** What a test reads of the Home surface (`?probe=1`). */
export interface HomeProbe {
  form: HomeState['form']
  stats: HomeStats | null
  pagesHidden: boolean
  /** Its middle in the room, and its size in metres. */
  center: V3
  size: [number, number]
  /** Its group's scale now: other than 1 only while it travels from one form to the other. */
  scale: [number, number] | null
  switch: { center: V3 }
  /** A press on the strip opens the large form now, and where the chevron that says so stands (null: none drawn). */
  opens: boolean
  chevron: V3 | null
  /** `does`: what a press on the line does now; a line that does nothing has no target. */
  lines: (HomeLine & { center: V3; does: LineDoes })[]
  more: number
  /** Whether the flat preview's quiet time holds its presses back now. */
  hushed: boolean
  /** "Done today": its lines with where each one's target is, and how many answers wait to be read. */
  done: (DoneLine & { center: V3 })[]
  toRead: number
  /** What the plate wrote of "to read" (the strip's, or the large form's head), and how much of it fits its room. */
  written: string[]
  fits: number
  /** Each done line's words as drawn, and how much of its room they take (px). */
  doneDrawn: { text: string; usedPx: number; roomPx: number }[]
  /** What the strip gave up of its right end for a close mark (metres, D54), and whether its counts kept their words. */
  cutM: number
  countWords: boolean
  /** Each count as the strip drew it: `2 working`, or cut short `2w` (a 0 is not drawn then). */
  counts: string[]
  /**
   * The line of names under the counts (`homeNames.ts`): each name as drawn, where its middle is, its press area's
   * lower left and upper right corners in the room, and whether a pinch on it does anything now; and the "+n".
   */
  names: { slug: string; text: string; center: V3; box: [V3, V3]; does: NameDoes; gone: boolean }[]
  namesMore: number
}

interface Props {
  home: HomeState
  /** The flat preview's quiet time: nothing takes a pointer. */
  hushed: boolean
  /**
   * A press on the line of a thing that waits. THE GRAB HOOKS IN HERE (D5, second step): the same board that a Bot
   * holds up comes out of the line, in place of what the workspace does with the press now.
   */
  onTake: (line: HomeLine) => void
  /**
   * What a press on a line does now (`home.ts`, `lineDoes`). A line that does nothing has no target: it is drawn,
   * lights for no pointer and takes no press, as while its board is in his hand or on the floor.
   */
  does: (line: HomeLine) => LineDoes
  /**
   * The flat preview's empty space, as a hand: a drag that begins on the plate turns the room, and a wheel on it
   * brings the eye nearer, as on empty space. Not there in a session.
   */
  drag?: EmptyHand
  report?: (read: (() => HomeProbe) | null) => void
  /**
   * A press on the plate itself that does not move, on none of its targets. With no row (`places.ts`) it opens the
   * strip to the large form, and puts the large form back to the strip. Left out, the plate does nothing with it.
   */
  onPlate?: () => void
  /**
   * A press on the strip opens the large form now (`usePlacesRoom`, `homeOpens`): only then is a small chevron drawn at
   * its left end and does the plate light under a pointer, and only then does a press on the strip reach `onPlate`.
   */
  opens?: boolean
  /** The strip stands above the first page, not over its upper rows (`homeLayout`). */
  above?: boolean
  /** A press on a line of "done today": that sheet comes up at its Bot again (D35). */
  onDone?: (line: DoneLine) => void
  /**
   * Who needs him, by name, as the strip's second row (`homeNames.ts`); left out or empty, the strip is as it was.
   * `onName`: a pinch on a name; `nameDoes`: what it does now (`useHomeNames.ts`). A name that does nothing now
   * (`none`, `fetch`) keeps its press area, which lights for no pointer and does nothing: the pinch does not fall
   * through to the plate, where it would open the large form. A name whose words are fetched is drawn dim.
   */
  names?: { names: readonly HomeName[]; more: number }
  onName?: (slug: string) => void
  nameDoes?: (slug: string) => NameDoes
  /** A pointer came onto a name's place, or left it: a name that leaves the line meanwhile keeps its place, empty (D75). */
  onNamesOver?: (on: boolean) => void
}

/** A name of the line as it is written: who, and how many asks of it wait, or that it failed. */
const nameText = (n: HomeName) => `${n.who} ${n.failed ? i18nT('xr.home_failed') : fmtNumber(n.asks)}`
/** The middles of the strip's two rows while the line of names stands (metres up from the strip's middle). */
const COUNTS_ROW_Y = HOME.compact.heightM / 2 - HOME_NAMES.countsRowM / 2
const NAMES_ROW_Y = -HOME.compact.heightM / 2 + HOME_NAMES.rowM / 2


/** Where the head is that the surface stands round: the point that the kept room's places are counted round. */
const HEAD = new Vector3(0, PAGES.eyeM, 0)
const KEYS = ['working', 'waiting', 'failed', 'done'] as const
const LABEL = { working: 'xr.home_working', waiting: 'xr.home_waiting', failed: 'xr.home_failed', done: 'xr.home_done' } as const
const meaning = (pal: XrPalette) => ({ working: pal.accent, waiting: pal.warn, failed: pal.danger, done: pal.ok })
/** A number as it is said; the format's own dash while the states cannot be known. */
const said = (stats: HomeStats | null, key: (typeof KEYS)[number]) => fmtNumber(stats ? stats[key] : Number.NaN)
/** How long the surface takes from one form to the other (ms): it is one thing that moves, not two that swap. */
const MOVE_MS = 320

/** `text` at the context's font, whole when it fits in `max` px, else cut there with an ellipsis. */
function fitLine(c: CanvasRenderingContext2D, text: string, max: number): string {
  if (c.measureText(text).width <= max) return text
  const chars = [...text]
  while (chars.length > 1 && c.measureText(`${chars.join('').trimEnd()}\u2026`).width > max) chars.pop()
  return chars.join('').trimEnd().concat('\u2026')
}

/** One thing of the plate that takes a press: it lights while a pointer is on it. */
function Target({ rect, name, onPress, dead = false, onOver }: { rect: HomeRect; name: string; onPress: () => void; dead?: boolean; onOver?: (on: boolean) => void }) {
  const [lit, setLitNow] = useState(false)
  const litNow = useRef(false)
  /** `onOver` hears each time a pointer comes onto it or leaves it, and once more if it goes while a pointer is on it. */
  const setLit = (on: boolean) => { if (litNow.current !== on) { litNow.current = on; onOver?.(on) } setLitNow(on) }
  const overNow = useRef(onOver)
  overNow.current = onOver
  useEffect(() => () => { if (litNow.current) overNow.current?.(false) }, [])
  const press = usePress(onPress, () => !dead)
  return (
    <mesh
      name={name}
      position={[rect.cx, rect.cy, 0.002]}
      scale={[rect.w, rect.h, 1]}
      onPointerEnter={() => setLit(true)}
      {...press}
      onPointerLeave={() => { setLit(false); press.onPointerLeave() }}
    >
      <planeGeometry />
      <meshBasicMaterial color={XR_GL.white} transparent opacity={lit && !dead ? 0.14 : 0} depthWrite={false} toneMapped={false} />
    </mesh>
  )
}

export function Home({ home, hushed, does, onTake, drag, report, onPlate, opens = false, above = false, onDone, names, onName, nameDoes, onNamesOver }: Props) {
  /** The press that is on the plate now: which pointer, and where on the screen it began. */
  const plateDown = useRef<{ id: number; x: number; y: number } | null>(null)
  const [over, setOver] = useState(false)
  // The plate's own pointer: it goes to nothing behind the plate. In the flat preview the empty space's hand gets
  // it, so a drag that begins on the plate turns the room; a press that does not move turns nothing.
  const plateHand = {
    onPointerDown: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation()
      plateDown.current = { id: e.pointerId, x: e.nativeEvent.clientX, y: e.nativeEvent.clientY }
      drag?.onPointerDown(e)
    },
    onPointerMove: (e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); drag?.onPointerMove(e) },
    onPointerUp: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation()
      drag?.onPointerUp(e)
      const began = plateDown.current
      plateDown.current = null
      if (!onPlate || began?.id !== e.pointerId) return
      // The strip takes a press only when it opens; the large form always puts itself back.
      if (form !== 'large' && !opens) return
      // A pinch that travelled or was held is a drag, as on every other thing that takes a press (`usePress`).
      if (pinchRoute.ending === 'drag') return
      // A press that moved turned the room (the flat preview); a hand's ray in a session has no place on a screen.
      const moved = Math.hypot(e.nativeEvent.clientX - began.x, e.nativeEvent.clientY - began.y)
      if (!(moved > TURN_DRAG_PX)) onPlate()
    },
    onPointerCancel: (e: ThreeEvent<PointerEvent>) => { plateDown.current = null; drag?.onPointerCancel(e) },
    onWheel: (e: ThreeEvent<WheelEvent>) => { e.stopPropagation(); drag?.onWheel(e) },
  }
  const host = useThree(s => s.gl.domElement)
  const group = useRef<Group>(null)
  const { form, stats, lines, more, pagesHidden, done, toRead } = home
  const doneHead = homeDoneRows(home).head
  // How much shorter the strip is, and how far its middle moves, so that no close mark's press area lies on it (D54,
  // small 8), in whole centimetres. The switch keeps its own width and a little air: the strip is never shorter.
  const [place, setPlace] = useState({ cutM: 0, shiftM: 0 })
  /** The side each page's mark makes the strip give way on, held with a degree of slack (`markSides`). */
  const sidesHeld = useRef(new Map<string, 1 | -1>())
  const cutM = place.cutM
  const layout = useMemo(() => homeLayout(form, lines.length, more > 0, above, { head: doneHead, lines: done.length }, form === 'large' ? 0 : cutM), [form, lines.length, more, above, doneHead, done.length, cutM])
  const at = useMemo(() => {
    // The strip faces him straight ahead, so its right is +x: a cut of its right end moves its middle left by half
    // of it, a cut of its left end right.
    const position = placeAt(HEAD, ROOM_FRONT_YAW_DEG, layout.pitchDeg, layout.distanceM)
    const full = position.clone()
    if (form !== 'large') position.x += place.shiftM
    return { position, full, quaternion: facing(position, HEAD) }
  }, [layout.pitchDeg, layout.distanceM, place, form])
  useFrame(() => {
    if (form === 'large') { if (place.cutM || place.shiftM) setPlace({ cutM: 0, shiftM: 0 }); return }
    // Home is one place in a room (D80 1): over a default first page, at `STRIP_TOP_MOST_DEG`, never lower and never
    // higher. Only a close mark cuts it or shifts it sideways, and not while a pinch is down: a page carried under it
    // does not take it along.
    if (pinchRoute.owner) return
    const marks = [...homePlace.closeMarks.entries()]
    const full = { center: at.full, widthM: HOME.compact.widthM, heightM: layout.heightM }
    const got = stripCutsLeaning(HEAD, full, marks.map(m => m[1]), markSides(HEAD, full, marks, sidesHeld.current))
    // Each mark's edges are rounded to whole centimetres away from the strip's room: its near side toward the
    // strip's middle, its far side away from it.
    const up = (v: number) => Math.ceil(v * 100) / 100
    const placed = stripFit(HOME.compact.widthM, HOME.compact.switchM[0] + 0.04, { ...got, rightInner: -up(-got.rightInner), rightOuter: up(got.rightOuter), leftInner: up(got.leftInner), leftOuter: -up(-got.leftOuter) })
    const want = { cutM: Math.round((HOME.compact.widthM - placed.widthM) * 100) / 100, shiftM: Math.round(placed.shiftM * 1000) / 1000 }
    if (want.cutM !== place.cutM || want.shiftM !== place.shiftM) setPlace(want)
  })
  // What must keep clear of the strip reads where it stands (`homePlace.ts`).
  useEffect(() => {
    homePlace.strip = form === 'large' ? null : { center: at.position, widthM: layout.widthM, heightM: layout.heightM }
    return () => { homePlace.strip = null }
  }, [form, at, layout.widthM, layout.heightM])
  // From one form to the other the surface travels, so the eye follows it to its new place. The new form's plate has
  // its own width and height from its first frame: the group is never scaled, so no word is ever drawn stretched
  // (D102 7: a strip in the large form's box stood 5.3 times too tall for a frame). With reduced motion set, it is
  // there at once.
  const still = useMemo(() => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true, [])
  const stood = useRef<{ form: HomeState['form']; w: number; h: number } | null>(null)
  const move = useRef<{ p: Vector3; q: Quaternion; t0: number } | null>(null)
  useLayoutEffect(() => {
    const g = group.current
    const was = stood.current
    stood.current = { form, w: layout.widthM, h: layout.heightM }
    if (!g) return
    if (was && was.form !== form && !still) {
      move.current = { p: g.position.clone(), q: g.quaternion.clone(), t0: performance.now() }
      return
    }
    if (move.current) return
    g.position.copy(at.position)
    g.quaternion.copy(at.quaternion)
    g.scale.set(1, 1, 1)
  }, [form, layout.widthM, layout.heightM, at, still])
  useFrame(() => {
    const g = group.current
    const m = move.current
    if (!g || !m) return
    const t = Math.min(1, (performance.now() - m.t0) / MOVE_MS)
    const k = 1 - (1 - t) ** 3
    g.position.lerpVectors(m.p, at.position, k)
    g.quaternion.slerpQuaternions(m.q, at.quaternion, k)
    if (t >= 1) move.current = null
  })
  // Drawn over what stands nearer (a page carried close), as it takes the press first (`drawOrder.ts`): the picture says
  // which takes it. Each of its meshes once, as it comes (the names' row comes and goes).
  const onTop = useRef(new WeakSet<Object3D>())
  useFrame(() => {
    if (group.current) layIn(group.current, 'home')
    group.current?.traverse(o => {
      if (onTop.current.has(o)) return
      onTop.current.add(o)
      const m = o as Mesh
      if (!m.isMesh) return
      for (const mat of Array.isArray(m.material) ? m.material : [m.material]) { mat.depthTest = DEPTH.home; mat.needsUpdate = true }
    })
  })
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(layout.widthM * HOME.pxPerM)
    canvas.height = Math.round(layout.heightM * HOME.pxPerM)
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [layout.widthM, layout.heightM])
  useEffect(() => () => plate.texture.dispose(), [plate])
  // The line of names: which fit between the strip's left end and its switch, and where each stands.
  const nameRow = useMemo(() => {
    if (form === 'large' || !names?.names.length) return null
    const c = document.createElement('canvas').getContext('2d')
    if (!c) return null
    c.font = `600 ${Math.round(HOME_NAMES.textM * HOME.pxPerM)}px ${readPalette(host.parentElement ?? document.body).sans}`
    const wide = (text: string) => c.measureText(text).width / HOME.pxPerM
    // The last name's press area ends short of the switch's.
    const right = layout.switch.cx - layout.switch.w / 2 - HOME_NAMES.gapM / 2 - 0.005
    // Each name cut to its width, by whole signs; alike names cut where they differ (D94 5).
    const cut = cutNames(names.names.map(n => n.who), wide, HOME_NAMES.nameMaxM)
    let list = names.names.map((n, i) => ({ ...n, who: cut[i] }))
    let more = names.more
    const left = -layout.widthM / 2 + 0.035
    const place = () => placeNames(list.map(n => wide(nameText(n))), HOME_NAMES.gapM, n => wide(`+${fmtNumber(n)}`), left, right, more)
    // Not all fit: an asker folds into "+n" before a failed Bot does.
    let row = place()
    while (row.at.length < list.length && list.length > 1) { const at = foldNext(list); list = list.filter((_, i) => i !== at); more += 1; row = place() }
    return { ...row, names: list }
  }, [form, names, layout, host])

  /** For the probe: the strip's "to read" words as last drawn, and how much of their width their room holds. */
  const drawn = useRef<string[]>([])
  const fit = useRef(1)
  /** For the probe: whether the counts were drawn with their words (a cut strip drops them, D54). */
  const wordsShown = useRef(true)
  /** For the probe: each count as drawn, with its word or its letter (`2 working`, `2w`). */
  const countsDrawn = useRef<string[]>([])
  /** For the probe: each done line's words as drawn, how wide they are and how wide their room is (px). */
  const doneDrawn = useRef<{ text: string; usedPx: number; roomPx: number }[]>([])
  useEffect(() => {
    const c = plate.canvas.getContext('2d')
    if (!c) return
    const written: string[] = []
    drawn.current = written
    const lined: { text: string; usedPx: number; roomPx: number }[] = []
    doneDrawn.current = lined
    fit.current = 1
    const pal = readPalette(host.parentElement ?? document.body)
    const colour = meaning(pal)
    const k = HOME.pxPerM
    const w = plate.canvas.width
    const h = plate.canvas.height
    const x = (m: number) => w / 2 + m * k
    const y = (m: number) => h / 2 - m * k
    const font = (weight: number, m: number) => `${weight} ${Math.round(m * k)}px ${pal.sans}`
    c.setTransform(1, 0, 0, 1, 0, 0)
    c.clearRect(0, 0, w, h)
    c.globalAlpha = 0.94
    c.fillStyle = css(XR_GL.glass)
    c.beginPath()
    c.roundRect(2, 2, w - 4, h - 4, 0.03 * k)
    c.fill()
    c.globalAlpha = 1
    c.strokeStyle = css(XR_GL.edge)
    c.lineWidth = 3
    c.stroke()
    c.textBaseline = 'middle'
    // A number that is nought is dim: what has a colour is what there is something of.
    const ink = (key: (typeof KEYS)[number]) => (!stats ? pal.faint : stats[key] > 0 ? colour[key] : pal.dim)
    if (layout.form === 'large') {
      c.textAlign = 'center'
      layout.cells.forEach((cell, i) => {
        const key = KEYS[i]
        c.fillStyle = ink(key)
        c.font = font(700, 0.16)
        c.fillText(said(stats, key), x(cell.cx), y(cell.numberY) + 0.008 * k, 0.28 * k)
        c.fillStyle = pal.dim
        c.font = font(500, 0.04)
        c.fillText(i18nT(LABEL[key]), x(cell.cx), y(cell.labelY), 0.29 * k)
      })
      c.textAlign = 'left'
      layout.lines.forEach((rect, i) => {
        const line = lines[i]
        const left = x(rect.cx - rect.w / 2) + 0.03 * k
        c.fillStyle = pal.warn
        c.beginPath()
        c.arc(left, y(rect.cy), 0.009 * k, 0, Math.PI * 2)
        c.fill()
        c.fillStyle = pal.ink
        c.font = font(600, 0.038)
        const who = line.who || '?'
        c.fillText(who, left + 0.035 * k, y(rect.cy), 0.4 * k)
        const after = left + 0.035 * k + Math.min(0.4 * k, c.measureText(who).width) + 0.025 * k
        c.fillStyle = pal.dim
        c.font = font(400, 0.038)
        c.fillText(line.words, after, y(rect.cy), x(rect.cx + rect.w / 2) - after - 0.02 * k)
      })
      if (layout.more) {
        c.fillStyle = pal.dim
        c.font = font(500, 0.036)
        c.fillText(i18nT('xr.home_more', { count: more }), x(layout.more.cx - layout.more.w / 2) + 0.065 * k, y(layout.more.cy))
      }
      // "Done today" (D35): its head says how many wait to be read at their Bots; under it the newest he filed.
      if (layout.doneHead) {
        const r = layout.doneHead
        const left = x(r.cx - r.w / 2) + 0.03 * k
        c.fillStyle = pal.dim
        c.font = font(600, 0.034)
        const title = i18nT('xr.home_filed')
        c.fillText(title, left, y(r.cy), 0.5 * k)
        if (toRead > 0) {
          c.fillStyle = colour.done
          const words = i18nT('xr.home_to_read', { count: toRead })
          c.fillText(words, left + Math.min(0.5 * k, c.measureText(title).width) + 0.03 * k, y(r.cy), 0.5 * k)
          written.push(words)
        }
      }
      layout.done.forEach((rect, i) => {
        const line = done[i]
        const left = x(rect.cx - rect.w / 2) + 0.03 * k
        const right = x(rect.cx + rect.w / 2) - 0.02 * k
        c.fillStyle = colour.done
        c.font = font(600, 0.036)
        c.fillText(line.who, left + 0.035 * k, y(rect.cy), 0.36 * k)
        const after = left + 0.035 * k + Math.min(0.36 * k, c.measureText(line.who).width) + 0.025 * k
        const time = fmtTime(line.ts * 1000)
        c.font = font(400, 0.034)
        const tw = c.measureText(time).width
        c.fillStyle = pal.dim
        // The answer's first line takes the whole width between the name and the time, cut only at that width.
        const room = right - tw - 0.03 * k - after
        const shown = fitLine(c, line.words, room)
        c.fillText(shown, after, y(rect.cy))
        lined.push({ text: shown, usedPx: Math.round(c.measureText(shown).width), roomPx: Math.round(room) })
        c.textAlign = 'right'
        c.fillText(time, right, y(rect.cy))
        c.textAlign = 'left'
      })
    } else {
      c.textAlign = 'left'
      // A press opens it: a small chevron at its left end says so (D50). No chevron, no press.
      if (opens) {
        c.strokeStyle = pal.ink
        c.lineWidth = 0.004 * k
        c.lineCap = 'round'
        c.lineJoin = 'round'
        const cx = x(-layout.widthM / 2) + 0.016 * k
        c.beginPath()
        c.moveTo(cx - 0.004 * k, h / 2 - 0.009 * k)
        c.lineTo(cx + 0.005 * k, h / 2)
        c.lineTo(cx - 0.004 * k, h / 2 + 0.009 * k)
        c.stroke()
      }
      let left = x(-layout.widthM / 2) + 0.035 * k
      // With the line of names under them the counts stand in the strip's upper row, their digits a little smaller.
      const mid = nameRow ? y(COUNTS_ROW_Y) : h / 2
      const digit = nameRow ? HOME_NAMES.digitM : 0.05
      // A strip cut short for a close mark (D54) drops the counts' words before the switch gives up any room.
      const roomEnd = x(layout.switch.cx - layout.switch.w / 2) - 0.012 * k
      let need = left
      for (const key of KEYS) {
        c.font = font(700, digit)
        need += c.measureText(said(stats, key)).width + 0.014 * k
        c.font = font(500, STRIP_LABEL_M)
        need += Math.min(0.16 * k, c.measureText(i18nT(LABEL[key])).width) + (toRead > 0 ? 0.022 : 0.045) * k
      }
      // "2 to read" stands after the counts, at its smallest size at the least: its room is part of what must fit.
      // Only on a strip cut short (D54): the full strip keeps D35's way (the four closer, the words smaller).
      if (toRead > 0 && layout.widthM < HOME.compact.widthM) { c.font = font(600, 0.024); need += c.measureText(i18nT('xr.home_to_read', { count: toRead })).width - 0.012 * k }
      const withWords = need - 0.022 * k <= roomEnd
      wordsShown.current = withWords
      const counted: string[] = []
      countsDrawn.current = counted
      for (const key of KEYS) {
        // Cut short (D54): a count of 0 is not drawn, and what is left keeps its colour and its word's first letter
        // (the strip draws no icons). With nothing left, only the switch stands.
        if (!withWords && (stats?.[key] ?? 0) === 0) continue
        c.fillStyle = ink(key)
        c.font = font(700, digit)
        const n = said(stats, key)
        c.fillText(n, left, mid + 0.002 * k)
        left += c.measureText(n).width + 0.006 * k
        if (!withWords) {
          const letter = [...i18nT(LABEL[key])][0] ?? ''
          c.fillStyle = pal.dim
          c.font = font(500, STRIP_LABEL_M)
          c.fillText(letter, left, mid + 0.004 * k)
          left += c.measureText(letter).width + 0.03 * k
          counted.push(`${n}${letter}`)
          continue
        }
        left += 0.008 * k
        c.fillStyle = pal.dim
        c.font = font(500, STRIP_LABEL_M)
        const label = i18nT(LABEL[key])
        c.fillText(label, left, mid + 0.004 * k, 0.16 * k)
        // With answers to read the four stand a little closer, so "2 to read" has its room before the switch.
        left += Math.min(0.16 * k, c.measureText(label).width) + (toRead > 0 ? 0.022 : 0.045) * k
        counted.push(`${n} ${label}`)
      }
      // Answers that wait at their Bots to be read, beside "done today", in its colour, only while there are some (D35).
      if (toRead > 0) {
        const words = i18nT('xr.home_to_read', { count: toRead })
        c.fillStyle = colour.done
        const room = x(layout.switch.cx - layout.switch.w / 2) - left - 0.004 * k
        // A longer language's words take a smaller size before the canvas squeezes them (never under 0.024 m).
        let size = 0.03
        c.font = font(600, size)
        while (size > 0.024 && c.measureText(words).width > room + 0.012 * k) { size -= 0.001; c.font = font(600, size) }
        c.fillText(words, left - 0.012 * k, mid + 0.004 * k, room + 0.012 * k)
        // For the probe: what the strip wrote, and whether it fits unsqueezed (canvas squeezes a text past its room).
        written.push(words)
        fit.current = Math.min(1, (room + 0.012 * k) / c.measureText(words).width)
      }
      // Who needs him, by name, under the counts: who in the plate's ink, the number of its asks in the colour of
      // "need you", "failed" in its own; a dim dot between two names, and "+n" for those that are not named.
      if (nameRow && names) {
        const ny = y(NAMES_ROW_Y) + 0.002 * k
        c.font = font(600, HOME_NAMES.textM)
        nameRow.at.forEach((r, i) => {
          const n = nameRow.names[i]
          // A name that left while his dot is on the line: its place stays, empty.
          if (n.gone) return
          const x0 = x(r.cx - r.w / 2)
          // A name whose pinch does nothing now is dim (W1): its words are on their way, or its board is in his hand.
          const act = nameDoes?.(n.slug)
          c.globalAlpha = act === 'fetch' || act === 'none' ? 0.4 : 1
          c.fillStyle = pal.ink
          c.fillText(n.who, x0, ny)
          c.fillStyle = n.failed ? colour.failed : colour.waiting
          c.fillText(n.failed ? i18nT('xr.home_failed') : fmtNumber(n.asks), x0 + c.measureText(`${n.who} `).width, ny)
          c.globalAlpha = 1
          if (i < nameRow.at.length - 1 || nameRow.more > 0) {
            c.fillStyle = pal.dim
            c.fillText('\u00B7', x(r.cx + r.w / 2 + HOME_NAMES.gapM / 2) - c.measureText('\u00B7').width / 2, ny)
          }
        })
        if (nameRow.more > 0) { c.fillStyle = pal.dim; c.fillText(`+${fmtNumber(nameRow.more)}`, x(nameRow.moreAt), ny) }
      }
    }
    // The switch: a raised pill with what a press on it does.
    const s = layout.switch
    c.fillStyle = css(XR_GL.raised)
    c.beginPath()
    c.roundRect(x(s.cx - s.w / 2), y(s.cy + s.h / 2), s.w * k, s.h * k, (s.h * k) / 2)
    c.fill()
    c.stroke()
    c.fillStyle = pal.ink
    c.textAlign = 'center'
    c.font = font(600, s.h * 0.42)
    c.fillText(i18nT(pagesHidden ? 'xr.home_pages_show' : 'xr.home_pages_hide'), x(s.cx), y(s.cy) + 0.002 * k, s.w * k - 0.03 * k)
    plate.texture.needsUpdate = true
  }, [plate, host, layout, stats, lines, more, pagesHidden, done, toRead, opens, names, nameRow, nameDoes])

  useEffect(() => {
    if (!report) return
    const point = (cx: number, cy: number): V3 => {
      const g = group.current
      if (!g) return [0, 0, 0]
      g.updateWorldMatrix(true, false)
      const p = g.localToWorld(new Vector3(cx, cy, 0))
      return [p.x, p.y, p.z]
    }
    report(() => ({
      form, stats, pagesHidden, more, hushed, opens: form !== 'large' && opens,
      chevron: form !== 'large' && opens ? point(-layout.widthM / 2 + 0.016, 0) : null,
      center: point(0, 0), size: [layout.widthM, layout.heightM], scale: group.current ? [group.current.scale.x, group.current.scale.y] : null,
      switch: { center: point(layout.switch.cx, layout.switch.cy) },
      lines: layout.lines.map((rect, i) => ({ ...lines[i], center: point(rect.cx, rect.cy), does: does(lines[i]) })),
      done: layout.done.map((rect, i) => ({ ...done[i], center: point(rect.cx, rect.cy) })),
      toRead, written: [...drawn.current], fits: fit.current, doneDrawn: [...doneDrawn.current], cutM: form === 'large' ? 0 : cutM, countWords: wordsShown.current, counts: [...countsDrawn.current],
      names: (nameRow?.at ?? []).map((r, i) => {
        const n = (nameRow?.names ?? [])[i]
        return { slug: n.slug, text: nameText(n), center: point(r.cx, NAMES_ROW_Y), box: [point(r.cx - r.press / 2, NAMES_ROW_Y - HOME_NAMES.rowM / 2), point(r.cx + r.press / 2, NAMES_ROW_Y + HOME_NAMES.rowM / 2)] as [V3, V3], does: nameDoes?.(n.slug) ?? 'none', gone: !!n.gone }
      }),
      namesMore: nameRow?.more ?? 0,
    }))
    return () => report(null)
  })

  return (
    // `xrPress`: the readout's plate reads this mark, so that it never stands over a thing that can be pressed.
    // Home and its names are drawn and pressed over a page that stands nearer, under a held board (`drawOrder.ts`, M37).
    <group ref={group} userData={{ xrPress: 'home' }} pointerEvents={hushed ? 'none' : undefined} pointerEventsOrder={PRESS.home}>
      {/*
        The plate takes its own presses and does nothing with them. The strip stands in front of the first page's top
        bar, and a press on its numbers must not click a button of that bar that he cannot see; the large form is
        the same thing in another shape.
      */}
      <mesh scale={[layout.widthM, layout.heightM, 1]} renderOrder={3} name="xr-home" {...plateHand} onPointerEnter={() => setOver(true)} onPointerLeave={() => setOver(false)}>
        <planeGeometry />
        <meshBasicMaterial map={plate.texture} transparent depthWrite={false} toneMapped={false} />
      </mesh>
      {/* The strip lights under a pointer only when a press opens it. */}
      <mesh scale={[layout.widthM, layout.heightM, 1]} position={[0, 0, 0.001]} renderOrder={4} visible={over && opens && form !== 'large'} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial color={XR_GL.white} transparent opacity={0.1} depthWrite={false} toneMapped={false} />
      </mesh>
      <Target rect={layout.switch} name="xr-home-switch" onPress={home.toggle} />
      {/* A line whose press does nothing is drawn and has no target: it lights for no pointer. */}
      {layout.lines.map((rect, i) => does(lines[i]) !== 'none' && <Target key={lines[i].id} rect={rect} name={`xr-home-line:${lines[i].id}`} onPress={() => onTake(lines[i])} />)}
      {/* A name whose pinch does nothing now keeps its area: the pinch stays the name's, lights nothing and does nothing. */}
      {onName && names && nameRow?.at.map((r, i) => {
        const act = nameDoes?.(nameRow.names[i].slug) ?? 'take'
        return <Target key={nameRow.names[i].slug} rect={{ cx: r.cx, cy: NAMES_ROW_Y, w: r.press, h: HOME_NAMES.rowM }} name={`xr-home-name:${nameRow.names[i].slug}`} dead={act === 'none' || act === 'fetch'} onPress={() => onName(nameRow.names[i].slug)} onOver={onNamesOver} />
      })}
      {onDone && layout.done.map((rect, i) => <Target key={done[i].id} rect={rect} name={`xr-home-done:${done[i].slot}`} onPress={() => onDone(done[i])} />)}
    </group>
  )
}

/** The same numbers, lines and switch for a screen reader and for Tab. */
export function HomeMirror({ home, does, onTake, onDone }: { home: HomeState; does: (line: HomeLine) => LineDoes; onTake: (line: HomeLine) => void; onDone?: (line: DoneLine) => void }) {
  return (
    <section className="sr-only" aria-label={i18nT('xr.home_label')} data-testid="xr-home" data-form={home.form}>
      <dl>
        {KEYS.map(key => (
          <div key={key}>
            <dt>{i18nT(LABEL[key])}</dt>
            <dd data-testid={`xr-home-${key}`}>{said(home.stats, key)}</dd>
          </div>
        ))}
      </dl>
      <ul>
        {home.lines.map(line => (
          <li key={line.id}>
            {does(line) !== 'none' ? <Btn onClick={() => onTake(line)}>{`${line.who} ${line.words}`}</Btn> : `${line.who} ${line.words}`}
          </li>
        ))}
        {home.more > 0 && <li>{i18nT('xr.home_more', { count: home.more })}</li>}
      </ul>
      {(home.done.length > 0 || home.toRead > 0) && (
        <>
          <p data-testid="xr-home-to-read">{home.toRead > 0 ? `${i18nT('xr.home_filed')} ${i18nT('xr.home_to_read', { count: home.toRead })}` : i18nT('xr.home_filed')}</p>
          <ul>
            {home.done.map(line => {
              const text = `${line.who} ${line.words} ${fmtTime(line.ts * 1000)}`
              return <li key={line.id}>{onDone ? <Btn onClick={() => onDone(line)}>{text}</Btn> : text}</li>
            })}
          </ul>
        </>
      )}
    </section>
  )
}
