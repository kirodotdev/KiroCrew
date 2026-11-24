/**
 * The stamp (Ray's D17: "a stamp appears before me, I grab it with my hand and press it on the board"): a real
 * stamp's shape, a wooden handle and knob on a block with a rubber face, about 8 cm tall, with no word on it. Its face
 * is as wide as the print it leaves (a wide APPROVED stamp). It stands at the right of a held board's middle, within
 * easy reach and off the paper, however tall the board is. He grabs it and carries it over the board, its face turned
 * to the paper; let go with its face on the paper, it presses there (`onPress`, in the picture's own fractions) and
 * goes; let go beside the paper, it goes back to its place and presses nothing.
 *
 * It follows the ray of the press that grabbed it, on the board's plane: the mouse in the flat preview, a raised
 * hand's ray in a session. A hand that rests (the head's dot holds the press) moves only a few centimetres, so there
 * the stamp follows the hand's own travel along the board, `restGain` times. Only its own down grabs it, so a drag
 * that began elsewhere and ends over the paper presses nothing. A press that ends as a cancel presses only when it is
 * his own release of a long pinch (`pinchRoute.ending === 'long'`, as an ask's card reads it); a hand that tracking
 * lost, or any other cancel, puts the stamp back and presses nothing.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXRStore } from '@react-three/xr'
import { useEffect, useMemo, useRef } from 'react'
import { BoxGeometry, DirectionalLight, DoubleSide, HemisphereLight, Mesh, MeshStandardMaterial, Plane, Quaternion, Ray, Scene, Vector3, type Group, type Object3D } from 'three'
import { visibilityTakes } from './botPress'
import { pinchRoute } from './pinchRoute'
import type { Uv } from './boardPicture'

/**
 * The stamp's parts, in metres (its block a little larger than its face, its face given by the print), and where it
 * rests beside the paper (`rest`): down from the board's middle, and out toward him.
 */
const STAMP = {
  handle: { r: 0.014, h: 0.05 }, knob: 0.021, blockOver: 0.005, blockH: 0.02, rubberH: 0.007,
  /**
   * At rest it is `scale` of its size and stands against the paper's right edge with `airM` of air (1 degree at the
   * held board's 0.75 m), never over the paper (D85): its middle is about 23 degrees from the paper's middle, so with
   * his head on the stamp the paper's middle, where the mark lands, and its right half stay in the lens. Grabbed, it
   * is its full size.
   */
  rest: { airM: 0.013, scale: 0.7, down: 0.03, out: 0.04 },
  /** While carried, its face stands this far off the paper. */
  hoverM: 0.03,
  /** A resting hand's travel along the board moves the stamp this many times as far. */
  restGain: 5,
  /** Carried, it leans so that its handle comes up toward him and its side shows (radians about x, y). */
  carryLean: [Math.PI / 2 - 0.62, 0.35],
} as const
/** Its colours, warm and light against the dark room: a honey wood handle, a red knob, a steel block, green rubber. */
const WOOD = 0xc98a4b
const KNOB = 0xc0442e
const BLOCK = 0xa7b0ba
const RUBBER = 0x1f7a45

interface Props {
  /** The board's face, as its group stands in the room: the stamp works in its frame. */
  board: () => Object3D | null
  widthM: number
  heightM: number
  /** The rubber face's width and height (the print's, on this board). */
  faceM: readonly [number, number]
  /** The paper's right edge, from the board's middle. */
  paperRightM: number
  onPress: (at: Uv) => void
}

/**
 * The ray of a press, from where its pointer starts through the point it is on now (the room's pointer events give
 * both, `Crew`'s carry reads them the same way); null when the event has neither.
 */
export function pressRay(e: ThreeEvent<PointerEvent>): Ray | null {
  const ev = e as unknown as { point?: Vector3; pointerPosition?: Vector3 }
  if (!ev.point || !ev.pointerPosition) return null
  const d = ev.point.clone().sub(ev.pointerPosition)
  if (d.lengthSq() < 1e-12) return null
  return new Ray(ev.pointerPosition.clone(), d.normalize())
}

/**
 * Builds the stamp's lit programs while the boards stand, so the first take of a board does not stall compiling them:
 * the stamp is the one lit thing in the room, and the renderer keeps one program for every material of the same kind
 * under the same lights. Its two lights and its two kinds of face (one side, both sides) are what the stamp draws.
 */
export function useStampWarm(session: boolean) {
  const gl = useThree(s => s.gl)
  const camera = useThree(s => s.camera)
  useEffect(() => {
    const g = new BoxGeometry()
    const front = new MeshStandardMaterial({ emissive: KNOB })
    const both = new MeshStandardMaterial({ emissive: RUBBER, side: DoubleSide })
    const scene = new Scene().add(new HemisphereLight(), new DirectionalLight(), new Mesh(g, front), new Mesh(g, both))
    // Again when a session starts: it draws through the session's own camera.
    gl.compile(scene, session && gl.xr.isPresenting ? gl.xr.getCamera() : camera)
    return () => { g.dispose(); front.dispose(); both.dispose() }
  }, [gl, camera, session])
}

/** The stamp of one held board. */
export function BoardStamp({ board, widthM, heightM, faceM, paperRightM, onPress }: Props) {
  const root = useRef<Group>(null)
  const grab = useRef<{ id: number; dot: boolean; from: Vector3 | null; at: Vector3; hand: Vector3 | null } | null>(null)
  const tmp = useMemo(() => ({ plane: new Plane(), n: new Vector3(), q: new Quaternion(), hit: new Vector3(), right: new Vector3(), up: new Vector3() }), [])
  const rest = useMemo(() => new Vector3(paperRightM + STAMP.rest.airM + (STAMP.rest.scale * (faceM[0] + 2 * STAMP.blockOver)) / 2, -STAMP.rest.down, STAMP.rest.out), [paperRightM, faceM])
  const rubber: [number, number, number] = [faceM[0], STAMP.rubberH, faceM[1]]
  const block: [number, number, number] = [faceM[0] + 2 * STAMP.blockOver, STAMP.blockH, faceM[1] + 2 * STAMP.blockOver]
  /** Where on the board's plane a ray falls, in the board's own frame; null when it misses the plane. */
  const onPlane = (e: ThreeEvent<PointerEvent>) => {
    const ray = pressRay(e)
    const g = board()
    if (!g || !ray) return null
    g.updateWorldMatrix(true, false)
    g.getWorldQuaternion(tmp.q)
    tmp.n.set(0, 0, 1).applyQuaternion(tmp.q)
    tmp.plane.setFromNormalAndCoplanarPoint(tmp.n, g.getWorldPosition(tmp.hit))
    const p = new Vector3()
    if (!ray.intersectPlane(tmp.plane, p)) return null
    return g.worldToLocal(p)
  }
  /** A place in the board's frame as the picture's fractions. */
  const uvOf = (p: Vector3): Uv => ({ u: p.x / widthM + 0.5, v: 0.5 - p.y / heightM })
  const hand = () => { const own = pinchRoute.owner; return own ? new Vector3(...pinchRoute.at[own.hand]) : null }
  // Each frame: at rest beside the board, standing; carried, over the board where the press holds it, face to the paper.
  useFrame(() => {
    const r = root.current
    if (!r) return
    const g = grab.current
    if (!g) { r.position.copy(rest); r.rotation.set(0, 0, 0); r.scale.setScalar(STAMP.rest.scale); return }
    r.scale.setScalar(1)
    if (g.dot && g.hand) {
      // A resting hand: its travel along the board, made larger.
      const now = hand()
      const b = board()
      if (now && b) {
        b.getWorldQuaternion(tmp.q)
        tmp.right.set(1, 0, 0).applyQuaternion(tmp.q)
        tmp.up.set(0, 1, 0).applyQuaternion(tmp.q)
        const d = now.clone().sub(g.hand)
        g.at.set((g.from?.x ?? 0) + d.dot(tmp.right) * STAMP.restGain, (g.from?.y ?? 0) + d.dot(tmp.up) * STAMP.restGain, 0)
      }
    }
    r.position.set(g.at.x, g.at.y, STAMP.hoverM)
    // Turned so that its face goes to the paper and its handle comes up toward him, its side seen.
    r.rotation.set(STAMP.carryLean[0], STAMP.carryLean[1], 0, 'YXZ')
  })
  const down = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation()
    // The press stays the stamp's until its up, wherever the ray goes (as a held board's does).
    ;(e.object as Object3D).setPointerCapture(e.pointerId)
    const dot = pinchRoute.owner?.to === 'dot'
    const at = onPlane(e) ?? rest.clone()
    grab.current = { id: e.pointerId, dot, from: at.clone(), at, hand: dot ? hand() : null }
  }
  const move = (e: ThreeEvent<PointerEvent>) => {
    const g = grab.current
    if (!g || g.id !== e.pointerId) return
    e.stopPropagation()
    if (g.dot) return
    const at = onPlane(e)
    if (at) g.at.copy(at)
  }
  const up = (e: ThreeEvent<PointerEvent>) => {
    const g = grab.current
    if (!g || g.id !== e.pointerId) return
    e.stopPropagation()
    grab.current = null
    onPress(uvOf(g.at))
  }
  const cancel = (e: ThreeEvent<PointerEvent>) => {
    const g = grab.current
    // Another pointer's cancel is not this press's end.
    if (!g || g.id !== e.pointerId) return
    grab.current = null
    // Only his own release of a long pinch comes as a cancel and presses (`pinchRoute.release` sets `ending` then); a
    // lost hand's, a hand that unmounts and a pointer's exit go back to the rest, unpressed.
    if (pinchRoute.ending === 'long') onPress(uvOf(g.at))
  }
  // A session that stops being visible, ends or begins leaves the stamp unpressed (D192 2), as the Crew hears the store.
  const xrStore = useXRStore()
  useEffect(() => xrStore.subscribe((state, prev) => {
    if (state.visibilityState !== prev.visibilityState && visibilityTakes(prev.visibilityState, state.visibilityState)) grab.current = null
  }), [xrStore])
  return (
    <group ref={root} userData={{ xrPress: 'stamp' }} name="board-stamp" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel}>
      {/* Its own light: nothing else in the room is lit (every other surface draws its own colour), so these light the stamp alone. */}
      <hemisphereLight args={[0xfff6e8, 0x2a3240, 1.4]} />
      <directionalLight position={[0.3, 0.6, 0.5]} intensity={1.6} />
      {/* The knob, the handle, the block and the rubber face, from the top down. */}
      <mesh position={[0, STAMP.rubberH + STAMP.blockH + STAMP.handle.h + STAMP.knob * 0.7, 0]}>
        <sphereGeometry args={[STAMP.knob, 24, 16]} />
        <meshStandardMaterial color={KNOB} emissive={KNOB} emissiveIntensity={0.18} roughness={0.35} metalness={0.05} />
      </mesh>
      <mesh position={[0, STAMP.rubberH + STAMP.blockH + STAMP.handle.h / 2, 0]}>
        <cylinderGeometry args={[STAMP.handle.r * 0.75, STAMP.handle.r, STAMP.handle.h, 24]} />
        <meshStandardMaterial color={WOOD} emissive={WOOD} emissiveIntensity={0.15} roughness={0.55} metalness={0.05} />
      </mesh>
      <mesh position={[0, STAMP.rubberH + STAMP.blockH / 2, 0]}>
        <boxGeometry args={block} />
        <meshStandardMaterial color={BLOCK} emissive={BLOCK} emissiveIntensity={0.12} roughness={0.3} metalness={0.55} />
      </mesh>
      <mesh position={[0, STAMP.rubberH / 2, 0]}>
        <boxGeometry args={rubber} />
        <meshStandardMaterial color={RUBBER} emissive={RUBBER} emissiveIntensity={0.25} roughness={0.9} side={DoubleSide} />
      </mesh>
      {/* What a press meets: a box a little larger than the stamp, never drawn. */}
      <mesh position={[0, 0.05, 0]}>
        <boxGeometry args={[faceM[0] + 0.03, 0.11, faceM[1] + 0.04]} />
        <meshBasicMaterial visible={false} />
      </mesh>
    </group>
  )
}
