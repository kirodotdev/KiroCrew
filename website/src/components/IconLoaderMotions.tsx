/**
 * Motions for a THEME's loader icons: alternatives to the cross-fade carousel
 * that work with whatever icon pool a theme registers (stock lucide symbols,
 * compiled artwork or a pack's own images), because they animate the slot
 * wrapper and never the icon itself. Timing lives in chat-transcript.css
 * (`.ilm-*`). Decorative and aria-hidden: the footer's `role=status` row
 * carries the accessible name.
 */
import type { ComponentType } from 'react'

/** Every motion, in the order they were designed. */
export const ICON_MOTIONS = ['spotlight', 'shuffle', 'drop'] as const
export type IconMotion = typeof ICON_MOTIONS[number]

/** What a theme's icon pool shows for one turn: the carousel or a motion. */
export type IconMotionChoice = 'carousel' | IconMotion

/** Draw one choice for a turn, uniformly over the carousel and every motion. */
export function pickIconMotion(rand: () => number = Math.random): IconMotionChoice {
  const pool: IconMotionChoice[] = ['carousel', ...ICON_MOTIONS]
  return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))]
}

/**
 * Up to four icons from `icons`, at the given pool indices. Indices wrap, so a
 * pool that shrank under a theme switch still renders real icons rather than
 * `undefined` (which would throw and blank the footer).
 */
export function IconMotionLoader({ motion, icons, picks }: {
  motion: IconMotion
  icons: ComponentType[]
  picks: readonly number[]
}) {
  if (icons.length === 0) return null
  const slots = picks.slice(0, 4).map(i => icons[i % icons.length])
  return (
    <span className={`ilm ilm-${motion}`} data-testid="loader-motion" data-motion={motion} aria-hidden="true">
      {slots.map((Icon, i) => (
        <span className="ilm-slot" key={i} style={{ ['--i' as string]: i }}><Icon /></span>
      ))}
      {motion === 'spotlight' && <span className="ilm-dot" />}
    </span>
  )
}
