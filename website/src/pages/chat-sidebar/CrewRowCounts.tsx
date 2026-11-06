/** An Ultra Mode root row's status: only what needs attention, in a fixed
 *  order, each part shown only when its count is above zero: "N needs you"
 *  (approvals and questions), "N stalled" (with the longest-stalled crew member
 *  on a second line), "N running". With none of them a finished crew reads
 *  "Done" and an unfinished one shows nothing extra. The full counts stay in
 *  the tooltip. Each glyph has its own shape, so it reads without colour. */
import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { Check, Circle, Hand, Pause } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'
import { fmtMinutes } from '../../lib/crewWork'
import type { CrewRowCount } from '../../lib/crewWorkers'

/** The glyph size of the row's other status marks. */
const ICON_PX = 11

/** Each count's words in the tooltip: "2 working", "1 waiting", "1 needs you". */
const COUNT_KEY: Record<CrewRowCount['bucket'], string> = {
  working: 'components.crewMode.ultra_row_count_working',
  waiting: 'components.crewMode.ultra_row_count_waiting',
  needs_you: 'components.crewMode.ultra_row_count_needs_you',
}

/** The tooltip: every count the row no longer draws, the session total first. */
export function crewRowTooltip(total: number, counts: readonly CrewRowCount[], stalled: number, needsYou: number): string {
  // The needs-you entry is the row's own total (the main chat's asks and the
  // ledger's included), so the tooltip never says less than the row.
  const parts = [i18nT('pages.chatSidebar.session', { count: total })]
  if (needsYou > 0) parts.push(i18nT(COUNT_KEY.needs_you, { count: needsYou }))
  parts.push(...counts.filter(c => c.bucket !== 'needs_you').map(c => i18nT(COUNT_KEY[c.bucket], { count: c.count })))
  if (stalled > 0) parts.push(i18nT('components.crewMode.ultra_row_count_stalled', { count: stalled }))
  return fmtList(parts)
}


/** How many of a wrapping one-line box's items sit below its first line: the
 *  parts a narrow row could not fit. Re-measured when the box resizes and when
 *  `content` changes. */
function useWrappedAwayCount<T extends HTMLElement>(content: string): { ref: RefObject<T>; hidden: number } {
  const ref = useRef<T>(null)
  const [hidden, setHidden] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') { setHidden(0); return }
    // The first item (the zero-width holder) sits on the visible line; any item
    // below it wrapped away.
    const measure = () => {
      const [first, ...rest] = [...el.children] as HTMLElement[]
      setHidden(first ? rest.filter(c => c.offsetTop > first.offsetTop).length : 0)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [content])
  return { ref, hidden }
}

export interface CrewRowAttentionProps {
  needsYou: number
  stalled: number
  running: number
  /** Every crew member finished. */
  done: boolean
  /** Pause all stands: the row says "Paused", after what needs the person. */
  paused?: boolean
  /** The longest-stalled crew member and how long it has been quiet, in ms. */
  stalledName?: string
  stalledForMs?: number
  title: string
}

const PART_CLS = 'inline-flex items-center gap-1 h-4 text-[12px] whitespace-nowrap tabular-nums'

export function CrewRowAttention({ needsYou, stalled, running, done, paused, stalledName, stalledForMs, title }: CrewRowAttentionProps) {
  const { ref, hidden } = useWrappedAwayCount<HTMLSpanElement>(`${needsYou}/${stalled}/${running}`)
  if (paused) {
    // What needs the person still comes first: the pause leaves a member that
    // waits on them as it is, so the row keeps saying so.
    return (
      <span className="min-w-0 flex items-center gap-x-2" title={title}>
        {needsYou > 0 && (
          <span className={`${PART_CLS} crew-tone-warn font-medium`} data-testid="crew-root-count" data-bucket="needs_you">
            <Hand size={ICON_PX} aria-hidden="true" className="inline-block shrink-0" />{i18nT('components.crewMode.ultra_row_count_needs_you', { count: needsYou })}
          </span>
        )}
        <span className={`${PART_CLS} crew-tone-paused font-medium`} data-testid="crew-root-paused">
          <Pause size={ICON_PX} aria-hidden="true" className="inline-block shrink-0" />{i18nT('components.crewMode.ultra_paused')}
        </span>
      </span>
    )
  }
  if (needsYou <= 0 && stalled <= 0 && running <= 0) {
    if (!done) return null
    return (
      <span className={`${PART_CLS} crew-tone-ok`} data-testid="crew-root-done" title={title}>
        <Check size={ICON_PX} aria-hidden="true" className="inline-block shrink-0" />{i18nT('commandCenter.worker_group_done')}
      </span>
    )
  }
  return (
    <span className="min-w-0 flex flex-col" data-testid="crew-root-attention" title={title}>
      <span className="min-w-0 flex items-center">
      {/* Whole parts only: one that does not fit wraps onto a hidden second
          line, and an ellipsis says some are hidden (the tooltip has them). A
          zero-width first item holds the line, so even the first part wraps
          away whole; its negative margin cancels the gap. */}
      <span ref={ref} className="min-w-0 flex flex-wrap content-start items-center gap-x-2 h-4 overflow-hidden">
        <span aria-hidden="true" className="w-0 h-4 -mr-2" />
        {needsYou > 0 && (
          <span className={`${PART_CLS} crew-tone-warn font-medium`} data-testid="crew-root-count" data-bucket="needs_you">
            <Hand size={ICON_PX} aria-hidden="true" className="inline-block shrink-0" />{i18nT('components.crewMode.ultra_row_count_needs_you', { count: needsYou })}
          </span>
        )}
        {stalled > 0 && (
          <span className={`${PART_CLS} text-danger font-medium`} data-testid="crew-root-count" data-bucket="stalled">
            <Pause size={ICON_PX} aria-hidden="true" className="inline-block shrink-0" />{i18nT('components.crewMode.ultra_row_count_stalled', { count: stalled })}
          </span>
        )}
        {running > 0 && (
          <span className={`${PART_CLS} text-[var(--ok)]`} data-testid="crew-root-count" data-bucket="running">
            <Circle size={ICON_PX} aria-hidden="true" fill="currentColor" className="inline-block shrink-0" />{i18nT('components.crewMode.ultra_row_count_running', { count: running })}
          </span>
        )}
      </span>
      {hidden > 0 && <span aria-hidden="true" className="shrink-0 pl-1 text-[12px] text-muted" data-testid="crew-root-hidden">…</span>}
      </span>
      {stalled > 0 && stalledName && stalledForMs !== undefined && (
        <span className="text-[12px] text-muted truncate" data-testid="crew-root-stalled-detail">
          {i18nT('components.crewMode.ultra_row_stalled_detail', { name: stalledName, duration: fmtMinutes(Math.round(stalledForMs / 60000)) })}
        </span>
      )}
    </span>
  )
}
