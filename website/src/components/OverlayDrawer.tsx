import { AnimatePresence, motion, useReducedMotion, type MotionValue } from 'framer-motion'

interface Props {
  open: boolean
  width: number
  dragging?: boolean
  /**
   * Slide mode (mobile): the panel keeps a fixed width and is positioned by
   * this offset in px — `-viewportWidth` offscreen, `0` at rest. Supplied as a
   * MotionValue so the gesture can drive it without re-rendering the chat pane
   * once per touchmove, and so the drag and the settle write the SAME channel
   * (a width tween cannot be handed a half-finished drag).
   *
   * Width was the wrong channel for this panel: animating 0 -> viewport width
   * under `overflow-hidden` reveals the sidebar left-to-right like a wipe
   * rather than sliding a panel in, and it cannot represent a partial drag
   * without reflowing the sidebar's own contents on every frame.
   *
   * When set, mount/unmount timing belongs to the CALLER: it holds the panel
   * mounted until the slide-out finishes, so this component does no exit
   * animation of its own.
   */
  slideX?: MotionValue<number>
  /**
   * Slide mode only: the panel element, so the caller can hand it to
   * `registerDrawerTargets` and let the settle run on the compositor. Without
   * it the settle falls back to sampling the MotionValue on the main thread.
   */
  slideRef?: React.Ref<HTMLDivElement>
  /**
   * Slide mode only: extra inline style MERGED into the panel motion.div's
   * style. The caller uses it to inset the panel's VERTICAL extent (block-axis
   * margins) to the visual viewport, so a software keyboard cannot strand its
   * lower content — the horizontal channel is off limits: `width` and the
   * `x` transform (owned by `slideX` / the compositor settle) are applied
   * AFTER this, so they win any collision and this can never fight the slide.
   */
  slideStyle?: React.CSSProperties
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

export default function OverlayDrawer({ open, width, dragging, slideX, slideRef, slideStyle, morph, morphTarget, expandFrom, contentH, className, children }: Props) {
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
  // Slide mode short-circuits every branch below. The offset is the ONLY
  // animated channel, and it is owned by `slideX` — so there is no `initial` /
  // `animate` / `exit` here at all, and no clip morph (that is the desktop
  // collapse-into-the-toggle motion, which has no mobile counterpart).
  //
  // `open` still gates the mount, but the caller keeps it true until the
  // slide-out completes: an `exit` animation would compete with the settle the
  // gesture hook is already running on this same value.
  if (slideX) {
    return (
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="drawer-slide"
            ref={slideRef}
            // `slideStyle` first, then `width` and `x` — the transform channel
            // stays owned by `slideX` (and the compositor settle) and `width`
            // by the caller's uncovered-strip math, so a vertical inset merged
            // in here can never override the horizontal slide.
            style={{ ...slideStyle, width, x: slideX }}
            className={`shrink-0 pb-2 overflow-hidden ${className || ''}`}
          >
            {children}
          </motion.div>
        )}
      </AnimatePresence>
    )
  }
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
