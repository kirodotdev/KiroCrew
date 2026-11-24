/**
 * The dot at the picture's centre in a session: the head's ray as a pointer of the framework, like a hand's ray or the
 * mouse, pressed by a pinch of a hand that rests anywhere (`hands.tsx`, `pinchRoute.ts`). It feeds the paths that
 * exist: on a streamed page the panel's own press path (the gateway, with its gates), on a page that the XR tab draws
 * itself the hand-in by script (`domAct.ts`), on the room's own things their own handlers. What the dot lies on lights
 * as it lights for a hand's ray.
 *
 * While a pinch is held the ray stays where it was at the down: a turn of the head changes nothing, and only the
 * hand's travel turns it (`heldTurn`): held and moved, a pinch drags (a page scrolls under it).
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useRayPointer, useXR } from '@react-three/xr'
import { useMemo, useRef } from 'react'
import { Euler, Matrix4, Quaternion, Raycaster, Vector3, type Group, type MeshBasicMaterial, type Object3D } from 'three'
import { fadedBots } from '../Bot'
import { XR_GL } from '../palette'
import { carryTurn, dotHold, dragFor, inHisFrame } from './carry'
import { DEPTH, DRAW } from './drawOrder'
import { botOf, frameFirst, hitKind } from './frameFirst'
import { ROOM_THING_M, downAt, headStillOn, heldTurn, holdsLong, pinchRoute, releaseCancels, whatIs, type DownAt, type PinchKind } from './pinchRoute'

const SORT = { customSort: frameFirst }
/** The ray that finds the Bots in front of a frame the dot takes (one dot, so one ray). */
const FADE_RAY = new Raycaster()

/**
 * The dot's size, as an angle at the eye (radians): about a centimetre across at a page's distance, so that it is
 * seen in the picture of a pair of glasses (with its rim about 20 pixels across of the eye's 1200). A dark rim keeps it seen on a white page.
 */
const DOT_RAD = 0.0045

/**
 * What a down on a panel's frame (its edge, a corner) carries: how far the held point is and how wide its page is,
 * in metres. `null` for a down on anything else, the page's own content included.
 */
export function carriedBy(hit: { object: Object3D; distance: number } | undefined): { holdM: number; widthM: number } | null {
  if (!hit || !whatIs(hit).startsWith('panel:')) return null
  // A press area of the frame (the close mark) is pressed, never carried: a click on it keeps the dot where it is (X3).
  for (let o: Object3D | null = hit.object; o && typeof o.userData.xrPanel !== 'string'; o = o.parent) if (o.userData.xrPress) return null
  let panel: Object3D | null = hit.object
  while (panel && typeof panel.userData.xrPanel !== 'string') panel = panel.parent
  let widthM = 0
  panel?.traverse(o => { if (typeof o.userData.xrPage === 'string') widthM = o.scale.x })
  return widthM > 0 ? { holdM: hit.distance, widthM } : null
}

/** How the hands press the dot's pointer: set by the dot while it is in the scene. */
export const headPress: { down: () => void; up: (kind: PinchKind) => void; cancel: () => void } = { down: () => {}, up: () => {}, cancel: () => {} }

export function HeadAim() {
  const session = useXR(s => s.session)
  const camera = useThree(s => s.camera)
  const gl = useThree(s => s.gl)
  const space = useRef<Group>(null)
  const dot = useRef<Group>(null)
  // The camera holds his head's pose only after a frame of the session was drawn with one (the renderer gives it
  // while it draws, after every `useFrame`); until then it holds the flat preview's. And the library moves each
  // pointer before this code runs in a frame, from where the ray stood a frame ago. So the dot's pointer is turned
  // on only once its ray took such a pose in an EARLIER frame: `posed`, then `aimed`. Its first word in a room is
  // then about what he looks at, not about what the flat preview's camera faced (one move went to that page).
  // Off is not enough: when a session shows, the library turns EVERY pointer on by itself
  // (`RootCombinedPointer`, `CombinedPointer.setEnabled`) and moves it once before this code runs. So until
  // then the ray is also marked as having no pose (`transformReady`, as the library marks a hand's space
  // with none): a ray with no pose lies on nothing, whoever turned its pointer on.
  const since = useMemo(() => ({ posed: false, aimed: false }), [])
  const disc = useRef<MeshBasicMaterial>(null)
  const state = useMemo(() => ({}), [])
  // A page's frame is taken before a Bot or its board that stands nearer (D78 1, `frameFirst.ts`).
  const pointer = useRayPointer(space, state, SORT, 'head')
  const scene = useThree(s => s.scene)
  const keep = useMemo(() => ({ plates: [] as Object3D[], platesAt: -Infinity, held: null as { q: Quaternion; p: Vector3 } | null, carry: null as { holdM: number; widthM: number } | null, longUse: false, downOn: null as DownAt | null, eye: new Vector3(), dir: new Vector3(), look: new Matrix4(), up: new Vector3(0, 1, 0), turn: new Quaternion(), euler: new Euler(0, 0, 0, 'YXZ') }), [])
  const at = () => ({ button: 0, timeStamp: performance.now() }) as unknown as PointerEvent
  headPress.down = () => {
    const g = space.current
    if (!g) return
    keep.held = { q: g.quaternion.clone(), p: g.position.clone() }
    keep.carry = carriedBy(pointer.getIntersection())
    keep.longUse = holdsLong(pointer.getIntersection()?.object)
    keep.downOn = downAt(pointer.getIntersection())
    // His frame for this pinch (D68): his head's yaw at the down, kept through the drag.
    dotHold.yaw = keep.euler.setFromQuaternion(keep.held.q, 'YXZ').y
    dotHold.corner = null
    pointer.down(at())
  }
  headPress.up = kind => {
    keep.held = null
    // A press held long with the hand still is a press (D84 2), unless it went down on a thing with its own use for
    // a long hold (`holdsLong`): there it is let go by a cancel. A page drawn by the tab turns that cancel into a
    // mouse's release with no click (`domSource.ts`); a streamed page sends nothing for it.
    // And a long one presses only what his head still looks at (M47): the held ray is the ray of the down.
    if (releaseCancels(kind, keep.longUse, true, () => headStillOn(camera, keep.downOn))) pointer.cancel(at())
    else pointer.up(at())
    keep.downOn = null
  }
  headPress.cancel = () => { keep.held = null; pointer.cancel(at()) }

  useFrame((_s, _dt, frame?: XRFrame) => {
    const g = space.current
    if (!g) return
    if (!session) { since.posed = false; since.aimed = false }
    // A finger near a pad or tile of the desk turns the dot off (`DeskTouch.tsx`).
    const on = !!session && since.aimed && pinchRoute.light() === 'dot' && !pinchRoute.tipNear
    if (pointer.getEnabled() !== on && !keep.held) pointer.setEnabled(on, at())
    // Said after the pointer was turned on or off: a rule that waits for the pointer to be on again counts these frames.
    pinchRoute.dotFrame(performance.now())
    if (!keep.held) {
      camera.getWorldPosition(g.position)
      camera.getWorldQuaternion(g.quaternion)
      // The ray has now taken a pose the session gave: from the next frame on the pointer may be on.
      if (since.posed) since.aimed = true
    } else {
      // Held: the ray of the down, turned only by the hand's travel since the down, up to where the fingers opened
      // (`pinchRoute.place`): what the hand does until the page lets the pinch go drags nothing further.
      const own = pinchRoute.owner
      const now = pinchRoute.place()
      // A page held by its frame (an edge or a corner) is carried farther than the hand goes (`carry.ts`, D65), the
      // hand's travel read in his frame at the down (D68). Inside a page the room's axes and `heldTurn`, as before.
      const travel = own && now ? ([now[0] - own.from[0], now[1] - own.from[1], now[2] - own.from[2]] as const) : null
      const mine = travel && keep.carry ? inHisFrame(travel, dotHold.yaw) : null
      // A drag that has become near and far moves nothing across: the ray stays, the panel brings the page.
      const depth = !!mine && !!own && dragFor(own, mine).kind === 'depth'
      const [yaw, pitch] = !travel || depth ? [0, 0] : mine && keep.carry ? carryTurn(mine, keep.carry.holdM, keep.carry.widthM) : heldTurn(travel)
      keep.euler.set(pitch, yaw, 0)
      g.position.copy(keep.held.p)
      g.quaternion.copy(keep.held.q).multiply(keep.turn.setFromEuler(keep.euler))
      // A corner held by the dot: the dot rides the corner, where the panel stands it now (X6).
      const c = keep.carry ? dotHold.corner : null
      if (c) { keep.look.lookAt(keep.held.p, keep.dir.set(c[0], c[1], c[2]), keep.up); g.quaternion.setFromRotationMatrix(keep.look) }
    }
    ;(g as Group & { transformReady?: boolean }).transformReady = !!session && since.aimed
    // This frame of the session is drawn with his head's pose (when the session has one): the camera holds it from now on.
    if (session && !since.posed && frame) { const ref = gl.xr.getReferenceSpace(); since.posed = !!ref && !!frame.getViewerPose(ref) }
    shown(on)
  })
  // The panels' frames run after this one (this is mounted first, and a page opened later subscribes later): a
  // corner sized in this frame aims the dot again in the same frame, so it never reads the corner a frame late (Q4).
  dotHold.ride = () => {
    const g = space.current
    const c = dotHold.corner
    if (!g || !keep.held || !keep.carry || !c) return
    keep.look.lookAt(keep.held.p, keep.dir.set(c[0], c[1], c[2]), keep.up)
    g.quaternion.setFromRotationMatrix(keep.look)
    shown(!!session && pinchRoute.light() === 'dot' && !pinchRoute.tipNear)
  }
  /** The ray's place is set: the dot where it hits, and what the probe reads. */
  function shown(on: boolean) {
    const g = space.current
    if (!g) return
    g.updateMatrixWorld()
    const hit = on || keep.held ? pointer.getIntersection() : undefined
    const d = dot.current
    if (d) {
      d.visible = on
      const far = hit && hit.distance < ROOM_THING_M ? hit.distance : 1.5
      keep.dir.set(0, 0, -1).applyQuaternion(g.quaternion)
      d.position.copy(g.position).addScaledVector(keep.dir, far * 0.995)
      d.quaternion.copy(g.quaternion)
      d.scale.setScalar(far * DOT_RAD)
    }
    // What the probe reads (`?probe=1`): the ray, what the dot lies on, and whose the light is.
    const near = hit && hit.distance < ROOM_THING_M ? hit : undefined
    // On a page's frame: the Bots nearer on the ray are drawn half seen (D78 1).
    fadedBots.clear()
    if (near && hitKind(near.object) === 'frame') {
      // Only the Bots' own plates (`xrBot`), found once a second: a ray through the whole room would test every
      // Bot's skinned body, which costs frames (close10 and level went red on `dist-m57` while it did).
      const now = performance.now()
      if (now - keep.platesAt > 1000) { keep.platesAt = now; keep.plates.length = 0; scene.traverse(o => { if (typeof o.userData.xrBot === 'string') keep.plates.push(o) }) }
      FADE_RAY.set(g.position, keep.dir.set(0, 0, -1).applyQuaternion(g.quaternion))
      FADE_RAY.far = near.distance
      for (const x of FADE_RAY.intersectObjects(keep.plates, false)) { const slug = botOf(x.object); if (slug) fadedBots.add(slug) }
    }
    const css = near ? (near.object.userData.css as [number, number] | undefined) : undefined
    const what = whatIs(near)
    // Over a page that would drop a press now the dot is not lit: drawn faint (an old picture, D49, D50; any other hold, D53 2).
    const onPage = near?.object.userData.xrPage as string | undefined
    const lit = !(onPage && pinchRoute.notYet.has(onPage))
    const discColor = disc.current?.color
    if (discColor && discColor.getHex() !== (lit ? XR_GL.white : XR_GL.faint)) discColor.setHex(lit ? XR_GL.white : XR_GL.faint)
    pinchRoute.probe.light = session ? pinchRoute.light() : null
    pinchRoute.probe.quietMs = Math.max(0, Math.round(pinchRoute.quietUntil - performance.now()))
    pinchRoute.probe.head = session ? {
      origin: [g.position.x, g.position.y, g.position.z], direction: [keep.dir.x, keep.dir.y, keep.dir.z], enabled: pointer.getEnabled(), faded: [...fadedBots],
      on: near ? { what, lit, panel: what.startsWith('page:') ? what.slice(5) : null, x: css && near.uv ? Math.round(near.uv.x * css[0]) : null, y: css && near.uv ? Math.round((1 - near.uv.y) * css[1]) : null } : null,
    } : null
  }
  return (
    <>
      <group ref={space} />
      <group ref={dot} visible={false} pointerEvents="none">
        {/* Nearest of all (M37, `drawOrder.ts`): both in the transparent pass, so nothing transparent is drawn after them. */}
        <mesh renderOrder={DRAW.dot}>
          <ringGeometry args={[1, 1.5, 32]} />
          <meshBasicMaterial color={XR_GL.raised} depthTest={DEPTH.dot} depthWrite={false} transparent opacity={0.85} toneMapped={false} />
        </mesh>
        <mesh renderOrder={DRAW.dot + 1}>
          <circleGeometry args={[1, 32]} />
          <meshBasicMaterial ref={disc} color={XR_GL.white} depthTest={DEPTH.dot} depthWrite={false} transparent toneMapped={false} />
        </mesh>
      </group>
    </>
  )
}
