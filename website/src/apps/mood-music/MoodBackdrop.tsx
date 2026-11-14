/**
 * The page's mood backdrop: two soft glows that drift slowly behind the content.
 *
 * Each glow's hue comes from `moods.ts` and is mixed into the theme's `--bg`
 * (each glow's class), so light and dark both read. The drift is a long,
 * slow loop, so it stops for reduced motion and whenever the person pauses it
 * (the page owns that toggle). A mood change cross-fades the colours; under
 * reduced motion it simply switches.
 * A glow's class mixes `hsl(var(--mood-hue) …)` at `--mood-strength` percent
 * into `var(--bg)`; the two variables are set per glow from the mood.
 */
import type { CSSProperties } from 'react'
import { motion } from 'framer-motion'
import { moodTheme, type MoodLook } from './moods'

export default function MoodBackdrop({ look, moving }: { look: MoodLook; moving: boolean }) {
  const theme = moodTheme(look)
  const vars = (index: 0 | 1) => ({ '--mood-hue': theme.hues[index], '--mood-strength': theme.strength }) as CSSProperties
  const drift = (dx: number, dy: number) =>
    moving
      ? { animate: { x: [0, dx, 0], y: [0, dy, 0] }, transition: { duration: theme.driftSeconds, repeat: Infinity, ease: 'easeInOut' as const } }
      : { animate: { x: 0, y: 0 }, transition: { duration: 0 } }

  return (
    <div
      aria-hidden="true"
      data-testid="mood-backdrop"
      data-look={look}
      data-moving={moving ? 'true' : 'false'}
      className="pointer-events-none absolute inset-0 overflow-hidden bg-[var(--bg)]"
    >
      <motion.div className="absolute rounded-full blur-3xl motion-safe:transition-[background] motion-safe:duration-1000 bg-[radial-gradient(closest-side,color-mix(in_oklab,hsl(var(--mood-hue)_85%_60%)_calc(var(--mood-strength)*1%),var(--bg)),transparent)] -left-[15%] -top-[20%] h-[75%] w-[70%]" style={vars(0)} data-testid="mood-glow" {...drift(60, 40)} />
      <motion.div className="absolute rounded-full blur-3xl motion-safe:transition-[background] motion-safe:duration-1000 bg-[radial-gradient(closest-side,color-mix(in_oklab,hsl(var(--mood-hue)_85%_60%)_calc(var(--mood-strength)*1%),var(--bg)),transparent)] -bottom-[25%] -right-[10%] h-[80%] w-[65%]" style={vars(1)} data-testid="mood-glow" {...drift(-50, -30)} />
    </div>
  )
}
