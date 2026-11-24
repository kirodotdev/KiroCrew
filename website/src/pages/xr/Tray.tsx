/**
 * The tray and its marbles, the bin, and the beads a Bot holds up, drawn the framework way.
 *
 * Where each one is comes from `tokens.ts`, as plain numbers the scene keeps up to date. These are plain stand-in
 * shapes. A hand does not meet them here: what a pointer can be on is in `Targets.tsx`.
 */
import { useFrame } from '@react-three/fiber'
import { useMemo, useRef, useState } from 'react'
import { DoubleSide, type Mesh, type MeshBasicMaterial } from 'three'
import { BEADS_MAX } from './beads'
import { At } from './Marks'
import { XR_GL } from './palette'
import type { XrScene } from './scene'
import { toGl } from './sceneParts'
import { BIN_AT, MARBLE_R, TRAY_AT } from './tokens'

const DOTS = Array.from({ length: BEADS_MAX }, (_, i) => i)

export function Tray({ scene }: { scene: XrScene }) {
  const { tokens } = scene
  const [marbles, setMarbles] = useState<readonly string[]>([])
  const [rows, setRows] = useState<readonly string[]>([])
  const keys = useRef({ marbles: '', rows: '' })
  const meshes = useRef(new Map<string, Mesh>())
  const colors = useMemo(() => ({ waits: toGl(scene.palette.warn), done: toGl(scene.palette.accent) }), [scene])
  const hold = (id: string) => (m: Mesh | null) => { if (m) meshes.current.set(id, m); else meshes.current.delete(id) }

  useFrame(() => {
    const m = [...tokens.marbles.keys()]
    const r = [...tokens.beadRows.keys()]
    if (m.join('|') !== keys.current.marbles) { keys.current.marbles = m.join('|'); setMarbles(m) }
    if (r.join('|') !== keys.current.rows) { keys.current.rows = r.join('|'); setRows(r) }
    for (const marble of tokens.marbles.values()) {
      const mesh = meshes.current.get(`marble:${marble.id}`)
      if (!mesh) continue
      mesh.position.copy(marble.at)
      mesh.visible = marble.shown
    }
    for (const [slug, row] of tokens.beadRows) {
      row.forEach((dot, i) => {
        const mesh = meshes.current.get(`bead:${slug}:${i}`)
        if (!mesh) return
        mesh.visible = dot.shown
        mesh.position.copy(dot.at)
        mesh.scale.setScalar(dot.size)
        ;(mesh.material as MeshBasicMaterial).color.setHex(dot.done ? colors.done : XR_GL.dim)
      })
    }
  })

  return (
    <At at={scene.seat}>
      <mesh position={TRAY_AT}>
        <boxGeometry args={[0.3, 0.008, 0.06]} />
        <meshBasicMaterial color={XR_GL.raised} />
      </mesh>
      <mesh position={BIN_AT}>
        <cylinderGeometry args={[0.07, 0.055, 0.12, 12, 1, true]} />
        <meshBasicMaterial color={XR_GL.faint} transparent opacity={0.7} side={DoubleSide} />
      </mesh>
      {marbles.map(id => (
        <mesh key={id} ref={hold(`marble:${id}`)} visible={false}>
          <sphereGeometry args={[MARBLE_R, 14, 10]} />
          <meshBasicMaterial color={colors.waits} />
        </mesh>
      ))}
      {rows.map(slug => DOTS.map(i => (
        <mesh key={`${slug}:${i}`} ref={hold(`bead:${slug}:${i}`)} visible={false}>
          <sphereGeometry args={[0.009, 10, 8]} />
          <meshBasicMaterial color={XR_GL.dim} />
        </mesh>
      )))}
    </At>
  )
}
