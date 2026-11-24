/**
 * The small marks of the interaction layer, drawn the framework way: the ring round what a pinch would take, and the
 * mark that travels when work is handed to a Bot. Where each one is, the scene says, as a plain transform.
 */
import { createPortal, useFrame, useThree } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { useMemo, useRef, type ReactNode } from 'react'
import { BufferGeometry, DoubleSide, Vector3, type Group, type Object3D } from 'three'
import { DEG, type ViewSpec } from './metrics'
import { XR_GL } from './palette'
import type { XrScene } from './scene'
import { toGl } from './sceneParts'

/** Shown, and every part it stands in is shown. */
export function shown(at: Object3D): boolean {
  for (let o: Object3D | null = at; o; o = o.parent) if (!o.visible) return false
  return true
}

/** Draws its children where a plain transform of the scene says, for as long as that transform is shown. */
export function At({ at, children }: { at: Object3D; children: ReactNode }) {
  const group = useRef<Group>(null)
  useFrame(() => {
    const g = group.current
    if (!g) return
    g.visible = shown(at)
    if (!g.visible) return
    g.matrix.copy(at.matrixWorld)
    g.matrixWorldNeedsUpdate = true
  })
  return <group ref={group} matrixAutoUpdate={false} visible={false}>{children}</group>
}

/**
 * For the desktop preview: a frame around what a pair of glasses shows, with the rest of the picture dimmed.
 * The preview's own lens is wider than the glasses, so without the frame a picture cannot show what fits.
 * It hangs on the camera, a little in front of it.
 */
function ViewFrame({ view }: { view: ViewSpec }) {
  const camera = useThree(s => s.camera)
  const d = 0.2
  const w = d * Math.tan((view.h / 2) * DEG)
  const h = d * Math.tan((view.v / 2) * DEG)
  const far = 2
  const edge = useMemo(() => new BufferGeometry().setFromPoints([new Vector3(-w, -h, -d), new Vector3(w, -h, -d), new Vector3(w, h, -d), new Vector3(-w, h, -d)]), [w, h])
  const shades = [[-(w + far / 2), 0, far, far * 2], [w + far / 2, 0, far, far * 2], [0, h + far / 2, w * 2, far], [0, -(h + far / 2), w * 2, far]]
  return createPortal(
    <>
      {shades.map(([x, y, sx, sy]) => (
        <mesh key={`${x}:${y}`} position={[x, y, -d]} renderOrder={900}>
          <planeGeometry args={[sx, sy]} />
          <meshBasicMaterial color={0x000000} transparent opacity={0.78} depthTest={false} depthWrite={false} />
        </mesh>
      ))}
      <lineLoop geometry={edge} renderOrder={901}><lineBasicMaterial color={XR_GL.faint} depthTest={false} /></lineLoop>
    </>,
    camera,
  )
}

export function Marks({ scene }: { scene: XrScene }) {
  const accent = useMemo(() => toGl(scene.palette.accent), [scene])
  // In a session the glasses are the frame.
  const session = useXR(s => s.session)
  return (
    <>
      {scene.view && !session && <ViewFrame view={scene.view} />}
      {/* A ring round the thing a pinch would take now. It shows before the pinch, so a hand can correct its aim. */}
      <At at={scene.halo}>
        <mesh renderOrder={90}>
          <ringGeometry args={[0.9, 1, 40]} />
          <meshBasicMaterial color={0xffffff} transparent opacity={0.85} depthTest={false} side={DoubleSide} />
        </mesh>
      </At>
      {/* Work that is handed over is seen to go. */}
      <At at={scene.travelling}>
        <mesh>
          <boxGeometry args={[0.03, 0.03, 0.03]} />
          <meshBasicMaterial color={accent} />
        </mesh>
      </At>
    </>
  )
}
