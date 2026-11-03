/**
 * The mark of a section that is being watched: a dashed accent ring just outside its
 * border, its dashes marching round it. The stroke and dashes keep their on-screen
 * size at any zoom, so a zoomed-out board still shows which sections are watched.
 * Under reduced motion the ring stands still.
 */

import { motion, useReducedMotion } from 'framer-motion'

const GAP = 6
const DASH = 10
const STROKE = 2

export default function WatchRing({ w, h, radius }: { w: number; h: number; radius: number }) {
  const reduce = useReducedMotion()
  return (
    <svg aria-hidden data-testid="watch-ring" width={w + GAP * 2} height={h + GAP * 2}
      style={{ position: 'absolute', left: -GAP, top: -GAP, overflow: 'visible', pointerEvents: 'none' }}>
      <motion.rect
        x={STROKE / 2} y={STROKE / 2} width={w + GAP * 2 - STROKE} height={h + GAP * 2 - STROKE} rx={radius + GAP} ry={radius + GAP}
        fill="none" stroke="var(--accent)" strokeOpacity={0.85} strokeWidth={STROKE} strokeLinecap="round"
        strokeDasharray={`${DASH} ${DASH}`} vectorEffect="non-scaling-stroke"
        animate={reduce ? undefined : { strokeDashoffset: [0, -DASH * 2] }}
        transition={reduce ? undefined : { duration: 1.6, ease: 'linear', repeat: Infinity }}
      />
    </svg>
  )
}
