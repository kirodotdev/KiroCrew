/**
 * A Slack code channel's crew in the folder view: the conductor session the
 * channel IS, and the worker sessions it opened (each cites it as its creator).
 *
 * The folder view draws the conductor's row and, by default, none of its
 * workers: they fold under it behind a "N workers" disclosure, and the
 * conductor's status line reports the crew's overall state, worst first. This is
 * display only -- every worker stays a session on the session page, in search
 * and in History, and a filter or search shows every row as it always did.
 *
 * Scoped to code-channel conductors (`slot.code_channel`); every other parent
 * keeps today's rows. Nothing here is specific to Slack beyond that test, so the
 * same fold could serve any conductor.
 *
 * Two sources feed the line:
 *
 *   - The conductor slot's OPTIONAL `work_aggregate` ({state, label, counts},
 *     see `WorkAggregate` in types/index.chat.ts), published by the work
 *     board's row model (#21993) and delivered by the ordinary slot patch. Its
 *     `state` is already resolved worst first (waiting > stuck > working > done
 *     > idle) and its `label` already redacted, so both are rendered as they
 *     come: never re-ranked, never re-derived. It is the only source of "done"
 *     (N of M) and "stuck" (a worker gone quiet or lost).
 *   - The slot list the sidebar already holds, through the same signals each
 *     worker's own row renders from: waiting on you (an approval, a question, a
 *     sub-agent's approval), interrupted, working, idle. The whole line while
 *     `work_aggregate` is null or absent, where "done" and "stuck" do not show.
 *
 * One rule holds over both: a worker waiting on the user in the slot list
 * ALWAYS takes the line, whatever the aggregate says, because that is the one
 * thing a fold must never hide and the slot list knows it first.
 *
 * Pure: no React, no store.
 */
import type { Slot } from './types'
import type { WorkAggregate } from '../../types'
import { citedCreatorOf } from './conductor'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import { hasLiveSessionWork } from '../chat/sessionLane'

/** A member's state as its own row reads it from the slot list. */
export type CrewStateKey = 'needs_you' | 'interrupted' | 'working' | 'idle'

/** What the folded conductor row can say, worst first. `stuck` and `done` come
 *  only from `work_aggregate`; `interrupted` only from the slot list, at the
 *  same rank as `stuck` (both mean a worker is not moving and needs a look). */
export type CrewLineKey = 'needs_you' | 'stuck' | 'interrupted' | 'working' | 'done'

const LINE_RANK: Record<CrewLineKey | 'idle', number> = { needs_you: 5, stuck: 4, interrupted: 4, working: 3, done: 2, idle: 0 }

/** What the row reads, per member, from the state the sidebar already has. */
export interface CrewSignals {
  /** The widened running signal the row's own spinner uses. */
  isRunning: (s: Slot) => boolean
  /** Sub-agents of this session held at the spawn gate. */
  subagentApprovals: (s: Slot) => number
  isUnread: (s: Slot) => boolean
}

/** The signals from the sidebar's own state, read only for LOCAL rows: the
 *  approval counts and the unread set are keyed by local slot key. */
export function crewSignalsFrom(src: {
  isRunning: (s: Slot) => boolean
  subagentApprovalCounts: Readonly<Record<string, number>>
  unreadSet: ReadonlySet<string>
}): CrewSignals {
  return {
    isRunning: src.isRunning,
    subagentApprovals: row => (isPeerRow(row) ? 0 : (src.subagentApprovalCounts[row.key] || 0)),
    isUnread: row => !isPeerRow(row) && src.unreadSet.has(row.key),
  }
}

/** One session's state, by the precedence its own row uses: an owed decision
 *  outranks every working signal (a blocking card keeps `running` true), and an
 *  interrupted turn outranks an armed loop or monitor -- the widened running
 *  signal counts those as running, but the row itself reads "interrupted" until
 *  raw live work (a turn, sub-agents, queued prompts) supersedes it. An
 *  `[OPTIONS:]` reply alone is not an ask of the user here: a worker's options
 *  are addressed to its conductor, and its own row does not call it waiting. */
export function memberState(s: Slot, sig: CrewSignals): CrewStateKey {
  if (s.pending_approval || s.needs_input || sig.subagentApprovals(s) > 0) return 'needs_you'
  if (s.interrupted && !hasLiveSessionWork(s)) return 'interrupted'
  if (sig.isRunning(s) || hasLiveSessionWork(s)) return 'working'
  return 'idle'
}

/** The folded crew's line, or null when the conductor's own line already says
 *  the worst thing about the crew (or nobody is doing anything). */
export interface CrewState {
  key: CrewLineKey
  /** Members in that state (the conductor included, from the slot list), or the
   *  aggregate's own count for it. */
  count: number
  /** `done` only: how many items the aggregate counts in all, when it says. */
  total?: number
  /** Trailing text after " · ": a tool approval's context (the worker's last
   *  message), or the aggregate's `label`. "" for none. */
  detail: string
}

/** The folder view's additions to a code-channel conductor's row. Compared by value. */
export interface CodeChannelCrewView {
  /** Direct workers filed in the same folder (0: an aggregate with no worker
   *  row on screen, so a line and no fold). */
  workers: number
  expanded: boolean
  /** Only while folded: open, the workers show their own states. */
  state: CrewState | null
  /** Only while folded: the most pressing thing a hidden worker wants, so the
   *  conductor row carries a dot for it beside the fold, whatever its own line
   *  says: an owed decision, an interrupted turn, then an unread result. */
  attention: CrewAttention | null
}

export type CrewAttention = 'needs_you' | 'interrupted' | 'unread'

const AGGREGATE_STATES = new Set(['waiting', 'stuck', 'working', 'done', 'idle'])
const count = (n: unknown): number | undefined =>
  typeof n === 'number' && Number.isFinite(n) && n >= 0 ? Math.floor(n) : undefined

/** A `work_aggregate` this build can read: present, non-null, a known state. */
function usableAggregate(agg: WorkAggregate | null | undefined): agg is WorkAggregate {
  return !!agg && typeof agg === 'object' && AGGREGATE_STATES.has(agg.state)
}

/** The aggregate's line, or null when it is absent, idle or not one this build
 *  can read. Read defensively: the field comes from another component's model,
 *  and a malformed one must cost the line, never the sidebar. */
export function aggregateLine(agg: WorkAggregate | null | undefined): CrewState | null {
  if (!usableAggregate(agg)) return null
  // Already redacted by the board: rendered verbatim (the line truncates it).
  const label = typeof agg.label === 'string' ? agg.label : ''
  const counts = agg.counts && typeof agg.counts === 'object' ? agg.counts : {}
  switch (agg.state) {
    // A state names at least one item in it, so a missing or zero count reads 1.
    case 'waiting': return { key: 'needs_you', count: Math.max(1, count(counts.waiting) ?? 1), detail: label }
    case 'stuck': return { key: 'stuck', count: Math.max(1, count(counts.stuck) ?? 1), detail: label }
    case 'working': return { key: 'working', count: Math.max(1, count(counts.working) ?? 1), detail: label }
    case 'done': {
      const total = count(counts.total)
      const done = count(counts.done) ?? total ?? 0
      // Never "5 of 3 done".
      return { key: 'done', count: total != null ? Math.min(done, total) : done, total: total ?? (done > 0 ? done : undefined), detail: label }
    }
    default: return null
  }
}

/** The conductor itself is owed an answer: an approval or question card (its
 *  `memberState`), or an armed loop or monitor holding on an `[OPTIONS:]` reply
 *  (its row's "Waiting on you" loop line: the widened running signal is on while
 *  no turn runs). A worker's `[OPTIONS:]` is addressed to its conductor; the
 *  conductor's own is addressed to the user. */
function conductorAsks(conductor: Slot, sig: CrewSignals): boolean {
  if (memberState(conductor, sig) === 'needs_you') return true
  return !!conductor.has_options && !conductor.running && !conductor.interrupted && sig.isRunning(conductor)
}

/** The crew's line from the slot list alone. */
function slotLine(conductor: Slot, workers: readonly Slot[], sig: CrewSignals): CrewState | null {
  let worst: CrewStateKey = 'idle'
  for (const w of workers) {
    const k = memberState(w, sig)
    if (LINE_RANK[k] > LINE_RANK[worst]) worst = k
  }
  if (worst === 'idle') return null
  const members = [conductor, ...workers].filter(m => memberState(m, sig) === worst)
  const firstWaiting = worst === 'needs_you' ? workers.find(w => memberState(w, sig) === 'needs_you') : undefined
  // Only a tool approval's last message is its context. A question card's is
  // what the agent said BEFORE asking (its own row leaves it out for that
  // reason), and a sub-agent's spawn approval has no message of its own.
  return { key: worst, count: members.length, detail: firstWaiting?.pending_approval ? firstWaiting.last_message ?? '' : '' }
}

/**
 * The folded conductor row's line, or null when it should say nothing of its
 * own (the conductor's own line then shows):
 *
 *   1. A worker waiting on the user in the slot list: that line, with its
 *      approval's context. Always, whatever the aggregate says.
 *   2. The conductor itself waiting on the user, or itself interrupted: null,
 *      so its own row names its own ask or stall in full (an aggregate about
 *      its workers must not cover the conductor's own Resume).
 *   3. A usable `work_aggregate`: its line, as the board resolved it (idle:
 *      null). Not re-ranked against the slot list -- the board already did. A
 *      worker the slot list shows interrupted still lights the attention dot
 *      (`crewAttention`), so the ledger's staleness window cannot hide it.
 *   4. Otherwise the slot list's line, unless the conductor's own state is more
 *      urgent (its own line names it in full: a loop's cycle).
 */
export function crewState(conductor: Slot, workers: readonly Slot[], sig: CrewSignals): CrewState | null {
  const fromSlots = slotLine(conductor, workers, sig)
  if (fromSlots?.key === 'needs_you' && workers.some(w => memberState(w, sig) === 'needs_you')) return fromSlots
  const own = memberState(conductor, sig)
  if (conductorAsks(conductor, sig) || own === 'interrupted') return null
  if (usableAggregate(conductor.work_aggregate)) return aggregateLine(conductor.work_aggregate)
  if (!fromSlots) return null
  if (LINE_RANK[own] > LINE_RANK[fromSlots.key]) return null
  return fromSlots
}

/** The most pressing thing a hidden worker wants, for the dot beside the fold.
 *  Read from the slot list only, so it never depends on `work_aggregate`. */
export function crewAttention(workers: readonly Slot[], sig: CrewSignals): CrewAttention | null {
  if (workers.some(w => memberState(w, sig) === 'needs_you')) return 'needs_you'
  if (workers.some(w => memberState(w, sig) === 'interrupted')) return 'interrupted'
  if (workers.some(w => sig.isUnread(w) && !sig.isRunning(w))) return 'unread'
  return null
}

/** Each code-channel conductor among *rows* (one folder's), with the direct
 *  workers in the same rows that cite it. Keyed by row identity. A worker whose
 *  conductor is not among *rows* is not folded: it stays where it is. */
export function codeChannelCrews(rows: readonly Slot[]): Map<string, Slot[]> {
  const conductors = new Map<string, string>()
  for (const s of rows) {
    if (s.code_channel && !isPeerRow(s)) conductors.set(s.key, sessionRowIdentity(s))
  }
  const crews = new Map<string, Slot[]>()
  if (conductors.size === 0) return crews
  for (const s of rows) {
    if (isPeerRow(s) || s.code_channel) continue
    const cited = citedCreatorOf(s)
    if (cited === null || cited.origin !== undefined) continue
    const conductor = conductors.get(cited.key)
    if (conductor === undefined) continue
    const list = crews.get(conductor)
    if (list) list.push(s)
    else crews.set(conductor, [s])
  }
  return crews
}

/** One container's rows with its code-channel crews folded (see `foldCodeChannelCrews`). */
export interface CrewFold {
  /** The container's fresh and stale rows, minus every folded worker. */
  fresh: Slot[]
  stale: Slot[]
  /** The crew view for a conductor row, or undefined for any other row. */
  viewOf: (conductor: Slot) => CodeChannelCrewView | undefined
  /** A conductor's workers to draw under it, or null while it is folded. */
  openWorkersOf: (conductor: Slot) => Slot[] | null
}

/**
 * Fold one container's code-channel crews (a folder body, or the ungrouped
 * rows). *all* is the container's whole membership; *fresh* and *stale* are its
 * split. Only a conductor drawn at the top of the body folds -- a fresh one, or
 * *alsoShown* (the folder's pinned card) -- so a folded worker is never hidden
 * behind a collapsed stale section. While the list is *narrowed* (a search, a
 * status chip, any filter) nothing folds: a filter must reach every match. A
 * worker *keepShown* answers true for (the open session, a pinned one) never
 * folds, and is not counted in its conductor's crew either -- it is on screen
 * with its own state. *expanded* is the persisted set of open conductors.
 *
 * A code-channel conductor with a `work_aggregate` and no worker row here still
 * gets the aggregate's line (its workers may have closed: "3 of 3 done"), with
 * no fold to open.
 */
export function foldCodeChannelCrews(all: readonly Slot[], fresh: Slot[], stale: Slot[], opts: {
  narrowed: boolean
  alsoShown?: Slot | null
  keepShown?: (s: Slot) => boolean
  expanded: ReadonlySet<string>
  signals: CrewSignals
}): CrewFold {
  const none: CrewFold = { fresh, stale, viewOf: () => undefined, openWorkersOf: () => null }
  if (opts.narrowed) return none
  const shown = new Set(fresh.map(sessionRowIdentity))
  if (opts.alsoShown) shown.add(sessionRowIdentity(opts.alsoShown))
  const keep = opts.keepShown
  const crews = new Map<string, Slot[]>()
  for (const [id, ws] of codeChannelCrews(all)) {
    if (!shown.has(id)) continue
    const folding = keep ? ws.filter(w => !keep(w)) : ws
    if (folding.length > 0) crews.set(id, folding)
  }
  const hasAggregate = all.some(s => s.code_channel && s.work_aggregate)
  if (crews.size === 0 && !hasAggregate) return none
  const folded = new Set<Slot>()
  for (const ws of crews.values()) for (const w of ws) folded.add(w)
  return {
    fresh: folded.size ? fresh.filter(r => !folded.has(r)) : fresh,
    stale: folded.size ? stale.filter(r => !folded.has(r)) : stale,
    viewOf: conductor => {
      const id = sessionRowIdentity(conductor)
      const workers = crews.get(id)
      if (!workers) {
        if (!conductor.code_channel || isPeerRow(conductor) || !shown.has(id)) return undefined
        const state = crewState(conductor, [], opts.signals)
        return state ? { workers: 0, expanded: false, state, attention: null } : undefined
      }
      const expanded = opts.expanded.has(id)
      return {
        workers: workers.length,
        expanded,
        state: expanded ? null : crewState(conductor, workers, opts.signals),
        attention: expanded ? null : crewAttention(workers, opts.signals),
      }
    },
    openWorkersOf: conductor => {
      const id = sessionRowIdentity(conductor)
      return opts.expanded.has(id) ? crews.get(id) ?? null : null
    },
  }
}

export function sameCrewView(a: CodeChannelCrewView | null, b: CodeChannelCrewView | null): boolean {
  if (a === b) return true
  if (!a || !b) return false
  if (a.workers !== b.workers || a.expanded !== b.expanded || a.attention !== b.attention) return false
  const x = a.state
  const y = b.state
  if (x === y) return true
  return !!x && !!y && x.key === y.key && x.count === y.count && x.total === y.total && x.detail === y.detail
}
