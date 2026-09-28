/**
 * The app's two animations, as Framer Motion rather than hand-written CSS.
 *
 * `website/AUTOSDE.yaml`'s `use-framer-motion` rule forbids adding CSS keyframe
 * blocks, and these were the only two: a 1s linear spinner and the 2.2s
 * ease-in-out sweep that crosses each screen while a critique runs.
 *
 * The reduced-motion behaviour is deliberately still driven by the app's own
 * `useReduceMotion()` rather than by Framer's internal handling: the original CSS
 * did not merely slow the animation down, it swapped the sweep for a flat tint
 * (WCAG 2.3.3, which this app checks for as atom U18). Keeping the branch here
 * preserves that exact substitution instead of trusting a library default.
 */
import { motion } from 'framer-motion'
import { Loader2 } from 'lucide-react'
import { S } from './styles'

/** Spinner. Renders a still icon when the user asked for reduced motion. */
export function Spinner({ size = 13, reduceMotion, style }: {
  size?: number
  reduceMotion: boolean
  style?: React.CSSProperties
}) {
  if (reduceMotion) return <Loader2 size={size} style={style} />
  return (
    <motion.span
      style={{ display: 'inline-flex', lineHeight: 0, ...style }}
      animate={{ rotate: 360 }}
      transition={{ duration: 1, ease: 'linear', repeat: Infinity }}
    >
      <Loader2 size={size} />
    </motion.span>
  )
}

/**
 * The scan sweep over one waiting screen. `index` staggers the screens so a flow
 * reads as a sequence rather than one synchronised flash.
 */
export function Sweep({ index, reduceMotion }: { index: number; reduceMotion: boolean }) {
  if (reduceMotion) return <span style={S.sweepStill} />
  return (
    <motion.span
      style={S.sweep}
      initial={{ top: '-32%' }}
      animate={{ top: '100%' }}
      transition={{ duration: 2.2, ease: 'easeInOut', repeat: Infinity, delay: index * 0.35 }}
    />
  )
}
