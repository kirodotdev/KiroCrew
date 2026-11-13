/** An Ultra Mode root's crew on its session row: the status line's counts and
 *  the title-only card's marks, all from one needs-you count. */
import { useEffect, useState, type ReactNode } from 'react'
import { Pause } from 'lucide-react'
import { shallowEqual } from 'react-redux'
import { useAppSelector } from '../../store'
import type { CrewWorkFacts } from '../../types'
import { crewNeedsYou, crewWorkCounts, longestStalledWork, stalledWorkKeys, useCrewWorkClock } from '../../lib/crewWork'
import { i18nT } from '../../i18n/t'
import { crewRootOf } from '../../lib/crewMode'
import { summarizeCrewWorkers, crewRowCounts } from '../../lib/crewWorkers'
import { CrewRowAttention, crewRowTooltip } from './CrewRowCounts'
import { CrewStateWord, type CrewRowOwnState } from './CrewRowState'
import { useCrewWorkerSummary } from '../chat/command-center/CrewWorkerList'
import ErrorNotice from '../../components/ErrorNotice'
import { ROW_ICON_PX, ROW_STATUS_LINE_CLS } from './rowLineStyle'

/** What an Ultra Mode root's crew holds for the person, the root's own asks
 *  aside: listed workers in the approval group, live rows waiting on a tool
 *  approval, and the ledger's items on a person (`crew_work`). The status line
 *  and the title-only card's marker both read it, so they agree. An empty
 *  *root* reads nothing and counts 0. */
export function useCrewSubtreeNeedsYou(root: string, work: CrewWorkFacts | null | undefined): number {
  const { lastWorkers } = useCrewWorkerSummary(root)
  const { live, person } = useAppSelector(st => {
    if (!root) return { live: '', person: 0 }
    // As one string so the selector compares by value.
    const live = st.dashboard.slots.filter(x => x.pending_approval && crewRootOf(x) === root).map(x => x.key).join(' ')
    return { live, person: crewWorkCounts(work, st.dashboard.slots, Date.now())?.person ?? 0 }
  }, shallowEqual)
  return crewNeedsYou(lastWorkers, live ? live.split(' ') : [], person, work?.person_workers)
}

/** An Ultra Mode root's status line: "N needs you", "N stalled" (plus who
 *  stalled longest), "N running", or "Done" -- only what applies. With no crew
 *  and nothing owed it shows *fallback*, the row's usual line. */
export function CrewRootSummaryLine({ root, fallback, own = null, work, rootOwes = 0, dot = null }: {
  root: string; fallback: ReactNode; own?: CrewRowOwnState | null; /** The row's unread dot, kept in front of the status. */ dot?: ReactNode
  /** The root's ledger in brief (`crew_work`): what waits on a person, what stalled. */
  work?: CrewWorkFacts | null
  /** The root itself holds an approval or a question for the person. */
  rootOwes?: number
}) {
  // The server's groups (the worker list API), so the row, the panel and the
  // breadcrumb agree. Only what needs attention is drawn (CrewRowAttention); the
  // full counts live in the tooltip. A failed poll says so in the status line.
  const { lastWorkers, failed, paused } = useCrewWorkerSummary(root)
  const { groups, done, total } = summarizeCrewWorkers(lastWorkers ?? [])
  const rowCounts = crewRowCounts(groups)
  const running = rowCounts.find(c => c.bucket === 'working')?.count ?? 0
  // The ledger's own stall rule (the Board's Milestones read the same one).
  useCrewWorkClock(work ? [work] : [])
  const stall = useAppSelector(st => {
    const now = Date.now()
    const counts = crewWorkCounts(work, st.dashboard.slots, now)
    // A running conductor turn is picking its stalls up: none are shown meanwhile.
    const handling = !!st.dashboard.slots.find(x => x.key === root)?.running
    return { stalled: handling ? 0 : counts?.stalled ?? 0, keys: handling ? '' : stalledWorkKeys(work, st.dashboard.slots, now) }
  }, shallowEqual)
  const subtreeNeedsYou = useCrewSubtreeNeedsYou(root, work)
  // A stalled line's duration reads the clock: step it once a minute while shown.
  const [, setMinute] = useState(0)
  useEffect(() => {
    if (stall.stalled <= 0) return
    const id = setInterval(() => setMinute(n => n + 1), 60_000)
    return () => clearInterval(id)
  }, [stall.stalled])
  const longest = longestStalledWork(stall.keys, worker => {
    const at = lastWorkers?.find(w => w.key === worker)?.last_activity
    return at ? Date.parse(at) : NaN
  })
  const longestSlot = useAppSelector(st => (longest?.worker ? st.dashboard.slots.find(x => x.key === longest.worker)?.title ?? '' : ''))
  const needsYou = subtreeNeedsYou + rootOwes
  const member = longest ? lastWorkers?.find(w => w.key === longest.worker) : undefined
  const stalledForMs = longest ? Date.now() - longest.since : 0
  const stalledName = member?.title || member?.agent || longestSlot || i18nT('commandCenter.worker_untitled')
  const title = crewRowTooltip(total, rowCounts, stall.stalled, needsYou)
  // Finished only when the main chat itself is settled too.
  const allDone = total > 0 && done === total && !own
  // An interrupted turn or a stalled goal loop of the main chat is said first.
  const ownFirst = !paused && own && (own.kind === 'paused' || own.kind === 'stalled') ? own : null
  // Nothing needs attention and the crew is not finished (or paused): the row's usual line.
  if (!failed && !paused && !ownFirst && !allDone && needsYou === 0 && stall.stalled === 0 && running === 0) return <>{fallback}</>
  return (
    <div className={ROW_STATUS_LINE_CLS} data-testid="crew-root-summary">{dot}
      {/* The row's own state outranks a failed crew read: it is said first. */}
      {failed && own && <CrewStateWord kind={own.kind} title={own.title} />}
      {!failed && ownFirst && <CrewStateWord kind={ownFirst.kind} title={ownFirst.title} />}
      {failed ? (
        <ErrorNotice variant="inline" askAgent className="min-w-0" messageClassName="truncate" message={i18nT('commandCenter.workers_error')} />
      ) : (
        <CrewRowAttention needsYou={needsYou} stalled={stall.stalled} running={running} done={allDone} paused={paused}
          stalledName={stall.stalled > 0 ? stalledName : undefined} stalledForMs={stall.stalled > 0 ? stalledForMs : undefined} title={title} />
      )}
    </div>
  )
}

/** Title-only cards hide the crew summary line, so a paused Ultra Mode crew
 *  keeps its Paused word as a title-line mark. It reads the query the line reads. */
export function CompactCrewPausedMark({ root }: { root: string }) {
  const { paused } = useCrewWorkerSummary(root)
  if (!paused) return null
  const label = i18nT('components.crewMode.ultra_paused')
  return (
    <span className="inline-flex items-center align-middle mr-1.5 crew-tone-paused" data-testid="session-compact-crew-paused" role="img" aria-label={label} title={label}>
      <Pause size={ROW_ICON_PX} className="shrink-0" aria-hidden />
    </span>
  )
}
