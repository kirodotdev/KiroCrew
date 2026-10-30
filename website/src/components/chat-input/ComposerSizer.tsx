import type { ReactNode } from 'react'
import { motion } from 'framer-motion'

// The box's height change between the one-row and two-row shapes and on a
// wrapped or removed line (`inlineToolbarWhileFits`). Short and barely bouncy:
// it fires mid-typing, so anything longer trails the caret.
export const COMPOSER_ROW_SPRING = { type: 'spring', stiffness: 520, damping: 38, mass: 0.8 } as const

/**
 * The box's height animator for `inlineToolbarWhileFits`. Enabled, it is a
 * bottom-aligned clip whose height follows `height` (the inner box's measured
 * natural height) and springs between the two shapes when `animate` is set;
 * the inner box keeps its natural height (`shrink-0`), so while the sizer is
 * still shorter it clips the TOP of the text column and the buttons at the
 * bottom never move. The clip is `overflow: clip`, never `hidden`: a `hidden`
 * box keeps a scroll position, and WebKit scrolls it to bring a caret in that
 * clipped top band into view, so the text jumps while the box opens.
 * Disabled -- every host that does not opt in -- it renders
 * its children bare, so the ordinary composer's tree is exactly what it was.
 */
export function ComposerSizer({ enabled, height, animate, className, children }: {
  enabled: boolean
  height: number | null
  animate: boolean
  className: string
  children: ReactNode
}) {
  if (!enabled) return <>{children}</>
  return (
    <motion.div
      data-testid="composer-sizer"
      initial={false}
      animate={{ height: height ?? 'auto' }}
      transition={animate ? COMPOSER_ROW_SPRING : { duration: 0 }}
      className={className}
    >
      {children}
    </motion.div>
  )
}
