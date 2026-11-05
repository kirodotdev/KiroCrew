import type { ReactNode } from 'react'
import { motion, useReducedMotion } from 'framer-motion'

import { OFFSTAGE, onstage } from '../lib/offstage'

interface Props {
  /** Another panel is expanded: fade out, then leave the layout. */
  hidden: boolean
  /** This slot's panel is the expanded one: fill the workspace column. */
  expanded?: boolean
  children: ReactNode
}

/**
 * One agent panel's place in the meeting grid (#21179).
 *
 * Expanding a panel moves and resizes it, so the slot carries a Framer Motion
 * `layout` animation: the panel slides and grows into the workspace instead of
 * jumping there in one frame, and restoring plays the same motion back. A panel
 * hidden by a sibling's expansion fades out before it leaves the layout.
 *
 * Hidden means OFFSTAGE after the fade (see ../lib/offstage), never unmounted:
 * the panel may hold an unsaved minutes draft, which unmounting would drop, and
 * staying laid out keeps its notes' scroll position. Reduced motion keeps the
 * same end states with no animation.
 */
export default function AgentPanelSlot({ hidden, expanded = false, children }: Props) {
  const reduceMotion = useReducedMotion()
  const transition = reduceMotion
    ? { duration: 0 }
    : { type: 'spring' as const, stiffness: 300, damping: 32 }

  return (
    <motion.div
      layout
      data-testid="agent-panel-slot"
      // Expanded, the slot fills the column but never below its panel's own
      // minimum content (a short phone workspace with the minutes editor open):
      // the column's scroller then reaches what would otherwise be clipped.
      className={`col-span-2 min-w-0 flex flex-col ${expanded ? 'flex-1 min-h-0' : 'min-h-0'}`}
      initial={false}
      animate={
        hidden
          ? { opacity: 0, transitionEnd: OFFSTAGE }
          : { ...onstage('flex'), opacity: 1 }
      }
      transition={transition}
    >
      {children}
    </motion.div>
  )
}
