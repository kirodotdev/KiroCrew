/**
 * The first-run "You're all set" ghost with one short burst of confetti.
 *
 * The burst plays once per showing and ends still: each piece fades out, and once
 * the last one lands the pieces leave the DOM. Every showing throws the same
 * pieces (a fixed seed). Colours are theme variables only. Under
 * `prefers-reduced-motion` no confetti renders at all.
 */
import { useMemo, useState } from 'react'
import { motion } from 'framer-motion'

import { LookingGhost } from '../assets/onboarding/LookingGhost'
import { useReducedMotion } from '../hooks/useReducedMotion'

const PIECES = 28
const SEED = 7
const COLORS = ['var(--accent)', 'var(--ok)', 'var(--warn)', 'var(--info)', 'var(--accent-hover)']

/** Park-Miller: a tiny fixed-seed generator, so the throw is the same every time. */
function seeded(seed: number): () => number {
  let s = seed
  return () => {
    s = (s * 16807) % 2147483647
    return (s - 1) / 2147483646
  }
}

function throwPieces() {
  const r = seeded(SEED)
  return Array.from({ length: PIECES }, (_, i) => ({
    i,
    x: (r() - 0.5) * 340,
    up: 70 + r() * 110,
    down: 160 + r() * 160,
    rot: (r() - 0.5) * 720,
    w: 5 + r() * 4,
    h: 8 + r() * 6,
    color: COLORS[i % COLORS.length],
    delay: r() * 0.08,
  }))
}

export function FirstRunConfetti() {
  const reduced = useReducedMotion()
  const pieces = useMemo(throwPieces, [])
  // The piece that starts last lands last: its end removes the whole burst.
  const last = useMemo(() => pieces.reduce((a, b) => (b.delay > a.delay ? b : a)).i, [pieces])
  const [over, setOver] = useState(false)
  return (
    <div className="relative" data-testid="harness-setup-confetti">
      <LookingGhost size={72} />
      {reduced || over
        ? null
        : pieces.map(p => (
            <motion.span
              key={p.i}
              aria-hidden="true"
              data-confetti-piece=""
              className="pointer-events-none absolute left-1/2 top-1/2 block rounded-[2px]"
              style={{ width: p.w, height: p.h, background: p.color, marginLeft: -p.w / 2, marginTop: -p.h / 2 }}
              initial={{ x: 0, y: 0, rotate: 0, opacity: 1 }}
              animate={{ x: p.x, y: [0, -p.up, p.down], rotate: p.rot, opacity: [1, 1, 0] }}
              transition={{ duration: 1.5, delay: p.delay, times: [0, 0.3, 1], ease: ['easeOut', 'easeIn'] }}
              onAnimationComplete={p.i === last ? () => setOver(true) : undefined}
            />
          ))}
    </div>
  )
}
