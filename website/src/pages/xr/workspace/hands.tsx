/**
 * The hands and devices of a session in the workspace of pages: each has a ray, and its pinch presses that ray's
 * pointer. Both are the framework's (`@react-three/xr`). In front of the press stands the page's own measured rule
 * (`pinchGate.ts`): a pinch that the tracker drops for a frame does not let go of what the hand holds.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { XRHandModel, XRSpace, createXRStore, useRayPointer, useXR, useXRInputSourceEvent, useXRInputSourceStateContext } from '@react-three/xr'
import { Suspense, createContext, useContext, useEffect, useMemo, useRef } from 'react'
import { Vector3, type Group } from 'three'
import { ModelGuard } from '../ModelGuard'
import { PAGES } from './layout'
import { headPress } from './HeadAim'
import { deskHands, type SetupHand } from './deskSetup'
import { PinchGate } from './pinchGate'
import { ROOM_THING_M, TipsPinch, holdsLong, pinchRoute, raised, releaseCancels, whatIs, type Handed, type Press, type PressTo, type TipsDesk, type V3 } from './pinchRoute'

export type PagesStore = ReturnType<typeof createXRStore>

/** `hands=1` with `probe=1` in the address: a test's AR session paints the hand models, so that a film shows the emulated hands. */
const HANDS_DRAWN = (() => { try { const q = new URLSearchParams(window.location.search); return q.get('probe') === '1' && q.get('hands') === '1' } catch { return false } })()

/** The joints that the desk's set-up reads of a hand: the wrist, the palm, and the five finger tips. */
const SETUP_JOINTS = ['wrist', 'middle-finger-metacarpal', 'thumb-tip', 'index-finger-tip', 'middle-finger-tip', 'ring-finger-tip', 'pinky-finger-tip'] as const

/** How long a pinch must stay open to count as open, in ms. A test sets 0 to measure the framework's press as it comes. */
export const PinchOpen = createContext<number>(PAGES.pinchOpenMs)

/** The session of the workspace: a floor-level space and tracked hands, and nothing else the framework would add by itself. */
export function makePagesStore(): PagesStore {
  return createXRStore({
    // Layers are asked for, and never required: a session that grants them shows the pages that are looked at on
    // layers (`pageLayer.ts`), and one that does not shows every page on its mesh.
    handTracking: true, anchors: false, layers: true, meshDetection: false, planeDetection: false, depthSensing: false,
    domOverlay: false, hitTest: false, bodyTracking: false,
    emulate: false, offerSession: false, enterGrantedSession: false, frameRate: false,
    // Text wants the whole view at full detail: the renderer would ask for the coarsest edges by itself.
    foveation: 0,
    hand: Hand, controller: Aim,
    transientPointer: false, gaze: false, screenInput: false,
    // Hand models are copied into the build: no request leaves this origin.
    baseAssetPath: new URL(`${import.meta.env.BASE_URL}vendor/xr-profiles/`, window.location.origin).href,
  })
}

/**
 * One tracked hand: its model and its ray. In an AR session the model is not painted: it only hides what is behind
 * the real hand. In a VR session there is no real hand to see, so the model is.
 */
function Hand() {
  const ar = useXR(s => s.mode) === 'immersive-ar'
  return (
    <>
      <ModelGuard><Suspense fallback={null}><XRHandModel colorWrite={!ar || HANDS_DRAWN} /></Suspense></ModelGuard>
      <Aim />
    </>
  )
}

/**
 * The ray of one hand or device, as a pointer of the framework, pressed by its pinch through the gate. Whose a pinch
 * is, is the route's (`pinchRoute.ts`): a hand points only while it is raised; else its ray is off and its pinch is
 * the dot's (`HeadAim.tsx`). A pinch is the device's select when the device has sent one for this hand in this
 * session, else the finger tips' distance; never both.
 */
function Aim() {
  const state = useXRInputSourceStateContext()
  const source = state.inputSource
  const handed: Handed = source.handedness === 'left' ? 'left' : 'right'
  const openMs = useContext(PinchOpen)
  const space = useRef<Group>(null)
  // The ray's pointer has a place of its own: the hand's, but for the time in which a pinch that presses it is open
  // and not yet let go. What it holds (a page, a board) lands where the fingers opened.
  const ray = useRef<Group>(null)
  const pointer = useRayPointer(ray, state)
  // After the framework has placed the hand's space (its priority is -100) and before any pointer moves.
  useFrame(() => {
    const from = space.current as (Group & { transformReady?: boolean }) | null
    const to = ray.current as (Group & { transformReady?: boolean }) | null
    if (!from || !to) return
    const own = pinchRoute.owner
    if (own?.open && own.to === handed) return
    to.matrix.copy(from.matrix)
    to.transformReady = from.transformReady
  }, -50)
  const gate = useMemo(() => new PinchGate(openMs), [openMs])
  const tips = useMemo(() => new TipsPinch(), [])
  const ref = useXR(s => s.originReferenceSpace)
  const camera = useThree(s => s.camera)
  const where = useMemo(() => ({ hand: new Vector3(), eye: new Vector3() }), [])
  /** What the pointer is told of a press: which button, and when. */
  const at = () => ({ button: 0, timeStamp: performance.now() }) as unknown as PointerEvent
  const here = (): V3 => [where.hand.x, where.hand.y, where.hand.z]
  // This hand's ray, as a pinch of either hand presses it while it is the one lit thing. A press held long with the
  // hand still presses, as the dot's does, unless it went down on a thing with its own use for a long hold
  // (`holdsLong`, D84 2): there it is let go by a cancel. Its head is not asked (M53): this ray follows the hand.
  const longUse = useRef(false)
  const press: Press = {
    down: () => { longUse.current = holdsLong(pointer.getIntersection()?.object); pointer.down(at()) },
    up: kind => { if (releaseCancels(kind, longUse.current, false, () => true)) pointer.cancel(at()); else pointer.up(at()) },
    cancel: () => pointer.cancel(at()),
  }
  pinchRoute.presses[handed] = press
  useEffect(() => () => { if (pinchRoute.presses[handed] === press) delete pinchRoute.presses[handed] })
  const pressOf = (to: PressTo | null | undefined): Press | undefined => (to === 'dot' ? headPress : to ? pinchRoute.presses[to] : undefined)
  const what = () => pinchRoute.lit()
  const begin = () => { pressOf(pinchRoute.start(handed, performance.now(), here(), what()))?.down() }
  // How long the pinch was held, and where its hand was, count to when the fingers opened (`pinchRoute.opened`),
  // not to when the gate let it go (`openMs` later).
  const finish = () => {
    const end = pinchRoute.end(handed, performance.now(), here(), what())
    if (!end) return
    // The pages read what the pinch was while its release is handed on (`pinchRoute.ending`).
    pinchRoute.release(end.kind, () => pressOf(end.to)?.up(end.kind))
  }
  const lose = () => { pressOf(pinchRoute.lost(handed))?.cancel() }
  // The other hand pinches while this hand's fingers are open: this pinch ends now, at its own time and place.
  const finishNow = () => { if (gate.tick(Infinity)) finish() }
  pinchRoute.finishers[handed] = finishNow
  useEffect(() => () => { if (pinchRoute.finishers[handed] === finishNow) delete pinchRoute.finishers[handed] })

  // A press that ends with no `select` before its `selectend` was called off by the device (the hand was lost): it
  // ends as a cancel, never as a click.
  const completed = useRef(false)
  useXRInputSourceEvent(source, 'selectstart', () => {
    completed.current = false
    pinchRoute.hands[handed].select = true
    // A pinch that has already stayed open for `openMs` was let go, though no frame has ticked the gate since: it ends
    // now, before the new one begins. Else a pinch 3 frames after a release was taken as the same pinch (taps-f1).
    // It ends first, so it keeps the time and place where its fingers opened (D55 6, small 1); only a pinch that
    // goes on is told that its fingers closed again.
    if (gate.tick(performance.now())) finish()
    pinchRoute.reclosed(handed)
    if (tips.held) { tips.held = false; lose() }
    if (gate.start()) begin()
  }, [gate, pointer])
  useXRInputSourceEvent(source, 'select', () => { completed.current = true }, [])
  useXRInputSourceEvent(source, 'selectend', () => {
    const done = completed.current
    completed.current = false
    if (!done) { if (gate.drop()) lose(); return }
    const now = performance.now()
    pinchRoute.opened(handed, now, here())
    if (gate.end(now)) finish()
  }, [gate, pointer])
  useFrame((_s, _dt, frame?: XRFrame) => {
    const g = space.current
    if (g) {
      g.getWorldPosition(where.hand)
      camera.getWorldPosition(where.eye)
      pinchRoute.move(handed, performance.now(), here())
      const up = raised(where.eye.y, where.hand.y, pinchRoute.deskY)
      const book = pinchRoute.hands[handed]
      book.raised = up
      // Who owns a press is decided at its down and kept until its up or cancel: while a pinch is held, the ray that it
      // presses stays on and every other ray stays off, however the hands move across the line.
      const owner = pinchRoute.owner
      // In the quiet time no ray lights anything (`pinchRoute.light`).
      const on = owner ? owner.to === handed : up && !pinchRoute.quiet(performance.now())
      if (pointer.getEnabled() !== on) pointer.setEnabled(on, at())
      const hit = on ? pointer.getIntersection() : undefined
      book.onPanel = !!hit && hit.distance < ROOM_THING_M
      book.on = book.onPanel ? whatIs(hit) : 'nothing'
      book.point = book.onPanel && hit ? [hit.point.x, hit.point.y, hit.point.z] : null
      // The book is read with the ray on: the light no longer waits for this hand (`pinchRoute.awaited`), and a
      // pinch that began in that wait goes down now, on what the ray lies on.
      if (on) { book.readAt = performance.now(); if (pinchRoute.taken(handed, book.on)) press.down() }
    }
    if (gate.tick(performance.now())) finish()
    // With a desk: this hand's joints for the set-up (`deskSetup.ts`), each at its lowest skin point (less its radius).
    if (pinchRoute.deskY !== null && frame && ref && 'hand' in source && source.hand) {
      const hand = source.hand
      const skin = SETUP_JOINTS.map(name => { const j = hand.get(name); const pose = j && frame.getJointPose?.(j, ref); return pose ? [pose.transform.position.x, pose.transform.position.y - (pose.radius ?? 0), pose.transform.position.z] as [number, number, number] : null })
      deskHands[handed] = skin.every(p => p !== null) ? ({ wrist: skin[0], points: skin } as SetupHand) : null
    } else deskHands[handed] = null
    // The finger tips, for a device that sends no select of its own for this hand.
    if (!pinchRoute.hands[handed].select && frame && ref && 'hand' in source && source.hand) {
      const a = source.hand.get('thumb-tip')
      const b = source.hand.get('index-finger-tip')
      const pa = a && frame.getJointPose?.(a, ref)
      const pb = b && frame.getJointPose?.(b, ref)
      const d = pa && pb ? Math.hypot(pa.transform.position.x - pb.transform.position.x, pa.transform.position.y - pb.transform.position.y, pa.transform.position.z - pb.transform.position.z) : null
      // With a desk: a flat hand (its middle and ring tips as low as the index tip and the thumb) pinches nothing,
      // and the wrist's place says whether the hand is coming to rest.
      let desk: TipsDesk | null = null
      if (pinchRoute.deskY !== null) {
        const hand = source.hand
        const poseOf = (name: XRHandJoint) => { const j = hand.get(name); return (j && frame.getJointPose?.(j, ref)) || null }
        const tipY = (name: XRHandJoint) => { const pose = poseOf(name); return pose ? pose.transform.position.y - (pose.radius ?? 0) : null }
        const ys = (['index-finger-tip', 'middle-finger-tip', 'ring-finger-tip', 'thumb-tip'] as const).map(tipY)
        const w = poseOf('wrist')?.transform.position
        desk = { flat: ys.every(y => y !== null) && Math.max(...(ys as number[])) - Math.min(...(ys as number[])) < 0.012, wrist: w ? [w.x, w.y, w.z] : null }
      }
      const change = tips.read(d, performance.now(), desk)
      if (change === 'down') begin()
      else if (change === 'up') { pinchRoute.opened(handed, performance.now(), here()); finish() }
      else if (change === 'lost') lose()
    }
  })
  // A hand that leaves lets go of nothing by itself: what it held is dropped where it is.
  // `lose` reads the route, the pointer and the hand's side only: the one of the first render does the same.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => () => { deskHands[handed] = null; if (gate.drop() || tips.held) lose() }, [gate, pointer, tips])
  return (
    <>
      <XRSpace ref={space} space="target-ray-space" />
      <group ref={ray} matrixAutoUpdate={false} />
    </>
  )
}
