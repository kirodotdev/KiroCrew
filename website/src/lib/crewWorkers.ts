/** Crew Mode workers grouped the way the person reads them: a tool approval only
 *  they can give, what is moving, what waits on its conductor, what is idle, what
 *  is finished.
 *
 *  A worker never asks the person itself: its questions and blockers go to its
 *  conductor (`with_conductor`), which answers or asks in its own chat. The one
 *  thing a conductor cannot answer is a tool approval (`approval`), resolved on
 *  the conductor's approval card. The rule is the server's (`crew_workers._group`),
 *  from real signals only (a closed tab, the worker's ledger item, an open
 *  approval), never from "it has replied". The session list's ghost stack and
 *  "Approval needed" / "Done x/y" line, the breadcrumb and the side panel's cards
 *  all read the server's group. A live slot row alone cannot say a worker
 *  finished, so its mirror (`crewWorkerGroupOfRow`) has no done. */
import type { CrewWorker } from '../types'
import { isNotFoundError } from '../api/apiError'

export type CrewWorkerGroup = 'approval' | 'working' | 'with_conductor' | 'waiting' | 'done'

/** The gateway does not serve a crew route (404): the route lands in a later
 *  part, or this is an older gateway. That is NO DATA, not a failure: nothing is
 *  said to the person, nothing is handed to the agent, and the route is not asked
 *  again (polling it would only 404 again). The live rows carry what they can. */
export function crewRouteNotServed(error: unknown): boolean {
  return isNotFoundError(error)
}

/** Display order: the approval only the person can give first. */
export const CREW_GROUP_ORDER: readonly CrewWorkerGroup[] = ['approval', 'working', 'with_conductor', 'waiting', 'done']

export const CREW_GROUP_LABEL_KEY: Record<CrewWorkerGroup, string> = {
  approval: 'commandCenter.worker_group_approval',
  working: 'commandCenter.worker_group_working',
  with_conductor: 'commandCenter.worker_group_with_conductor',
  waiting: 'commandCenter.worker_group_waiting',
  done: 'commandCenter.worker_group_done',
}

/** The tone each group's ghost and dot wear: theme tokens, so light and dark follow. */
export const CREW_GROUP_TONE: Record<CrewWorkerGroup, string> = {
  approval: 'crew-tone-warn',
  working: 'crew-tone-aim',
  with_conductor: 'crew-tone-muted',
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
  if (row.pending_approval) return 'approval'
  if (row.running) return 'working'
  if (row.needs_input) return 'with_conductor'
  return 'waiting'
}

/** The slot-row fields a live worker row is shaped from. */
export interface LiveCrewWorkerRow extends CrewWorkerRowLike {
  key: string
  title?: string
  agent?: string
  crew_root?: string
  created_by?: string
  last_turn_ts?: string
  peer_id?: string
}

/** A root's OPEN workers from the slot store, shaped as worker-list rows. The
 *  store already carries what the cards and groups read -- the `crew_root` stamp,
 *  title, agent and state -- so a root's open workers are listed whether or not
 *  the gateway serves the worker list (`/crew-workers`): a hidden row is never
 *  out of reach. That route adds what the store has not: closed workers, the
 *  ledger item, the one-line summary and the server's own group. A peer's rows
 *  describe another gateway's crew and are left out. */
export function liveCrewWorkers(slots: readonly LiveCrewWorkerRow[], root: string): CrewWorker[] {
  if (!root) return []
  return slots.filter(s => s.crew_root === root && !s.peer_id).map(s => ({
    key: s.key,
    title: s.title || '',
    agent: s.agent || '',
    status: s.pending_approval ? 'needs_approval' : s.running ? 'working' : s.needs_input ? 'asking' : 'idle',
    group: crewWorkerGroupOfRow(s),
    // The settled turn stamp, not `last_ts`: a row's `last_ts` can be a whole
    // turn old (mid-turn bumps live in the store's overlay).
    last_activity: s.last_turn_ts || '',
    ledger_item: '',
    created_by: s.created_by || root,
    dispatched_by: s.created_by || root,
  }))
}

/** The worker list as shown: the route's rows (which win where both name a key,
 *  since they carry more), then every open live worker the route did not name
 *  -- the whole list while the route has not answered or is not served. */
export function mergeCrewWorkers(listed: readonly CrewWorker[] | undefined, live: readonly CrewWorker[]): CrewWorker[] {
  const out = [...(listed ?? [])]
  const seen = new Set(out.map(w => w.key))
  for (const w of live) if (!seen.has(w.key)) out.push(w)
  return out
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

/** The finished group: a closed tab, a closed ledger item, or a `done` report.
 *  The side panel folds it away under one toggle so live workers stay on top. */
export const CREW_FINISHED_GROUP: CrewWorkerGroup = 'done'

/** Fold text for the worker search: NFKC (full-width forms match their ASCII
 *  twins), then case-insensitive. CJK has no case and is left as typed. */
function foldForSearch(text: string): string {
  return text.normalize('NFKC').toLocaleLowerCase()
}

/** The workers whose title or task line (`summary`) holds `query`, live and
 *  finished alike, in their given order. A blank query matches every worker. */
export function searchCrewWorkers(workers: CrewWorker[], query: string): CrewWorker[] {
  const q = foldForSearch(query.trim())
  if (!q) return workers
  return workers.filter(w => foldForSearch(`${w.title}\n${w.summary || ''}`).includes(q))
}

/** A worker's dispatcher when that is not the root: a sub-conductor's key, or "". */
export function crewSubDispatcher(worker: CrewWorker, root: string): string {
  const by = worker.dispatched_by || ''
  return by && by !== root && by !== worker.key ? by : ''
}

/** One group's rows with each sub-conductor's crew right under it, when both are
 *  in the group; any other row keeps the server's order. Cycle safe. */
export function orderUnderDispatcher(rows: CrewWorker[], root: string): CrewWorker[] {
  const here = new Set(rows.map(w => w.key))
  const children = new Map<string, CrewWorker[]>()
  const tops: CrewWorker[] = []
  for (const w of rows) {
    const by = crewSubDispatcher(w, root)
    if (by && here.has(by)) children.set(by, [...(children.get(by) ?? []), w])
    else tops.push(w)
  }
  const out: CrewWorker[] = []
  const seen = new Set<string>()
  const visit = (w: CrewWorker) => {
    if (seen.has(w.key)) return
    seen.add(w.key)
    out.push(w)
    for (const c of children.get(w.key) ?? []) visit(c)
  }
  tops.forEach(visit)
  // A cycle among dispatchers leaves rows no top reaches: keep them, in order.
  for (const w of rows) if (!seen.has(w.key)) visit(w)
  return out
}

export interface CrewRootSummary {
  /** One entry per worker, approval first, for the row's ghost stack. */
  groups: CrewWorkerGroup[]
  /** Workers waiting on a tool approval: the conductor card's one "Approval
   *  needed" count. Questions and blockers are the conductor's, never counted. */
  approval: number
  done: number
  total: number
}

/** One count on a Crew Mode root's row: how many workers are in one bucket. */
export interface CrewRowCount {
  bucket: 'working' | 'waiting' | 'needs_you'
  /** The group whose dot and tone the count wears. */
  group: CrewWorkerGroup
  count: number
}

/** The row's per-bucket counts, needs you first (a narrow row drops the last
 *  one whole, so the urgent count stays), zero buckets left out.
 *  Needs you is a tool approval (the only thing a worker owes the person);
 *  working is a moving worker or one whose question sits with its conductor;
 *  waiting is idle. Done is not counted. */
export function crewRowCounts(groups: readonly CrewWorkerGroup[]): CrewRowCount[] {
  const n = (...gs: CrewWorkerGroup[]) => groups.filter(g => gs.includes(g)).length
  const all: CrewRowCount[] = [
    { bucket: 'needs_you', group: 'approval', count: n('approval') },
    { bucket: 'working', group: 'working', count: n('working', 'with_conductor') },
    { bucket: 'waiting', group: 'waiting', count: n('waiting') },
  ]
  return all.filter(c => c.count > 0)
}

/** A Crew Mode root's workers, as the worker list API groups them, summarized
 *  for its session-list row. */
export function summarizeCrewWorkers(workers: readonly CrewWorker[]): CrewRootSummary {
  const groups = workers.map(crewWorkerGroup)
  groups.sort((a, b) => CREW_GROUP_ORDER.indexOf(a) - CREW_GROUP_ORDER.indexOf(b))
  return {
    groups,
    approval: groups.filter(g => g === 'approval').length,
    done: groups.filter(g => g === 'done').length,
    total: groups.length,
  }
}
