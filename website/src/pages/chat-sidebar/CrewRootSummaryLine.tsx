/** An Ultra Mode root's crew summary line on its session row, and the needs-you
 *  count it says, which a title-only card's crew mark reads too
 *  (`CompactCrewMarks`), so the two never disagree. */
import { useEffect, useState, type ReactNode } from 'react'
import { shallowEqual } from 'react-redux'
import { useAppSelector } from '../../store'
import type { CrewWorker, CrewWorkFacts } from '../../types'
import { crewNeedsYou, crewWorkCounts, longestStalledWork, stalledWorkKeys, useCrewWorkClock } from '../../lib/crewWork'
import { crewRootOf } from '../../lib/crewMode'
import { summarizeCrewWorkers, crewRowCounts } from '../../lib/crewWorkers'
import { CrewRowAttention, crewRowTooltip } from './CrewRowCounts'
import { CrewStateWord, type CrewRowOwnState } from './CrewRowState'
import { useCrewWorkerSummary } from '../chat/command-center/CrewWorkerList'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/** What *root*'s crew holds for the person: each worker waiting on a tool
 *  approval (the worker list's, or a live row's before the list answers) and
 *  each ledger item waiting on a person (`crewNeedsYou`). */
export function useCrewRootNeedsYou(root: string, work: CrewWorkFacts | null | undefined, listed: CrewWorker[] | undefined): number {
  const facts = useAppSelector(st => ({
    person: crewWorkCounts(work, st.dashboard.slots, Date.now())?.person ?? 0,
    // The crew members whose live row waits on a tool approval, as one string so
    // the selector compares by value.
    live: st.dashboard.slots.filter(x => x.pending_approval && crewRootOf(x) === root).map(x => x.key).join(' '),
  }), shallowEqual)
  return crewNeedsYou(listed, facts.live ? facts.live.split(' ') : [], facts.person, work?.person_workers)
}

/** An Ultra Mode root's status line: "N needs you", "N stalled" (plus who
 *  stalled longest), "N running", or "Done" -- only what applies. With no crew
 *  and nothing owed it shows *fallback*, the row's usual line. */
export function CrewRootSummaryLine({ root, fallback, own = null, work, rootOwes = 0, dot = null, lineCls }: {
  root: string; lineCls: string; fallback: ReactNode; own?: CrewRowOwnState | null; /** The row's unread dot, kept in front of the status. */ dot?: ReactNode
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
  const needsYou = useCrewRootNeedsYou(root, work, lastWorkers) + rootOwes
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
    <div className={lineCls} data-testid="crew-root-summary">{dot}
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
