import { featureProgress, milestonesLoadFailed, untriedNow, useMilestones, useMilestonesPanel, toggleMilestonesPanel } from './useMilestones'
import { i18nT } from '../../i18n/t'
import { Glass } from '../Glass'

/**
 * A thin progress ring: `done / total` of the new-feature rows, drawn as a
 * conic fill masked to a 2px band (no inline SVG). `dot` adds a small mark
 * while "Try today" still has something to try.
 */
export function MilestoneRing({ done, total, dot = false, size = 14 }: { done: number; total: number; dot?: boolean; size?: number }) {
  const ratio = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0
  const band = 'radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px))'
  return (
    <span className="relative inline-flex shrink-0" aria-hidden="true">
      <span
        data-testid="milestones-ring"
        data-ratio={ratio.toFixed(3)}
        className="inline-block rounded-full"
        style={{
          width: size,
          height: size,
          background: `conic-gradient(var(--accent) ${ratio * 360}deg, var(--border) 0deg)`,
          WebkitMask: band,
          mask: band,
        }}
      />
      {dot && <span data-testid="milestones-ring-dot" className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-accent" />}
    </span>
  )
}

/** The accessible name: progress, and the streak when there is one. */
export function indicatorLabel(done: number, total: number, streak: number): string {
  return streak >= 1
    ? i18nT('milestones.indicator_label_streak', { done, total, count: streak })
    : i18nT('milestones.indicator_label', { done, total })
}

/** The ring's numbers, the streak and the dot, from one read. */
export function useIndicatorState() {
  const { data, isError, error } = useMilestones()
  // A failed read (not a 404) still draws an empty ring, so Explore can open
  // and say what went wrong instead of the feature silently vanishing.
  const failed = milestonesLoadFailed(isError, error) && !data
  const shown = failed || (!!data?.enabled && !!data.features)
  const { done, total } = featureProgress(data)
  const streak = data?.streak?.current ?? 0
  return { shown, done, total, streak, dot: untriedNow(data).length > 0 }
}

/**
 * The top-bar entry to Explore: a ring for the new-feature rows, a dot while
 * "Try today" has items, a visible "Explore 2/7", and the current streak as
 * smaller "5-day streak" text. On desktop it
 * sits in its own pill next to the readout capsule (`MilestonesPill`); on a phone
 * it stands beside the bell. Nothing renders until the read
 * has answered `enabled: true` (or failed for a reason other than a missing
 * route), so nothing flashes in before load.
 */
export default function MilestonesIndicator({ className, streakClassName = '', compact = false }: { className?: string; streakClassName?: string; compact?: boolean }) {
  const { shown, done, total, streak, dot } = useIndicatorState()
  const { open } = useMilestonesPanel()
  if (!shown) return null
  const label = indicatorLabel(done, total, streak)
  return (
    <button
      type="button"
      data-milestones-trigger=""
      data-testid="milestones-indicator"
      className={className
        ? `${className} ${open ? 'text-accent' : 'text-muted hover:text-text'} text-[12px]`
        : `flex items-center justify-center gap-1 h-7 min-w-7 px-1.5 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 whitespace-nowrap ${open ? 'text-accent' : 'text-muted hover:text-text'}`}
      onClick={toggleMilestonesPanel}
      aria-label={label}
      title={label}
      aria-haspopup="dialog"
      aria-expanded={open}
    >
      <MilestoneRing done={done} total={total} dot={dot} />
      {/* Says what the ring is and where it goes; the streak is the smaller second line of meaning. */}
      <span className="text-[12px] font-medium tabular-nums text-text" data-testid="milestones-count">{i18nT('milestones.pill_count', { done, total })}</span>
      {/* A phone bar has room for the ring and its name, not a third label: the
          streak there lives in Explore's Your progress. */}
      {streak >= 1 && !compact && <span className={`text-[11px] tabular-nums text-muted ${streakClassName}`} data-testid="milestones-streak">{i18nT('milestones.streak_short', { count: streak })}</span>}
    </button>
  )
}

/**
 * The desktop entry: its own Liquid Glass pill next to the readout capsule,
 * shaped like the feedback pill (28px tall, 12px radius), holding the one
 * indicator button. Not a capsule segment, so the capsule keeps its own
 * buttons and nothing else; an install without milestones draws nothing here.
 * The streak words fold with the feedback labels on the top bar's ladder, so a
 * narrow window keeps the ring.
 */
export function MilestonesPill() {
  const { shown } = useIndicatorState()
  if (!shown) return null
  return (
    <Glass variant="chip" radius={12} className="glass-shadow flex items-center h-7 shrink-0" data-testid="milestones-pill">
      <MilestonesIndicator
        className="flex items-center gap-1.5 h-full px-2.5 transition-colors cursor-pointer whitespace-nowrap bg-transparent border-0"
        streakClassName="tb-drop-feedback-label"
      />
    </Glass>
  )
}
