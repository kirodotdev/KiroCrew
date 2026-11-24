/**
 * The Bots' desks with no row (`places.ts`): each a small sloped top on one short leg and a foot, where its Bot's
 * place is. The top leans toward him, as a writing desk does, so that he sees it and not its edge.
 *
 * A desk says, with no motion, whether its Bot works: it is lit in the Bot's own colour while the Bot works at it,
 * and dark wood while the Bot has nothing to do or has failed. A desk that a page or Home's large surface covers is
 * not drawn. Nothing stands on a desk yet, and it takes no pointer.
 *
 * All the desks are one draw: one shape, drawn once for each desk with its own place and colour.
 */
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { BoxGeometry, BufferAttribute, CircleGeometry, Color, DoubleSide, MeshBasicMaterial, Object3D, type BufferGeometry, type InstancedMesh } from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { PLACES } from './places'

/** The wood of a desk that nobody works at: dim, since black is clear glass in the glasses and a dark desk must still be seen. */
const DARK = 0x4a3d30
/** How bright each part is against the top: the edge, the leg, the foot. */
const SHADE = { top: 1, edge: 0.62, leg: 0.5, foot: 0.42 } as const

export interface DeskView { key: string; at: readonly [number, number, number]; yaw: number; lit: boolean; hidden: boolean; tint: number }

/** One desk's shape, its top's middle at the origin, facing +z: every part with its own shade of the desk's colour. */
function deskShape(): BufferGeometry {
  const d = PLACES.desk
  const tilt = (d.tiltDeg * Math.PI) / 180
  const shaded = (g: BufferGeometry, shade: number) => {
    const n = g.getAttribute('position').count
    g.setAttribute('color', new BufferAttribute(new Float32Array(n * 3).fill(shade), 3))
    return g.index ? g.toNonIndexed() : g
  }
  const parts = [
    // The top: its near edge (toward him) lower than its far one; and the lip at that edge.
    shaded(new BoxGeometry(d.wideM, 0.008, d.deepM).rotateX(tilt), SHADE.top),
    shaded(new BoxGeometry(d.wideM, 0.012, 0.006).translate(0, -0.006, d.deepM / 2).rotateX(tilt), SHADE.edge),
    shaded(new BoxGeometry(0.016, d.legM, 0.016).translate(0, -d.legM / 2, 0), SHADE.leg),
    shaded(new CircleGeometry(0.035, 20).rotateX(-Math.PI / 2).translate(0, -d.legM, 0), SHADE.foot),
  ]
  const merged = mergeGeometries(parts)
  for (const p of parts) p.dispose()
  if (!merged) throw new Error('the desk has no shape')
  return merged
}

export function PlaceDesks({ desks }: { desks: readonly DeskView[] }) {
  const shape = useMemo(deskShape, [])
  const wood = useMemo(() => new MeshBasicMaterial({ vertexColors: true, side: DoubleSide, toneMapped: false }), [])
  useEffect(() => () => { shape.dispose(); wood.dispose() }, [shape, wood])
  const mesh = useRef<InstancedMesh>(null)
  useLayoutEffect(() => {
    const m = mesh.current
    if (!m) return
    const at = new Object3D()
    const colour = new Color()
    desks.forEach((desk, i) => {
      at.position.set(desk.at[0], desk.at[1], desk.at[2])
      at.rotation.set(0, desk.yaw, 0)
      at.scale.setScalar(desk.hidden ? 0 : 1)
      at.updateMatrix()
      m.setMatrixAt(i, at.matrix)
      m.setColorAt(i, colour.set(desk.lit ? desk.tint : DARK))
    })
    m.count = desks.length
    m.instanceMatrix.needsUpdate = true
    if (m.instanceColor) m.instanceColor.needsUpdate = true
  }, [desks])
  // The desks stand all round him: the shape's own bounds say nothing of where they are.
  return <instancedMesh key={desks.length} ref={mesh} args={[shape, wood, Math.max(1, desks.length)]} frustumCulled={false} raycast={() => null} name="xr-place-desks" />
}
