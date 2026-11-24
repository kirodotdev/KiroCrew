/**
 * The palette of pages, on the hand. In a session a person has no bar and no keyboard: an open palm turned to the
 * face, held for about a third of a second, shows the pages they can open above that palm, two rows of four tiles.
 * The other hand's ray lights a tile and its pinch opens that page. The palette goes when the palm turns away.
 * Either hand may hold it.
 *
 * The rule is `palm.ts` and the list is `pageMenu.tsx`; this reads the hands' joints from the session, stands the
 * tiles above the fingertips at a size that is the same angle at the eye wherever the hand is, and draws them
 * (`palettePicture.ts`). Outside a session it is not there: the flat preview has the plus in its bar.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { CanvasTexture, Quaternion, SRGBColorSpace, Vector3, type Group, type Mesh } from 'three'
import type { OpenHand } from '../gestures'
import type { V3 } from '../hand'
import { XR_GL } from '../palette'
import { fingerShape, type Joints } from '../pointTalk'
import { DEPTH, PRESS, layIn } from './drawOrder'
import type { PagesStore } from './hands'
import { lookAt, underStrip } from './homePlace'
import { facing } from './layout'
import { PAGE_MENU, menuLabel } from './pageMenu'
import { NEW_CHAT_DP, PaletteCardTile, newChatY, twoTileY } from './PaletteCardTile'
import { PX_PER_DP, drawPalette, iconPicture } from './palettePicture'
import { PALM_MENU, PalmHold, holder, mPerDp, paletteSize, tiles, type Side } from './palm'
import { pinchRoute } from './pinchRoute'
import { copyMarkAt, ROOM_CARD_DP, RoomCards, cardSpots, cardsBlock, type RoomCardsProps } from './RoomCards'
import { ROOMS_MOST } from './rooms'

/** The joints the rule reads: the palm's three, and each finger's four from the knuckle to the tip. */
const JOINTS: readonly string[] = [
  'wrist', 'index-finger-metacarpal', 'pinky-finger-metacarpal',
  ...['index', 'middle', 'ring', 'pinky'].flatMap(finger => ['phalanx-proximal', 'phalanx-intermediate', 'phalanx-distal', 'tip'].map(joint => `${finger}-finger-${joint}`)),
]

/** One tracked hand as the rule reads it, or null when it is no hand or a joint it needs is not known. */
function readHand(frame: XRFrame, space: XRReferenceSpace, source: XRInputSource): OpenHand | null {
  const hand = source.hand
  if (!hand || (source.handedness !== 'left' && source.handedness !== 'right') || !frame.getJointPose) return null
  const at: Record<string, V3 | null> = {}
  for (const name of JOINTS) {
    const joint = hand.get(name as XRHandJoint)
    const pose = joint ? frame.getJointPose(joint, space) : null
    at[name] = pose ? [pose.transform.position.x, pose.transform.position.y, pose.transform.position.z] : null
  }
  const wrist = at.wrist
  const index = at['index-finger-metacarpal']
  const pinky = at['pinky-finger-metacarpal']
  const tip = at['middle-finger-tip']
  if (!wrist || !index || !pinky || !tip) return null
  return { joints: { wrist, index, pinky }, handed: source.handedness, shape: fingerShape(at as Joints), tip }
}

/** What a test reads of the palette: which hand holds it, which tile is lit, and where each tile's middle is in the room. */
export interface PaletteProbe { side: Side | null; lit: number; tiles: { path: string; at: [number, number, number] }[]; newChat: [number, number, number] | null; two?: { on: boolean; at: [number, number, number] } | null; rooms: { id: string; at: [number, number, number] }[]; /** The copy mark on the card of the room he is in. */ roomCopy?: [number, number, number] | null; /** The corners of what the cards take, of each card, of the line when it shows, and of the tiles' plate. */ boxes: { block: V3[]; cards: V3[][]; line: V3[] | null; plate: V3[] } | null }

interface Props {
  store: PagesStore
  /** Where the person's head is, kept up to date by the workspace. */
  head: Vector3
  /** Where the workspace has drawn the dashboard's own icons, one in each `[data-menu-icon]`, out of sight. */
  icons: RefObject<HTMLElement>
  onOpen: (path: string) => void
  /** The "new chat" button over the tiles: the new chat card comes out (`chatCard.ts`). With none, no button. */
  onNewChat?: () => void
  report?: (read: (() => PaletteProbe) | null) => void
  /** The rooms he keeps, as cards over the tiles (`RoomCards.tsx`). With none, no cards. */
  rooms?: RoomCardsProps
  /** "Two in view" (`twoUp.ts`): offered while two pages are open or a pair stands. With none, no button. */
  two?: { on: boolean; onPress: () => void }
}

/** The hand a pointer of the framework belongs to, when it is a hand's. */
const handOf = (e: ThreeEvent<PointerEvent>): string | undefined => (e as unknown as { pointerState?: { inputSource?: { handedness?: string } } }).pointerState?.inputSource?.handedness
/*
 * What is on his palm is nearest but for the dot and a sheet he speaks on (D81, M37): its picture, tiles and cards are
 * drawn in the palette's band of `drawOrder.ts` with no depth test, and its group stands in the palette's press order.
 * With the palm away it is not mounted, so nothing of it is drawn or takes a pinch.
 */
const TARGET = new Vector3()
const CORNER = new Vector3()
const TURNED = new Quaternion()

export function Palette({ store, head, icons, onOpen, onNewChat, report, rooms, two }: Props) {
  const gl = useThree(s => s.gl)
  const session = useXR(s => s.session)
  const origin = useXR(s => s.originReferenceSpace)
  const group = useRef<Group>(null)
  const palms = useMemo(() => ({ left: new PalmHold(), right: new PalmHold() }), [])
  const [side, setSide] = useState<Side | null>(null)
  const held = useRef<Side | null>(null)
  const [lit, setLit] = useState(-1)
  const litNow = useRef(-1)
  litNow.current = lit
  /** The tile a pinch began on, and whose pinch it was. */
  const pressed = useRef<{ tile: number; pointerId: number } | null>(null)
  const stood = useRef(false)
  const [widthDp, heightDp] = paletteSize(PAGE_MENU.length)
  // The cards' row stands over the new chat button, or over the tiles when there is none.
  // The cards' row stands over the highest button: "two in view" when it is offered, else "new chat".
  const cardsY = (two ? twoTileY(heightDp) + NEW_CHAT_DP[1] / 2 : onNewChat ? newChatY(heightDp) + NEW_CHAT_DP[1] / 2 : heightDp / 2) + PALM_MENU.gapDp + ROOM_CARD_DP[1] / 2
  const twoNow = useRef(two)
  twoNow.current = two
  const roomsNow = useRef(rooms)
  roomsNow.current = rooms
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = widthDp * PX_PER_DP
    canvas.height = heightDp * PX_PER_DP
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    return { canvas, texture }
  }, [widthDp, heightDp])
  useEffect(() => () => picture.texture.dispose(), [picture])

  // The tiles are drawn when the palette first shows, and again when the dashboard has more of an icon to show
  // (an app's mark arrives a moment after its place does).
  const shown = side !== null
  useEffect(() => {
    if (!shown) return
    let gone = false
    const draw = async () => {
      const pictures = await Promise.all(PAGE_MENU.map((_page, i) => iconPicture(icons.current?.querySelector(`[data-menu-icon="${i}"]`) ?? null, PALM_MENU.iconDp * PX_PER_DP)))
      if (gone) return
      drawPalette(picture.canvas, PAGE_MENU.map(menuLabel), pictures)
      picture.texture.needsUpdate = true
    }
    void draw()
    const drawn = icons.current
    const watch = drawn ? new MutationObserver(() => { void draw() }) : null
    if (drawn) watch?.observe(drawn, { childList: true, subtree: true, attributes: true })
    return () => { gone = true; watch?.disconnect() }
  }, [shown, icons, picture])

  useFrame((_state, _dt, frame) => {
    const space = origin ?? gl.xr.getReferenceSpace()
    let left: OpenHand | null = null
    let right: OpenHand | null = null
    if (frame && space && gl.xr.isPresenting) {
      for (const state of store.getState().inputSourceStates) {
        if (state.type !== 'hand') continue
        const hand = readHand(frame, space, state.inputSource)
        if (hand?.handed === 'left') left = hand
        else if (hand) right = hand
      }
    }
    const eye: V3 = [head.x, head.y, head.z]
    const now = performance.now()
    const next = holder(palms.left.feed(left, eye, now), palms.right.feed(right, eye, now), held.current)
    if (next !== held.current) {
      held.current = next
      stood.current = false
      pressed.current = null
      setSide(next)
      if (!next) setLit(-1)
    }
    const g = group.current
    const hand = next === 'left' ? left : next === 'right' ? right : null
    if (!g || !hand) return
    layIn(g, 'palette')
    g.traverse(o => {
      const m = o as Mesh
      if (!m.isMesh) return
      for (const mat of [m.material].flat()) mat.depthTest = DEPTH.palette
    })
    // Above the fingertips, so the hand does not cover it, and as big as the same angle at the eye wherever the hand is.
    const dp = mPerDp(Math.hypot(hand.tip[0] - eye[0], hand.tip[1] - eye[1], hand.tip[2] - eye[2]))
    TARGET.set(hand.tip[0], hand.tip[1] + (PALM_MENU.liftDp + heightDp / 2) * dp, hand.tip[2])
    // The palette is his hand's, Home is the room's: where the palette with its cards would lie on Home's strip, it
    // comes down until its cards' row is under the strip, as the eye sees both.
    if (roomsNow.current) {
      facing(TARGET, head, TURNED)
      const block = cardsBlock(cardsY)
      const wide = Math.max(block.width, widthDp)
      const seen = [[-1, -heightDp / 2], [1, -heightDp / 2], [-1, block.top], [1, block.top]].map(([side, y]) => lookAt(head, CORNER.set((side * wide) / 2, y, 0).multiplyScalar(dp).applyQuaternion(TURNED).add(TARGET)))
      const down = underStrip(head, { yaw0: Math.min(...seen.map(s => s.yaw)), yaw1: Math.max(...seen.map(s => s.yaw)), low: Math.min(...seen.map(s => s.pitch)), top: Math.max(...seen.map(s => s.pitch)) })
      if (down > 0) TARGET.y -= Math.tan((down * Math.PI) / 180) * TARGET.distanceTo(head)
    }
    // It follows the hand a little behind it, so a tracker's shake is not the palette's.
    if (stood.current) g.position.lerp(TARGET, 0.4)
    else { g.position.copy(TARGET); stood.current = true }
    facing(g.position, head, g.quaternion)
    g.scale.setScalar(dp)
  })

  useEffect(() => {
    if (!report) return
    report(() => ({
      side: held.current, lit: litNow.current,
      tiles: tiles(PAGE_MENU.length).map((tile, i) => {
        const at = group.current ? group.current.localToWorld(new Vector3(tile.x, tile.y, 0)) : new Vector3()
        return { path: PAGE_MENU[i].path, at: [at.x, at.y, at.z] }
      }),
      newChat: onNewChat && group.current ? (group.current.localToWorld(new Vector3(0, newChatY(heightDp), 0)).toArray() as [number, number, number]) : null,
      two: twoNow.current && group.current ? { on: twoNow.current.on, at: group.current.localToWorld(new Vector3(0, twoTileY(heightDp), 0)).toArray() as [number, number, number] } : null,
      rooms: (() => {
        const list = roomsNow.current?.rooms.rooms ?? []
        const ids = [...list.map(k => k.id), ...(roomsNow.current && list.length < ROOMS_MOST && !roomsNow.current.gone ? ['add'] : [])]
        const g = group.current
        return g ? cardSpots(ids.length, cardsY).map((spot, i) => ({ id: ids[i], at: g.localToWorld(new Vector3(spot.x, spot.y, 0)).toArray() as [number, number, number] })) : []
      })(),
      roomCopy: (() => {
        const r = roomsNow.current
        const g = group.current
        const i = r ? r.rooms.rooms.findIndex(k => k.id === r.rooms.now) : -1
        if (!r || !g || i < 0 || !r.onCopy) return null
        const spot = cardSpots(r.rooms.rooms.length + (r.rooms.rooms.length < ROOMS_MOST && !r.gone ? 1 : 0), cardsY)[i]
        return g.localToWorld(new Vector3(spot.x + copyMarkAt()[0], spot.y + copyMarkAt()[1], 0)).toArray() as [number, number, number]
      })(),
      boxes: (() => {
        const g = group.current
        if (!g || !roomsNow.current) return null
        const box = (x: number, y: number, w: number, h: number): V3[] => [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => g.localToWorld(new Vector3(x + (sx * w) / 2, y + (sy * h) / 2, 0)).toArray() as V3)
        const block = cardsBlock(cardsY)
        const line = g.getObjectByName('room-cards-line')
        const n = roomsNow.current.rooms.rooms.length
        return {
          block: box(0, (block.low + block.top) / 2, block.width, block.top - block.low),
          cards: cardSpots(Math.min(ROOMS_MOST, n + 1), cardsY).map(spot => box(spot.x, spot.y, ...ROOM_CARD_DP)),
          line: line ? box(0, line.position.y, line.scale.x, line.scale.y) : null,
          plate: box(0, 0, widthDp, heightDp),
        }
      })(),
    }))
    return () => report(null)
  }, [report, onNewChat, heightDp, widthDp, cardsY])

  if (!session || !side) return null
  // The hand that holds the palette does not press it: its ray points past its own fingers.
  const own = (e: ThreeEvent<PointerEvent>) => handOf(e) === held.current
  return (
    <group ref={group} userData={{ xrPress: 'palette' }} pointerEventsOrder={PRESS.palette}>
      <mesh scale={[widthDp, heightDp, 1]} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial map={picture.texture} transparent toneMapped={false} />
      </mesh>
      {tiles(PAGE_MENU.length).map((tile, i) => (
        <mesh
          key={PAGE_MENU[i].path}
          // In the picture's own plane: a press area in front of it reached past the drawn card at its edge, as the eye
          // saw it, over the page behind (D55 5). Drawn after the picture, so its light lies on the card.
          position={[tile.x, tile.y, 0]}
          renderOrder={1}
          scale={[tile.w, tile.h, 1]}
          onPointerEnter={(e: ThreeEvent<PointerEvent>) => { if (!own(e)) setLit(i) }}
          onPointerLeave={() => setLit(now => (now === i ? -1 : now))}
          onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); if (!own(e)) pressed.current = { tile: i, pointerId: e.pointerId } }}
          onPointerUp={(e: ThreeEvent<PointerEvent>) => {
            e.stopPropagation()
            const began = pressed.current
            pressed.current = null
            // A pinch that began and ended on this tile, the hand still (a long one too, D84 2): the page opens.
            if (pinchRoute.ending === 'drag') return
            if (began && began.tile === i && began.pointerId === e.pointerId) onOpen(PAGE_MENU[i].path)
          }}
        >
          <planeGeometry />
          {/* What a pinch would take lights before the pinch. Unlit, the tile is still there for a ray to find. */}
          <meshBasicMaterial color={XR_GL.white} visible={lit === i} transparent opacity={0.16} depthWrite={false} polygonOffset polygonOffsetFactor={-1} polygonOffsetUnits={-4} toneMapped={false} />
        </mesh>
      ))}
      {onNewChat && <PaletteCardTile heightDp={heightDp} own={own} onPress={onNewChat} />}
      {two && <PaletteCardTile key={two.on ? 'two-on' : 'two-off'} heightDp={heightDp} own={own} onPress={two.onPress} two={{ on: two.on }} />}
      {rooms && <RoomCards {...rooms} y={cardsY} own={own} />}
    </group>
  )
}
