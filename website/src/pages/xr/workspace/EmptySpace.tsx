/**
 * The empty space of the preview, where a drag turns the head and the wheel brings the eye nearer, and the room's
 * walls when they are drawn. Moved out of `Workspace.tsx` whole.
 */
import { useThree, type ThreeEvent } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { useEffect, useRef, type MutableRefObject, type ReactNode } from 'react'
import { BackSide, type Object3D } from 'three'
import { PAGES } from './layout'
import { hold, leastNear, lensFor, mostNear, shiftShare, under, wheelNear, type Direction, type Turn, type View } from './previewLook'
import { RoomWalls, type EmptyHand } from './RoomWalls'

/** One drag that turns the preview's head: the pointer that drags, and the direction it took hold of. */
interface Grip { pointerId: number; grab: Direction }
export type TurnRef = MutableRefObject<Turn>
/** The picture on its way to a page: where it began, where it ends, and when it began. A hand on the room ends it. */
/** A turn of the picture that is under way, and the frame's shift that goes with it. */
export type TurningRef = MutableRefObject<{ from: Turn; to: Turn; t0: number; shift?: { from: number; to: number } } | null>
/** How many times nearer than the start view the preview's eye is. 1 is the start view. */
export type NearRef = MutableRefObject<number>
/** The last notch of the wheel on empty space: where the pointer was (client px) and when. */
export type ZoomNotch = { x: number; y: number; at: number }

/** How far away the empty space of the preview is that a drag takes hold of. Farther than any panel can go. */
const BACKDROP_M = 50

/**
 * The canvas and its lens with the eye `near` times nearer. The frame's shift is the one that `view` has: the
 * head is turned so that what was held stays where it was in THAT frame. Less of the shift is in force with the
 * eye nearer (`shiftShare`), so the picture then slides down under the fingers by what the shift lost, and a page
 * that stood flush under the picture's upper edge comes to the picture's middle with its upper edge in view. Were
 * the place under the fingers held to the pixel, the head would tip up by as much, and the page's upper edge
 * would leave the picture.
 */
export const viewAt = (view: View, near: number): View => ({ width: view.width, height: view.height, fovDeg: lensFor(view.width, view.height, near), shift: view.shift })

/**
 * The empty space of the preview. A mouse or a finger that goes down on it, and so on no panel, takes hold of the
 * room there, and the head turns so that the room follows it. The drag stays this one's until it ends, so letting go
 * over a page presses nothing. A mouse wheel on it brings the eye nearer about the picture's middle, or takes it
 * back; the head does not turn to the pointer (`zoomAt` latches the next notch at that place, `wheelLatched`). It is never drawn, and in a session it is not there: a head turns itself.
 *
 * The drawn room, when it is on, is empty space too, and it is nearer: a pointer on a wall, the floor or the desk
 * does the same there, and does not reach a page behind it. In a session the room only stops the ray.
 */
export function EmptySpace({ bridge, turn, turning, near, shift, zoomAt, zoomer, onPress, onLens, walls, noDesk = false, children }: { noDesk?: boolean; bridge: MutableRefObject<{ view: View }>; turn: TurnRef; turning: TurningRef; near: NearRef; shift: MutableRefObject<number>; zoomAt: MutableRefObject<ZoomNotch | null>; zoomer: MutableRefObject<((deltaY: number, x: number, y: number) => void) | null>; onPress: () => void; onLens?: () => void; walls: boolean; children?: (hand: EmptyHand | undefined) => ReactNode }) {
  const session = useXR(s => s.session)
  const canvas = useThree(s => s.gl.domElement)
  const grip = useRef<Grip | null>(null)
  // The room is switched off while a finger holds a wall: the wall goes, and with it the finger's own lift. The
  // drag ends here, so that the next one turns the head.
  useEffect(() => { if (!walls) grip.current = null }, [walls])
  if (session) return <>{walls && <RoomWalls noDesk={noDesk} />}{children?.(undefined)}</>
  const at = (e: ThreeEvent<PointerEvent | WheelEvent>): [number, number] => {
    const box = canvas.getBoundingClientRect()
    return [e.nativeEvent.clientX - box.left, e.nativeEvent.clientY - box.top]
  }
  const letGo = (e: ThreeEvent<PointerEvent>) => { if (grip.current?.pointerId === e.pointerId) grip.current = null }
  /** A notch of the wheel at the client place `(x, y)`: nearer or back about the picture's middle, so what stands there stays there. */
  const zoom = (deltaY: number, x: number, y: number) => {
    turning.current = null
    const view = bridge.current.view
    const [cx, cy] = [view.width / 2, view.height / 2]
    const grab = under(turn.current, cx, cy, view)
    near.current = wheelNear(near.current, deltaY, mostNear(view.width, view.height), leastNear(view.width, view.height))
    onLens?.()
    // The frame's shift in force changes with the nearness (`shiftShare`): the new lens is held with the new one, so
    // a notch in and the same notch back leave the picture where it stood.
    turn.current = hold(grab, cx, cy, { ...viewAt(view, near.current), shift: shift.current * shiftShare(near.current) }, turn.current)
    zoomAt.current = { x, y, at: performance.now() }
  }
  // A page under the pointer hands on a notch that the wheel's last notch on empty space latched (`wheelLatched`).
  zoomer.current = zoom
  const hand: EmptyHand = {
      onPointerDown: (e: ThreeEvent<PointerEvent>) => {
        // A press on no page: the keyboard is no page's from here on.
        onPress()
        // One drag at a time: a second finger on empty space does not take the room from the first.
        if (grip.current) return
        // The finger wins: a turn of the picture that is under way ends where it is.
        turning.current = null
        ;(e.object as Object3D).setPointerCapture(e.pointerId)
        const [x, y] = at(e)
        grip.current = { pointerId: e.pointerId, grab: under(turn.current, x, y, bridge.current.view) }
      },
      onPointerMove: (e: ThreeEvent<PointerEvent>) => {
        const held = grip.current
        if (held?.pointerId !== e.pointerId) return
        // A finger that was on the room before a turn of the picture began wins too.
        turning.current = null
        turn.current = hold(held.grab, ...at(e), bridge.current.view, turn.current)
      },
      onPointerUp: letGo,
      onPointerCancel: letGo,
      onWheel: (e: ThreeEvent<WheelEvent>) => { e.stopPropagation(); zoom(e.nativeEvent.deltaY, e.nativeEvent.clientX, e.nativeEvent.clientY) },
  }
  return (
    <>
      <mesh visible={false} position={[0, PAGES.eyeM, 0]} {...hand}>
        <sphereGeometry args={[BACKDROP_M, 16, 8]} />
        <meshBasicMaterial side={BackSide} />
      </mesh>
      {walls && <RoomWalls hand={hand} noDesk={noDesk} />}
      {children?.(hand)}
    </>
  )
}
