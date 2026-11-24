/**
 * The rooms he keeps, as cards (D37): one row over the palette's tiles, each a small card with the room's plan drawn
 * on it (`rooms.ts`, `planOf`) and its name. The card he stands in is lit, and its plan is drawn from the pages as
 * they stand now. A pinch on a card goes to that room; on "+" the room as it stands becomes a new one. The first
 * time there is more than one room, one small line over the cards says where rooms are kept, until the palette goes.
 *
 * Drawn in the palette's own frame (dp at the eye, `Palette.tsx`). The same rooms are small buttons in the preview's
 * bar, for a mouse and a screen reader (`RoomsBar`).
 */
import type { ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, SRGBColorSpace, Vector3, type Group, type Object3D } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { handSpeed, previewMoved, sessionMoved, type Moved } from './board'
import { DESK, deskPoint, type DeskPlace } from './desk'
import { PX_PER_DP } from './palettePicture'
import { pinchRoute } from './pinchRoute'
import { PALM_MENU } from './palm'
import { readRoom } from './room'
import { signsOf, ROOMS_MOST, cardThrown, copyParts, planOf, type NamedRoom, type Rooms } from './rooms'

/** A card's size in dp: five fit across the palette's four columns of tiles. */
export const ROOM_CARD_DP: readonly [number, number] = [(PALM_MENU.columns * PALM_MENU.tileDp[0] + (PALM_MENU.columns - 1) * PALM_MENU.gapDp - (ROOMS_MOST - 1) * PALM_MENU.gapDp) / ROOMS_MOST, 92]
/** The plan's part of a card, over the name. */
const PLAN_DP = 58
/** The dot of a warm room's card: its radius, and how far its middle is from the card's upper right corner (dp). */
export const WARM_DOT_DP = { r: 3.5, inset: 15 } as const
/** The line that says where rooms are kept, over the cards. */
const TOLD_DP = 22
/** A pinch held this long on a card, or moved, takes the card into his hand: it lifts toward him, this much bigger. */
export const CARD_HOLD = { ms: 450, startM: 0.015, liftDp: 40, scale: 1.5 } as const

/** How wide a row of `n` cards is. */
export const rowWidth = (n: number): number => n * ROOM_CARD_DP[0] + Math.max(0, n - 1) * PALM_MENU.gapDp

/** Where each card's middle is in the palette's frame, the row's middle at `y` and centred: the rooms, then "+" while there is room. */
export function cardSpots(count: number, y: number): { x: number; y: number }[] {
  const [w] = ROOM_CARD_DP
  const n = Math.min(ROOMS_MOST, count)
  return Array.from({ length: n }, (_, i) => ({ x: (i - (n - 1) / 2) * (w + PALM_MENU.gapDp), y }))
}

/**
 * What the cards take of the palette's frame, the row's middle at `y`: as wide as five cards, from the row's lower
 * edge to over the line and over a card that is in his hand (it is bigger there). What keeps clear of the cards
 * keeps clear of this, so nothing moves when the line comes or a card is taken.
 */
export function cardsBlock(y: number): { width: number; low: number; top: number } {
  const [w, h] = ROOM_CARD_DP
  const held = (h * (CARD_HOLD.scale - 1)) / 2
  return { width: ROOMS_MOST * w + (ROOMS_MOST - 1) * PALM_MENU.gapDp + 2 * held, low: y - h / 2 - held, top: y + h / 2 + Math.max(held, PALM_MENU.gapDp / 2 + TOLD_DP) }
}

const family = (): string => getComputedStyle(document.body).fontFamily || 'sans-serif'

/** A name that fits `room` px: the font a little smaller first (down to 11 dp, as the palette's tiles), then cut with "…". */
function fit(c: CanvasRenderingContext2D, text: string, room: number, tail = ''): string {
  const m = /^(.*?)([\d.]+)px (.*)$/.exec(c.font)
  for (let px = m ? Number(m[2]) : 0; m && px > 11 * PX_PER_DP && c.measureText(text).width > room; px -= PX_PER_DP / 2) c.font = `${m[1]}${px - PX_PER_DP / 2}px ${m[3]}`
  if (c.measureText(text).width <= room) return text
  // A copy's name keeps its end (`tail`: the word for "copy" and its number), so two copies' cards differ (D109 5).
  const end = tail && text.endsWith(tail) && c.measureText(`…${tail}`).width < room ? tail : ''
  let chars = signsOf(text.slice(0, text.length - end.length).trimEnd())
  while (chars.length > 1 && c.measureText(`${chars.join('')}…${end}`).width > room) chars = chars.slice(0, -1)
  return `${chars.join('')}…${end}`
}

/**
 * Draw one card: its plan, its name; lit, it is the room he stands in. `room` null: the "+" card. `words`: the card is
 * in his hand, and these are the words he says for its new name so far (none yet: the card asks for them). `warm`:
 * the room's pages are open already (`useWarmRooms.ts`), so its press is the quick one: one small lit dot in the
 * card's upper right corner, with a dark ring so that it reads over a page of the plan. Not the standing mark, which
 * is the whole card's bright edge and name.
 */
export function drawCard(canvas: HTMLCanvasElement, room: NamedRoom | null, lit: boolean, words: string | null = null, warm = false): void {
  const held = words !== null
  const k = PX_PER_DP
  const [w, h] = ROOM_CARD_DP
  canvas.width = w * k
  canvas.height = h * k
  const c = canvas.getContext('2d')
  if (!c) return
  const r = 12 * k
  const line = (lit ? 3 : 1.5) * k
  c.clearRect(0, 0, w * k, h * k)
  c.fillStyle = css(lit ? XR_GL.ink : XR_GL.edge)
  c.beginPath(); c.roundRect(0, 0, w * k, h * k, r); c.fill()
  c.fillStyle = css(held ? XR_GL.edge : XR_GL.raised)
  c.beginPath(); c.roundRect(line, line, w * k - 2 * line, h * k - 2 * line, r - line); c.fill()
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  if (!room) {
    c.fillStyle = css(XR_GL.dim)
    c.font = `300 ${40 * k}px ${family()}`
    c.fillText('+', (w * k) / 2, (PLAN_DP * k) / 2 + 4 * k)
    c.font = `600 ${PALM_MENU.labelDp * k}px ${family()}`
    c.fillText(fit(c, i18nT('xr.room_add'), (w - 12) * k), (w * k) / 2, (PLAN_DP + (h - PLAN_DP) / 2) * k)
    return
  }
  // The plan: the space round him from above his eyes, each page where it stands, at its size in his view.
  const pad = 8 * k
  const px = pad
  const py = pad
  const pw = w * k - 2 * pad
  const ph = PLAN_DP * k - pad - 2 * k
  c.fillStyle = css(XR_GL.glass)
  c.beginPath(); c.roundRect(px, py, pw, ph, 6 * k); c.fill()
  // The look straight ahead: a short mark at the plan's middle, so that left and right read at once.
  c.fillStyle = css(XR_GL.faint)
  c.fillRect(px + pw / 2 - k / 2, py + ph - 6 * k, k, 6 * k)
  const away = JSON.parse(room.layout).pagesHidden === true
  c.fillStyle = css(lit ? XR_GL.ink : XR_GL.dim)
  c.globalAlpha = away ? 0.45 : 1
  // Each page with a dark edge of its own, so that pages that touch in his view still read as two.
  c.strokeStyle = css(XR_GL.glass)
  c.lineWidth = 1.5 * k
  for (const p of planOf(readRoom(room.layout))) {
    const rw = Math.max(4 * k, p.w * pw)
    const rh = Math.max(4 * k, p.h * ph)
    c.beginPath(); c.roundRect(px + p.x * pw - rw / 2, py + (1 - p.y) * ph - rh / 2, rw, rh, 2 * k); c.fill(); c.stroke()
  }
  c.globalAlpha = 1
  if (warm && !lit) {
    const dx = (w - WARM_DOT_DP.inset) * k
    const dy = WARM_DOT_DP.inset * k
    c.fillStyle = css(XR_GL.glass)
    c.beginPath(); c.arc(dx, dy, (WARM_DOT_DP.r + 2.5) * k, 0, 2 * Math.PI); c.fill()
    c.fillStyle = css(XR_GL.white)
    c.globalAlpha = 0.28
    c.beginPath(); c.arc(dx, dy, (WARM_DOT_DP.r + 2) * k, 0, 2 * Math.PI); c.fill()
    c.globalAlpha = 1
    c.beginPath(); c.arc(dx, dy, WARM_DOT_DP.r * k, 0, 2 * Math.PI); c.fill()
  }
  const y = (PLAN_DP + (h - PLAN_DP) / 2) * k
  if (held) {
    // The microphone's dot, and the words as he says them; before the first word, what to do.
    c.fillStyle = css(XR_GL.white)
    c.beginPath(); c.arc(10 * k, y, 3.5 * k, 0, 2 * Math.PI); c.fill()
    c.fillStyle = css(words ? XR_GL.white : XR_GL.dim)
    c.font = `${words ? 700 : 500} ${PALM_MENU.labelDp * k}px ${family()}`
    c.fillText(fit(c, words || i18nT('xr.room_name_say'), (w - 26) * k), (w * k) / 2 + 6 * k, y)
    return
  }
  c.fillStyle = css(lit ? XR_GL.ink : XR_GL.dim)
  c.font = `${lit ? 700 : 600} ${PALM_MENU.labelDp * k}px ${family()}`
  c.fillText(fit(c, room.name, (w - 12) * k, copyParts(room.name, name => i18nT('xr.room_copy_name', { name }))?.tail), (w * k) / 2, y)
}

/**
 * The line over the cards: where rooms are kept, a line for him (`said`: why a card went back), or the room he threw
 * away with the press that brings it back.
 * False: the words of where rooms are kept do not fit whole at a size he can read, and nothing is drawn.
 */
export function drawTold(canvas: HTMLCanvasElement, width: number, gone: NamedRoom | null = null, said: string | null = null): boolean {
  const k = PX_PER_DP
  canvas.width = width * k
  canvas.height = TOLD_DP * k
  const c = canvas.getContext('2d')
  if (!c) return false
  if (!gone) {
    // Whole or absent: never cut.
    c.font = `500 ${(PALM_MENU.labelDp - 1) * k}px ${family()}`
    const words = said ?? i18nT('xr.rooms_kept_here')
    if (fit(c, words, canvas.width - 20 * k) !== words) { c.clearRect(0, 0, canvas.width, canvas.height); return false }
  }
  c.clearRect(0, 0, canvas.width, canvas.height)
  // On a plate of its own: what stands behind the palette does not show through the words.
  c.fillStyle = css(XR_GL.raised)
  c.beginPath(); c.roundRect(0, 0, canvas.width, canvas.height, canvas.height / 2); c.fill()
  c.fillStyle = css(XR_GL.dim)
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  c.font = `500 ${(PALM_MENU.labelDp - 1) * k}px ${family()}`
  if (!gone) { c.fillText(fit(c, said ?? i18nT('xr.rooms_kept_here'), canvas.width - 20 * k), canvas.width / 2, canvas.height / 2); return true }
  const back = i18nT('xr.room_back')
  c.font = `700 ${PALM_MENU.labelDp * k}px ${family()}`
  const bw = c.measureText(back).width + 24 * k
  c.fillStyle = css(XR_GL.ink)
  c.beginPath(); c.roundRect(canvas.width - bw, 0, bw, canvas.height, canvas.height / 2); c.fill()
  c.fillStyle = css(XR_GL.room)
  c.fillText(back, canvas.width - bw / 2, canvas.height / 2)
  c.fillStyle = css(XR_GL.dim)
  c.textAlign = 'right'
  c.font = `500 ${(PALM_MENU.labelDp - 1) * k}px ${family()}`
  c.fillText(fit(c, i18nT('xr.room_gone', { name: gone.name }), canvas.width - bw - 12 * k), canvas.width - bw - 10 * k, canvas.height / 2)
  return true
}

/** A canvas at its final size, and its texture: a texture's size is fixed at its first upload, which may come before the first drawing. */
const picture = (w: number, h: number): { canvas: HTMLCanvasElement; texture: CanvasTexture } => {
  const canvas = document.createElement('canvas')
  canvas.width = w * PX_PER_DP
  canvas.height = h * PX_PER_DP
  const texture = new CanvasTexture(canvas)
  texture.colorSpace = SRGBColorSpace
  return { canvas, texture }
}

interface CardProps {
  room: NamedRoom | null
  lit: boolean
  at: { x: number; y: number }
  own: (e: ThreeEvent<PointerEvent>) => boolean
  onPress: () => void
  /** The card is in his hand: he may speak its new name, or throw it. */
  onHold?: () => void
  onLetGo?: (thrown: boolean) => void
  /** In his hand: the words he says for it so far. */
  words: string | null
  /** Its pages are open already: the dot. */
  warm: boolean
  /** "Copy this room": given on the card of the room he is in. `copyDim`: the rooms are at their most, so it only says why. */
  onCopy?: () => void
  copyDim?: boolean
  /** A pointer rests on the card (`CARD_REST_MS`), or no longer (D52). */
  onRest?: (on: boolean) => void
}

/** How long a pointer lies on a room's card before it counts as resting there (ms, D52). */
export const CARD_REST_MS = 150

/** A press on a card: where it began, and what a throw is told from (the screen's points in the preview, the pinching hand in a session). */
interface Press {
  id: number; from: Vector3; held: boolean; timer: ReturnType<typeof setTimeout> | null; m: number
  x: number; y: number; t: number; trail: { x: number; y: number; t: number }[]
  pinch: 'left' | 'right' | null; handM: number; hand: { at: [number, number, number]; t: number }[]
}

/** In a session, where the pinching hand is now, kept for its speed at the release (its last 300 ms), as a board keeps it. */
function sampleHand(p: Press): void {
  const own = pinchRoute.owner
  if (!own) return
  const t = performance.now()
  const [x, y, z] = pinchRoute.at[own.hand]
  p.hand.push({ at: [x, y, z], t })
  while (p.hand.length > 2 && t - p.hand[0].t > 300) p.hand.shift()
}

/** How the press was let go: the pinching hand's travel and speed in a session, the screen's points in the preview. */
function movedOf(p: Press, up: { clientX: number; clientY: number; timeStamp: number }): Moved {
  if (p.pinch) return sessionMoved({ hand: p.pinch, handM: Math.max(p.handM, pinchRoute.travel()), handMps: handSpeed(p.hand) }, pinchRoute.released)
  return previewMoved({ x: p.x, y: p.y, t: p.t }, p.trail, { x: up.clientX, y: up.clientY, t: up.timeStamp })
}

/**
 * "Copy this room" on a room's card (D101 4): the word "Copy" on a small plate in the card's upper left corner, in the
 * letters of the card's name, so that it reads at the card's size. Its size in dp, by the word's own width.
 */
const COPY_MARK_H_DP = 22
function copyMarkSize(): [number, number] {
  const c = document.createElement('canvas').getContext('2d')
  if (!c) return [48, COPY_MARK_H_DP]
  c.font = `600 ${PALM_MENU.labelDp}px ${family()}`
  // The plate is at most half the card wide (T5): a longer word (German "Kopieren") is cut by whole signs with "…" (`fit`).
  return [Math.min(ROOM_CARD_DP[0] / 2, Math.ceil(c.measureText(i18nT('xr.room_copy_word')).width) + 14), COPY_MARK_H_DP]
}
/** True once the page's fonts are ready: a width that was measured before is measured again (T6). */
function useFontsReady(): boolean {
  const [ready, setReady] = useState(() => document.fonts?.status === 'loaded')
  useEffect(() => { let on = true; void document.fonts?.ready.then(() => { if (on) setReady(true) }); return () => { on = false } }, [])
  return ready
}
/** The copy mark's middle from its card's middle. */
export function copyMarkAt(): [number, number] {
  const [mw, mh] = copyMarkSize()
  return [-ROOM_CARD_DP[0] / 2 + 5 + mw / 2, ROOM_CARD_DP[1] / 2 - 5 - mh / 2]
}
function copyMark(dim: boolean) {
  const [mw, mh] = copyMarkSize()
  const pic = picture(mw, mh)
  const c = pic.canvas.getContext('2d')
  if (c) {
    const [w, h] = [pic.canvas.width, pic.canvas.height]
    const k = PX_PER_DP
    c.clearRect(0, 0, w, h)
    // At the most rooms it stays, dim: a pinch on it says why no copy is made (N8).
    c.fillStyle = css(dim ? XR_GL.dim : XR_GL.ink)
    c.beginPath(); c.roundRect(0, 0, w, h, h / 2); c.fill()
    c.fillStyle = css(XR_GL.raised)
    c.textAlign = 'center'
    c.textBaseline = 'middle'
    c.font = `600 ${PALM_MENU.labelDp * k}px ${family()}`
    c.fillText(fit(c, i18nT('xr.room_copy_word'), w - 10 * k), w / 2, h / 2 + k)
    pic.texture.needsUpdate = true
  }
  return { ...pic, size: [mw, mh] as [number, number], at: copyMarkAt() }
}

function Card({ room, lit, at, own, onPress, onHold, onLetGo, words, warm, onRest, onCopy, copyDim = false }: CardProps) {
  const copyDown = useRef<number | null>(null)
  const fonts = useFontsReady()
  const copyPic = useMemo(() => (onCopy ? copyMark(copyDim) : null), [!!onCopy, copyDim, fonts]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => copyPic?.texture.dispose(), [copyPic])
  const [hover, setHover] = useState(false)
  // A pointer that lies on the card a moment rests there; a sweep across it does not.
  const restNow = useRef(onRest)
  restNow.current = onRest
  useEffect(() => {
    if (!hover) return
    let rested = false
    const timer = setTimeout(() => { rested = true; restNow.current?.(true) }, CARD_REST_MS)
    return () => { clearTimeout(timer); if (rested) restNow.current?.(false) }
  }, [hover])
  const pressed = useRef<Press | null>(null)
  const group = useRef<Group>(null)
  const pic = useMemo(() => picture(...ROOM_CARD_DP), [])
  useEffect(() => () => pic.texture.dispose(), [pic])
  // Drawn again when the room's plan or name changes, which room he is in, or the words he says for it.
  useEffect(() => { drawCard(pic.canvas, room, lit, words, warm); pic.texture.needsUpdate = true }, [pic, room, lit, words, warm])
  const [w, h] = ROOM_CARD_DP
  const home = () => { const g = group.current; if (g) { g.position.set(at.x, at.y, 0); g.scale.setScalar(1) } }
  const take = (p: Press) => {
    if (p.timer) clearTimeout(p.timer)
    p.timer = null
    if (p.held || !onHold) return
    p.held = true
    group.current?.scale.setScalar(CARD_HOLD.scale)
    if (group.current) group.current.position.z = CARD_HOLD.liftDp
    onHold()
  }
  const end = (thrown: boolean | null) => {
    const p = pressed.current
    pressed.current = null
    if (!p) return
    if (p.timer) clearTimeout(p.timer)
    home()
    if (thrown === null) return
    if (p.held) onLetGo?.(thrown)
    else onPress()
  }
  useEffect(() => () => { const t = pressed.current?.timer; if (t) clearTimeout(t) }, [])
  return (
    <group ref={group} position={[at.x, at.y, 0]} userData={{ xrPress: 'tile' }}>
      <mesh scale={[w, h, 1]} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial map={pic.texture} transparent toneMapped={false} />
      </mesh>
      <mesh
        position={[0, 0, 0]}
        renderOrder={1}
        scale={[w, h, 1]}
        name={room ? `room-card-${room.id}` : 'room-card-add'}
        userData={{ xrPress: true }}
        onPointerEnter={(e: ThreeEvent<PointerEvent>) => { if (!own(e)) setHover(true) }}
        onPointerLeave={() => setHover(false)}
        onPointerDown={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          if (own(e)) return
          ;(e.object as Object3D).setPointerCapture(e.pointerId)
          const p: Press = { id: e.pointerId, from: e.point.clone(), held: false, timer: null, m: 0, x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, t: e.nativeEvent.timeStamp, trail: [], pinch: pinchRoute.owner?.hand ?? null, handM: 0, hand: [] }
          sampleHand(p)
          p.timer = setTimeout(() => take(p), CARD_HOLD.ms)
          pressed.current = p
        }}
        onPointerMove={(e: ThreeEvent<PointerEvent>) => {
          const p = pressed.current
          const g = group.current
          if (!p || p.id !== e.pointerId || !g) return
          e.stopPropagation()
          p.m = e.point.distanceTo(p.from)
          p.handM = Math.max(p.handM, pinchRoute.travel())
          p.trail.push({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, t: e.nativeEvent.timeStamp })
          if (p.trail.length > 24) p.trail.shift()
          sampleHand(p)
          if (!p.held && p.m > CARD_HOLD.startM) take(p)
          if (!p.held || !g.parent) return
          // The card follows his hand, so that a throw is seen as it is made.
          const by = g.parent.worldToLocal(e.point.clone()).sub(g.parent.worldToLocal(p.from.clone()))
          g.position.set(at.x + by.x, at.y + by.y, CARD_HOLD.liftDp)
        }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          const p = pressed.current
          if (!p || p.id !== e.pointerId) return
          sampleHand(p)
          end(cardThrown(movedOf(p, e.nativeEvent)))
        }}
        // A press that ends as a cancel (the tracking dropped, the pinch was taken) is never a throw: a held card goes
        // back and takes the name he said.
        onPointerCancel={(e: ThreeEvent<PointerEvent>) => { if (pressed.current?.id === e.pointerId) end(pressed.current.held ? false : null) }}
      >
        <planeGeometry />
        <meshBasicMaterial color={XR_GL.white} visible={hover} transparent opacity={0.16} depthWrite={false} polygonOffset polygonOffsetFactor={-1} polygonOffsetUnits={-4} toneMapped={false} />
      </mesh>
      {/* "Copy this room" (D97 4, D101 4): the word "Copy" in the card's corner, on the card of the room he is in. It takes its own press, so the card under it does nothing. */}
      {onCopy && copyPic && (
        <mesh
          position={[copyPic.at[0], copyPic.at[1], 0.5]} scale={[copyPic.size[0], copyPic.size[1], 1]} renderOrder={2} name="room-copy" userData={{ xrPress: true }}
          onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); if (!own(e)) copyDown.current = e.pointerId }}
          onPointerUp={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); const mine = copyDown.current === e.pointerId; copyDown.current = null; if (mine && pinchRoute.ending !== 'drag') onCopy() }}
          onPointerCancel={() => { copyDown.current = null }}
        >
          <planeGeometry />
          <meshBasicMaterial map={copyPic.texture} transparent depthWrite={false} toneMapped={false} />
        </mesh>
      )}
    </group>
  )
}

export interface RoomCardsProps {
  rooms: Rooms
  onGo: (id: string) => void
  onAdd: () => void
  /** A pinch on the card of the room he is in: its name is asked (D93 3). */
  onAsk?: (id: string) => void
  /** "Copy this room": the mark on the card of the room he is in. */
  onCopy?: () => void
  /** A card is in his hand, and is let go: thrown away, or where it was. */
  onHold?: (id: string) => void
  onLetGo?: (id: string, thrown: boolean) => void
  /** The card in his hand, and the words he says for it so far. */
  naming?: { id: string; words: string } | null
  /** The room he threw away a moment ago, and the press that brings it back. */
  gone?: NamedRoom | null
  onBack?: () => void
  /** One line for him over the cards, for a little while (the card of the room he stands in went back). */
  said?: string | null
  /** The line that says where rooms are kept was shown, and the palette went: it is not shown again. */
  onTold?: () => void
  /** The rooms whose pages are open already (`useWarmRooms.ts`): each one's card has the dot. */
  warm?: readonly string[]
  /** A pointer rests on a room's card, or no longer (`useWarmRooms.ts`, `rest`; D52). */
  onRest?: (id: string, on: boolean) => void
}

/** The row of cards, its middle at `y` in the palette's frame. */
export function RoomCards({ rooms, onGo, onAdd, onAsk, onCopy, onTold, onHold, onLetGo, naming = null, gone = null, onBack, said = null, warm = [], onRest, y, own }: RoomCardsProps & { y: number; own: (e: ThreeEvent<PointerEvent>) => boolean }) {
  // At the most rooms there is no "+": the line says why, for as long as the cards stand (D109 4).
  const most = rooms.rooms.length >= ROOMS_MOST && !gone ? i18nT('xr.rooms_most', { count: ROOMS_MOST }) : null
  const says = said ?? most
  const told = rooms.rooms.length > 1 && !rooms.told && !says
  const shownOnce = useRef(false)
  if (told) shownOnce.current = true
  const toldNow = useRef(onTold)
  toldNow.current = onTold
  useEffect(() => () => { if (shownOnce.current) toldNow.current?.() }, [])
  // While a removed room is offered back there is no "+": its place in the row is kept for it.
  const list: (NamedRoom | null)[] = [...rooms.rooms, ...(rooms.rooms.length < ROOMS_MOST && !gone ? [null] : [])]
  const spots = cardSpots(list.length, y)
  // The line of where rooms are kept is as wide as the cards that stand; the line that offers a room back, and a
  // line for him (it is whole or absent, and a full store's words do not fit over two cards), as five.
  const width = gone || says ? rowWidth(ROOMS_MOST) : rowWidth(list.length)
  const line = useMemo(() => picture(width, TOLD_DP), [width])
  useEffect(() => () => line.texture.dispose(), [line])
  const [lineFits, setLineFits] = useState(false)
  useEffect(() => { if (told || gone || says) { setLineFits(drawTold(line.canvas, width, says ? null : gone, says)); line.texture.needsUpdate = true } }, [line, told, width, gone, says])
  const backDown = useRef<number | null>(null)
  return (
    <>
      {(told || gone || says) && lineFits && (
        <mesh
          position={[0, y + ROOM_CARD_DP[1] / 2 + PALM_MENU.gapDp / 2 + TOLD_DP / 2, 0]} scale={[width, TOLD_DP, 1]} name="room-cards-line"
          userData={gone ? { xrPress: true } : undefined}
          onPointerDown={(e: ThreeEvent<PointerEvent>) => { if (!gone || own(e)) return; e.stopPropagation(); backDown.current = e.pointerId }}
          onPointerUp={(e: ThreeEvent<PointerEvent>) => { if (!gone) return; e.stopPropagation(); if (backDown.current === e.pointerId) onBack?.(); backDown.current = null }}
        >
          <planeGeometry />
          <meshBasicMaterial map={line.texture} transparent toneMapped={false} />
        </mesh>
      )}
      {list.map((room, i) => (
        <Card
          key={room?.id ?? 'add'} room={room} lit={!!room && room.id === rooms.now} at={spots[i]} own={own}
          onPress={() => (room ? (room.id === rooms.now ? onAsk?.(room.id) : onGo(room.id)) : onAdd())}
          onHold={room && onHold ? () => onHold(room.id) : undefined} onLetGo={room && onLetGo ? thrown => onLetGo(room.id, thrown) : undefined}
          words={room && naming?.id === room.id ? naming.words : null} warm={!!room && warm.includes(room.id)}
          onRest={room && room.id !== rooms.now && onRest ? on => onRest(room.id, on) : undefined}
          onCopy={room && room.id === rooms.now ? onCopy : undefined} copyDim={rooms.rooms.length >= ROOMS_MOST}
        />
      ))}
    </>
  )
}

/** Where the cards stand on the desk: behind the tiles at its left, leaning back to the eye, this wide at most. */
export const DESK_CARDS = { v: 0.44, leanDeg: 30, mostM: 0.34, upM: 0.006 } as const
const NO_HAND = () => false

/**
 * The same row on the desk (`desk=1`, where there is no palette on the hand): the cards stand behind the desk's
 * tiles, each as wide as a tile; five of them are a little smaller, so that the row ends before the first seat.
 */
export function DeskRoomCards({ desk, ...cards }: RoomCardsProps & { desk: DeskPlace }) {
  const slots = Math.min(ROOMS_MOST, cards.rooms.rooms.length + 1)
  const scale = Math.min(DESK.tileM / ROOM_CARD_DP[0], DESK_CARDS.mostM / rowWidth(slots))
  const lean = (DESK_CARDS.leanDeg * Math.PI) / 180
  const up = DESK_CARDS.upM + (ROOM_CARD_DP[1] * scale * Math.cos(lean)) / 2
  return (
    <group position={deskPoint(desk, -DESK.widthM / 2 + (rowWidth(slots) * scale) / 2, DESK_CARDS.v, up)} rotation={[-lean, (-desk.yawDeg * Math.PI) / 180, 0, 'YXZ']} scale={scale} userData={{ xrPress: 'desk' }}>
      <RoomCards {...cards} y={0} own={NO_HAND} />
    </group>
  )
}

/**
 * The same rooms in the preview's bar, for a mouse and for a screen reader: each room a small button that goes there,
 * with a small cross that removes it; the room he stands in is a small field with its name, which he may change;
 * "+" keeps the room as a new one. A removed room is offered back in the same place for a while.
 */
export function RoomsBar({ rooms, onGo, onAdd, gone = null, onBack, onRename, onRemove, warm = [] }: RoomCardsProps & { onRename?: (id: string, name: string) => void; onRemove?: (id: string) => void }) {
  const pill = 'inline-flex h-7 items-center rounded-md border border-border bg-bg text-[12px] text-text'
  return (
    <nav className="flex min-w-0 flex-wrap items-center gap-1" aria-label={i18nT('xr.rooms_label')} data-testid="xr-rooms">
      {rooms.rooms.map(room => (room.id === rooms.now ? (
        <input
          key={`${room.id}:${room.name}`} type="text" defaultValue={room.name} maxLength={48} aria-current="true"
          aria-label={i18nT('xr.room_here', { name: room.name })} title={i18nT('xr.room_rename', { name: room.name })} placeholder={i18nT('xr.room_name_field')}
          className={`${pill} w-[9.5rem] max-w-full border-accent px-2 font-semibold`} data-testid={`xr-room-name-${room.id}`}
          onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); else if (e.key === 'Escape') { e.currentTarget.value = room.name; e.currentTarget.blur() } }}
          onBlur={e => { const name = e.currentTarget.value.trim(); if (name && name !== room.name) onRename?.(room.id, name); else e.currentTarget.value = room.name }}
        />
      ) : (
        <span key={room.id} className={`${pill} max-w-[11rem]`}>
          <button type="button" className="h-full min-w-0 truncate rounded-l-md px-2 hover:bg-bg-elevated" onClick={() => onGo(room.id)} aria-label={i18nT('xr.room_go', { name: room.name })} title={i18nT('xr.room_go', { name: room.name })} data-testid={`xr-room-${room.id}`}>{warm.includes(room.id) && <span aria-hidden="true" data-testid={`xr-room-warm-${room.id}`} className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-accent align-middle" />}{room.name}</button>
          {onRemove && <button type="button" className="h-full rounded-r-md border-l border-border px-1.5 text-muted hover:bg-bg-elevated hover:text-text" onClick={() => onRemove(room.id)} aria-label={i18nT('xr.room_remove', { name: room.name })} title={i18nT('xr.room_remove', { name: room.name })} data-testid={`xr-room-remove-${room.id}`}>×</button>}
        </span>
      )))}
      {rooms.rooms.length < ROOMS_MOST && !gone && <button type="button" className={`${pill} px-2 text-muted hover:bg-bg-elevated hover:text-text`} onClick={onAdd} aria-label={i18nT('xr.room_add')} title={i18nT('xr.room_add')} data-testid="xr-room-add">+</button>}
      {gone && (
        <span role="status" className="inline-flex h-7 items-center gap-1.5 text-[12px] text-muted">
          {i18nT('xr.room_gone', { name: gone.name })}
          <button type="button" className={`${pill} px-2 font-semibold`} onClick={onBack} data-testid="xr-room-back">{i18nT('xr.room_back')}</button>
        </span>
      )}
      {rooms.rooms.length > 1 && !rooms.told && <span className="sr-only">{i18nT('xr.rooms_kept_here')}</span>}
    </nav>
  )
}
