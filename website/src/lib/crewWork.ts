/** A conductor's open work, read against the live slot rows: what the sidebar row
 *  and the board lane need to tell a conductor whose work still moves (a worker
 *  parked between checks, a check waiting on CI) from one holding for the person,
 *  and from one whose work has gone quiet.
 *
 *  The ledger half arrives on the conductor's row as `crew_work`
 *  (`dashboard/crew_work.py`), with the stale moment already worked out by the
 *  ledger's own rule. The one live fact left here is whether a worker's turn is
 *  running, which this tab holds and which the ledger reads the same way: a
 *  running worker is never stale. */
import { useEffect, useState } from 'react'
import type { CrewWorkFacts } from '../types'

export interface CrewWorkCounts {
  /** Open items waiting on a person. */
  person: number
  /** Open items still moving: not stalled and not on a person. */
  moving: number
  /** Open items gone quiet with their next move unmade. */
  stalled: number
}

/** The slot-row fields this reads. */
export interface CrewWorkRow {
  key: string
  running?: boolean
}

/** `facts` counted against the live rows at `now`; `null` when there are none. */
export function crewWorkCounts(
  facts: CrewWorkFacts | undefined | null,
  rows: readonly CrewWorkRow[],
  now: number,
): CrewWorkCounts | null {
  if (!facts || typeof facts !== 'object' || !Array.isArray(facts.items)) return null
  const running = new Set(rows.filter(r => r.running).map(r => r.key))
  let moving = 0
  let stalled = 0
  for (const item of facts.items) {
    if (!item || typeof item !== 'object') continue
    const at = typeof item.stale_at_ms === 'number' ? item.stale_at_ms : 0
    const stale = !(item.worker && running.has(item.worker)) && at > 0 && now >= at
    if (stale) stalled += 1
    else moving += 1
  }
  return { person: Math.max(0, facts.person | 0), moving, stalled }
}

/** The earliest `stale_at_ms` after `now` across `facts`, or `null` for none. */
export function nextCrewWorkDeadline(facts: readonly (CrewWorkFacts | undefined | null)[], now: number): number | null {
  let next: number | null = null
  for (const f of facts) {
    if (!f || !Array.isArray(f.items)) continue
    for (const item of f.items) {
      const at = item && typeof item.stale_at_ms === 'number' ? item.stale_at_ms : 0
      if (at > now && (next === null || at < next)) next = at
    }
  }
  return next
}

/** A counter that steps at the next stale moment in `facts`, so a reader whose
 *  data has not changed still re-reads the clock when an item goes stale. One
 *  timer, re-armed after each step; none while no moment is ahead. */
export function useCrewWorkClock(facts: readonly (CrewWorkFacts | undefined | null)[]): number {
  const [tick, setTick] = useState(0)
  const next = nextCrewWorkDeadline(facts, Date.now())
  useEffect(() => {
    if (next === null) return
    // Capped so a far deadline cannot overflow the timer (about 24.8 days).
    const id = setTimeout(() => setTick(n => n + 1), Math.min(Math.max(0, next - Date.now()) + 50, 2_000_000_000))
    return () => clearTimeout(id)
  }, [next, tick])
  return tick
}
