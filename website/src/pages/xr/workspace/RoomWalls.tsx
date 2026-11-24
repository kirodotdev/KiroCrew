/**
 * The drawn room: a floor, four walls, a ceiling, a skirting line, a door, a window and a desk (`walls.ts`).
 * Flat colours with no light: nothing shines, and nothing here is brighter than a quarter of a page's white.
 *
 * Every piece of it is solid to a pointer: a ray stops at the first piece it meets, so a page that stands behind a
 * wall or under the desk takes no press, no hover and no wheel through it. What a pointer does on the room is what
 * `hand` says, the same as on empty space; with no `hand` (a session) it does nothing there.
 */
import { BackSide, DoubleSide } from 'three'
import type { ThreeEvent } from '@react-three/fiber'
import { WALLS, WALLS_GL } from './walls'

/** What a pointer does on empty space (`Workspace.tsx`). */
export interface EmptyHand {
  onPointerDown: (e: ThreeEvent<PointerEvent>) => void
  onPointerMove: (e: ThreeEvent<PointerEvent>) => void
  onPointerUp: (e: ThreeEvent<PointerEvent>) => void
  onPointerCancel: (e: ThreeEvent<PointerEvent>) => void
  onWheel: (e: ThreeEvent<WheelEvent>) => void
}

const RAD = Math.PI / 180
const FLAT_UP: [number, number, number] = [-Math.PI / 2, 0, 0]
const FLAT_DOWN: [number, number, number] = [Math.PI / 2, 0, 0]

/** A flat piece of one colour. */
function Flat({ at, turn, size, color }: { at: [number, number, number]; turn?: [number, number, number]; size: [number, number]; color: number }) {
  return (
    <mesh position={at} rotation={turn ?? [0, 0, 0]} scale={[size[0], size[1], 1]}>
      <planeGeometry />
      <meshBasicMaterial color={color} toneMapped={false} />
    </mesh>
  )
}

/** `noDesk`: with `desk=1` the set desk is the room's one desk (`Desk.tsx`), and the room draws none of its own. */
export function RoomWalls({ hand, noDesk = false }: { hand?: EmptyHand; noDesk?: boolean }) {
  const { halfM: h, heightM: up, skirtingM: skirt, door, window: win, desk } = WALLS
  const wide = 2 * h
  // Each wall: where its middle is, and how it is turned to face the room.
  const walls: { at: [number, number, number]; turn: [number, number, number]; color: number }[] = [
    { at: [0, up / 2, -h], turn: [0, 0, 0], color: WALLS_GL.ahead },
    { at: [0, up / 2, h], turn: [0, Math.PI, 0], color: WALLS_GL.behind },
    { at: [-h, up / 2, 0], turn: [0, Math.PI / 2, 0], color: WALLS_GL.left },
    { at: [h, up / 2, 0], turn: [0, -Math.PI / 2, 0], color: WALLS_GL.right },
  ]
  // A little off the wall, so that it is drawn over it.
  const off = 0.01
  const doorX = door.leftM + door.widthM / 2
  const winZ = win.fromM + win.widthM / 2
  const winY = win.sillM + win.heightM / 2
  // The desk's arc: a ring's angle runs from the right-hand side toward straight ahead, a cylinder's from behind.
  const ring: [number, number, number, number, number, number] = [desk.innerM, desk.outerM, 48, 1, (90 - desk.halfDeg) * RAD, 2 * desk.halfDeg * RAD]
  const round = (r: number, height: number): [number, number, number, number, number, boolean, number, number] => [r, r, height, 48, 1, true, (180 - desk.halfDeg) * RAD, 2 * desk.halfDeg * RAD]
  const deep = desk.outerM - desk.innerM
  return (
    // `auto`: a piece stops a ray whether or not a listener is on it.
    <group pointerEvents="auto" {...hand}>
      <Flat at={[0, 0, 0]} turn={FLAT_UP} size={[wide, wide]} color={WALLS_GL.floor} />
      <Flat at={[0, up, 0]} turn={FLAT_DOWN} size={[wide, wide]} color={WALLS_GL.ceiling} />
      {walls.map((w, i) => (
        <group key={i} position={w.at} rotation={w.turn}>
          <Flat at={[0, 0, 0]} size={[wide, up]} color={w.color} />
          <Flat at={[0, -up / 2 + skirt / 2, off]} size={[wide, skirt]} color={WALLS_GL.skirting} />
        </group>
      ))}
      {/* The door, in the wall ahead: a frame, and a leaf that is darker than the wall. */}
      <Flat at={[doorX, door.heightM / 2 + door.frameM / 2, -h + off * 2]} size={[door.widthM + 2 * door.frameM, door.heightM + door.frameM]} color={WALLS_GL.frame} />
      <Flat at={[doorX, door.heightM / 2, -h + off * 3]} size={[door.widthM, door.heightM]} color={WALLS_GL.door} />
      {/* The window, in the wall at the left: a frame, the dark outside, and a bar across. */}
      <group position={[-h, winY, winZ]} rotation={[0, Math.PI / 2, 0]}>
        <Flat at={[0, 0, off * 2]} size={[win.widthM + 2 * win.frameM, win.heightM + 2 * win.frameM]} color={WALLS_GL.frame} />
        <Flat at={[0, 0, off * 3]} size={[win.widthM, win.heightM]} color={WALLS_GL.glass} />
        <Flat at={[0, 0, off * 4]} size={[win.frameM, win.heightM]} color={WALLS_GL.frame} />
      </group>
      {/* The desk: its top, the edge that faces the person, the panel at its far side and one at each end. */}
      {!noDesk && <>
      <mesh position={[0, desk.topM, 0]} rotation={FLAT_UP}>
        <ringGeometry args={ring} />
        <meshBasicMaterial color={WALLS_GL.desk} toneMapped={false} />
      </mesh>
      <mesh position={[0, desk.topM - desk.thickM / 2, 0]}>
        <cylinderGeometry args={round(desk.innerM, desk.thickM)} />
        <meshBasicMaterial color={WALLS_GL.deskSide} side={DoubleSide} toneMapped={false} />
      </mesh>
      <mesh position={[0, (desk.topM - desk.thickM) / 2, 0]}>
        <cylinderGeometry args={round(desk.outerM, desk.topM - desk.thickM)} />
        <meshBasicMaterial color={WALLS_GL.deskSide} side={BackSide} toneMapped={false} />
      </mesh>
      {[-1, 1].map(side => (
        <group key={side} rotation={[0, -side * desk.halfDeg * RAD, 0]}>
          <mesh position={[0, (desk.topM - desk.thickM) / 2, -(desk.innerM + deep / 2)]} rotation={[0, Math.PI / 2, 0]} scale={[deep, desk.topM - desk.thickM, 1]}>
            <planeGeometry />
            <meshBasicMaterial color={WALLS_GL.deskSide} side={DoubleSide} toneMapped={false} />
          </mesh>
        </group>
      ))}
      </>}
    </group>
  )
}
