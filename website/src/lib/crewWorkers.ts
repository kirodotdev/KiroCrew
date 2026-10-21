/** Crew Mode workers grouped the way the person reads them: what needs them,
 *  what is moving, what is idle, what is finished.
 *
 *  The rule is the server's (`crew_workers._group`): done and needs-you come from
 *  real signals only (a closed tab, the worker's ledger item, an open approval or
 *  question), never from "it has replied". The session list's ghost stack and
 *  "N needs you" / "Done x/y" line, the breadcrumb and the side panel's cards all
 *  read the server's group from the worker list API. A live slot row alone cannot
 *  say a worker finished, so its mirror (`crewWorkerGroupOfRow`, used only until
 *  that list answers) has no done. */
import type { CrewWorker } from '../types'

export type CrewWorkerGroup = 'needs_you' | 'working' | 'waiting' | 'done'

/** Display order: what needs the person first. */
export const CREW_GROUP_ORDER: readonly CrewWorkerGroup[] = ['needs_you', 'working', 'waiting', 'done']

export const CREW_GROUP_LABEL_KEY: Record<CrewWorkerGroup, string> = {
  needs_you: 'commandCenter.worker_group_needs_you',
  working: 'commandCenter.worker_group_working',
  waiting: 'commandCenter.worker_group_waiting',
  done: 'commandCenter.worker_group_done',
}

/** The tone each group's ghost and dot wear: theme tokens, so light and dark follow. */
export const CREW_GROUP_TONE: Record<CrewWorkerGroup, string> = {
  needs_you: 'crew-tone-warn',
  working: 'crew-tone-aim',
  waiting: 'crew-tone-muted',
  done: 'crew-tone-ok',
}

/** The slot-row fields the grouping reads. */
export interface CrewWorkerRowLike {
  running?: boolean
  pending_approval?: boolean
  needs_input?: boolean
  messages?: number
}

/** One live worker row's group, before the worker list has answered: a slot row
 *  carries no finished signal, so an idle row is waiting, never done. */
export function crewWorkerGroupOfRow(row: CrewWorkerRowLike): CrewWorkerGroup {
  if (row.pending_approval || row.needs_input) return 'needs_you'
  if (row.running) return 'working'
  return 'waiting'
}

/** One row of the worker list API's group: the server's own (`crew_workers._group`),
 *  which every row carries. `crewWorkerGroupOfRow` is the one client mirror of
 *  that rule, for live slot rows that have no API row. */
export function crewWorkerGroup(worker: CrewWorker): CrewWorkerGroup {
  return worker.group
}

/** The worker list in display order, grouped. Empty groups are left out. */
export function groupCrewWorkers(workers: CrewWorker[]): { group: CrewWorkerGroup; workers: CrewWorker[] }[] {
  return CREW_GROUP_ORDER
    .map(group => ({ group, workers: workers.filter(w => crewWorkerGroup(w) === group) }))
    .filter(g => g.workers.length > 0)
}

export interface CrewRootSummary {
  /** One entry per worker, needs-you first, for the row's ghost stack. */
  groups: CrewWorkerGroup[]
  needsYou: number
  done: number
  total: number
}

/** A Crew Mode root's workers, as the worker list API groups them, summarized
 *  for its session-list row. */
export function summarizeCrewWorkers(workers: readonly CrewWorker[]): CrewRootSummary {
  const groups = workers.map(crewWorkerGroup)
  groups.sort((a, b) => CREW_GROUP_ORDER.indexOf(a) - CREW_GROUP_ORDER.indexOf(b))
  return {
    groups,
    needsYou: groups.filter(g => g === 'needs_you').length,
    done: groups.filter(g => g === 'done').length,
    total: groups.length,
  }
}
