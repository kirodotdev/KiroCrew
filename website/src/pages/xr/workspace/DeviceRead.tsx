/**
 * Reads the session each frame for the readout's device lines (`deviceLines.ts`): what the session says of itself,
 * how many pages stand on layers, what the session's book of pinches holds (`pinchRoute.ts`), how high each hand's
 * ray begins while the device tracks it, and with a desk how high each index tip is over it (`deskSetup.ts`). It draws nothing, presses
 * nothing and writes nothing into those books. Mounted only with `fps=1`.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { useEffect, useMemo } from 'react'
import { Vector3 } from 'three'
import type { LayerShare } from './Layers'
import { deskHands } from './deskSetup'
import { HandWatch, StillMeter, deviceBook, viewDeg, type DeviceFacts, type PinchFacts } from './deviceLines'
import { layerBudget } from './layerPick'
import { pinchRoute } from './pinchRoute'

/** The fields of a session that the lines read; a browser may leave any of them out. */
interface SessionFacts {
  enabledFeatures?: readonly string[] | null
  environmentBlendMode?: string
  frameRate?: number | null
  maxRenderLayers?: number | null
  renderState?: { baseLayer?: { framebufferWidth?: number; framebufferHeight?: number } | null; layers?: readonly { textureWidth?: number; textureHeight?: number }[] | null }
}

/**
 * The picture the page draws into, and which one was read: the session's one layer of the old kind (its whole
 * frame buffer, which holds every eye's part), else its first layer of the new kind (that layer's texture).
 */
function pictureOf(s: SessionFacts, eyes: number): DeviceFacts['picture'] {
  const base = s.renderState?.baseLayer
  if (base?.framebufferWidth && base.framebufferHeight) return { size: [base.framebufferWidth, base.framebufferHeight], from: 'base', eyes }
  const first = s.renderState?.layers?.[0]
  return first?.textureWidth && first.textureHeight ? { size: [first.textureWidth, first.textureHeight], from: 'layer', eyes } : null
}

export function DeviceRead({ share, layersOff }: { share: LayerShare; layersOff: boolean }) {
  const gl = useThree(s => s.gl)
  const camera = useThree(s => s.camera)
  const mode = useXR(s => s.mode)
  const ref = useXR(s => s.originReferenceSpace)
  const hands = useMemo(() => ({ left: new HandWatch(), right: new HandWatch(), eye: new Vector3() }), [])
  const still = useMemo(() => new StillMeter(), [])
  useEffect(() => () => { deviceBook.live = false }, [])
  useFrame((_s, _dt, frame?: XRFrame) => {
    const session = gl.xr.getSession() as (SessionFacts & { inputSources?: Iterable<XRInputSource> }) | null
    if (!session || !frame) { deviceBook.live = false; return }
    const now = performance.now()
    // Each hand while the device gives its ray a place: the height of where that ray begins, as the page's own rule
    // of a raised hand has it (`pinchRoute.at`, written by `hands.tsx`), and the wrist's where there are joints.
    const seen: Record<'left' | 'right', { ray: number | null; wrist: number | null }> = { left: { ray: null, wrist: null }, right: { ray: null, wrist: null } }
    if (ref) for (const source of session.inputSources ?? []) {
      if (source.handedness !== 'left' && source.handedness !== 'right') continue
      if (!frame.getPose(source.targetRaySpace, ref)) continue
      const wrist = source.hand?.get('wrist')
      const pose = wrist ? frame.getJointPose?.(wrist, ref) : null
      seen[source.handedness] = { ray: pinchRoute.at[source.handedness][1], wrist: pose ? pose.transform.position.y : null }
    }
    camera.getWorldPosition(hands.eye)
    hands.left.read(now, seen.left.ray, hands.eye.y, seen.left.wrist)
    hands.right.read(now, seen.right.ray, hands.eye.y, seen.right.wrist)
    // The set-up's points of a hand: the wrist, the palm, then the thumb's and the index finger's tips (skin points).
    const deskY = pinchRoute.deskY
    const tipOver = (hand: 'left' | 'right') => { const h = deskHands[hand]; return h && deskY !== null ? h.points[3][1] - deskY : null }
    const features = session.enabledFeatures ?? null
    const eyes = gl.xr.getCamera().cameras
    const eye = eyes[0]
    const owner = pinchRoute.owner
    still.readRoute(now, pinchRoute)
    const last = pinchRoute.probe.last
    const held = !!owner && !!last && owner.hand === last.hand && last.up === ''
    const pinch: PinchFacts | null = last ? { hand: last.hand, select: pinchRoute.hands[last.hand].select, held, kind: last.kind, ms: held && owner ? Math.round((owner.open?.at ?? now) - owner.at) : last.ms, on: last.down } : null
    const facts: DeviceFacts = {
      mode,
      blend: session.environmentBlendMode ?? null,
      features,
      frameRate: typeof session.frameRate === 'number' ? session.frameRate : null,
      view: eye ? viewDeg(eye.projectionMatrix.elements) : null,
      picture: pictureOf(session, eyes.length),
      layers: { on: share.onLayer.size, most: layersOff ? 0 : layerBudget(features, session.maxRenderLayers), granted: !!features?.includes('layers'), off: layersOff },
      pinch,
      still: still.last ? { ...still.last } : null,
      hands: { left: hands.left.facts(), right: hands.right.facts() },
      tip: deskY === null ? null : { left: tipOver('left'), right: tipOver('right') },
    }
    deviceBook.facts = facts
    deviceBook.live = true
  })
  return null
}
