import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { animate, motion, useMotionValue } from 'framer-motion'

interface FlyingQuoteProps {
  /** Bounding rect of the selected text (source) */
  from: DOMRect
  /** Target element to fly toward (the input box) */
  targetRef: React.RefObject<HTMLElement | null>
  /** Text snippet to display in the flying element */
  text: string
  /** Called when animation completes */
  onComplete: () => void
}

/** Vertical lift of the pop, in px, before the drop begins. Clamped so a quote
 *  taken right above the composer still pops visibly, and one taken from the
 *  top of a long transcript does not launch off-screen. */
const LIFT_MIN = 34
const LIFT_MAX = 96
/** The pop is a fixed beat — a distance-scaled one reads as hesitation. */
const POP_MS = 190
/** Half the overlay's own height (two lines of 12px mono in a padded pill), and
 *  the gap kept below the viewport's top edge, so the apex stays visible. */
const CHIP_HALF_H = 24
const VIEWPORT_MARGIN = 14

/** How far into the composer the quoted text begins — the point the quote is
 *  aimed at, capped so a narrow composer is targeted at its centre instead. */
const TEXT_START_INSET = 120

/** Horizontal landing point: where the quoted line will actually appear in the
 *  composer. */
export function quoteLandingX(boxLeft: number, boxWidth: number): number {
  return boxLeft + Math.min(boxWidth / 2, TEXT_START_INSET)
}

export interface QuoteFlight {
  /** Total flight time, in seconds (framer-motion's unit). */
  duration: number
  /** Vertical stops: source → apex of the pop → target. */
  y: [number, number, number]
  /** Normalised times for the `y` / `scale` stops. */
  times: [number, number, number]
  /** Size at each stop: grows on the pop, then is swallowed by the input. */
  scale: [number, number, number]
}

/** Safari-download choreography: a short pop straight up that decelerates and
 *  hangs at the apex, then a gravity-like fall that accelerates into the target.
 *
 *  The fall grows with distance but sub-linearly and capped, because the
 *  composer is unusable while the overlay is in flight — a long drop must still
 *  land promptly rather than play in slow motion.
 *
 *  `ceilingY` is the highest apex the viewport can show. Quoting the first line
 *  of a transcript starts within a lift of the top edge, and an unclamped pop
 *  would throw the quote off-screen — the user would see it vanish and
 *  reappear falling, not pop. */
export function quoteFlight(startY: number, targetY: number, ceilingY = 0): QuoteFlight {
  const dist = Math.abs(targetY - startY)
  const lift = Math.min(LIFT_MAX, Math.max(LIFT_MIN, dist * 0.18))
  // Never above the ceiling, and never below the source (a source already at
  // the ceiling simply drops, rather than sagging downward first).
  const apex = Math.min(startY, Math.max(ceilingY, startY - lift))
  const fallMs = Math.min(470, 250 + dist * 0.3)
  const totalMs = POP_MS + fallMs
  return {
    duration: totalMs / 1000,
    y: [startY, apex, targetY],
    times: [0, POP_MS / totalMs, 1],
    scale: [0.94, 1.14, 0.24],
  }
}

/** Rise to the apex with an overshoot, so the quote springs past the top of its
 *  arc and settles back — the "hang" that makes the pop feel elastic. */
const POP_EASE: [number, number, number, number] = [0.22, 1.28, 0.5, 1]
/** Fall under something like gravity: barely moving at the apex, fastest at the
 *  moment it enters the input. */
const FALL_EASE: [number, number, number, number] = [0.42, 0, 0.86, 0.36]
/** Horizontal sweep across the whole flight: nearly still during the pop, then
 *  carried sideways as it falls. */
const X_EASE: [number, number, number, number] = [0.3, 0, 0.7, 1]
/** Overshooting ease for the composer's landing recoil. */
const BOUNCE_EASE: [number, number, number, number] = [0.34, 1.56, 0.64, 1]

/** Landing recoil on the composer itself. Safari bounces the download button
 *  when the file lands; the same beat here is what reads as "dropped IN" rather
 *  than "faded out near". The final keyframe is the identity transform, so the
 *  composer is left exactly as it was found. */
function bounceTarget(el: HTMLElement) {
  animate(
    el,
    { y: [0, 3, -1.5, 0], scale: [1, 0.988, 1.006, 1] },
    { duration: 0.32, times: [0, 0.34, 0.68, 1], ease: BOUNCE_EASE },
  )
}

export default function FlyingQuote({ from, targetRef, text, onComplete }: FlyingQuoteProps) {
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete

  const startX = from.left + from.width / 2
  const startY = from.top + from.height / 2

  const x = useMotionValue(startX)
  const y = useMotionValue(startY)
  const scale = useMotionValue(0.94)
  const opacity = useMotionValue(0)

  useEffect(() => {
    const el = targetRef.current
    if (!el) { onCompleteRef.current(); return }
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      onCompleteRef.current()
      return
    }
    // Aim at the composer's own text box when there is one, so the quote lands
    // where its text will appear rather than on the surrounding chrome.
    const box = el.querySelector('textarea') ?? el
    const rect = box.getBoundingClientRect()
    // Enter through the TOP edge, near the start of the text run — the point a
    // falling object would pass through on its way into the box.
    const targetX = quoteLandingX(rect.left, rect.width)
    const targetY = rect.top + 8

    const flight = quoteFlight(startY, targetY, CHIP_HALF_H + VIEWPORT_MARGIN)
    let cancelled = false

    const runs = [
      // X is timed to finish WITH the fall, so the quote lands on the text start
      // whatever column it was taken from. A spring here cannot keep up on a
      // full-width message: the quote fades out mid-air, at a different point
      // for every source, instead of arriving.
      animate(x, targetX, { duration: flight.duration, ease: X_EASE }),
      // Hold full opacity almost to the landing: fading early reads as the
      // quote evaporating in mid-air instead of dropping into the box.
      animate(opacity, [0, 1, 1, 0], {
        duration: flight.duration,
        times: [0, 0.08, 0.87, 1],
        ease: 'linear',
      }),
      animate(scale, flight.scale, {
        duration: flight.duration,
        times: flight.times,
        ease: [POP_EASE, FALL_EASE],
      }),
    ]

    const fall = animate(y, flight.y, {
      duration: flight.duration,
      times: flight.times,
      ease: [POP_EASE, FALL_EASE],
    })
    runs.push(fall)
    fall.then(() => {
      if (cancelled) return
      bounceTarget(el)
      onCompleteRef.current()
    }, () => { /* stopped by unmount — the overlay is already gone */ })

    return () => {
      cancelled = true
      runs.forEach(run => run.stop())
    }
  }, [targetRef, startY, x, y, scale, opacity])

  const truncated = text.length > 60 ? text.slice(0, 57) + '…' : text

  return createPortal(
    <motion.div
      style={{
        position: 'fixed',
        left: x,
        top: y,
        x: '-50%',
        y: '-50%',
        scale,
        opacity,
      }}
      className="fixed z-[99999] pointer-events-none max-w-[280px]"
    >
      <div className="px-3 py-2 rounded-lg bg-accent/15 border border-accent/30 backdrop-blur-sm shadow-lg">
        <div className="flex items-start gap-2">
          <div className="w-0.5 h-full min-h-[16px] bg-accent rounded-full shrink-0" />
          <span className="text-[12px] text-text font-mono leading-snug line-clamp-2">{truncated}</span>
        </div>
      </div>
    </motion.div>,
    document.body
  )
}
