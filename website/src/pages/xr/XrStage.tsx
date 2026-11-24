/**
 * The framework side of `/xr`.
 *
 * React Three Fiber owns the canvas, the renderer, its size, the frame loop and the scene graph: everything that is
 * drawn is a component (`Land.tsx`, `Bot.tsx`, `Panels.tsx`, `Tray.tsx`, `Marks.tsx`). `@react-three/xr` owns the
 * immersive session, the hands and devices in it, their pointers and the hand models. Every pointer is the
 * framework's: the mouse of the preview, one ray for each hand or device of a session, and one that follows the
 * head. This file hands the scene (`scene.ts`) what they report. The scene keeps every rule about what a hand means.
 */
import { Suspense, createContext, useContext, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Canvas, useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import {
  PointerEvents, XR, XRHandModel, XRSpace, createXRStore, noEvents, useRayPointer, useXR, useXRInputSourceEvent, useXRInputSourceStateContext,
} from '@react-three/xr'
import { BackSide, Quaternion, Ray, Vector3, type Group, type Object3D, type PerspectiveCamera } from 'three'
import { Land } from './Land'
import { Marks } from './Marks'
import { ModelGuard } from './ModelGuard'
import { XR_GL } from './palette'
import { Panels } from './Panels'
import type { XrScene } from './scene'
import { PREVIEW_FOV } from './sceneBase'
import type { Pointer } from './sceneTypes'
import type { XrSourceLike } from './sources'
import { Targets, inLine } from './Targets'
import { Tray } from './Tray'

export type XrStore = ReturnType<typeof createXRStore>

/** The scene, for the parts the framework mounts by itself: the hands and devices of a session. */
const Live = createContext<XrScene | null>(null)

/**
 * The session the page asks for is small on purpose: a floor-level space is required and tracked hands are wanted.
 * Every other feature the framework would add by itself is off.
 */
export function makeXrStore(): XrStore {
  return createXRStore({
    handTracking: true, anchors: false, layers: false, meshDetection: false, planeDetection: false, depthSensing: false,
    domOverlay: false, hitTest: false, bodyTracking: false,
    // The page has its own entry button, the tests bring their own emulated runtime, and the device keeps its own frame rate.
    emulate: false, offerSession: false, enterGrantedSession: false, frameRate: false,
    // A hand is drawn and has a ray. A device has a ray and is not drawn. What either one means is the scene's rule.
    hand: Hand, controller: Device,
    transientPointer: false, gaze: false, screenInput: false,
    // Hand models and device profiles are copied into the build (`public/vendor/xr-profiles`, which the gateway
    // serves with the other vendored files): no request leaves this origin.
    baseAssetPath: new URL(`${import.meta.env.BASE_URL}vendor/xr-profiles/`, window.location.origin).href,
  })
}

/**
 * One tracked hand: its model and its ray. In an AR session the model is not painted: it only hides what is behind
 * the real hand, so the hand is seen in front of the Bots, as a hand in a room is. In a VR session there is no real
 * hand to see, so the model is.
 */
function Hand() {
  const ar = useXR(s => s.mode) === 'immersive-ar'
  return (
    <>
      <ModelGuard><Suspense fallback={null}><XRHandModel colorWrite={!ar} /></Suspense></ModelGuard>
      <Aim />
    </>
  )
}

/** A device that is held: a controller, a puck. It aims and pinches like a hand, and the scene never lets it approve. */
function Device() { return <Aim /> }

const AHEAD = new Vector3()
/** Every ray of the session takes the part in its way by the same rule as the mouse. */
const RAY = { customSort: inLine }
const TURN = new Quaternion()

/**
 * The ray of one hand or device, as a pointer of the framework, and what the scene is told about it: that it is
 * there, where it aims, where a hand's joints are, and its pinch.
 */
function Aim() {
  const scene = useContext(Live)
  const state = useXRInputSourceStateContext()
  const source = state.inputSource
  const session = useXR(s => s.session)
  const origin = useXR(s => s.originReferenceSpace)
  const gl = useThree(s => s.gl)
  const space = useRef<Group>(null)
  const me = useRef<Pointer | null>(null)
  const ray = useRef(new Ray())
  useRayPointer(space, state, RAY)

  // The framework can keep a hand or device it is still loading the profile of after the session dropped it.
  // The session's own list says who is there, at the moment it changes: one that left must stop aiming at once.
  const [there, setThere] = useState(true)
  useEffect(() => {
    if (!session) return
    const sync = () => setThere([...session.inputSources].includes(source))
    sync()
    session.addEventListener('inputsourceschange', sync)
    return () => session.removeEventListener('inputsourceschange', sync)
  }, [session, source])
  useEffect(() => {
    if (!scene || !there) return
    const c = scene.addSource(source, source.hand ? [...source.hand.keys()] : [])
    me.current = c
    return () => { me.current = null; scene.removeSource(c) }
  }, [scene, source, there])

  // A pinch that begins or ends is read where the hand is at that moment: the event carries its own frame, which can be
  // a frame ahead of the last one drawn. A flick that ends as the pinch opens is read from exactly that.
  const select = (what: 'start' | 'done' | 'end') => (e: XRInputSourceEvent) => {
    const c = me.current
    const ref = origin ?? gl.xr.getReferenceSpace()
    if (!scene || !c) return
    const pose = ref ? e.frame.getPose(source.targetRaySpace, ref) : null
    if (pose) {
      const { position: p, orientation: q } = pose.transform
      ray.current.origin.set(p.x, p.y, p.z)
      ray.current.direction.set(0, 0, -1).applyQuaternion(TURN.set(q.x, q.y, q.z, q.w))
      scene.aim(c.id, ray.current)
    }
    scene.select(c, what)
  }
  useXRInputSourceEvent(source, 'selectstart', select('start'), [scene, origin])
  useXRInputSourceEvent(source, 'select', select('done'), [scene, origin])
  useXRInputSourceEvent(source, 'selectend', select('end'), [scene, origin])

  // Before the scene's own frame: where it aims, from the framework's own space for its ray, and where a hand's joints are.
  useFrame((_state, _dt, frame) => {
    const c = me.current
    const g = space.current
    if (!scene || !c || !g) return
    if (g.visible) {
      g.updateWorldMatrix(true, false)
      ray.current.origin.setFromMatrixPosition(g.matrixWorld)
      ray.current.direction.copy(AHEAD.set(0, 0, -1)).transformDirection(g.matrixWorld)
      scene.aim(c.id, ray.current)
    }
    const ref = origin ?? gl.xr.getReferenceSpace()
    if (!frame || !ref || !source.hand) return
    for (const [name, joint] of source.hand) {
      const j = c.joints[name]
      const pose = frame.getJointPose?.(joint, ref)
      j.visible = !!pose
      if (pose) j.position.set(pose.transform.position.x, pose.transform.position.y, pose.transform.position.z)
    }
  }, -1)
  return <XRSpace ref={space} space="target-ray-space" />
}

/** The preview's camera: a wide lens at eye height. The scene aims it; the framework keeps its aspect. */
const CAMERA = { fov: PREVIEW_FOV, near: 0.01, far: 200 } as const
/** Antialiased, and see-through for the room behind an AR session. With `flat` there is no tone curve: colours are drawn as the palette names them. */
const GL = { alpha: true, antialias: true } as const

interface StageProps {
  store: XrStore
  /** Makes the scene once the framework has a camera for it. A new function makes a new scene. */
  make: (camera: PerspectiveCamera) => XrScene
  onScene: (scene: XrScene | null) => void
  /** The box around the canvas takes the keys, and says what the picture is to a screen reader. */
  label: string
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void
  onKeyUp: (e: KeyboardEvent<HTMLDivElement>) => void
}

export function XrStage({ store, make, onScene, label, onKeyDown, onKeyUp }: StageProps) {
  const [scene, setScene] = useState<XrScene | null>(null)
  return (
    <Canvas
      className="block h-full w-full outline-none"
      tabIndex={0}
      aria-label={label}
      data-testid="xr-canvas"
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      flat
      dpr={[1, 2]}
      gl={GL}
      camera={CAMERA}
      // The framework's own pointer events, for the mouse as for the hands: one kind of event for every pointer.
      events={noEvents}
    >
      <Live.Provider value={scene}>
        {/* The mouse is found again every frame: the head of the preview sways, and things move under a mouse that rests. */}
        <PointerEvents intersectEveryFrame pointerTypePrefix="screen-" customSort={inLine} />
        <XR store={store}>
          <Stage make={make} onScene={s => { setScene(s); onScene(s) }} />
        </XR>
      </Live.Provider>
    </Canvas>
  )
}

/** What a pointer event of the framework carries beyond the browser's own: which pointer it is. */
type Pointed = ThreeEvent<PointerEvent> & { pointerType?: string; pointerState?: { inputSource?: XrSourceLike; head?: true } }

/** A pointer of the screen: the mouse of the preview. */
const onScreen = (e: Pointed): boolean => (e.pointerType ?? '').startsWith('screen')

/** The pointer that follows the head. It only looks: until a hand points, what the person looks at is what lights. */
const HEAD = { head: true } as const

function HeadAim({ scene }: { scene: XrScene }) {
  const at = useRef<Group>(null)
  useRayPointer(at, HEAD, RAY)
  useFrame(() => {
    if (!at.current) return
    at.current.position.copy(scene.viewer.position)
    at.current.quaternion.copy(scene.viewer.quaternion)
  }, -60)
  return <group ref={at} />
}

const MOUSE = new Ray()
/**
 * Where the mouse aims: from the eye, through the point it is on. (For a pointer of the screen the event's own `ray`
 * is the camera's line of sight, not the line through the mouse.)
 */
const through = (e: ThreeEvent<PointerEvent>): Ray => MOUSE.set(e.ray.origin, AHEAD.copy(e.point).sub(e.ray.origin).normalize())

/** Inside the canvas: makes the scene, mounts the parts that draw it, runs its frame, and tells it about the session and the mouse. */
function Stage({ make, onScene }: Pick<StageProps, 'make' | 'onScene'>) {
  const gl = useThree(s => s.gl)
  const three = useThree(s => s.scene)
  const size = useThree(s => s.size)
  const get = useThree(s => s.get)
  const [scene, setScene] = useState<XrScene | null>(null)
  const session = useXR(s => s.session)
  const visibility = useXR(s => s.visibilityState)
  const space = useXR(s => s.originReferenceSpace)

  useEffect(() => {
    // The preview's camera, as the framework made it. It goes into the scene graph so the frame that hangs on it is drawn.
    const camera = get().camera as PerspectiveCamera
    const made = make(camera)
    three.add(camera)
    setScene(made)
    onScene(made)
    return () => {
      onScene(null)
      setScene(null)
      three.remove(camera)
      made.dispose()
    }
    // `onScene` is the page's own setter; a new `make` is what asks for a new scene.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [make, three, get])

  useEffect(() => { scene?.fit(size.width, size.height) }, [scene, size])

  // The room shows through an AR session; the preview has the page's own dark behind it.
  useEffect(() => {
    gl.setClearColor(XR_GL.room, session ? 0 : 1)
    if (!scene || !session) return
    scene.sessionStarted()
    return () => scene.sessionEnded()
  }, [gl, scene, session])
  useEffect(() => { if (scene && session && visibility && visibility !== 'visible') scene.sessionHidden() }, [scene, session, visibility])

  useFrame((_state, _dt, frame) => {
    if (!scene) return
    // The head: the viewer pose itself, which is between the eyes. (three's own XR camera sits on the left eye.)
    const ref = space ?? gl.xr.getReferenceSpace()
    const pose = frame && ref ? frame.getViewerPose(ref) : null
    if (pose) {
      const { position: p, orientation: q } = pose.transform
      scene.viewer.position.set(p.x, p.y, p.z)
      scene.viewer.quaternion.set(q.x, q.y, q.z, q.w)
      scene.viewer.updateMatrixWorld()
    }
    scene.step()
  })

  if (!scene) return null
  return (
    // Every pointer event of the scene comes up to here. The mouse's are passed on; a hand's ray and pinch come through `Aim`.
    <group
      onPointerMove={(e: Pointed) => {
        // What the pointer is on is the framework's to say: the part its ray met first. The scene is told which part, by which pointer.
        const on = e.object as Object3D
        if (onScreen(e)) {
          const ray = through(e)
          scene.aim('mouse', ray, e.pointer)
          scene.meets('mouse', on, e.point, e.uv, ray)
        } else if (e.pointerState === HEAD) scene.meets('head', on, e.point, e.uv, e.ray)
        else if (e.pointerState?.inputSource) scene.meets(e.pointerState.inputSource, on, e.point, e.uv, e.ray)
      }}
      onPointerDown={(e: Pointed) => {
        if (!onScreen(e) || e.button !== 0) return
        // The press keeps the mouse when it leaves the picture.
        ;(e.nativeEvent.target as Element | null)?.setPointerCapture?.(e.nativeEvent.pointerId)
        scene.mouseDown()
      }}
      onPointerUp={(e: Pointed) => { if (onScreen(e) && e.button === 0) scene.mouseUp(e.nativeEvent.timeStamp) }}
      onPointerCancel={(e: Pointed) => { if (onScreen(e)) scene.mouseCancel() }}
    >
      {/* Far behind everything: a pointer that is on nothing else is on this, so it still says where it aims. */}
      <mesh>
        <sphereGeometry args={[150, 16, 12]} />
        <meshBasicMaterial side={BackSide} visible={false} />
      </mesh>
      {/* What is only drawn is in no pointer's way. A pointer meets the parts made for it: `Targets` and `Panels`. */}
      <group pointerEvents="none">
        <Land world={scene.world} />
        <Tray scene={scene} />
        <Marks scene={scene} />
      </group>
      <Targets scene={scene} />
      <Panels scene={scene} />
      <HeadAim scene={scene} />
    </group>
  )
}
