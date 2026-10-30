/** The small marks every Crew Mode surface shares: the solid ghost badge, the
 *  gradient "CREW MODE" eyebrow, a worker's state dot and a stack of tiny worker
 *  ghosts coloured by state. Colours are theme tokens (`styles/crew-mode.css`,
 *  `lib/crewWorkers.ts`), so light and dark themes follow without a branch here. */
import { motion, useReducedMotion } from 'framer-motion'
import { KiroGhostMark } from '../KiroGhostMark'
import { Zap } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { CREW_GROUP_LABEL_KEY, CREW_GROUP_TONE, type CrewWorkerGroup } from '../../lib/crewWorkers'

/** The solid ghost on the crew gradient: the breadcrumb's root. */
export function CrewGhostBadge({ size = 28 }: { size?: number }) {
  return (
    <span aria-hidden="true" data-testid="crew-ghost-badge" className="crew-grad shrink-0 rounded-full grid place-items-center" style={{ width: size, height: size }}>
      <KiroGhostMark size={Math.round(size * 0.5)} className="inline-block shrink-0 text-aim-fg" />
    </span>
  )
}

/** "CREW MODE" in the crew gradient, the root row's first line. Tracked a whole
 *  pixel: an em fraction at this size lands letters on fractional pixels, which
 *  renders one gap wider than the rest. */
export function CrewEyebrow() {
  return (
    <span data-testid="crew-eyebrow" className="crew-eyebrow-text font-bold uppercase tracking-[1px] truncate">
      {i18nT('components.crewMode.eyebrow')}
    </span>
  )
}

/** A worker's state as a dot and a word. The word is the state, not a hint. */
export function CrewStatusDot({ group, withLabel = true, className = '' }: { group: CrewWorkerGroup; withLabel?: boolean; className?: string }) {
  const label = i18nT(CREW_GROUP_LABEL_KEY[group])
  return (
    <span data-testid="crew-status" data-group={group} className={`inline-flex items-center gap-1 text-[12px] shrink-0 ${CREW_GROUP_TONE[group]} ${className}`}>
      <span aria-hidden="true" className={`w-2 h-2 rounded-full ${group === 'waiting' ? 'border border-current' : 'bg-current'}`} />
      {withLabel ? <span>{label}</span> : <span className="sr-only">{label}</span>}
    </span>
  )
}

/** Up to `max` tiny worker ghosts, one per worker, needs-you first. */
export function CrewWorkerStack({ groups, max = 6 }: { groups: CrewWorkerGroup[]; max?: number }) {
  const shown = groups.slice(0, max)
  const extra = groups.length - shown.length
  return (
    <span aria-hidden="true" data-testid="crew-worker-stack" className="inline-flex items-center -space-x-1">
      {shown.map((g, i) => (
        // Tinted by the worker's own state colour, so the stack reads at a glance.
        <span key={i} data-group={g} className={`w-[18px] h-[18px] rounded-full grid place-items-center border border-current ring-2 ring-bg ${CREW_GROUP_TONE[g]}`}
          style={{ backgroundColor: 'color-mix(in srgb, currentColor 22%, var(--bg-elevated))' }}>
          <KiroGhostMark size={11} className="inline-block shrink-0" />
        </span>
      ))}
      {extra > 0 && <span className="pl-2 text-[11px] text-muted">+{extra}</span>}
    </span>
  )
}

/** The composer's Crew Mode mark on a conductor chat: the lightning bolt and the
 *  name. With `onTurnOff` (no message sent yet) it is a pressed toggle: pressing
 *  it turns Crew Mode off. It renders in place, with no layout move.
 *  Without it, a read-only status: no pill, no hover or pressed state, not focusable. */
export function CrewModeComposerChip({ onTurnOff }: { onTurnOff?: () => void } = {}) {
  const reduceMotion = useReducedMotion()
  if (onTurnOff) {
    return (
      <motion.button
        type="button"
        data-testid="crew-mode-composer-chip"
        aria-label={i18nT('components.crewMode.chip_off')}
        aria-pressed={true}
        title={i18nT('components.crewMode.chip_hint_on')}
        onClick={onTurnOff}
        initial={reduceMotion ? { opacity: 0 } : false}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.2 }}
        className="inline-flex items-center gap-1.5 h-7 px-1.5 -mx-1.5 rounded-md text-[12px] font-semibold shrink-0 cursor-pointer hover:bg-bg-hover"
      >
        <Zap size={13} aria-hidden="true" className="inline-block shrink-0 ultra-accent" />
        <span className="crew-grad-text">{i18nT('components.crewMode.chip_off')}</span>
      </motion.button>
    )
  }
  return (
    <motion.span
      data-testid="crew-mode-composer-chip"
      role="img"
      aria-label={i18nT('components.crewMode.composer_chip_label')}
      initial={reduceMotion ? { opacity: 0 } : false}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
      className="inline-flex items-center gap-1.5 h-7 text-[12px] font-semibold shrink-0 cursor-default select-none"
    >
      <Zap size={13} aria-hidden="true" className="inline-block shrink-0 ultra-accent" />
      <span className="crew-grad-text">{i18nT('components.crewMode.chip_off')}</span>
    </motion.span>
  )
}
