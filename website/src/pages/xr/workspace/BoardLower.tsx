/**
 * A board that leaves without a throw is lowered (the lead's ruling of 21 Nov 07:37, D4a): its picture goes down out
 * of sight below where it stood and fades, as a Bot lowers its clipboard. Particles are for a throw only
 * (`BoardDust.tsx`, D4a "直接散成粒子消失"). With reduced motion it is gone at once.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { CanvasTexture, DoubleSide, Quaternion, SRGBColorSpace, Vector3, type Mesh, type MeshBasicMaterial } from 'three'
import type { DustFrom } from './BoardDust'

/** How long the lowering takes, and how far down it goes, in board heights. */
export const LOWER = { ms: 420, drop: 0.9 } as const

export function BoardLower({ from, still, onDone }: { from: DustFrom; still: boolean; onDone: () => void }) {
  const mesh = useRef<Mesh>(null)
  const born = useRef<number | null>(null)
  const done = useRef(false)
  const at = useMemo(() => { const p = new Vector3(), q = new Quaternion(), s = new Vector3(); from.matrix.decompose(p, q, s); return { p, q, s } }, [from])
  const texture = useMemo(() => { const t = new CanvasTexture(from.canvas); t.colorSpace = SRGBColorSpace; return t }, [from])
  useEffect(() => () => texture.dispose(), [texture])
  useEffect(() => { if (still) onDone() }, [still, onDone])
  useFrame(() => {
    const m = mesh.current
    if (!m || done.current) return
    const now = performance.now()
    born.current ??= now
    const t = Math.min(1, (now - born.current) / LOWER.ms)
    const ease = t * t * (3 - 2 * t)
    m.position.copy(at.p)
    m.position.y -= from.heightM * at.s.y * LOWER.drop * ease
    m.quaternion.copy(at.q)
    m.scale.set(from.widthM * at.s.x, from.heightM * at.s.y, 1)
    ;(m.material as MeshBasicMaterial).opacity = 1 - ease
    if (t >= 1) { done.current = true; onDone() }
  })
  if (still) return null
  return (
    <mesh ref={mesh} renderOrder={2}>
      <planeGeometry />
      <meshBasicMaterial map={texture} transparent side={DoubleSide} depthWrite={false} toneMapped={false} />
    </mesh>
  )
}
