/** One conductor's crew in brief: how many workers need the person, have
 *  stalled, run or wait. The sidebar's conductor row (`CrewRootSummaryLine`)
 *  and the status row under the conductor's latest reply (`CrewReplyStatus`)
 *  both read it, so the two never count differently. It reads the worker list
 *  through `useCrewWorkerSummary`, the query and cache the Workers panel uses,
 *  so a second reader adds no poll. */
import { useEffect, useState } from 'react'
import { shallowEqual } from 'react-redux'
import { useAppSelector } from '../../store'
import type { CrewWorkFacts } from '../../types'
import { i18nT } from '../../i18n/t'
import { crewNeedsYou, crewWorkCounts, longestStalledWork, stalledWorkKeys, useCrewWorkClock } from '../../lib/crewWork'
import { crewRootOf } from '../../lib/crewMode'
import { crewRowCounts, summarizeCrewWorkers, type CrewRowCount } from '../../lib/crewWorkers'
import { useCrewWorkerSummary } from '../../pages/chat/command-center/CrewWorkerList'
import { crewRowTooltip } from './CrewRowCounts'

export interface CrewRootAttention {
  /** The latest worker read failed. */
  failed: boolean
  /** Pause all stands on this crew. */
  paused: boolean
  /** Every worker the read listed, and how many of them finished. */
  total: number
  done: number
  rowCounts: CrewRowCount[]
  running: number
  /** Idle workers, and workers held on the conductor, less those counted as stalled. */
  waiting: number
  needsYou: number
  stalled: number
  /** The longest-stalled worker's name and how long it has been quiet, in ms. */
  stalledName: string
  stalledForMs: number
  /** The full counts as one sentence, for a tooltip or an `aria-label`. */
  title: string
}

/** *root*'s crew in brief. *work* is the root's ledger summary (`crew_work`);
 *  *rootOwes* counts an approval or question the root itself holds. */
export function useCrewRootAttention(root: string, work: CrewWorkFacts | null | undefined, rootOwes = 0): CrewRootAttention {
  const { lastWorkers, failed, paused, closedOmitted } = useCrewWorkerSummary(root)
  const { groups, done, total } = summarizeCrewWorkers(lastWorkers ?? [])
  const rowCounts = crewRowCounts(groups)
  const running = rowCounts.find(c => c.bucket === 'working')?.count ?? 0
  const waiting = rowCounts.find(c => c.bucket === 'waiting')?.count ?? 0
  // The ledger's own stall rule (the Board's Milestones read the same one).
  useCrewWorkClock(work ? [work] : [])
  const stall = useAppSelector(st => {
    const now = Date.now()
    const counts = crewWorkCounts(work, st.dashboard.slots, now)
    // A running conductor turn is picking its stalls up: none are shown meanwhile.
    const handling = !!st.dashboard.slots.find(x => x.key === root)?.running
    // The crew members whose live row waits on a tool approval, as one string so
    // the selector compares by value.
    const live = st.dashboard.slots.filter(x => x.pending_approval && crewRootOf(x) === root).map(x => x.key).join(' ')
    return { person: counts?.person ?? 0, stalled: handling ? 0 : counts?.stalled ?? 0, keys: handling ? '' : stalledWorkKeys(work, st.dashboard.slots, now), live }
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
  // A waiting worker whose ledger item went quiet is counted once, as stalled:
  // the parts drawn never add up to more workers than the crew has.
  const stalledWorkers = new Set(stall.keys ? stall.keys.split(' ').map(p => p.slice(0, p.lastIndexOf('@'))).filter(Boolean) : [])
  const stalledWaiting = (lastWorkers ?? []).filter(w => (w.group === 'waiting' || w.group === 'with_conductor') && stalledWorkers.has(w.key)).length
  const needsYou = crewNeedsYou(lastWorkers, stall.live ? stall.live.split(' ') : [], stall.person, work?.person_workers) + rootOwes
  const member = longest ? lastWorkers?.find(w => w.key === longest.worker) : undefined
  return {
    failed,
    paused,
    total,
    done,
    rowCounts,
    running,
    waiting: Math.max(0, waiting - stalledWaiting),
    needsYou,
    stalled: stall.stalled,
    stalledName: member?.title || member?.agent || longestSlot || i18nT('commandCenter.worker_untitled'),
    stalledForMs: longest ? Date.now() - longest.since : 0,
    title: crewRowTooltip(total + closedOmitted, rowCounts, stall.stalled, needsYou),
  }
}
