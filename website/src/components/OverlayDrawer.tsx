import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

interface Props {
  open: boolean
  width: number
  dragging?: boolean
  /** Morph mode: the panel's visible window (clip-path) collapses into
   *  `morphTarget` and expands back out — the content itself never moves or
   *  deforms. The outer width still animates for layout reflow. */
  morph?: boolean
  /** Container-space rect the panel deforms into (the sidebar toggle button). */
  morphTarget?: { x: number; y: number; size: number }
  /** Container-space rect the panel's clip window expands FROM on this mount,
   *  overriding `morphTarget` for the opening half only. Set when the expand
   *  was driven by a surface bigger than the button — the hover flyout — so the
   *  panel grows out of what the user was already looking at instead of
   *  snapping back to the button first. Collapse always converges on
   *  `morphTarget`, because that is where the button actually is. */
  expandFrom?: { x: number; y: number; w: number; h: number } | null
  /** Panel pixel height, for the vertical squash ratio. */
  contentH?: number
  className?: string
  children: React.ReactNode
}

// Near-linear on purpose: a strong ease-out front-loads the travel, which
// visually freezes the near edges while the far edges are still sweeping.
const EASE = [0.32, 0.72, 0, 1] as const
const DUR = 0.24

export default function OverlayDrawer({ open, width, dragging, morph, morphTarget, expandFrom, contentH, className, children }: Props) {
  const reduce = useReducedMotion()
  // Gesture end settles from the live presentation value via a critically
  // damped spring (no overshoot, no visible jump) — never a fixed ease tween.
  // Reduced motion: drop the spring for a short opacity-only settle.
  const settle = reduce
    ? { duration: 0.2 }
    : { type: 'spring' as const, bounce: 0, duration: 0.35 }
  // The panel never moves or deforms — only its VISIBLE WINDOW morphs: a
  // clip-path inset animates between the full panel rect and the toggle
  // button's rect, so collapsing looks like the panel's visibility converging
  // into the button and expanding like it pouring back out (iPadOS-style
  // masked container transform). Pure px strings so Framer can interpolate.
  const morphable = morph && !reduce && morphTarget && contentH && width > 0 && contentH > 0
  const panelH = contentH ?? 0
  /** One clamped CSS length. Every side is floored at 0 because a negative inset
   *  is invalid and drops the clip entirely — the panel would flash full-size. */
  const px = (n: number) => `${Math.max(0, n)}px`
  /** Clip inset for a container-space rect. */
  const insetFor = (x: number, y: number, w: number, h: number, r: number) =>
    `inset(${px(y)} ${px(width - x - w)} ${px(panelH - y - h)} ${px(x)} round ${px(r)})`
  const clips = morphable
    ? {
        full: 'inset(0px 0px 0px 0px round 12px)',
        button: insetFor(morphTarget.x, morphTarget.y, morphTarget.size, morphTarget.size, 6),
        // Radius matches the flyout's `rounded-xl`, so the corner curvature is
        // continuous across the handoff instead of stepping 12 -> 6 -> 12.
        from: expandFrom ? insetFor(expandFrom.x, expandFrom.y, expandFrom.w, expandFrom.h, 12) : null,
      }
    : null
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="drawer"
          initial={{ width: 0 }}
          animate={{ width }}
          exit={{ width: 0 }}
          transition={
            dragging
              ? { duration: 0 }
              : morph && !reduce
                ? { width: { duration: DUR, ease: EASE } }
                : settle
          }
          className={`shrink-0 pb-2 ${clips ? 'relative z-[60] overflow-visible' : 'overflow-hidden'} ${className || ''}`}
        >
          {clips ? (
            /* Fixed pixel width so the width collapse never reflows the
               content mid-motion. The clip-path is the only visible boundary
               (the outer is overflow-visible + z-[60], so the still-wide
               panel paints over the chat pane while the layout column
               closes). The final clipped chip — a button-sized patch of
               empty panel header under the real toggle — fades in the last
               ~15% so the unmount never pops. */
            <motion.div
              style={{ width, height: '100%' }}
              initial={{ clipPath: clips.from ?? clips.button, opacity: 1 }}
              animate={{ clipPath: clips.full, opacity: 1 }}
              exit={{ clipPath: clips.button, opacity: [1, 1, 0], transition: { duration: DUR, ease: EASE, opacity: { duration: DUR, times: [0, 0.85, 1] } } }}
              transition={dragging ? { duration: 0 } : { duration: DUR, ease: EASE }}
            >
              {children}
            </motion.div>
          ) : children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
