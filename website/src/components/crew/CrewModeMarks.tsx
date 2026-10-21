/** The small marks every Crew Mode surface shares: the solid ghost badge, the
 *  gradient "CREW MODE" eyebrow, a worker's state dot and a stack of tiny worker
 *  ghosts coloured by state. Colours are theme tokens (`styles/crew-mode.css`,
 *  `lib/crewWorkers.ts`), so light and dark themes follow without a branch here. */
import { KiroGhostMark } from '../KiroGhostMark'
import { i18nT } from '../../i18n/t'
import { CREW_GROUP_LABEL_KEY, CREW_GROUP_TONE, type CrewWorkerGroup } from '../../lib/crewWorkers'

/** The solid ghost on the crew gradient: the root row's badge, the breadcrumb's root. */
export function CrewGhostBadge({ size = 28 }: { size?: number }) {
  return (
    <span aria-hidden="true" data-testid="crew-ghost-badge" className="crew-grad shrink-0 rounded-full grid place-items-center" style={{ width: size, height: size }}>
      <KiroGhostMark size={Math.round(size * 0.5)} className="inline-block shrink-0 text-aim-fg" />
    </span>
  )
}

/** "CREW MODE" in the crew gradient, the root row's first line. */
export function CrewEyebrow() {
  return (
    <span data-testid="crew-eyebrow" className="crew-grad-text font-bold uppercase tracking-[0.12em] truncate">
      {i18nT('components.crewMode.eyebrow')}
    </span>
  )
}

/** A worker's state as a dot and a word. The word is the state, not a hint. */
export function CrewStatusDot({ group, withLabel = true }: { group: CrewWorkerGroup; withLabel?: boolean }) {
  const label = i18nT(CREW_GROUP_LABEL_KEY[group])
  return (
    <span data-testid="crew-status" data-group={group} className={`inline-flex items-center gap-1 text-[12px] shrink-0 ${CREW_GROUP_TONE[group]}`}>
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

/** The composer's Crew Mode mark on a conductor chat: a label, not a control. */
export function CrewModeComposerChip() {
  return (
    <span data-testid="crew-mode-composer-chip" className="crew-grad inline-flex items-center gap-1.5 h-7 rounded-lg px-2.5 text-[12px] font-medium shrink-0" title={i18nT('components.crewMode.composer_chip_title')}>
      <KiroGhostMark size={13} className="inline-block shrink-0" />
      {i18nT('components.crewMode.chip_off')}
    </span>
  )
}
