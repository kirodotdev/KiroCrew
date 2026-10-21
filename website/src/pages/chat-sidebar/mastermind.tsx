/** The Mastermind row's decoration: the session is drawn by the ordinary
 *  `SessionRow` (status, badges, unread, keyboard order, menus all unchanged),
 *  and while it is enabled the row model marks it (`SessionRowView.mastermind`)
 *  so the row adds these two pieces and withholds its close / rename / move /
 *  drag affordances. Kept here rather than in SessionRow.tsx, which is already
 *  past the 1,600-line working target.
 *
 *  - `MastermindRowGlow`: a faint accent ring that a slow gradient sweeps
 *    around. Framer Motion drives the sweep (the repo adds no CSS @keyframes);
 *    the ring itself is `.mastermind-row-glow` in styles/sidebar-and-side-panel.css,
 *    built only from the theme's `--accent`, so it follows every theme in light
 *    and dark. Reduced motion renders the ring static: the shared live reader
 *    stops the sweep, and the stylesheet's `prefers-reduced-motion` rule pins
 *    the gradient even if an animation were running.
 *  - `MastermindMeta`: the "N messages remembered" note on the row's agent/meta
 *    line (the row has a fixed set of lines, so no new line under the title). */
import { motion } from 'framer-motion'
import { Brain } from 'lucide-react'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** One full sweep of the ring, in seconds: slow enough to read as ambient. */
const GLOW_SWEEP_S = 8

export function MastermindRowGlow() {
  const reduceMotion = useReducedMotion()
  return (
    <motion.span
      aria-hidden="true"
      data-testid="mastermind-row-glow"
      data-motion={reduceMotion ? 'static' : 'sweep'}
      className="mastermind-row-glow"
      initial={false}
      // 0% -> 200% moves the 200%-wide gradient by exactly one tile, so the
      // repeat is seamless.
      animate={reduceMotion ? undefined : { backgroundPositionX: ['0%', '200%'] }}
      transition={reduceMotion ? undefined : { duration: GLOW_SWEEP_S, ease: 'linear', repeat: Infinity }}
    />
  )
}

export function MastermindMeta({ remembered }: { remembered: number | null }) {
  const label = remembered === null
    ? i18nT('pages.chatSidebar.mastermind')
    : i18nT('pages.chatSidebar.mastermind_remembered', { count: remembered, formattedCount: fmtNumber(remembered) })
  return (
    <span className="shrink-0 inline-flex items-center gap-0.5 min-w-0 max-w-[60%] text-accent font-normal" title={label}
      data-testid="mastermind-row-meta">
      <Brain size={10} className="shrink-0" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </span>
  )
}
