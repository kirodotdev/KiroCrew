/**
 * The desk (`desk.ts`): its top, drawn faint so that the real desk shows through it in the glasses, and the palette's
 * tiles that lie on it. A tile is pressed as a Bot is: the dot and a pinch, a raised hand's ray, the mouse in the
 * flat preview. A press that travels (a drag) presses nothing. The plus's tile shows the pages that the plus lists
 * (`pageMenu.tsx`, `pagesToOpen`) on a small card that stands on the desk beside the tiles; a row of a list that
 * came in the last second takes no press. Each tile and row takes a pointer on an upright box over it, since a flat
 * tile is a thin target for the head's coarse aim.
 */
import type { ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { BufferGeometry, CanvasTexture, DoubleSide, LinearFilter, SRGBColorSpace, Vector3 } from 'three'
import { fmtNumber } from '../../../i18n/format'
import { XR_GL, css } from '../palette'
import { DESK, deskPoint, tilePlaces, type DeskPlace } from './desk'
import { touchTargets } from './deskTouch'
import { pinchRoute } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import { WALLS_GL } from './walls'

/** A row of the list that came this recently takes no press (ms), as on the plus's own list (`OpenPage.tsx`). */
const LIST_CALM_MS = 1000
/**
 * The list's card: its width, one row's height, the most rows that it shows, and where it stands (across, in from
 * the near edge). It is short enough that every row it shows is in the picture from the look that lit the plus's
 * tile, in the glasses too; a longer list scrolls by the two small tiles at its right, `most - 1` rows a press.
 */
const CARD = { widthM: 0.2, rowM: 0.032, most: 5, u: -DESK.widthM / 2 + 0.1, v: DESK.restM + DESK.bandM + 0.06, scrollM: 0.03 } as const
/** A tile's upright target is this tall over the tile. */
const TARGET_M = 0.05

export interface DeskTile { id: string; label: string }
export interface DeskRow { path: string; label: string }

/** What a test reads of the desk. */
export interface DeskProbe {
  at: [number, number, number]
  yawDeg: number
  from: DeskPlace['from']
  confirmed: boolean
  tiles: { id: string; label: string; center: [number, number, number]; lit: boolean }[]
  rows: { path: string; center: [number, number, number] }[]
  listOpen: boolean
  /** The first row the card shows, and how many the list has; the scroll tiles' middles while it is longer. */
  listFrom: number
  listCount: number
  scroll: { up: [number, number, number]; down: [number, number, number] } | null
  /** The others' pad, past the last seat: how many it stands for, and its middle. */
  more: { count: number; center: [number, number, number] } | null
}

interface Props {
  desk: DeskPlace
  tiles: readonly DeskTile[]
  rows: readonly DeskRow[]
  onTile: (id: string) => void
  onRow: (path: string) => void
  /** The others' pad (`seatOrder`): how many Bots have no seat, and its place; a press on it is the tile `more`. */
  more?: { count: number; u: number; v: number } | null
  /** The flat preview's quiet time: nothing takes a pointer. */
  hushed: boolean
  /** With the drawn room on the desk is solid, the room's own desk at the set desk's place. */
  solid?: boolean
  report?: (read: (() => DeskProbe) | null) => void
}

/** A label drawn on a canvas, as wide as `wPx` and as tall as `hPx`, pale on a dark plate. */
function useLabel(text: string, wPx: number, hPx: number, lit: boolean) {
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = wPx
    canvas.height = hPx
    const c = canvas.getContext('2d')
    if (c) {
      c.fillStyle = css(lit ? XR_GL.ink : XR_GL.room)
      c.globalAlpha = 0.92
      c.beginPath()
      c.roundRect(2, 2, wPx - 4, hPx - 4, Math.min(wPx, hPx) * 0.18)
      c.fill()
      c.globalAlpha = 1
      c.fillStyle = css(lit ? XR_GL.room : XR_GL.ink)
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
      // One size of letter on every square tile, smaller only when a word does not fit.
      let size = Math.round(hPx * (wPx === hPx ? 0.17 : 0.42))
      do { c.font = `600 ${size}px ${family}`; size -= 2 } while (size > 12 && c.measureText(text).width > wPx - 16)
      const words = text.split(' ')
      // A label of two words or more on a square tile is set on two lines.
      if (wPx === hPx && words.length > 1 && c.measureText(text).width > wPx * 0.8) {
        const half = Math.ceil(words.length / 2)
        c.fillText(words.slice(0, half).join(' '), wPx / 2, hPx * 0.38, wPx - 12)
        c.fillText(words.slice(half).join(' '), wPx / 2, hPx * 0.64, wPx - 12)
      } else c.fillText(text, wPx / 2, hPx / 2 + 2, wPx - 12)
    }
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return texture
  }, [text, wPx, hPx, lit])
  useEffect(() => () => picture.dispose(), [picture])
  return picture
}

/** A press that begins and ends on one thing, and did not travel, is a press of that thing. */
export function usePress(act: () => void, mayAct: () => boolean = () => true) {
  const held = useRef<{ pointerId: number; at: [number, number] } | null>(null)
  return {
    onPointerDown: (e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); held.current = { pointerId: e.pointerId, at: [e.nativeEvent.clientX, e.nativeEvent.clientY] } },
    onPointerUp: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation()
      const h = held.current
      held.current = null
      if (!h || h.pointerId !== e.pointerId) return
      // A pinch that travelled, or a mouse or finger that moved, is a drag: it presses nothing. A long one presses (D84 2).
      if (pinchRoute.ending === 'drag') return
      if (Math.hypot(e.nativeEvent.clientX - h.at[0], e.nativeEvent.clientY - h.at[1]) > TURN_DRAG_PX) return
      if (mayAct()) act()
    },
    onPointerCancel: () => { held.current = null },
    onPointerLeave: () => { held.current = null },
  }
}

function Tile({ desk, tile, u, v, onTile, sideM = DESK.tileM }: { desk: DeskPlace; tile: DeskTile; u: number; v: number; onTile: (id: string) => void; sideM?: number }) {
  const [lit, setLit] = useState(false)
  const label = useLabel(tile.label, 256, 256, lit)
  const press = usePress(() => onTile(tile.id))
  const yaw = (-desk.yawDeg * Math.PI) / 180
  return (
    <group position={deskPoint(desk, u, v)} rotation-y={yaw}>
      <mesh rotation-x={-Math.PI / 2} position-y={0.002} pointerEvents="none">
        <planeGeometry args={[sideM, sideM]} />
        <meshBasicMaterial map={label} transparent side={DoubleSide} toneMapped={false} />
      </mesh>
      <mesh
        position-y={TARGET_M / 2}
        scale={[sideM, TARGET_M, sideM]}
        userData={{ xrDeskTile: tile.id, xrPress: 'desk' }}
        onPointerEnter={() => setLit(true)}
        {...press}
        onPointerLeave={() => { setLit(false); press.onPointerLeave() }}
      >
        <boxGeometry />
        <meshBasicMaterial visible={false} />
      </mesh>
    </group>
  )
}

/** The height of the row at `i` of the card's rows, at its middle. */
const rowY = (i: number) => CARD.rowM * (CARD.most - i - 0.5)

/** One of the card's two scroll tiles, upright at its right: up at the top row, down at the bottom one. */
function Scroll({ up, onPress }: { up: boolean; onPress: () => void }) {
  const [lit, setLit] = useState(false)
  const label = useLabel(up ? '\u25B2' : '\u25BC', 128, 128, lit)
  const press = usePress(onPress)
  return (
    <mesh
      position={[CARD.widthM / 2 + 0.006 + CARD.scrollM / 2, rowY(up ? 0 : CARD.most - 1), 0.002]}
      scale={[CARD.scrollM, CARD.scrollM, 1]}
      userData={{ xrDeskScroll: up ? 'up' : 'down', xrPress: 'desk' }}
      onPointerEnter={() => setLit(true)}
      {...press}
      onPointerLeave={() => { setLit(false); press.onPointerLeave() }}
    >
      <planeGeometry />
      <meshBasicMaterial map={label} transparent side={DoubleSide} toneMapped={false} />
    </mesh>
  )
}

function Row({ row, i, calmUntil, onRow }: { row: DeskRow; i: number; calmUntil: number; onRow: (path: string) => void }) {
  const [lit, setLit] = useState(false)
  const label = useLabel(row.label, 512, 80, lit)
  const press = usePress(() => onRow(row.path), () => performance.now() >= calmUntil)
  return (
    <mesh
      position={[0, rowY(i), 0.002]}
      scale={[CARD.widthM, CARD.rowM * 0.92, 1]}
      userData={{ xrDeskRow: row.path, xrPress: 'desk' }}
      onPointerEnter={() => setLit(true)}
      {...press}
      onPointerLeave={() => { setLit(false); press.onPointerLeave() }}
    >
      <planeGeometry />
      <meshBasicMaterial map={label} transparent side={DoubleSide} toneMapped={false} />
    </mesh>
  )
}

export function Desk({ desk, tiles, rows, onTile, onRow, more = null, hushed, solid = false, report }: Props) {
  const [listOpen, setListOpen] = useState(false)
  const [calmUntil, setCalmUntil] = useState(0)
  const [from, setFrom] = useState(0)
  const longest = Math.max(0, rows.length - CARD.most)
  const first = Math.min(from, longest)
  const shownRows = rows.slice(first, first + CARD.most)
  const scrolls = rows.length > CARD.most
  const scrollBy = (by: number) => setFrom(f => Math.max(0, Math.min(longest, Math.min(f, longest) + by)))
  // The list's rows read from the dashboard's state can change while it stands: a row that came takes no press for a second.
  const rowsKey = shownRows.map(r => r.path).join('|')
  useEffect(() => { setCalmUntil(performance.now() + LIST_CALM_MS) }, [rowsKey, listOpen])
  const places = tilePlaces(tiles.length)
  const tap = (id: string) => { if (id === 'plus') { setFrom(0); setListOpen(on => !on) } else { setListOpen(false); onTile(id) } }
  const row = (path: string) => { setListOpen(false); onRow(path) }
  // Each tile and the others' pad are targets of a finger's touch (`deskTouch.ts`), pressed as a press on them is.
  useEffect(() => {
    if (hushed) return
    const ids = tiles.map((t, i) => { const id = `tile:${t.id}`; touchTargets.set(id, { center: deskPoint(desk, places[i].u, places[i].v), radiusM: DESK.tileM / 2, press: () => tap(t.id), box: { halfM: DESK.tileM / 2, heightM: TARGET_M, yawDeg: desk.yawDeg } }); return id })
    if (more) { touchTargets.set('tile:more', { center: deskPoint(desk, more.u, more.v), radiusM: DESK.padM / 2, press: () => tap('more'), box: { halfM: DESK.padM / 2, heightM: TARGET_M, yawDeg: desk.yawDeg } }); ids.push('tile:more') }
    return () => { for (const id of ids) touchTargets.delete(id) }
  })
  const yaw = (-desk.yawDeg * Math.PI) / 180
  const middle = deskPoint(desk, 0, DESK.depthM / 2)
  const rim = useMemo(() => new BufferGeometry().setFromPoints([[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => new Vector3((x * DESK.widthM) / 2, (y * DESK.depthM) / 2, 0))), [])
  useEffect(() => () => rim.dispose(), [rim])
  useEffect(() => {
    if (!report) return
    report(() => ({
      at: [...desk.at] as [number, number, number], yawDeg: desk.yawDeg, from: desk.from, confirmed: desk.confirmed, listOpen,
      tiles: tiles.map((t, i) => ({ id: t.id, label: t.label, center: deskPoint(desk, places[i].u, places[i].v, TARGET_M / 2), lit: false })),
      rows: listOpen ? shownRows.map((r, i) => ({ path: r.path, center: deskPoint(desk, CARD.u, CARD.v, rowY(i)) })) : [],
      listFrom: first, listCount: rows.length,
      scroll: listOpen && scrolls ? { up: deskPoint(desk, CARD.u + CARD.widthM / 2 + 0.006 + CARD.scrollM / 2, CARD.v, rowY(0)), down: deskPoint(desk, CARD.u + CARD.widthM / 2 + 0.006 + CARD.scrollM / 2, CARD.v, rowY(CARD.most - 1)) } : null,
      more: more ? { count: more.count, center: deskPoint(desk, more.u, more.v, TARGET_M / 2) } : null,
    }))
    return () => report(null)
  })
  return (
    <group pointerEvents={hushed ? 'none' : undefined}>
      {/* The top: faint, and fainter while it is a guess that nobody confirmed. It takes no pointer: a ray goes on to what is behind. */}
      <group position={middle} rotation-y={yaw}>
        <mesh rotation-x={-Math.PI / 2} pointerEvents="none">
          <planeGeometry args={[DESK.widthM, DESK.depthM]} />
          <meshBasicMaterial color={solid ? WALLS_GL.desk : XR_GL.faint} transparent={!solid} opacity={solid ? 1 : desk.confirmed ? 0.14 : 0.06} side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
        <lineLoop geometry={rim} rotation-x={-Math.PI / 2} position-y={0.001}>
          <lineBasicMaterial color={XR_GL.faint} transparent opacity={desk.confirmed ? 0.7 : 0.3} toneMapped={false} />
        </lineLoop>
      </group>
      {tiles.map((t, i) => <Tile key={t.id} desk={desk} tile={t} u={places[i].u} v={places[i].v} onTile={tap} />)}
      {/* The others' pad: the number of the Bots with no seat; a press opens the Bots page. */}
      {more && <Tile desk={desk} tile={{ id: 'more', label: fmtNumber(more.count, { signDisplay: 'always' }) }} u={more.u} v={more.v} onTile={tap} sideM={DESK.padM} />}
      {listOpen && (
        <group position={deskPoint(desk, CARD.u, CARD.v)} rotation-y={yaw}>
          {shownRows.map((r, i) => <Row key={r.path} row={r} i={i} calmUntil={calmUntil} onRow={row} />)}
          {scrolls && first > 0 && <Scroll up onPress={() => scrollBy(-(CARD.most - 1))} />}
          {scrolls && first < longest && <Scroll up={false} onPress={() => scrollBy(CARD.most - 1)} />}
        </group>
      )}
    </group>
  )
}
