import { useEffect, useRef } from 'react'
import { motion, useMotionValue, useSpring } from 'framer-motion'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import bodyUrl from './looking-ghost-body.svg'
import eyesUrl from './looking-ghost-eyes.svg'

// How far the eyes may move, in the mark's own units (the body is ~700 wide).
export const EYE_REACH_X = 55
export const EYE_REACH_Y = 40
// The pointer distance, in px, over which the eyes ease out to their full reach,
// so a pointer close to the ghost gets a small glance rather than a full stare.
const EASE_DISTANCE = 300
const EYE_SPRING = { stiffness: 500, damping: 40, mass: 0.4 }
// The asset layers place the mark at scale(0.095) inside a 100-unit viewBox.
const MARK_SCALE = 0.095

/**
 * The Kiro ghost peeking up from an accent disc, the signed-out avatar of
 * Settings > Kiro account. Its eyes follow the pointer on desktop; on a phone
 * (no pointer to follow) and under `prefers-reduced-motion` they stay put.
 *
 * The art is two asset layers cut from the brand mark (`kiro-ghost-mark.svg`):
 * `looking-ghost-body.svg` (white body, no outline) and `looking-ghost-eyes.svg`
 * (ink eyes), both placed on the same 100-unit canvas. They render as plain
 * images stacked over the theme's accent disc; only the eyes layer moves. The
 * disc's rounded, overflow-hidden box clips everything, so nothing spills past
 * its edge.
 */
export function LookingGhost({ size = 72 }: { size?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const isMobile = useIsMobile()
  const reducedMotion = useReducedMotion()
  const still = isMobile || reducedMotion
  const eyeX = useMotionValue(0)
  const eyeY = useMotionValue(0)
  const x = useSpring(eyeX, EYE_SPRING)
  const y = useSpring(eyeY, EYE_SPRING)
  useEffect(() => {
    if (still) {
      eyeX.jump(0)
      eyeY.jump(0)
      x.jump(0)
      y.jump(0)
      return
    }
    // Mark units to on-screen px at this size.
    const toPx = (MARK_SCALE * size) / 100
    const onMove = (e: PointerEvent) => {
      const el = ref.current
      if (!el) return
      const r = el.getBoundingClientRect()
      // The eyes sit right of and below the disc's centre.
      const dx = e.clientX - (r.left + r.width * 0.56)
      const dy = e.clientY - (r.top + r.height * 0.72)
      const d = Math.hypot(dx, dy) || 1
      const k = Math.min(d / EASE_DISTANCE, 1)
      eyeX.set((dx / d) * EYE_REACH_X * k * toPx)
      eyeY.set((dy / d) * EYE_REACH_Y * k * toPx)
    }
    window.addEventListener('pointermove', onMove)
    return () => window.removeEventListener('pointermove', onMove)
  }, [still, size, eyeX, eyeY, x, y])
  return (
    <div
      ref={ref}
      className="relative overflow-hidden rounded-full"
      style={{ width: size, height: size, background: 'var(--accent)' }}
      aria-hidden="true"
      data-testid="looking-ghost"
    >
      <img src={bodyUrl} alt="" draggable={false} className="absolute inset-0 h-full w-full" />
      <motion.img
        src={eyesUrl}
        alt=""
        draggable={false}
        className="absolute inset-0 h-full w-full"
        style={{ x, y }}
        data-testid="looking-ghost-eyes"
      />
    </div>
  )
}
