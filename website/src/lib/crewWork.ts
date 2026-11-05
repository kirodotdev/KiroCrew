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
import { fmtDuration } from '../i18n/format'

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

/** The stalled items (same rule as `crewWorkCounts`), each as its worker's slot
 *  key ("" for a remote or unbound one) and its `stale_at_ms`, joined into one
 *  string so a selector can compare it by value. `stale_at_ms` only says THAT an
 *  item is stale: the gateway stamps every already-stale item with the moment it
 *  summarized, so it cannot rank them. The caller ranks by the worker's own last
 *  activity. */
export function stalledWorkKeys(
  facts: CrewWorkFacts | undefined | null,
  rows: readonly CrewWorkRow[],
  now: number,
): string {
  if (!facts || typeof facts !== 'object' || !Array.isArray(facts.items)) return ''
  const running = new Set(rows.filter(r => r.running).map(r => r.key))
  const out: string[] = []
  for (const item of facts.items) {
    if (!item || typeof item !== 'object') continue
    const at = typeof item.stale_at_ms === 'number' ? item.stale_at_ms : 0
    if ((item.worker && running.has(item.worker)) || at <= 0 || now < at) continue
    out.push(`${item.worker || ''}@${at}`)
  }
  return out.join(' ')
}

/** Of `stalled` (from `stalledWorkKeys`), the one quiet longest: the oldest
 *  `quietSince(worker)` wins, an item with none falls back to its stale moment.
 *  Returns the worker and the moment it went quiet; `null` for none. */
export function longestStalledWork(stalled: string, quietSince: (worker: string) => number): { worker: string; since: number } | null {
  let best: { worker: string; since: number } | null = null
  for (const part of stalled ? stalled.split(' ') : []) {
    const cut = part.lastIndexOf('@')
    const worker = part.slice(0, cut)
    const q = worker ? quietSince(worker) : NaN
    const since = Number.isFinite(q) ? q : Number(part.slice(cut + 1))
    if (!best || since < best.since) best = { worker, since }
  }
  return best
}

/** How many things a crew holds for the person: each worker waiting on a tool
 *  approval, plus each ledger item waiting on a person, counting a worker that
 *  is in both once. `listed` is the worker list (its `approval` group),
 *  `liveApprovals` the keys of the live rows waiting on an approval (in before
 *  the list has answered), `person` and `personWorkers` the ledger's
 *  (`crew_work`). An item with no worker key, or a gateway that sends no keys,
 *  adds its own count. */
export function crewNeedsYou(
  listed: readonly { key: string; group: string }[] | undefined,
  liveApprovals: readonly string[],
  person: number,
  personWorkers: readonly string[] | undefined,
): number {
  const approvals = new Set([...(listed ?? []).filter(w => w.group === 'approval').map(w => w.key), ...liveApprovals])
  const keyed = (personWorkers ?? []).filter(Boolean)
  const unkeyed = Math.max(0, person - keyed.length)
  const extra = new Set(keyed.filter(k => !approvals.has(k))).size
  return approvals.size + extra + unkeyed
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

/** A quiet or elapsed time in whole minutes, short: "32m", "1h 5m". The one form
 *  the Board's Milestones and the Sessions row's stalled line both use; '' when
 *  `mins` is not a number. */
export function fmtMinutes(mins: number): string {
  if (!Number.isFinite(mins)) return ''
  if (mins < 60) return fmtDuration([[Math.max(1, mins), 'minute']], { unitDisplay: 'narrow' } as never)
  return fmtDuration([[Math.floor(mins / 60), 'hour'], [mins % 60, 'minute']], { dropZero: true, unitDisplay: 'narrow' } as never)
}
