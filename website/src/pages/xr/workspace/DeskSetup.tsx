/**
 * The desk's set-up as it is drawn and timed (`deskSetup.ts`): while it is armed, both hands that lie flat and still
 * spread a grid of dots from under them over the desk-to-be, and when the dots fill it the desk is set there. It ends,
 * and the desk that stands stays, at his first press of anything (a pinch's or a touch's down takes the button:
 * `pinchRoute.owner`, `pinchRoute.touch`)
 * or `SETUP.armedMs` after it was armed, whichever comes first.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { BufferGeometry, Float32BufferAttribute, Vector3, type Group, type Points } from 'three'
import { XR_GL } from '../palette'
import { DESK } from './desk'
import { SETUP, SetupClock, deskHands, onADesk, placeOf, type SetupHand } from './deskSetup'
import { pinchRoute } from './pinchRoute'

type V3 = [number, number, number]

/** The grid: from one dot to the next, and how far from under the hands the dots have spread when the time is whole. */
const GRID = { stepM: 0.04, reachM: 0.7, dotM: 0.006 } as const

/** What a test reads of the set-up. */
/** `spread`: each hand's points, from the lowest to the highest (m), or `null` while it is not tracked. */
export interface SetupProbe { armed: boolean; share: number; seen: boolean; spread: { left: number | null; right: number | null } }

/** How far a hand's points lie from the lowest to the highest (m). */
const spreadOf = (h: SetupHand | null) => (h ? Math.max(...h.points.map(p => p[1])) - Math.min(...h.points.map(p => p[1])) : null)

interface Props {
  /** When the set-up was armed (`performance.now()`), or `null` while it is not. */
  armedAt: number | null
  /** The place that the hands set, or `null` when no hand came. */
  onDone: (place: { at: V3; yawDeg: number } | null) => void
  report?: (read: (() => SetupProbe) | null) => void
}

export function DeskSetup({ armedAt, onDone, report }: Props) {
  const clock = useMemo(() => new SetupClock(), [])
  const camera = useThree(s => s.camera)
  const { eye, look } = useMemo(() => ({ eye: new Vector3(), look: new Vector3() }), [])
  const group = useRef<Group>(null)
  const points = useRef<Points>(null)
  const state = useRef({ share: 0, seen: false })
  const done = useRef(onDone)
  done.current = onDone
  // The desk's dots, the nearest to the middle of the hands first, so that a share of them is a disc that grows.
  const grid = useMemo(() => {
    const out: [number, number][] = []
    for (let u = -DESK.widthM / 2; u <= DESK.widthM / 2 + 1e-9; u += GRID.stepM) for (let v = 0; v <= DESK.depthM + 1e-9; v += GRID.stepM) out.push([u, v])
    const near = (p: [number, number]) => Math.hypot(p[0], p[1] - 0.1)
    out.sort((a, b) => near(a) - near(b))
    const g = new BufferGeometry()
    g.setAttribute('position', new Float32BufferAttribute(out.flatMap(([u, v]) => [u, 0.002, -v]), 3))
    return { geometry: g, reach: out.map(near) }
  }, [])
  useEffect(() => () => grid.geometry.dispose(), [grid])
  useEffect(() => { state.current = { share: 0, seen: false } }, [armedAt])
  useEffect(() => {
    if (!report) return
    report(() => ({ armed: armedAt !== null, share: state.current.share, seen: state.current.seen, spread: { left: spreadOf(deskHands.left), right: spreadOf(deskHands.right) } }))
    return () => report(null)
  })
  useFrame(() => {
    const g = group.current
    const dots = points.current
    if (armedAt === null) { if (g) g.visible = false; return }
    const now = performance.now()
    // Hands that do not lie ahead of and under the eyes are no desk, however flat (`onADesk`).
    camera.getWorldPosition(eye)
    camera.getWorldDirection(look)
    const ahead = Math.hypot(look.x, look.z) > 1e-6 ? ([look.x / Math.hypot(look.x, look.z), look.z / Math.hypot(look.x, look.z)] as const) : ([0, -1] as const)
    const eyeAt: V3 = [eye.x, eye.y, eye.z]
    const left = deskHands.left && onADesk(deskHands.left, eyeAt, ahead) ? deskHands.left : null
    const right = deskHands.right && onADesk(deskHands.right, eyeAt, ahead) ? deskHands.right : null
    if (deskHands.left || deskHands.right) state.current.seen = true
    if (now - armedAt > SETUP.armedMs || pinchRoute.owner || pinchRoute.touch) { if (g) g.visible = false; done.current(null); return }
    const { share, place } = clock.read(left, right, now)
    state.current.share = share
    if (g && dots && left && right && share > 0) {
      const at = placeOf(left, right)
      g.visible = true
      g.position.set(...at.at)
      g.rotation.set(0, (-at.yawDeg * Math.PI) / 180, 0)
      const r = share * GRID.reachM
      let n = 0
      while (n < grid.reach.length && grid.reach[n] <= r) n += 1
      dots.geometry.setDrawRange(0, n)
    } else if (g) g.visible = false
    if (place) done.current(place)
  })
  return (
    <group ref={group} visible={false}>
      <points ref={points} geometry={grid.geometry} pointerEvents="none">
        <pointsMaterial color={XR_GL.ink} size={GRID.dotM} sizeAttenuation transparent opacity={0.85} depthWrite={false} toneMapped={false} />
      </points>
    </group>
  )
}
