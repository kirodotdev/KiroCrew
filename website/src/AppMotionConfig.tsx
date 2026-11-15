import type { ReactNode } from 'react'
import { MotionConfig } from 'framer-motion'

/**
 * Wraps the whole render tree so every Framer Motion animation follows the
 * system "reduce motion" setting. The CSS reset in `index.css` only shortens CSS
 * animations; Framer runs its own animations in JS, which that rule never sees.
 * With `reducedMotion="user"` Framer jumps position and size animations (x/y,
 * scale, rotate, width/height, layout) to their end value and keeps opacity and
 * colour, so a fade still shows that something changed.
 */
export function AppMotionConfig({ children }: { children: ReactNode }) {
  return <MotionConfig reducedMotion="user">{children}</MotionConfig>
}
