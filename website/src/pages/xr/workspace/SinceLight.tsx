/**
 * The light of a Bot that changed since he left (D36, `since.ts`): a warm pool of light under where it stands. It
 * says "new since you were here" and nothing else: a desk's own light, in its Bot's colour, keeps meaning that the
 * Bot works, and the ring at the feet keeps the state. The pool is the one sign: at a desk's size a lamp does not
 * read as a lamp.
 *
 * The Bot that stands in front has none: the pools say "look over here", and he looks at that one already.
 *
 * The pool faces him, low at the feet and behind the body, wider than the body: a pool that lay flat would be seen
 * on edge for a Bot at the height of his eyes. Black is clear glass in the glasses, so the light is added to what
 * is behind it. It breathes slowly; with reduced motion it is still. It takes no pointer.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { AdditiveBlending, CanvasTexture, LinearFilter, SRGBColorSpace, type MeshBasicMaterial } from 'three'

/** The light's colour: a lamp's warm white, which no state and no Bot's own colour is. */
export const SINCE_WARM = 0xffc878
/** The pool: how wide and how high it is as he sees it, how far behind the feet, and how it breathes. */
export const SINCE_POOL = { wideM: 0.62, highM: 0.2, behindM: 0.1, liftM: 0.02, opacity: 0.9, breath: 0.18, everyMs: 2600 } as const

/** A soft round light: bright in the middle, nothing at the edge. */
function useGlow(): CanvasTexture {
  const texture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = 128
    canvas.height = 128
    const c = canvas.getContext('2d')
    if (c) {
      const g = c.createRadialGradient(64, 64, 0, 64, 64, 64)
      g.addColorStop(0, 'rgba(255,255,255,1)')
      g.addColorStop(0.35, 'rgba(255,255,255,0.55)')
      g.addColorStop(0.7, 'rgba(255,255,255,0.16)')
      g.addColorStop(1, 'rgba(255,255,255,0)')
      c.fillStyle = g
      c.fillRect(0, 0, 128, 128)
    }
    const t = new CanvasTexture(canvas)
    t.colorSpace = SRGBColorSpace
    t.minFilter = LinearFilter
    t.generateMipmaps = false
    return t
  }, [])
  useEffect(() => () => texture.dispose(), [texture])
  return texture
}

/** The pool under a Bot, inside the group that rides with its body: `yaw` turns it to him. */
export function SincePool({ yaw, still }: { yaw: number; still: boolean }) {
  const glow = useGlow()
  const paint = useRef<MeshBasicMaterial>(null)
  useFrame(() => {
    if (paint.current) paint.current.opacity = SINCE_POOL.opacity * (still ? 1 : 1 - SINCE_POOL.breath * (0.5 + 0.5 * Math.sin((performance.now() / SINCE_POOL.everyMs) * Math.PI * 2)))
  })
  return (
    <group rotation-y={yaw} name="xr-since-pool">
      <mesh position={[0, SINCE_POOL.liftM, -SINCE_POOL.behindM]} scale={[SINCE_POOL.wideM, SINCE_POOL.highM, 1]} raycast={() => null} renderOrder={1}>
        <planeGeometry />
        <meshBasicMaterial ref={paint} map={glow} color={SINCE_WARM} transparent opacity={SINCE_POOL.opacity} blending={AdditiveBlending} depthWrite={false} toneMapped={false} />
      </mesh>
    </group>
  )
}
