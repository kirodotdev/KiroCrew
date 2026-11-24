/**
 * The part of the layer path that touches the renderer, the session and the framework: it hands them to the
 * director (`pageLayer.ts`) and calls it once a frame.
 *
 * In a session that grants no layers (the emulator, always) the director's budget is 0: it makes nothing, asks the
 * renderer for nothing, and every page stays on its mesh as it is without this component.
 *
 * NEVER RUN in a session with layers. See `pageLayer.ts`.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, type MutableRefObject } from 'react'
import { Quaternion, Vector3, type Object3D } from 'three'
import type { PagesStore } from './hands'
import { LayerDirector, type LayerBinding, type LayerEntry, type LayerGl, type LayerPanel, type QuadLayer } from './pageLayer'

/** What the panels and the layer path share: each panel puts itself in `panels`, and reads `onLayer` to know that its page is shown by a layer. */
export interface LayerShare { panels: Map<string, LayerPanel>; onLayer: Set<string>; writes: Record<string, number> }

/** A quad layer's own fields that are set when it is placed (`XRQuadLayer`). */
interface Placed { space: unknown; transform: unknown; width: number; height: number }
type RigidTransform = new (position: { x: number; y: number; z: number; w: number }, orientation: { x: number; y: number; z: number; w: number }) => unknown

const P = new Vector3()
const Q = new Quaternion()
const S = new Vector3()
const FORWARD = new Vector3()

/** `off` (`layers=0` in the address): the director is told of no session, so every page stays on its mesh, for comparing the two by eye. */
export function Layers({ store, share, head, off = false }: { store: PagesStore; share: LayerShare; head: MutableRefObject<{ head: Vector3 }>; off?: boolean }) {
  const gl = useThree(s => s.gl)
  const director = useMemo(() => {
    const last = new WeakMap<QuadLayer, string>()
    return new LayerDirector({
      session: () => (off ? null : gl.xr.getSession() as { enabledFeatures?: readonly string[]; maxRenderLayers?: number } | null),
      binding: () => (gl.xr.getBinding() as unknown as LayerBinding | null) ?? null,
      gl: () => gl.getContext() as unknown as LayerGl,
      bind: (target, texture) => gl.state.bindTexture(target, texture as WebGLTexture),
      space: () => store.getState().originReferenceSpace ?? gl.xr.getReferenceSpace(),
      add: (entry: LayerEntry) => store.addLayerEntry(entry as never),
      remove: (entry: LayerEntry) => store.removeLayerEntry(entry as never),
      // The layer stands where the page's mesh stands, at the mesh's size. The workspace never moves the session's
      // origin, so a place in the scene is a place in the session's reference space.
      place: (layer, anchor) => {
        const mesh = anchor as Object3D | null
        const Rigid = (globalThis as { XRRigidTransform?: RigidTransform }).XRRigidTransform
        if (!mesh || !Rigid) return
        mesh.updateWorldMatrix(true, false)
        mesh.matrixWorld.decompose(P, Q, S)
        // Set only when it moved: a page that stands still costs nothing here either.
        const key = `${P.x.toFixed(4)},${P.y.toFixed(4)},${P.z.toFixed(4)},${Q.x.toFixed(4)},${Q.y.toFixed(4)},${Q.z.toFixed(4)},${Q.w.toFixed(4)},${S.x.toFixed(4)},${S.y.toFixed(4)}`
        if (last.get(layer) === key) return
        last.set(layer, key)
        const placed = layer as unknown as Placed
        placed.transform = new Rigid({ x: P.x, y: P.y, z: P.z, w: 1 }, { x: Q.x, y: Q.y, z: Q.z, w: Q.w })
        placed.width = S.x / 2
        placed.height = S.y / 2
      },
    }, share.onLayer)
  }, [gl, store, share, off])
  useEffect(() => () => director.dispose(), [director])
  // Not before the frame's other work (a priority of 0, and this component stands after the panels): a picture
  // that a panel hands over in this frame is written in this frame.
  useFrame((s, _dt, frame) => {
    const presenting = s.gl.xr.isPresenting
    const at = head.current.head
    ;(presenting ? s.gl.xr.getCamera() : s.camera).getWorldDirection(FORWARD)
    director.frame(presenting ? frame ?? null : null, [at.x, at.y, at.z], [FORWARD.x, FORWARD.y, FORWARD.z], [...share.panels.values()])
    share.writes = director.writes()
  })
  return null
}
