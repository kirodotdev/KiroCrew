/**
 * The mark of a watched doc section: a dashed accent rail down its left margin,
 * from its heading to its last line, its dashes marching downward like the board's
 * watch ring. Placed by `docGoals` from the editor's line geometry; still under
 * reduced motion.
 */
import { motion, useReducedMotion } from 'framer-motion'

export interface Rail { key: string; top: number; height: number; left: number }

/** How far left of the text the rail runs: inside the margin, clear of answer pins. */
const OFFSET = 10
const DASH = 6
const STROKE = 2

export default function DocGoalRails({ rails }: { rails: Rail[] }) {
  const reduce = useReducedMotion()
  return (
    <>
      {rails.map(r => (
        <svg key={r.key} data-testid="doc-goal-rail" width={STROKE} height={r.height}
          style={{ position: 'absolute', left: r.left - OFFSET, top: r.top, overflow: 'visible' }}>
          <motion.line x1={STROKE / 2} x2={STROKE / 2} y1={2} y2={Math.max(2, r.height - 2)}
            stroke="var(--accent)" strokeOpacity={0.75} strokeWidth={STROKE} strokeLinecap="round" strokeDasharray={`${DASH} ${DASH}`}
            animate={reduce ? undefined : { strokeDashoffset: [0, -DASH * 2] }}
            transition={reduce ? undefined : { duration: 1.2, ease: 'linear', repeat: Infinity }} />
        </svg>
      ))}
    </>
  )
}
